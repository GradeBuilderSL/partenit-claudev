import os
import shutil
import subprocess
import time
import logging

from orchestrator import analyze_result, suggest_labels
from telegram_notifier import (
    notify_artifact_done,
    notify_pr_created,
    notify_all_done,
    notify_error,
)
from jira_client import JiraClient
from github_client import GitHubClient
from dependency_tracker import (
    collect_artifact_context,
    trigger_next_stages,
    all_stages_done,
)
from prompts import build_stage_prompt
from config import (
    GITHUB_TOKEN,
    GITHUB_REPO,
    STAGE_BRANCH,
    JOB_TIMEOUT_MINUTES,
    ARTIFACT_STAGES,
    CODE_STAGES,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_IN_PROGRESS,
    JIRA_PROJECT_KEY,
    PIPELINE_LABEL_PREFIX,
    ALL_STAGES,
    STAGE_PREREQUISITES,
)

logger = logging.getLogger("pipeline.worker")
jira = JiraClient()
github = GitHubClient()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clone_repo(work_dir: str, branch_name: str) -> None:
    repo_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    subprocess.run(
        ["git", "clone", "--depth=1", repo_url, work_dir],
        check=True,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=work_dir,
        check=True,
        capture_output=True,
    )


def _run_claude(prompt: str, work_dir: str) -> subprocess.CompletedProcess:
    """Run Claude Code Opus — used only for development and testing stages."""
    return subprocess.run(
        [
            "claude", "-p", prompt,
            "--model", "claude-opus-4-6",
            "--output-format", "text",
            "--max-turns", "50",
        ],
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=JOB_TIMEOUT_MINUTES * 60,
    )


def _git_changed_files(work_dir: str) -> list[str]:
    diff = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=work_dir, capture_output=True, text=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=work_dir, capture_output=True, text=True,
    )
    return [f for f in (diff.stdout + untracked.stdout).strip().split("\n") if f]


# ── Setup job: create pipeline subtasks for a parent task ────────────────────

_STAGE_SUMMARIES = {
    "sys-analysis":  "Системный анализ",
    "architecture":  "Архитектурное решение",
    "development":   "Разработка",
    "testing":       "Тестирование",
}


def run_setup_job(job: dict) -> None:
    """When a parent task moves to In Progress: create 4 pipeline subtasks,
    then immediately transition the ones with no prerequisites to In Progress.

    Idempotent: if pipeline subtasks already exist, skips creation.
    """
    issue_key = job["issue_key"]
    job_id = job["job_id"]

    try:
        # Auto-tag parent with domain/service labels
        description_text = job.get("description_text", "")
        auto_labels = suggest_labels(job["summary"], description_text)
        if auto_labels:
            jira.add_labels(issue_key, auto_labels)

        # Check which stages already have subtasks
        existing = jira.get_subtasks(issue_key)
        from dependency_tracker import get_stage
        existing_stages = {
            get_stage(sub["labels"])
            for sub in existing
            if get_stage(sub["labels"])
        }

        created: dict[str, str] = {}  # stage → subtask key

        for stage in ALL_STAGES:
            if stage in existing_stages:
                logger.info("[%s] subtask for stage %s already exists, skipping", issue_key, stage)
                continue

            label = f"{PIPELINE_LABEL_PREFIX}{stage}"
            summary = f"[{issue_key}] {_STAGE_SUMMARIES.get(stage, stage)}"
            subtask_key = jira.create_subtask(
                parent_key=issue_key,
                summary=summary,
                labels=[label] + (auto_labels or []),
                project_key=JIRA_PROJECT_KEY,
            )
            created[stage] = subtask_key
            logger.info("[%s] created subtask %s for stage %s", issue_key, subtask_key, stage)

        jira.add_comment(
            issue_key,
            "🤖 Pipeline подготовлен.\n"
            + "\n".join(
                f"• {_STAGE_SUMMARIES.get(s, s)}: {k}"
                for s, k in created.items()
            )
            + ("\n\nЭтапы без зависимостей запускаются автоматически." if created else ""),
        )

        # Trigger stages with no prerequisites → transition to In Progress
        # Jira will fire a webhook for each, which will process them
        all_subtasks = jira.get_subtasks(issue_key)
        for sub in all_subtasks:
            stage = get_stage(sub["labels"])
            if stage and not STAGE_PREREQUISITES.get(stage):
                jira.transition(sub["key"], STATUS_IN_PROGRESS)
                logger.info("[%s] auto-started stage %s (%s)", issue_key, stage, sub["key"])

    except Exception as e:
        logger.error("[%s] setup FAIL: %s", issue_key, e)
        try:
            jira.add_comment(issue_key, f"❌ Pipeline setup ошибка: {str(e)[:500]}\nJob: {job_id}")
        except Exception:
            pass


