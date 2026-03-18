"""
Stage-specific Claude Code prompt builders.

Each function receives the issue dict + enrichment context and returns
a ready-to-use prompt string for `claude -p <prompt>`.

Stages:
  sys-analysis  → produce SYSTEM_ANALYSIS.md artifact
  architecture  → produce ARCHITECTURE_DECISION.md artifact
  development   → write code, open PR to stage branch
  testing       → write tests, push to dev branch
"""
from __future__ import annotations


# ── Shared header ─────────────────────────────────────────────────────────────

def _base_header(issue: dict) -> str:
    parent_summary = issue.get('parent_summary', issue['summary'])
    epic_section = ""
    if issue.get('epic_context'):
        epic_section = (
            "## Epic context\n"
            f"{issue['epic_context']}\n\n"
        )

    desc_text = issue.get('description_text', '')
    if desc_text:
        desc_section = (
            "## Parent task description\n"
            f"{desc_text}\n\n"
        )
    else:
        desc_section = (
            "## Parent task description\n"
            "(No description provided. Use the task title "
            "and epic context above as guidance.)\n\n"
        )

    return (
        f"## Task: {issue['parent_key']} — {parent_summary}\n\n"
        f"Subtask: {issue['key']} | "
        f"Stage: **{issue['stage']}** | "
        f"Priority: {issue.get('priority', 'Medium')}\n"
        f"Components: {', '.join(issue.get('components', []) or [])}\n\n"
        + epic_section
        + desc_section
    )


# ── Mandatory reading + coding standards ──────────────────────────────────────

def _pre_flight(stage: str, parent_key: str = "") -> str:
    """Files to read BEFORE starting any work."""
    base_files = (
        "## Mandatory reading before starting\n\n"
        "Read these files IN FULL before writing anything "
        "(if they exist in the repo):\n\n"
        "1. **CLAUDE.md** — project rules, conventions, "
        "priorities for AI assistants\n"
        "2. **ARCHITECTURE.md** — project structure, components, "
        "dependencies, data flows\n"
        "3. **STEERING.md** — design principles, constraints, "
        "things that must not be changed\n"
    )

    if stage in ("architecture", "development", "testing"):
        sa_file = f"SYSTEM_ANALYSIS_{parent_key}.md" if parent_key else "SYSTEM_ANALYSIS*.md"
        base_files += (
            f"4. **{sa_file}** — system analysis for this task "
            "(if present in the repo)\n"
        )

    if stage in ("development", "testing"):
        ad_file = (f"ARCHITECTURE_DECISION_{parent_key}.md"
                   if parent_key else "ARCHITECTURE_DECISION*.md")
        base_files += (
            f"5. **{ad_file}** — architecture decision for this task "
            "(if present in the repo)\n"
        )

    return base_files + "\n"


def _coding_standards() -> str:
    """Coding standards that apply to ALL stages.

    NOTE: Customize these to match your project's conventions.
    The rules below are sensible defaults — adjust as needed.
    """
    return (
        "## Coding standards\n\n"
        "### Architecture\n"
        "- **No code duplication.** Before creating a new function/class, "
        "search the codebase for existing implementations. "
        "Use `Grep` to search.\n"
        "- **No unnecessary abstractions.** "
        "Three similar lines are better than a premature abstraction.\n"
        "- **Follow existing patterns.** Look at how similar features "
        "are implemented in the project and stay consistent.\n"
        "- **Keep files focused.** One module = one responsibility. "
        "Split large files by concern.\n\n"
        "### Style\n"
        "- Follow the coding style already used in the project.\n"
        "- Logging: use the project's logging pattern, NOT print().\n"
        "- Type hints for public functions.\n"
        "- Docstrings only for non-obvious logic.\n\n"
        "### What NOT to do\n"
        "- Do NOT refactor code unrelated to the task.\n"
        "- Do NOT add comments/docstrings to code you didn't change.\n"
        "- Do NOT add error handling for impossible scenarios.\n"
        "- Do NOT create utility helpers for one-off operations.\n"
        "- Do NOT add feature flags or backwards-compatibility shims.\n"
        "- Do NOT create git commits — the pipeline handles that.\n\n"
    )


def _post_flight() -> str:
    """Checklist AFTER completing work."""
    return (
        "## After completing work\n\n"
        "1. If you changed the project structure or added a component "
        "— **update ARCHITECTURE.md** (if it exists).\n"
        "2. If you noticed tech debt — add to TECH_DEBT.md.\n"
        "3. If unclear → leave a TODO with explanation, "
        "don't guess.\n\n"
    )


