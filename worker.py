import os
import shutil
import subprocess
import time
import logging

from orchestrator import analyze_result, suggest_labels
from telegram_notifier import (
    notify_pipeline_started,
    notify_subtasks_created,
    notify_stage_started,
    notify_artifact_done,
    notify_pr_created,
    notify_testing_done,
    notify_all_done,
    notify_merged,
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
    GITHUB_TOKEN_TRUST_LAYER,
    GITHUB_REPO,
    STAGE_BRANCH,
    JOB_TIMEOUT_MINUTES,
    MAX_RETRIES,
    RETRY_DELAY_MINUTES,
    ARTIFACT_STAGES,
    CODE_STAGES,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_IN_PROGRESS,
    STATUS_MERGE,
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
    """Clone repo and create a new local branch (no remote tracking)."""
    repo_url = f"https://x-access-token:{GITHUB_TOKEN_TRUST_LAYER}@github.com/{GITHUB_REPO}.git"
    result = subprocess.run(
        ["git", "clone", "--depth=1", repo_url, work_dir],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise Exception(f"git clone failed (rc={result.returncode}): {stderr[:400]}")
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=work_dir,
        check=True,
        capture_output=True,
    )


def _clone_repo_with_branch(work_dir: str, branch_name: str) -> None:
    """Clone repo; if branch_name exists on remote, check it out.
    Otherwise create a new branch. This allows code stages to pick up
    artifacts committed by earlier artifact stages."""
    repo_url = (
        f"https://x-access-token:{GITHUB_TOKEN_TRUST_LAYER}"
        f"@github.com/{GITHUB_REPO}.git"
    )
    # Try cloning the existing branch first
    result = subprocess.run(
        ["git", "clone", "--depth=50", "-b", branch_name, repo_url, work_dir],
        capture_output=True,
        timeout=120,
    )
    if result.returncode == 0:
        logger.info("Cloned existing branch %s", branch_name)
        return
    # Branch doesn't exist on remote — clone default and create it
    result = subprocess.run(
        ["git", "clone", "--depth=1", repo_url, work_dir],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise Exception(f"git clone failed (rc={result.returncode}): {stderr[:400]}")
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=work_dir,
        check=True,
        capture_output=True,
    )


_RATE_LIMIT_MARKERS = ("rate limit", "429", "overloaded", "exceeded your current quota")


def _run_claude(prompt: str, work_dir: str, job: dict) -> subprocess.CompletedProcess:
    """Run Claude Code Opus via Popen; stores process in job["process"] for cancellation."""
    proc = subprocess.Popen(
        [
            "claude", "-p", prompt,
            "--model", "claude-opus-4-6",
            "--output-format", "text",
            "--max-turns", "50",
        ],
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    job["process"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=JOB_TIMEOUT_MINUTES * 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise Exception(f"Claude Code timed out after {JOB_TIMEOUT_MINUTES}m")
    finally:
        job.pop("process", None)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _sleep_interruptible(seconds: int, job: dict) -> None:
    """Sleep in 5s chunks, aborting early if job is cancelled."""
    end = time.time() + seconds
    while time.time() < end:
        if job.get("cancelled"):
            raise Exception("Cancelled during retry wait")
        time.sleep(5)


def _run_claude_with_retry(prompt: str, work_dir: str, job: dict) -> subprocess.CompletedProcess:
    """Run Claude Code with automatic retry on rate limit errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        if job.get("cancelled"):
            raise Exception("Cancelled")
        result = _run_claude(prompt, work_dir, job)
        if result.returncode == 0:
            return result
        err_lower = (result.stderr or "").lower()
        is_rate_limit = any(m in err_lower for m in _RATE_LIMIT_MARKERS)
        if is_rate_limit and attempt < MAX_RETRIES:
            logger.warning(
                "[%s] Rate limit hit, attempt %d/%d, waiting %dm",
                job["issue_key"], attempt, MAX_RETRIES, RETRY_DELAY_MINUTES,
            )
            notify_error(
                job["issue_key"], job.get("stage", "?"),
                f"Rate limit — retry {attempt}/{MAX_RETRIES} через {RETRY_DELAY_MINUTES}м",
                job.get("jira_domain", ""),
            )
            _sleep_interruptible(RETRY_DELAY_MINUTES * 60, job)
            continue
        out = (result.stdout or "")[:300]
        err = (result.stderr or "")[:300]
        raise Exception(f"Claude Code rc={result.returncode}\nstdout: {out}\nstderr: {err}")
    raise Exception(f"Claude Code failed after {MAX_RETRIES} retries")


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
        jira_domain = job.get("jira_domain", "")
        notify_pipeline_started(issue_key, job["summary"], jira_domain)

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

        if created:
            notify_subtasks_created(issue_key, list(created.values()),
                                    auto_labels or [], jira_domain)

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
            if not stage or STAGE_PREREQUISITES.get(stage):
                continue
            # Don't re-trigger subtasks that are already running or finished
            sub_status = sub.get("status", "").lower()
            if sub_status in ("in progress", "done", "in review",
                              "в работе", "готово", "в процессе проверки"):
                logger.info("[%s] stage %s (%s) already '%s', skipping auto-start",
                            issue_key, stage, sub["key"], sub["status"])
                continue
            ok = jira.transition(sub["key"], STATUS_IN_PROGRESS)
            if ok:
                logger.info("[%s] auto-started stage %s (%s)", issue_key, stage, sub["key"])
            else:
                available = jira.get_transitions(sub["key"])
                msg = (f"⚠️ Не могу перевести {sub['key']} в '{STATUS_IN_PROGRESS}'.\n"
                       f"Доступные переходы: {available}")
                logger.warning(msg)
                from telegram_notifier import _send
                _send(msg)

    except Exception as e:
        logger.error("[%s] setup FAIL: %s", issue_key, e)
        try:
            jira.add_comment(issue_key, f"❌ Pipeline setup ошибка: {str(e)[:500]}\nJob: {job_id}")
        except Exception:
            pass


# ── Artifact stage (sys-analysis, architecture) ───────────────────────────────

_ARTIFACT_FILENAMES = {
    "sys-analysis": "SYSTEM_ANALYSIS",
    "architecture": "ARCHITECTURE_DECISION",
}


def _artifact_filename(stage: str, parent_key: str) -> str:
    """E.g. SYSTEM_ANALYSIS_AIDEV-38.md"""
    base = _ARTIFACT_FILENAMES.get(stage, stage.upper())
    return f"{base}_{parent_key}.md"


def _ensure_description_text(job: dict) -> None:
    """Enrich job with parent summary and description.

    Subtasks only have a pipeline-generated name like "[AIDEV-38] Разработка".
    We need the parent's actual summary ("ActionGate в robot_bridge") and
    description to give Claude Code enough context.
    """
    from orchestrator import parse_adf_to_text

    # Convert own ADF description
    if not job.get("description_text") and job.get("description"):
        job["description_text"] = parse_adf_to_text(job["description"])

    # For subtasks: fetch parent info (summary + description + epic context)
    if job.get("parent_key") and job["parent_key"] != job.get("issue_key"):
        try:
            parent = jira.get_issue(job["parent_key"])
            parent_fields = parent.get("fields", {})

            # Store parent summary (the actual task name)
            parent_summary = parent_fields.get("summary", "")
            if parent_summary:
                job["parent_summary"] = parent_summary

            # Fetch parent description if we don't have one
            if not job.get("description_text"):
                parent_desc = parent_fields.get("description", {})
                if parent_desc:
                    job["description_text"] = parse_adf_to_text(parent_desc)

            # Try to get epic context too
            epic_ref = parent_fields.get("parent", {})
            if epic_ref and epic_ref.get("key"):
                try:
                    epic = jira.get_issue(epic_ref["key"])
                    epic_fields = epic.get("fields", {})
                    epic_summary = epic_fields.get("summary", "")
                    epic_desc = epic_fields.get("description", {})
                    parts = []
                    if epic_summary:
                        parts.append(f"Эпик: {epic_summary}")
                    if epic_desc:
                        parts.append(parse_adf_to_text(epic_desc))
                    if parts:
                        job["epic_context"] = "\n".join(parts)
                except Exception:
                    pass

        except Exception as e:
            logger.warning("Failed to fetch parent info: %s", e)


def run_artifact_stage(job: dict) -> None:
    """Run sys-analysis or architecture via Claude Code (git clone + claude CLI).

    Claude Code reads the codebase and writes SYSTEM_ANALYSIS.md or
    ARCHITECTURE_DECISION.md. Pipeline reads the file, posts it to Jira,
    and marks the subtask Done.
    """
    _ensure_description_text(job)
    issue_key = job["issue_key"]
    parent_key = job["parent_key"]
    stage = job["stage"]
    job_id = job["job_id"]
    work_dir = f"/tmp/pipeline-work/{job_id}"

    try:
        jira.transition(issue_key, STATUS_IN_PROGRESS)
        jira.add_comment(issue_key, f"🤖 Этап {stage} начат (Claude Code). Job: {job_id}")
        notify_stage_started(stage, issue_key, parent_key, job.get("jira_domain", ""))

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
        if job.get("cancelled"):
            raise Exception("Cancelled")
        logger.info("[%s] Claude Code: running stage %s", issue_key, stage)
        result = _run_claude_with_retry(prompt, work_dir, job)
        duration = int(time.time() - start)

        if result.returncode != 0:
            raise Exception(
                f"Claude Code rc={result.returncode}: {result.stderr[:500]}"
            )

        artifact_fname = _artifact_filename(stage, parent_key)
        # Claude writes the generic name; rename to task-specific
        generic_fname = _ARTIFACT_FILENAMES.get(stage, stage.upper()) + ".md"
        generic_path = os.path.join(work_dir, generic_fname)
        artifact_path = os.path.join(work_dir, artifact_fname)
        if os.path.exists(generic_path) and generic_fname != artifact_fname:
            os.rename(generic_path, artifact_path)
        if os.path.exists(artifact_path):
            with open(artifact_path, encoding="utf-8") as fh:
                artifact_text = fh.read()
        else:
            artifact_text = result.stdout.strip() or "Артефакт не создан — проверить вручную."
            logger.warning("[%s] %s not found, using stdout", issue_key, artifact_fname)
            # Write stdout as artifact so it gets committed
            if artifact_text and artifact_text != "Артефакт не создан — проверить вручную.":
                with open(artifact_path, "w", encoding="utf-8") as fh:
                    fh.write(artifact_text)

        # Commit and push artifact to feature branch
        branch_name = f"feature/{parent_key.lower()}"
        try:
            subprocess.run(["git", "checkout", "-B", branch_name], cwd=work_dir,
                           check=True, capture_output=True)
            subprocess.run(["git", "add", artifact_fname], cwd=work_dir,
                           check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m",
                 f"{parent_key}: {_STAGE_SUMMARIES.get(stage, stage)} [{stage}]\n\n"
                 "Automated by Trust Layer Pipeline"],
                cwd=work_dir, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "origin", branch_name],
                cwd=work_dir, check=True, capture_output=True, timeout=60,
            )
            logger.info("[%s] pushed artifact %s to %s", issue_key, artifact_fname, branch_name)
        except Exception as e:
            logger.warning("[%s] failed to push artifact: %s", issue_key, e)

        jira_domain = job.get("jira_domain", "")
        parent_url = f"https://{jira_domain}/browse/{parent_key}"
        github_url = f"https://github.com/{GITHUB_REPO}/blob/{branch_name}/{artifact_fname}"

        jira.add_comment(
            issue_key,
            f"🔗 Задача: [{parent_key}]({parent_url})\n\n"
            f"## Результат этапа: {stage}\n\n{artifact_text[:24000]}\n\n"
            f"---\n⏱ {duration // 60}м {duration % 60}с | Job: {job_id}",
        )
        jira.add_comment(
            parent_key,
            f"✅ Этап **{stage}** завершён (Claude Code).\n"
            f"📄 [{artifact_fname}]({github_url})\n"
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
    _ensure_description_text(job)
    issue_key = job["issue_key"]
    parent_key = job["parent_key"]
    stage = job["stage"]
    job_id = job["job_id"]
    work_dir = f"/tmp/pipeline-work/{job_id}"

    try:
        jira.transition(issue_key, STATUS_IN_PROGRESS)
        jira.add_comment(issue_key, f"🤖 Этап {stage} начат. Job: {job_id}")
        notify_stage_started(stage, issue_key, parent_key, job.get("jira_domain", ""))

        # Auto-tag issue and parent with domain/service labels
        auto_labels = suggest_labels(job["summary"], job.get("description_text", ""))
        if auto_labels:
            jira.add_labels(issue_key, auto_labels)
            if parent_key != issue_key:
                jira.add_labels(parent_key, auto_labels)

        artifact_context = collect_artifact_context(parent_key, jira)
        prompt = build_stage_prompt(job, artifact_context)

        branch_name = f"feature/{parent_key.lower()}"
        logger.info("[%s] Cloning for code stage %s (branch %s)",
                    issue_key, stage, branch_name)
        os.makedirs(work_dir, exist_ok=True)
        _clone_repo_with_branch(work_dir, branch_name)

        start = time.time()
        if job.get("cancelled"):
            raise Exception("Cancelled")
        logger.info("[%s] Running Claude Code (stage=%s)", issue_key, stage)
        result = _run_claude_with_retry(prompt, work_dir, job)
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
            notify_testing_done(issue_key, parent_key, job.get("jira_domain", ""), duration)
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

def run_merge_job(job: dict) -> None:
    """Triggered when parent task moves to STATUS_MERGE ('На мерж').

    Finds the open PR for feature/<issue_key>, merges it into main,
    then transitions Jira task to Done.
    """
    issue_key = job["issue_key"]
    jira_domain = job.get("jira_domain", "")

    try:
        branch_name = f"feature/{issue_key.lower()}"
        pr = github.find_pr(branch_name)

        if not pr:
            jira.add_comment(
                issue_key,
                f"⚠️ Pipeline: не найден открытый PR для ветки `{branch_name}`. "
                "Смерджите вручную.",
            )
            return

        pr_number = pr["number"]
        pr_url = pr["html_url"]
        logger.info("[%s] Merging PR #%s into %s", issue_key, pr_number, pr["base"]["ref"])

        merge_result = github.merge_pr(
            pr_number,
            commit_message=f"{issue_key}: {job['summary']} (auto-merge)",
        )

        if not merge_result.get("merged"):
            raise Exception(f"GitHub merge failed: {merge_result.get('message')}")

        jira.transition(issue_key, STATUS_DONE)
        jira.add_comment(
            issue_key,
            f"🎉 Смерджено в `{pr['base']['ref']}`!\n"
            f"PR: {pr_url}\n"
            f"Commit: {merge_result.get('sha', '')[:8]}",
        )
        notify_merged(issue_key, pr_url, pr["base"]["ref"], jira_domain)
        logger.info("[%s] merged PR #%s → Done", issue_key, pr_number)

    except Exception as e:
        logger.error("[%s] merge FAIL: %s", issue_key, e)
        notify_error(issue_key, "merge", str(e), jira_domain)
        try:
            jira.add_comment(
                issue_key,
                f"❌ Авто-мердж не удался: {str(e)[:400]}\n"
                "Смерджите PR вручную и переведите задачу в Done.",
            )
        except Exception:
            pass


def run_job(job: dict) -> None:
    """Route job to the correct handler.

    Parent task (no stage)  → run_setup_job: create subtasks, start first stages
    Sub-task artifact stage → run_artifact_stage: Claude Code writes markdown
    Sub-task code stage     → run_code_stage: Claude Code writes code + PR
    """
    stage = job.get("stage")

    if job.get("trigger") == STATUS_MERGE:
        run_merge_job(job)
    elif stage is None:
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
        result = _run_claude_with_retry(prompt, work_dir, job)
        duration = int(time.time() - start)
        logger.info("[%s] Claude Code: %ds rc=%d", issue_key, duration, result.returncode)

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
