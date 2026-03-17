# Trust Layer Pipeline

Automated development pipeline: Jira task → Claude Code → GitHub PR → auto-merge → Done.

When you move a task to **In Progress**, the pipeline takes over: it creates subtasks, runs system analysis and architecture design, writes code, writes tests, opens a PR. When you approve and move to **Ready to Merge** — it merges automatically.

---

## How it works

```
You create a task with business requirements
           │
           ▼
    ┌─────────────┐
    │   To Do     │  You write the description
    └──────┬──────┘
           │  you move it
           ▼
    ┌─────────────┐
    │ In Progress │ ◄── 🤖 TRIGGER: pipeline starts
    └──────┬──────┘
           │
           │  🤖 DeepSeek suggests labels (service:, domain:, lib:)
           │  🤖 Creates 4 subtasks automatically:
           │
           ├──► [TASK-X] System Analysis    (pipeline:sys-analysis)
           │         └─ 🤖 Claude Code reads codebase → writes SYSTEM_ANALYSIS.md
           │
           ├──► [TASK-X] Architecture       (pipeline:architecture)
           │         └─ 🤖 Claude Code designs solution → writes ARCHITECTURE_DECISION.md
           │
           │  (both run in parallel, no dependencies between them)
           │
           ▼  when both are Done ↓
           │
           ├──► [TASK-X] Development        (pipeline:development)
           │         └─ 🤖 Claude Code writes code using both artifacts
           │            → git commit → opens PR to `stage` branch
           │
           ▼  when Development is Done ↓
           │
           ├──► [TASK-X] Testing            (pipeline:testing)
                     └─ 🤖 Claude Code writes tests → pushes to branch
                        → subtask marked Done
           │
           ▼
    ┌─────────────┐
    │  In Review  │ ◄── 🤖 Pipeline moves here after PR is created
    └──────┬──────┘
           │  you review the PR on GitHub
           ▼
    ┌──────────────────┐
    │  Ready for Test  │  👤 You move here after approving the code
    └────────┬─────────┘
             │  you test the feature
             ▼
    ┌──────────────┐
    │  In Testing  │  👤 You move here while testing
    └──────┬───────┘
           │  tests pass
           ▼
    ┌──────────────────┐
    │ Ready to Merge   │  👤 You move here  ◄── 🤖 TRIGGER: auto-merge
    └────────┬─────────┘
             │  🤖 Pipeline finds the PR, squash-merges into main
             ▼
    ┌──────────┐
    │   Done   │ ◄── 🤖 Pipeline moves here after successful merge
    └──────────┘
```

---

## Jira statuses

| Status | Who | Action |
|--------|-----|--------|
| **To Do** | You | Task is in backlog |
| **In Progress** | You → 🤖 | You move it → pipeline starts |
| **In Review** | 🤖 | Pipeline sets this after opening PR |
| **Ready for Test** | You | Code review passed, ready to test |
| **In Testing** | You | You are testing the feature |
| **Ready to Merge** | You → 🤖 | Testing passed, you move it → pipeline merges |
| **Done** | 🤖 | Pipeline sets this after successful merge |

**Important:** create all statuses with these exact English names in Jira. The pipeline matches by name.

---

## Telegram notifications

The pipeline sends you a message when:

| Event | Message |
|-------|---------|
| 📊 System analysis ready | Artifact posted to Jira subtask |
| 🏗 Architecture ready | Artifact posted to Jira subtask |
| 🔀 PR created | Link to PR + file count |
| ✅ All stages done | Task ready for your review |
| 🚀 Merged | Squash-merged into main → Done |
| ❌ Error | Stage name + error message |

---

## Technology split

| Tool | Role |
|------|------|
| **Claude Code** (Pro subscription) | All intellectual work: analysis, architecture, code, tests |
| **DeepSeek** | Pipeline orchestration only: parse Jira description, classify task type, suggest labels, summarize git diff for PR |

---

## Setup

### 1. Environment variables

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `CLAUDE_AUTH_JSON` | Base64 of `~/.claude/.credentials.json` — Claude Code auth |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `JIRA_DOMAIN` | Subdomain only: `mycompany` for `mycompany.atlassian.net` |
| `JIRA_EMAIL` | Your Jira account email |
| `JIRA_API_TOKEN` | Jira API token from id.atlassian.com |
| `JIRA_PROJECT_KEY` | Project key, e.g. `TRUST` |
| `GITHUB_TOKEN` | Fine-grained token: Contents + Pull requests (read/write) |
| `GITHUB_REPO` | `owner/repo` |
| `WEBHOOK_SECRET` | Random string — same value in Jira webhook URL |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID |

Status variables (only change if your Jira workflow uses different names):

```
STATUS_TODO=To Do
STATUS_IN_PROGRESS=In Progress
STATUS_IN_REVIEW=In Review
STATUS_READY_FOR_TEST=Ready for Test
STATUS_IN_TESTING=In Testing
STATUS_MERGE=Ready to Merge
STATUS_DONE=Done
```

### 2. Claude Code auth

On your machine:
```bash
base64 -w0 ~/.claude/.credentials.json
```
Paste the output as `CLAUDE_AUTH_JSON`.

### 3. GitHub token

Go to **github.com → Settings → Developer settings → Personal access tokens → Fine-grained tokens**.

Required permissions for your repository:
- **Contents** — Read and write
- **Pull requests** — Read and write
- **Metadata** — Read (set automatically)

### 4. Jira webhook

Go to **Settings → System → Webhooks → Create webhook**:

- **URL:** `https://your-domain/webhook/jira?secret=YOUR_WEBHOOK_SECRET`
- **Events:** Issue → `updated` and `created`
- **JQL filter:** `project = TRUST` ← limits to your project only, other boards are ignored

### 5. Deploy to Railway

Push to GitHub → connect repo in Railway → add all variables in the **Variables** tab.

Railway sets `PORT` automatically — do not set it manually.

---

## Local run with Docker

```bash
# Mount your ~/.claude for Max subscription auth
docker compose up --build
```

Check:
```bash
curl http://localhost:8090/health
# {"status":"ok","active_jobs":0,"total_jobs":0}
```

---

## Monitor jobs

```bash
# All recent jobs
curl https://your-domain/jobs | python3 -m json.tool

# Specific job
curl https://your-domain/jobs/<job_id>
```

---

## Pipeline labels

The pipeline uses Jira labels to identify subtask stages and categorize tasks by domain:

**Stage labels** (set automatically on subtasks):
```
pipeline:sys-analysis
pipeline:architecture
pipeline:development
pipeline:testing
```

**Auto-suggested domain labels** (DeepSeek suggests based on task content):
```
service:constraint-solver    service:robot-bridge    service:operator-ui  ...
lib:ontology                 lib:rlm                 lib:validator-math   ...
domain:safety                domain:navigation       domain:perception    ...
```
