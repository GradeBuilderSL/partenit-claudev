import json
import logging

import httpx

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger("pipeline.orchestrator")


def _call_llm(system: str, user: str, max_tokens: int = 2000) -> str:
    """Call the orchestrator LLM (any OpenAI-compatible API).

    Works with DeepSeek, OpenAI, Groq, Together, Ollama, LiteLLM proxy, etc.
    Configure via LLM_BASE_URL, LLM_API_KEY, LLM_MODEL env vars.
    """
    response = httpx.post(
        f"{LLM_BASE_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def parse_adf_to_text(adf_json) -> str:
    """Jira description (ADF JSON) → plain markdown."""
    if isinstance(adf_json, str):
        return adf_json
    if not adf_json:
        return ""
    return _call_llm(
        system=(
            "Convert Atlassian Document Format JSON to clean markdown. "
            "Keep headings, lists, code blocks. Do not add anything extra."
        ),
        user=json.dumps(adf_json, ensure_ascii=False),
    )


def classify_issue(summary: str, description: str, labels: list) -> dict:
    """Classify a task to adapt the Claude Code prompt."""
    result = _call_llm(
        system=(
            "You are a task classifier for a software project.\n"
            "Reply with ONLY JSON, no backticks:\n"
            "{\n"
            '  "type": "bug|endpoint|feature|test|refactor|config",\n'
            '  "complexity": "simple|medium|complex",\n'
            '  "main_files": ["likely files"],\n'
            '  "needs_tests": true,\n'
            '  "safety_relevant": false\n'
            "}"
        ),
        user=f"Summary: {summary}\nLabels: {labels}\nDescription:\n{description}",
    )
    try:
        cleaned = (
            result.strip()
            .removeprefix("```json")
            .removesuffix("```")
            .strip()
        )
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM classification, fallback default.")
        return {
            "type": "feature",
            "complexity": "medium",
            "main_files": [],
            "needs_tests": True,
            "safety_relevant": False,
        }


def suggest_labels(summary: str, description: str) -> list[str]:
    """Use the orchestrator LLM to suggest Jira labels from the project taxonomy.

    Returns a list of label strings (max 5) to add to the issue.
    Existing pipeline:xxx labels are NOT touched — this only adds domain/service tags.

    NOTE: Customize the taxonomy below to match your project's services, libraries,
    and domains. The labels here are examples — replace them with your own.
    """
    # ── CUSTOMIZE THIS: replace with your project's actual labels ──
    taxonomy = (
        "# Examples (replace with your own):\n"
        "service:backend             — main backend service\n"
        "service:frontend            — web frontend\n"
        "lib:core                    — core business logic\n"
        "domain:api                  — HTTP endpoints, integrations\n"
        "domain:infra                — Docker, CI, configs, deploy\n"
    )

    result = _call_llm(
        system=(
            "You are a task tagger for a software project.\n"
            "Pick 1 to 5 tags from the taxonomy below.\n"
            "Reply with ONLY a JSON array of strings, no backticks, e.g.:\n"
            '["service:auth", "domain:security", "lib:core"]\n\n'
            + taxonomy
        ),
        user=f"Summary: {summary}\nDescription (first 800 chars):\n{description[:800]}",
        max_tokens=200,
    )
    try:
        cleaned = result.strip().removeprefix("```json").removesuffix("```").strip()
        labels = json.loads(cleaned)
        if isinstance(labels, list):
            return [lbl for lbl in labels
                    if isinstance(lbl, str) and lbl in _VALID_LABELS][:5]
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse LLM label suggestions")
    return []


# ── CUSTOMIZE THIS: must match the taxonomy above ──
_VALID_LABELS = {
    "service:backend",
    "service:frontend",
    "lib:core",
    "domain:api",
    "domain:infra",
}


def build_claude_prompt(issue: dict, classification: dict) -> str:
    """Build prompt for Claude Code (legacy single-stage flow)."""
    safety_warning = ""
    if classification.get("safety_relevant"):
        safety_warning = (
            "## ⚠️ SAFETY-RELEVANT\n"
            "Read STEERING.md before starting (if it exists). "
            "Pay extra attention to error handling. Prefer fail-safe defaults.\n\n"
        )

    type_instructions = {
        "bug": "Find the bug → write a failing test → fix → test passes.",
        "endpoint": "Find the service → add handler → tests for happy path + error.",
        "feature": "Break into steps → implement → tests.",
        "test": "Write tests: happy path, edge cases, errors.",
        "refactor": "Tests green BEFORE → refactor → tests green AFTER.",
        "config": "Change config → verify it starts.",
    }

    task_type = classification.get("type", "feature")
    instruction = type_instructions.get(task_type, type_instructions["feature"])

    return (
        f"{safety_warning}"
        f"## Task: {issue['key']} — {issue['summary']}\n\n"
        f"Type: {issue['issue_type']} | Priority: {issue['priority']}\n"
        f"Components: {', '.join(issue.get('components', []))}\n\n"
        "## Description\n"
        f"{issue['description_text']}\n\n"
        "## Approach\n"
        f"{instruction}\n\n"
        "## Rules\n"
        "1. Read CLAUDE.md for project context.\n"
        "2. Minimal changes — only what the task requires.\n"
        "3. pytest tests/ — ALL tests must pass.\n"
        "4. If you modify a service — update ARCHITECTURE.md.\n"
        "5. Do NOT refactor unrelated code. Do NOT create commits.\n"
        "6. If unclear → leave a TODO with explanation.\n"
    ).strip()


def analyze_result(claude_output: str, changed_files: list) -> dict:
    """Orchestrator LLM analyzes what Claude Code produced."""
    result = _call_llm(
        system=(
            "Analyze the Claude Code output. Reply with JSON, no backticks:\n"
            '{"summary_ru":"2-3 sentences","files_changed":["..."],'
            '"tests_status":"passed|failed|unknown","concerns":["if any"]}'
        ),
        user=(
            f"Output (last 3000 chars):\n{claude_output[-3000:]}\n\n"
            f"Files:\n{changed_files}"
        ),
    )
    try:
        cleaned = (
            result.strip()
            .removeprefix("```json")
            .removesuffix("```")
            .strip()
        )
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM analysis, using fallback.")
        return {
            "summary_ru": "Task completed. Check the PR.",
            "files_changed": changed_files,
            "tests_status": "unknown",
            "concerns": [],
        }