def _test_loop() -> str:
    """Instructions to iterate until tests pass."""
    return (
        "## Test loop (MANDATORY)\n\n"
        "After finishing code/tests, run this loop:\n\n"
        "```\n"
        "repeat:\n"
        "  1. pytest tests/unit/ -x -v\n"
        "  2. if all green → STOP, work is done\n"
        "  3. if FAIL/ERROR → read the traceback\n"
        "  4. determine the cause: bug in code or wrong expectation in test\n"
        "  5. fix the ROOT CAUSE — if it's a code bug, fix the code, "
        "don't adjust the test to match the bug\n"
        "  6. goto 1\n"
        "```\n\n"
        "Maximum 5 iterations. If tests still fail after 5 — "
        "leave a TODO describing the issue.\n\n"
        "IMPORTANT: do not delete failing tests! Fix the code or "
        "correct the test if the expectation is wrong.\n\n"
    )


# ── Stage: sys-analysis ────────────────────────────────────────────────────────

def build_sys_analysis_prompt(issue: dict) -> str:
    jira_domain = issue.get("jira_domain", "")
    parent_key = issue.get("parent_key", issue["key"])
    parent_summary = issue.get("parent_summary", issue["summary"])
    parent_url = (f"https://{jira_domain}/browse/{parent_key}"
                  if jira_domain else parent_key)
    subtask_url = (f"https://{jira_domain}/browse/{issue['key']}"
                   if jira_domain else issue['key'])

    file_header = (
        f"# System Analysis: [{parent_key}]({parent_url})"
        f" — {parent_summary}\n\n"
        f"> **Jira:** [{parent_key}]({parent_url}) · "
        f"Subtask: [{issue['key']}]({subtask_url})  \n"
        f"> **Stage:** sys-analysis  \n"
        "> Auto-generated by Claudev\n\n"
        "---\n\n"
    )

    return (
        _base_header(issue)
        + _pre_flight("sys-analysis", parent_key)
        + "## What to do: System Analysis\n\n"
        "Perform a system analysis of the task. Read the code of affected "
        "components, understand the current state, and create the file "
        f"`SYSTEM_ANALYSIS_{parent_key}.md` in the repository root.\n\n"
        f"The file MUST start with exactly this header "
        f"(copy verbatim):\n\n"
        f"```\n{file_header}```\n\n"
        "Then add these sections:\n"
        "1. **Problem summary** — what exactly is required\n"
        "2. **Current state of the code** — how it works now "
        "(read real code, don't guess!)\n"
        "3. **Affected components** — list of modules/packages "
        "with file paths\n"
        "4. **Dependencies** — upstream/downstream, who calls whom\n"
        "5. **Existing utilities** — what's already in the codebase that can "
        "be reused (check with Grep!)\n"
        "6. **Risks** — potential issues during implementation\n"
        "7. **Edge cases** — non-standard situations\n"
        "8. **Recommended approach** — concrete implementation steps "
        "with file paths\n\n"
        "Format: markdown, lists, code examples where needed.\n"
        "Length: 200-500 lines — detailed but to the point.\n\n"
    ).strip()


# ── Stage: architecture ────────────────────────────────────────────────────────

def build_architecture_prompt(issue: dict, sys_analysis: str = "") -> str:
    jira_domain = issue.get("jira_domain", "")
    parent_key = issue.get("parent_key", issue["key"])
    parent_summary = issue.get("parent_summary", issue["summary"])
    parent_url = (f"https://{jira_domain}/browse/{parent_key}"
                  if jira_domain else parent_key)
    subtask_url = (f"https://{jira_domain}/browse/{issue['key']}"
                   if jira_domain else issue['key'])

    file_header = (
        f"# Architecture Decision: [{parent_key}]({parent_url})"
        f" — {parent_summary}\n\n"
        f"> **Jira:** [{parent_key}]({parent_url}) · "
        f"Subtask: [{issue['key']}]({subtask_url})  \n"
        f"> **Stage:** architecture  \n"
        "> Auto-generated by Claudev\n\n"
        "---\n\n"
    )

    context_section = ""
    if sys_analysis:
        context_section = (
            "## System analysis result (previous stage)\n\n"
            f"{sys_analysis[:4000]}\n\n"
        )

    return (
        _base_header(issue)
        + _pre_flight("architecture", parent_key)
        + context_section
        + "## What to do: Architecture Decision\n\n"
        "Study the system analysis and current code. Create the file "
        f"`ARCHITECTURE_DECISION_{parent_key}.md` in the repository root.\n\n"
        f"The file MUST start with exactly this header "
        f"(copy verbatim):\n\n"
        f"```\n{file_header}```\n\n"
        "The file should contain:\n"
        "1. **Context** — briefly, why we are making this change\n"
        "2. **Decision** — concrete architectural decision "
        "with justification. Specify WHICH files to change and HOW.\n"
        "3. **Reuse** — what existing code to use. "
        "Check the codebase with Grep!\n"
        "4. **Alternatives** — what was considered and why rejected\n"
        "5. **API contract** — new/changed endpoints, "
        "data formats\n"
        "6. **Data schema** — if models or storage change\n"
        "7. **Implementation sequence** — step-by-step order "
        "(what to do in the dev stage)\n"
        "8. **Success metrics** — how to know the task is done\n\n"
        "Important:\n"
        "- Follow project principles from CLAUDE.md and STEERING.md "
        "(if they exist)\n"
        "- Do not duplicate existing functionality — "
        "search the codebase before proposing new code\n\n"
    ).strip()


