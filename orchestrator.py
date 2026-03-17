import json
import logging

import httpx

from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

logger = logging.getLogger("pipeline.orchestrator")


def _call_deepseek(system: str, user: str, max_tokens: int = 2000) -> str:
    """Один вызов DeepSeek чат-модели."""
    response = httpx.post(
        f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json={
            "model": "deepseek-chat",
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
    return _call_deepseek(
        system=(
            "Конвертируй Atlassian Document Format JSON в чистый markdown. "
            "Сохрани заголовки, списки, код. Ничего не добавляй от себя."
        ),
        user=json.dumps(adf_json, ensure_ascii=False),
    )


def classify_issue(summary: str, description: str, labels: list) -> dict:
    """Классификация задачи для адаптации промпта Claude Code."""
    result = _call_deepseek(
        system=(
            "Ты классификатор задач для проекта Trust Layer (Python, ~50 сервисов).\n"
            "Ответь ТОЛЬКО JSON без backticks:\n"
            "{\n"
            '  \"type\": \"bug|endpoint|feature|test|refactor|config\",\n'
            '  \"complexity\": \"simple|medium|complex\",\n'
            '  \"main_files\": [\"предположительные файлы\"],\n'
            '  \"needs_tests\": true,\n'
            '  \"safety_relevant\": false\n'
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
        logger.warning("Failed to parse DeepSeek classification, fallback default.")
        return {
            "type": "feature",
            "complexity": "medium",
            "main_files": [],
            "needs_tests": True,
            "safety_relevant": False,
        }


def run_deepseek_artifact(stage: str, issue: dict, artifact_context: dict) -> str:
    """Run sys-analysis or architecture entirely via DeepSeek — no git clone needed.

    Returns the full markdown artifact text to be posted to Jira.
    """
    from prompts import build_stage_prompt

    prompt = build_stage_prompt(issue, artifact_context)

    jira_domain = issue.get("jira_domain", "")
    parent_url = f"https://{jira_domain}/browse/{issue['parent_key']}" if jira_domain else issue['parent_key']
    subtask_url = f"https://{jira_domain}/browse/{issue['key']}" if jira_domain else issue['key']

    _STAGE_TITLES = {
        "sys-analysis": "Системный анализ",
        "architecture": "Архитектурное решение",
    }
    title = _STAGE_TITLES.get(stage, stage)

    header = (
        f"# {title}: [{issue['parent_key']}]({parent_url}) — {issue['summary']}\n\n"
        f"> **Jira:** [{issue['parent_key']}]({parent_url}) · "
        f"Подзадача: [{issue['key']}]({subtask_url})  \n"
        f"> **Этап:** {stage}  \n"
        f"> Сгенерировано автоматически Trust Layer Pipeline (DeepSeek)\n\n"
        "---\n\n"
    )

    result = _call_deepseek(
        system=(
            "Ты старший архитектор и технический аналитик проекта Trust Layer — "
            "safety middleware для роботов (Python, ~50 микросервисов).\n"
            "Отвечай только на русском языке. Формат: markdown.\n"
            "Не добавляй заголовок файла — он уже есть. Начни сразу с первого раздела."
        ),
        user=prompt,
        max_tokens=4000,
    )

    return header + result


def suggest_labels(summary: str, description: str) -> list[str]:
    """Use DeepSeek to suggest Jira labels from the project taxonomy.

    Returns a list of label strings (max 5) to add to the issue.
    Existing pipeline:xxx labels are NOT touched — this only adds domain/service tags.
    """
    taxonomy = (
        "# Сервисы (service:*)\n"
        "service:constraint-solver   — решение конфликтов правил, priority/weight\n"
        "service:mode-controller     — переключение SHADOW/ADVISORY/FULL\n"
        "service:decision-log        — аудит решений, хранение событий\n"
        "service:world-simulator     — симуляция Isaac Sim, сценарии\n"
        "service:skill-library       — библиотека навыков робота\n"
        "service:fleet-policy-hub    — политики для флота роботов\n"
        "service:operator-ui         — фронтенд оператора, fleet dashboard\n"
        "service:sim-dashboard       — дашборд Isaac Sim\n"
        "service:test-dashboard      — дашборд тестирования реального робота\n"
        "service:robot-bridge        — мост между Trust Layer и роботом\n"
        "service:pipeline            — CI/CD автоматизация через Jira\n"
        "\n"
        "# Библиотеки (lib:*)\n"
        "lib:ontology                — правила ISO/IEC, регуляторные нормы\n"
        "lib:rlm                     — rule lifecycle manager, YAML rules\n"
        "lib:validator-math          — GateEngine, числовые проверки\n"
        "lib:fleet-trust-metrics     — метрики токенов, дедлоков флота\n"
        "lib:decision-math           — алгоритмы принятия решений\n"
        "\n"
        "# Домены (domain:*)\n"
        "domain:safety               — безопасность, L1/L2a, e-stop, тилт\n"
        "domain:navigation           — движение, скорость, маршруты\n"
        "domain:perception           — восприятие, камера, LiDAR, VLM\n"
        "domain:robot-control        — управление роботом, адаптеры H1/N2\n"
        "domain:fleet                — управление несколькими роботами\n"
        "domain:infra                — Docker, CI, конфиги, деплой\n"
        "domain:api                  — HTTP эндпоинты, интеграции\n"
        "domain:testing              — тесты, pytest, QA\n"
        "domain:docs                 — документация, ARCHITECTURE.md\n"
    )

    result = _call_deepseek(
        system=(
            "Ты тегировщик задач для проекта Trust Layer.\n"
            "Выбери от 1 до 5 тегов из таксономии ниже.\n"
            "Ответь ТОЛЬКО JSON-массивом строк без backticks, например:\n"
            '["service:ontology", "domain:safety", "lib:rlm"]\n\n'
            + taxonomy
        ),
        user=f"Summary: {summary}\nDescription (first 800 chars):\n{description[:800]}",
        max_tokens=200,
    )
    try:
        cleaned = result.strip().removeprefix("```json").removesuffix("```").strip()
        labels = json.loads(cleaned)
        if isinstance(labels, list):
            # Only allow labels from our taxonomy
            valid_prefixes = ("service:", "lib:", "domain:")
            return [
                lbl for lbl in labels
                if isinstance(lbl, str) and any(lbl.startswith(p) for p in valid_prefixes)
            ][:5]
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse DeepSeek label suggestions")
    return []


def build_claude_prompt(issue: dict, classification: dict) -> str:
    """Собрать промпт для Claude Code."""
    safety_warning = ""
    if classification.get("safety_relevant"):
        safety_warning = (
            "## ⚠️ SAFETY-RELEVANT\n"
            "Прочитай STEERING.md §4 перед работой. "
            "L1/L2a — без ML. Fail-closed. audit_ref обязателен.\n\n"
        )

    type_instructions = {
        "bug": "Найди баг → напиши падающий тест → исправь → тест зелёный.",
        "endpoint": "Найди сервис → добавь handler → тесты happy path + error.",
        "feature": "Разбей на шаги → реализуй → тесты.",
        "test": "Напиши тесты: happy path, edge cases, errors.",
        "refactor": "Тесты зелёные ДО → рефакторинг → тесты зелёные ПОСЛЕ.",
        "config": "Измени конфиг → проверь что стартует.",
    }

    task_type = classification.get("type", "feature")
    instruction = type_instructions.get(task_type, type_instructions["feature"])

    return (
        f"{safety_warning}"
        f"## Задача: {issue['key']} — {issue['summary']}\n\n"
        f"Тип: {issue['issue_type']} | Приоритет: {issue['priority']}\n"
        f"Компоненты: {', '.join(issue.get('components', []))}\n\n"
        "## Описание\n"
        f"{issue['description_text']}\n\n"
        "## Подход\n"
        f"{instruction}\n\n"
        "## Правила\n"
        "1. Прочитай CLAUDE.md для контекста.\n"
        "2. Минимальные изменения — только по задаче.\n"
        "3. pytest tests/ — ВСЕ тесты зелёные.\n"
        "4. Если меняешь сервис — обнови ARCHITECTURE.md.\n"
        "5. НЕ рефакторь \"заодно\". НЕ создавай коммиты.\n"
        "6. Непонятно → TODO с объяснением.\n"
    ).strip()


def analyze_result(claude_output: str, changed_files: list) -> dict:
    """DeepSeek анализирует, что сделал Claude Code."""
    result = _call_deepseek(
        system=(
            "Проанализируй результат Claude Code. JSON без backticks:\n"
            '{"summary_ru":"2-3 предложения","files_changed":["..."],'
            '"tests_status":"passed|failed|unknown","concerns":["если есть"]}'
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
        logger.warning("Failed to parse DeepSeek analysis, using fallback.")
        return {
            "summary_ru": "Задача выполнена. Проверь PR.",
            "files_changed": changed_files,
            "tests_status": "unknown",
            "concerns": [],
        }
