import os

PORT = int(os.environ.get("PORT", 8090))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")

# DeepSeek (оркестратор)
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# Jira
JIRA_DOMAIN = os.environ["JIRA_DOMAIN"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "TRUST")

# GitHub
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]            # trust-layer-pipeline repo
GITHUB_TOKEN_TRUST_LAYER = os.environ.get(           # trust-layer repo (clone + PR)
    "GITHUB_TOKEN_TRUST_LAYER", os.environ["GITHUB_TOKEN"]
)
GITHUB_REPO = os.environ["GITHUB_REPO"]

# Pipeline
TRIGGER_STATUS = os.environ.get("TRIGGER_STATUS", "In Progress")
STAGE_BRANCH = os.environ.get("STAGE_BRANCH", "stage")
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", 3))
JOB_TIMEOUT_MINUTES = int(os.environ.get("JOB_TIMEOUT_MINUTES", 30))

# ── Jira status names (must match your Jira workflow exactly) ─────────────────
STATUS_TODO = os.environ.get("STATUS_TODO", "To Do")
STATUS_IN_PROGRESS = os.environ.get("STATUS_IN_PROGRESS", "In Progress")
STATUS_DONE = os.environ.get("STATUS_DONE", "Done")
STATUS_READY_FOR_TEST = os.environ.get("STATUS_READY_FOR_TEST", "Ready for Test")
STATUS_IN_REVIEW = os.environ.get("STATUS_IN_REVIEW", "In Review")
STATUS_IN_TESTING = os.environ.get("STATUS_IN_TESTING", "In Testing")
STATUS_MERGE = os.environ.get("STATUS_MERGE", "Ready to Merge")

# ── Pipeline stage labels (applied to Jira sub-tasks) ─────────────────────────
# Label format: "pipeline:<stage>" — человек навешивает при создании подзадачи
PIPELINE_LABEL_PREFIX = "pipeline:"
STAGE_SYS_ANALYSIS = "sys-analysis"    # Claude Code: читает код → SYSTEM_ANALYSIS.md
STAGE_ARCHITECTURE = "architecture"     # Claude Code: читает архитектуру → ARCHITECTURE_DECISION.md
STAGE_DEVELOPMENT = "development"       # Claude Code: пишет код → PR в stage
STAGE_TESTING = "testing"              # Claude Code: пишет тесты → push в dev branch

ALL_STAGES = [STAGE_SYS_ANALYSIS, STAGE_ARCHITECTURE, STAGE_DEVELOPMENT, STAGE_TESTING]

# Этапы, которые могут стартовать одновременно (без предпосылок)
STAGE_PREREQUISITES: dict[str, list[str]] = {
    STAGE_SYS_ANALYSIS: [],
    STAGE_ARCHITECTURE: [],
    STAGE_DEVELOPMENT: [STAGE_SYS_ANALYSIS, STAGE_ARCHITECTURE],
    STAGE_TESTING: [STAGE_DEVELOPMENT],
}

# Этапы, которые пишут артефакты в Jira (markdown), а не код
ARTIFACT_STAGES = {STAGE_SYS_ANALYSIS, STAGE_ARCHITECTURE}
# Этапы, которые пушат код в GitHub
CODE_STAGES = {STAGE_DEVELOPMENT, STAGE_TESTING}