# ── Stage: development ─────────────────────────────────────────────────────────

def build_development_prompt(
    issue: dict,
    sys_analysis: str = "",
    architecture: str = "",
) -> str:
    parent_key = issue.get("parent_key", issue["key"])

    context_parts = []
    if sys_analysis:
        context_parts.append(
            "## System analysis (from previous stage)\n\n"
            + sys_analysis[:3000]
        )
    if architecture:
        context_parts.append(
            "## Architecture decision (from previous stage)\n\n"
            + architecture[:3000]
        )
    context_section = (
        ("\n\n".join(context_parts) + "\n\n") if context_parts else ""
    )

    safety_warning = ""
    if issue.get("safety_relevant"):
        safety_warning = (
            "## SAFETY-RELEVANT\n"
            "Read STEERING.md before starting (if it exists). "
            "Pay extra attention to error handling and edge cases. "
            "Prefer fail-safe defaults.\n\n"
        )

    return (
        _base_header(issue)
        + _pre_flight("development", parent_key)
        + safety_warning
        + context_section
        + _coding_standards()
        + "## What to do: Implementation\n\n"
        "Implement the task STRICTLY following the architecture decision. "
        "If no architecture decision exists — use the system analysis "
        "and task description as guidance.\n\n"
        "### Workflow\n"
        f"1. Read `ARCHITECTURE_DECISION_{parent_key}.md` and "
        f"`SYSTEM_ANALYSIS_{parent_key}.md` (if present in repo).\n"
        "2. Read ARCHITECTURE.md — find related services "
        "and libraries.\n"
        "3. **Find existing code** to reuse:\n"
        "   - Grep for keywords across the codebase\n"
        "   - Look for similar functions in existing modules\n"
        "   - Do NOT create duplicates!\n"
        "4. Implement with minimal changes.\n"
        "5. Write basic tests (pytest) for the new code.\n"
        "6. Update ARCHITECTURE.md if you added/changed a service "
        "or endpoint.\n\n"
        + _test_loop()
        + _post_flight()
    ).strip()


# ── Stage: testing ─────────────────────────────────────────────────────────────

def build_testing_prompt(
    issue: dict,
    sys_analysis: str = "",
    architecture: str = "",
) -> str:
    parent_key = issue.get("parent_key", issue["key"])

    context_parts = []
    if sys_analysis:
        context_parts.append(
            "## System analysis\n\n" + sys_analysis[:2000]
        )
    if architecture:
        context_parts.append(
            "## Architecture decision\n\n" + architecture[:2000]
        )
    context_section = (
        ("\n\n".join(context_parts) + "\n\n") if context_parts else ""
    )

    return (
        _base_header(issue)
        + _pre_flight("testing", parent_key)
        + context_section
        + "## What to do: Testing\n\n"
        "### Step 1: Decide if tests are needed\n\n"
        "NOT every change needs tests. Before writing anything, evaluate:\n"
        "- Config changes, docs, minor refactors → **0 tests** (skip this stage)\n"
        "- Simple bug fix or small feature → **1-2 tests** for the core behavior\n"
        "- New API endpoint, complex logic, safety-critical code → "
        "**more tests** as appropriate\n\n"
        "If the change doesn't warrant tests, just write a brief comment "
        "in the code explaining why, and stop. Do NOT write tests "
        "just to have tests.\n\n"
        "### Step 2: Understand the changes\n"
        "1. Read the code that was changed for this task "
        f"(see `ARCHITECTURE_DECISION_{parent_key}.md` → "
        "section 'Implementation sequence')\n"
        "2. Look at existing tests in `tests/` — "
        "use the same patterns and fixtures\n"
        "3. Check `tests/conftest.py` — "
        "what fixtures are already available\n\n"
        "### Step 3: Write only meaningful tests\n"
        "Focus on what matters most:\n"
        "1. **Happy path** — does the main use case work?\n"
        "2. **Edge cases** — only if there are real edge cases\n"
        "3. **Error handling** — only if the code has explicit "
        "error handling worth testing\n\n"
        "Quality over quantity. 2 good tests > 10 trivial ones.\n\n"
        "### Test rules\n"
        "- pytest, NOT unittest\n"
        "- Deterministic (no time.sleep, no random without seed)\n"
        "- Each test checks one thing\n"
        "- Names: `test_<what>_<when>_<expected_result>`\n"
        "- Reuse fixtures from conftest.py\n"
        "- Do NOT mock what you can test directly\n"
        "- Do NOT write redundant tests for trivial getters/setters\n\n"
        + _test_loop()
        + _post_flight()
    ).strip()