# ── Artifact stage (sys-analysis, architecture) ───────────────────────────────

_ARTIFACT_FILENAMES = {
    "sys-analysis": "SYSTEM_ANALYSIS.md",
    "architecture": "ARCHITECTURE_DECISION.md",
}


def run_artifact_stage(job: dict) -> None:
    """Run sys-analysis or architecture via Claude Code (git clone + claude CLI).

    Claude Code reads the codebase and writes SYSTEM_ANALYSIS.md or
    ARCHITECTURE_DECISION.md. Pipeline reads the file, posts it to Jira,
    and marks the subtask Done.
    """
    issue_key = job["issue_key"]
    parent_key = job["parent_key"]
    stage = job["stage"]
    job_id = job["job_id"]
    work_dir = f"/tmp/pipeline-work/{job_id}"

    try:
        jira.transition(issue_key, "In Progress")
        jira.add_comment(issue_key, f"🤖 Этап {stage} начат (Claude Code). Job: {job_id}")

        auto_labels = suggest_labels(job["summary"], job.get("description_text", ""))
        if auto_labels:
            jira.add_labels(issue_key, auto_labels)
            if parent_key != issue_key:
                jira.add_labels(parent_key, auto_labels)

        artifact_context = collect_artifact_context(parent_key, jira)
        prompt = build_stage_prompt(job, artifact_context)

        logger.info("[%s] Cloning for artifact stage %s", issue_key, stage)
        os.makedirs(work_dir, exist_ok=True)
        _clone_repo(work_dir, f"analysis/{issue_key.lower()}")

        start = time.time()
        logger.info("[%s] Claude Code: running stage %s", issue_key, stage)
        result = _run_claude(prompt, work_dir)
        duration = int(time.time() - start)

        if result.returncode != 0:
            raise Exception(
                f"Claude Code rc={result.returncode}: {result.stderr[:500]}"
            )

        artifact_filename = _ARTIFACT_FILENAMES.get(stage, f"{stage.upper()}.md")
        artifact_path = os.path.join(work_dir, artifact_filename)
        if os.path.exists(artifact_path):
            with open(artifact_path, encoding="utf-8") as fh:
                artifact_text = fh.read()
        else:
            artifact_text = result.stdout.strip() or "Артефакт не создан — проверить вручную."
            logger.warning("[%s] %s not found, using stdout", issue_key, artifact_filename)

        jira_domain = job.get("jira_domain", "")
        parent_url = f"https://{jira_domain}/browse/{parent_key}"
        github_url = f"https://github.com/{GITHUB_REPO}/blob/main/{artifact_filename}"

        jira.add_comment(
            issue_key,
            f"🔗 Задача: [{parent_key}]({parent_url})\n\n"
            f"## Результат этапа: {stage}\n\n{artifact_text[:24000]}\n\n"
            f"---\n⏱ {duration // 60}м {duration % 60}с | Job: {job_id}",
        )
        jira.add_comment(
            parent_key,
            f"✅ Этап **{stage}** завершён (Claude Code).\n"
            f"📄 [{artifact_filename}]({github_url})\n"
            f"⏱ {duration // 60}м {duration % 60}с",
        )

        jira.transition(issue_key, STATUS_DONE)
        logger.info("[%s] stage %s done (%ds)", issue_key, stage, duration)
        notify_artifact_done(stage, issue_key, parent_key,
                             job.get("jira_domain", ""), duration)

        triggered = trigger_next_stages(parent_key, stage, jira)
        if triggered:
            jira.add_comment(
                issue_key,
                f"🤖 Автоматически запущены этапы: {', '.join(triggered)}",
            )

    except Exception as e:
        logger.error("[%s] artifact stage FAIL: %s", issue_key, e)
        notify_error(issue_key, stage, str(e), job.get("jira_domain", ""))
        try:
            jira.add_comment(
                issue_key,
                f"❌ Pipeline ошибка (stage={stage}): {str(e)[:500]}\nJob: {job_id}",
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── Code stage (development, testing) ─────────────────────────────────────────

def run_code_stage(job: dict) -> None:
    """Run development or testing stage.

    Claude Code writes actual code. Pipeline creates a PR (development)
    or pushes to dev branch (testing) and transitions to In Review / Done.
    """
    issue_key = job["issue_key"]
    parent_key = job["parent_key"]
    stage = job["stage"]
    job_id = job["job_id"]
    work_dir = f"/tmp/pipeline-work/{job_id}"

    try:
        jira.transition(issue_key, "In Progress")
        jira.add_comment(issue_key, f"🤖 Этап {stage} начат. Job: {job_id}")

        # Auto-tag issue and parent with domain/service labels
        auto_labels = suggest_labels(job["summary"], job.get("description_text", ""))
        if auto_labels:
            jira.add_labels(issue_key, auto_labels)
            if parent_key != issue_key:
                jira.add_labels(parent_key, auto_labels)

        artifact_context = collect_artifact_context(parent_key, jira)
        prompt = build_stage_prompt(job, artifact_context)

        branch_name = f"feature/{issue_key.lower()}"
        logger.info("[%s] Cloning for code stage %s", issue_key, stage)
        os.makedirs(work_dir, exist_ok=True)
        _clone_repo(work_dir, branch_name)

        start = time.time()
        logger.info("[%s] Running Claude Code (stage=%s)", issue_key, stage)
        result = _run_claude(prompt, work_dir)
        duration = int(time.time() - start)
        logger.info("[%s] Claude Code: %ds rc=%d", issue_key, duration, result.returncode)

        if result.returncode != 0:
            raise Exception(
                f"Claude Code rc={result.returncode}: {result.stderr[:500]}"
            )

        changed = _git_changed_files(work_dir)
        if not changed:
            jira.add_comment(
                issue_key,
                "🤖 Claude Code не внёс изменений. Задача требует уточнения.",
            )
            jira.transition(issue_key, "Ready for Dev")
            return

        analysis = analyze_result(result.stdout, changed)

        logger.info("[%s] Pushing %s", issue_key, branch_name)
        subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
        subprocess.run(
            [
                "git", "commit", "-m",
                f"{issue_key}: {job['summary']} [{stage}]\n\n"
                "Automated by Trust Layer Pipeline",
            ],
            cwd=work_dir, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", branch_name],
            cwd=work_dir, check=True, capture_output=True, timeout=60,
        )

        if stage == "development":
            jira_domain = job.get("jira_domain", os.environ.get("JIRA_DOMAIN", "x"))
            files_list = "\n".join(
                "- " + f for f in analysis.get("files_changed", changed)
            )
            pr_body = (
                f"## {issue_key}: {job['summary']}\n\n"
                f"**Jira:** https://{jira_domain}/browse/{parent_key}\n"
                f"**Subtask:** {issue_key} (stage: {stage})\n"
                "**Automated by:** Trust Layer Pipeline\n\n"
                f"### Что сделано\n{analysis.get('summary_ru', 'N/A')}\n\n"
                f"### Файлы\n{files_list}\n\n"
                f"### Тесты: {analysis.get('tests_status', '?')}\n"
            )
            pr = github.create_pr(
                head=branch_name,
                base=STAGE_BRANCH,
                title=f"{issue_key}: {job['summary']}",
                body=pr_body,
            )
            github.add_labels(pr["number"], ["automated", "claude-code", stage])
            notify_pr_created(issue_key, parent_key, pr["html_url"],
                              job.get("jira_domain", ""), len(changed))

            concerns = (
                "\n⚠️ " + "; ".join(analysis["concerns"])
                if analysis.get("concerns") else ""
            )
            jira.transition(issue_key, STATUS_IN_REVIEW)
            jira.add_comment(
                issue_key,
                f"🤖 PR создан: {pr['html_url']}\n"
                f"Файлов: {len(changed)} | "
                f"Тесты: {analysis.get('tests_status', '?')} | "
                f"Время: {duration // 60}м {duration % 60}с\n"
                f"{analysis.get('summary_ru', '')}{concerns}",
            )
            logger.info("[%s] Done! PR #%s", issue_key, pr["number"])

        else:  # testing
            jira.transition(issue_key, STATUS_DONE)
            jira.add_comment(
                issue_key,
                f"🤖 Тесты написаны и запушены в {branch_name}.\n"
                f"Файлов: {len(changed)} | "
                f"Статус: {analysis.get('tests_status', '?')} | "
                f"Время: {duration // 60}м {duration % 60}с\n"
                f"{analysis.get('summary_ru', '')}",
            )
            logger.info("[%s] Testing stage done (%ds)", issue_key, duration)

        if all_stages_done(parent_key, jira):
            jira.add_comment(
                parent_key,
                "🎉 Все этапы pipeline завершены!\n"
                "sys-analysis ✅ | architecture ✅ | development ✅ | testing ✅\n"
                "Задача готова к ревью.",
            )
            notify_all_done(parent_key, job.get("jira_domain", ""))

        triggered = trigger_next_stages(parent_key, stage, jira)
        if triggered:
            jira.add_comment(
                issue_key,
                f"🤖 Автоматически запущены этапы: {', '.join(triggered)}",
            )

    except Exception as e:
        logger.error("[%s] code stage FAIL: %s", issue_key, e)
        notify_error(issue_key, stage, str(e), job.get("jira_domain", ""))
        try:
            jira.add_comment(
                issue_key,
                f"❌ Pipeline ошибка (stage={stage}): {str(e)[:500]}\nJob: {job_id}",
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_job(job: dict) -> None:
    """Route job to the correct handler.

    Parent task (no stage)  → run_setup_job: create subtasks, start first stages
    Sub-task artifact stage → run_artifact_stage: Claude Code writes markdown
    Sub-task code stage     → run_code_stage: Claude Code writes code + PR
    """
    stage = job.get("stage")

    if stage is None:
        run_setup_job(job)
    elif stage in ARTIFACT_STAGES:
        run_artifact_stage(job)
    elif stage in CODE_STAGES:
        run_code_stage(job)
    else:
        logger.warning("[%s] Unknown stage '%s', falling back to legacy", job["issue_key"], stage)
        _run_legacy_job(job)


# ── Legacy single-stage flow (backward compatibility) ─────────────────────────

def _run_legacy_job(job: dict) -> None:
    """Original single-stage worker for tasks without pipeline labels."""
    from orchestrator import (
        parse_adf_to_text,
        classify_issue,
        build_claude_prompt,
        analyze_result as _analyze,
    )

    issue_key = job["issue_key"]
    job_id = job["job_id"]
    work_dir = f"/tmp/pipeline-work/{job_id}"

    try:
        jira.transition(issue_key, "In Progress")
        jira.add_comment(issue_key, f"🤖 Pipeline начал работу. Job: {job_id}")

        description_text = parse_adf_to_text(job.get("description", ""))
        issue = {
            "key": issue_key,
            "summary": job["summary"],
            "description_text": description_text,
            "issue_type": job.get("issue_type", "Task"),
            "priority": job.get("priority", "Medium"),
            "labels": job.get("labels", []),
            "components": job.get("components", []),
        }

        classification = classify_issue(
            issue["summary"], description_text, issue["labels"]
        )
        prompt = build_claude_prompt(issue, classification)

        branch_name = f"feature/{issue_key.lower()}"
        os.makedirs(work_dir, exist_ok=True)
        _clone_repo(work_dir, branch_name)

        start = time.time()
        result = _run_claude(prompt, work_dir)
        duration = int(time.time() - start)
        logger.info("[%s] Claude Code: %ds rc=%d", issue_key, duration, result.returncode)

        if result.returncode != 0:
            raise Exception(
                f"Claude Code rc={result.returncode}: {result.stderr[:500]}"
            )

        changed = _git_changed_files(work_dir)
        if not changed:
            jira.add_comment(
                issue_key,
                "🤖 Claude Code не внёс изменений. Задача требует уточнения.",
            )
            jira.transition(issue_key, "Ready for Dev")
            return

        analysis = _analyze(result.stdout, changed)

        subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
        subprocess.run(
            [
                "git", "commit", "-m",
                f"{issue_key}: {issue['summary']}\n\nAutomated by Trust Layer Pipeline",
            ],
            cwd=work_dir, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", branch_name],
            cwd=work_dir, check=True, capture_output=True, timeout=60,
        )

        jira_domain = job.get("jira_domain", os.environ.get("JIRA_DOMAIN", "x"))
        files_list = "\n".join(
            "- " + f for f in analysis.get("files_changed", changed)
        )
        pr_body = (
            f"## {issue_key}: {issue['summary']}\n\n"
            f"**Jira:** https://{jira_domain}/browse/{issue_key}\n"
            "**Automated by:** Trust Layer Pipeline\n\n"
            f"### Что сделано\n{analysis.get('summary_ru', 'N/A')}\n\n"
            f"### Файлы\n{files_list}\n\n"
            f"### Тесты: {analysis.get('tests_status', '?')}\n"
        )
        pr = github.create_pr(
            head=branch_name,
            base=STAGE_BRANCH,
            title=f"{issue_key}: {issue['summary']}",
            body=pr_body,
        )
        github.add_labels(pr["number"], ["automated", "claude-code"])

        concerns = (
            "\n⚠️ " + "; ".join(analysis["concerns"])
            if analysis.get("concerns") else ""
        )
        jira.transition(issue_key, "In Review")
        jira.add_comment(
            issue_key,
            f"🤖 PR создан: {pr['html_url']}\n"
            f"Файлов: {len(changed)} | "
            f"Тесты: {analysis.get('tests_status', '?')} | "
            f"Время: {duration // 60}м {duration % 60}с\n"
            f"{analysis.get('summary_ru', '')}{concerns}",
        )
        logger.info("[%s] Done! PR #%s", issue_key, pr["number"])

    except Exception as e:
        logger.error("[%s] FAIL: %s", issue_key, e)
        try:
            jira.add_comment(
                issue_key,
                f"❌ Pipeline ошибка: {str(e)[:500]}\nJob: {job_id}",
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