# ── Router ─────────────────────────────────────────────────────────────────────

def build_stage_prompt(issue: dict, artifact_context: dict) -> str:
    """Route to the correct prompt builder based on issue['stage']."""
    stage = issue.get("stage", "")
    sys_analysis = artifact_context.get("sys-analysis", "")
    architecture = artifact_context.get("architecture", "")

    if stage == "sys-analysis":
        return build_sys_analysis_prompt(issue)
    elif stage == "architecture":
        return build_architecture_prompt(
            issue, sys_analysis=sys_analysis
        )
    elif stage == "development":
        return build_development_prompt(
            issue, sys_analysis=sys_analysis,
            architecture=architecture,
        )
    elif stage == "testing":
        return build_testing_prompt(
            issue, sys_analysis=sys_analysis,
            architecture=architecture,
        )
    else:
        from orchestrator import build_claude_prompt
        return build_claude_prompt(
            issue,
            {"type": "feature", "complexity": "medium",
             "needs_tests": True, "safety_relevant": False,
             "main_files": []},
        )


# ── Planning pipeline ─────────────────────────────────────────────────────────

def build_plan_prompt(issue: dict) -> str:
    """Build prompt for the planning pipeline (PLAN: prefix tasks).

    Claude Code reads the codebase and breaks down a feature/project
    description into epics and tasks, outputting structured JSON.
    """
    desc_text = issue.get('description_text', '')
    summary = issue.get('summary', '')
    epic_context = issue.get('epic_context', '')

    context_section = ""
    if epic_context:
        context_section = (
            "## Project/epic context\n"
            f"{epic_context}\n\n"
        )

    return (
        "## Planning task\n\n"
        f"Feature/project: **{summary}**\n\n"
        + context_section
        + (f"## Description\n\n{desc_text}\n\n" if desc_text else "")
        + "## Mandatory reading\n\n"
        "Read these files (if they exist) to understand the project:\n"
        "1. **CLAUDE.md** — project rules and conventions\n"
        "2. **ARCHITECTURE.md** — project structure, components, "
        "dependencies\n"
        "3. **STEERING.md** — design principles, constraints\n\n"
        "## What to do\n\n"
        "Break down the feature/project described above into "
        "**epics and tasks**. Each task should be small enough "
        "for one dev pipeline run (a few hours of coding).\n\n"
        "Study the codebase first:\n"
        "- Understand the current architecture\n"
        "- Identify which components need changes\n"
        "- Find existing code that can be reused\n"
        "- Consider dependencies between tasks\n\n"
        "## Output format\n\n"
        "Reply with ONLY a JSON object (no backticks, no markdown), "
        "structured exactly like this:\n\n"
        "{\n"
        '  "epics": [\n'
        "    {\n"
        '      "title": "Epic title — short and clear",\n'
        '      "description": "What this epic achieves, 2-3 sentences",\n'
        '      "tasks": [\n'
        "        {\n"
        '          "title": "Task title — actionable, starts with verb",\n'
        '          "description": "What exactly to do. Include: '
        "which files to change, what the expected behavior is, "
        'acceptance criteria. 3-5 sentences.",\n'
        '          "labels": ["domain:api", "service:backend"]\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "## Rules\n\n"
        "- Each epic = a logical group of related changes\n"
        "- Each task = one focused unit of work (1 PR)\n"
        "- Task titles start with a verb: "
        '"Add...", "Fix...", "Implement...", "Refactor..."\n'
        "- Task descriptions must have enough detail for Claude Code "
        "to implement without asking questions\n"
        "- Order tasks by dependency: independent tasks first, "
        "dependent tasks later\n"
        "- Include infrastructure/config tasks if needed "
        "(DB migrations, new configs, etc.)\n"
        "- Typically 2-5 epics, 2-6 tasks per epic\n"
        "- Do NOT include testing as a separate task — "
        "the dev pipeline adds tests automatically\n"
    ).strip()
