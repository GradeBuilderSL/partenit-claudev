<p align="center">
  <img src="partenit.png" alt="Partenit" width="320">
</p>

<h1 align="center">Claudev</h1>

<p align="center">
  <strong>Jira task → Claude Code → GitHub PR → auto-merge → Done</strong><br>
  Fully automated 4-stage development pipeline powered by AI
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#how-it-works">How it Works</a> &bull;
  <a href="#setup">Setup</a> &bull;
  <a href="#telegram-bot">Telegram Bot</a> &bull;
  <a href="#deploy-to-railway">Deploy</a>
</p>

---

Move a Jira task to **In Progress** and the pipeline takes over: creates subtasks, runs system analysis and architecture design via Claude Code, writes code, writes tests, and opens a PR. When you approve and move to **Ready to Merge** — it merges automatically.

---

## How it works

```
You create a task with business requirements
           |
           v
    +-------------+
    |   To Do     |  You write the description
    +------+------+
           |  you move it (or use /new in Telegram)
           v
    +-------------+
    | In Progress | <-- TRIGGER: pipeline starts
    +------+------+
           |
           |  LLM suggests labels (service:, domain:, lib:)
           |  Creates 4 subtasks automatically:
           |
           +-->  System Analysis        (pipeline:sys-analysis)
           |         \-- Claude Code reads codebase -> SYSTEM_ANALYSIS.md
           |
           +-->  Architecture           (pipeline:architecture)
           |         \-- Claude Code designs solution -> ARCHITECTURE_DECISION.md
           |
           |  (both run in parallel, no dependencies)
           |
           v  when both Done:
           |
           +-->  Development            (pipeline:development)
           |         \-- Claude Code writes code -> PR to stage branch
           |
           v  when Development Done:
           |
           +-->  Testing                (pipeline:testing)
           |         \-- Claude Code writes tests -> pushes to branch
           |
           v  all subtasks Done
    +-------------+
    |  In Review  | <-- Auto-transitioned when all stages complete
    +------+------+
           |  you review the PR on GitHub
           v
    +------------------+
    |  Ready to Merge  |  You move here -> auto-merge
    +--------+---------+
             v
    +----------+
    |   Done   | <-- Pipeline sets after merge
    +----------+
```

### Technology split

| Tool | Role | Cost |
|------|------|------|
| **Claude Code** (Max subscription) | All intellectual work: analysis, architecture, code, tests | Included in Max sub |
| **Orchestrator LLM** (configurable) | Lightweight tasks: parse Jira descriptions, classify issues, suggest labels, summarize output | ~$0.01-0.05 per run |

The orchestrator LLM can be **any OpenAI-compatible API**: DeepSeek, GPT-4o-mini, Groq, Together, Ollama, etc. Use whatever is cheapest — these tasks don't need a powerful model.

---

## Quick start

```bash
git clone https://github.com/GradeBuilderSL/partenit-claudev.git
cd partenit-claudev
cp .env.example .env
# Edit .env — see sections below
docker compose up --build
```

---

## Setup

### 1. Claude Code authentication

The pipeline uses Claude Code CLI with a **Max subscription** (not API keys).

1. Install Claude Code: `npm install -g @anthropic-ai/claude-code`
2. Run `claude` once and complete the login flow in your browser
3. This creates `~/.claude/.credentials.json`
4. For cloud deployment, base64-encode it:

```bash
base64 -w0 ~/.claude/.credentials.json
```

5. Set the result as `CLAUDE_AUTH_JSON` in your `.env` or Railway variables

> **Local Docker:** Skip `CLAUDE_AUTH_JSON` — `docker-compose.yml` mounts `~/.claude` directly.

### 2. Orchestrator LLM

Any OpenAI-compatible API works. Set three variables:

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Cost |
|----------|----------------|-------------|------|
| **DeepSeek** (default) | `https://api.deepseek.com` | `deepseek-chat` | ~$0.01/run |
| **OpenAI** | `https://api.openai.com` | `gpt-4o-mini` | ~$0.02/run |
| **Groq** (free tier) | `https://api.groq.com/openai` | `llama-3.1-70b-versatile` | Free |
| **Together** | `https://api.together.xyz` | `meta-llama/Llama-3-70b-chat-hf` | ~$0.01/run |
| **Ollama** (local) | `http://localhost:11434` | `llama3.1` | Free |

Set `LLM_API_KEY` with the API key for your chosen provider.

> Backward compatible: `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` still work if `LLM_*` vars aren't set.

### 3. GitHub token

**github.com → Settings → Developer settings → Fine-grained personal access tokens**

Required permissions for the target repository:

| Permission | Access |
|-----------|--------|
| **Contents** | Read and write |
| **Pull requests** | Read and write |
| **Metadata** | Read (auto-selected) |

### 4. Jira API token

1. Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Create a new API token
3. Set `JIRA_EMAIL` and `JIRA_API_TOKEN`

### 5. Jira workflow (statuses)

The pipeline matches Jira statuses **by name**. You need these statuses in your workflow:

| Status | Required? | Default name | Purpose |
|--------|-----------|-------------|---------|
| Backlog / To Do | Yes | `To Do` | Task is waiting |
| In Progress | **Yes** | `In Progress` | **Trigger** — pipeline starts here |
| In Review | Recommended | `In Review` | Auto-set when all stages complete |
| Ready for Test | Optional | `Ready for Test` | You move here after code review |
| In Testing | Optional | `In Testing` | You're testing the feature |
| Ready to Merge | **Yes** | `Ready to Merge` | **Trigger** — pipeline auto-merges |
| Done | **Yes** | `Done` | Auto-set after merge |
| Cancelled | Recommended | `Cancelled` | Stops the pipeline immediately |

**Don't want all these columns?** You only need: **To Do → In Progress → Ready to Merge → Done**. The others are optional review steps.

**Using different names?** Override them in `.env`:
```
STATUS_IN_PROGRESS=Working
STATUS_MERGE=Merge Me
STATUS_DONE=Completed
```

The pipeline also understands Russian status names automatically (e.g., "В работе", "Готово", "Отменено").

### 6. Jira webhook

**Project Settings → System → Webhooks → Create webhook**:

- **URL:** `https://your-app.up.railway.app/webhook/jira?secret=YOUR_WEBHOOK_SECRET`
- **Events:** Issue → `updated`
- **JQL filter:** `project = MYPROJECT`

---

## Telegram bot

For notifications + interactive commands:

1. Open Telegram → **@BotFather** → `/newbot` → get the **bot token**
2. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
3. **Set the webhook** (run once after deploying):

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://your-app.up.railway.app/webhook/telegram"
```

### Bot commands

| Command | Description |
|---------|-------------|
| `/new Fix login timeout` | Create a Jira task and start the pipeline immediately |
| `/start PROJ-123` | Move an existing task to In Progress |
| `/cancel PROJ-123` | Cancel a running pipeline |
| `/status` | Show active pipelines and queue |
| `/status PROJ-123` | Show task status with all stage progress |
| `/help` | List available commands |

`/new` is the fastest way to get something into the pipeline — one message in Telegram and Claude Code starts working on it.

---

## Deploy to Railway

1. Push this repo to GitHub
2. [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
3. Select your repo
4. Go to the **Variables** tab and add all variables from `.env.example`
   - Railway sets `PORT` automatically — **do not set it**
   - Set `CLAUDE_AUTH_JSON` with the base64-encoded credentials
5. Copy the generated URL for Jira and Telegram webhooks
6. Railway builds from the `Dockerfile` and deploys automatically

**Health check:** `GET /health` — monitored automatically.

---

## Local run with Docker

```bash
docker compose up --build
```

```bash
curl http://localhost:8090/health
# {"status":"ok","active_jobs":0,"total_jobs":0}
```

---

## Concurrency and rate limits

### Pipeline concurrency

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_CONCURRENT_PIPELINES` | `1` | How many parent tasks can run through the pipeline simultaneously. Others are queued FIFO. |
| `MAX_CONCURRENT_JOBS` | `3` | Max simultaneous Claude Code processes (across all pipelines). |

With `MAX_CONCURRENT_PIPELINES=1`, one task goes through all 4 stages before the next starts. Set to `2+` if you have a higher-tier Claude subscription.

### Timeouts and retries

| Setting | Default | Description |
|---------|---------|-------------|
| `JOB_TIMEOUT_MINUTES` | `60` | Max runtime for a single Claude Code call. Kill after this. |
| `MAX_RETRIES` | `3` | How many times to retry on rate limit (429) errors. |
| `RETRY_DELAY_MINUTES` | `10` | Minutes to wait between retries. |

### Rate limit handling

If Claude Code hits subscription rate limits, the pipeline **automatically waits and retries**:

- Detects rate limits by error text (`rate limit`, `429`, `overloaded`)
- Waits `RETRY_DELAY_MINUTES` between attempts
- Sends a Telegram notification on each retry
- If all attempts fail — marks the task as error in Jira

Progress within a single attempt is not preserved — Claude Code restarts from the same prompt.

### Queue behavior

When `MAX_CONCURRENT_PIPELINES` is reached:
- New tasks are queued with a Jira comment showing queue position
- When a slot opens, the next task starts automatically
- Telegram notifies when a queued task starts (with wait time)

---

## Auto-transition

When all 4 pipeline subtasks reach **Done**, the parent task is automatically moved to the status defined by `AUTO_TRANSITION_ON_COMPLETE` (default: `In Review`).

Set it to match your workflow:
```
AUTO_TRANSITION_ON_COMPLETE=In Review          # default
AUTO_TRANSITION_ON_COMPLETE=Ready for Test     # if you want to test first
AUTO_TRANSITION_ON_COMPLETE=                   # empty = disabled, you move it manually
```

---

## Cancellation

**Via Jira:** Move the task or subtask to **Cancelled** — pipeline stops immediately, kills Claude Code process, no changes pushed.

**Via Telegram:** `/cancel PROJ-123`

**Via API:** `curl -X POST https://your-domain/jobs/<job_id>/cancel`

---

## Monitoring

```bash
curl https://your-domain/health     # Health + active/queued pipelines
curl https://your-domain/jobs       # Recent jobs (last 20)
curl https://your-domain/jobs/<id>  # Specific job details
curl https://your-domain/queue      # Pipeline queue
```

Or use `/status` in Telegram.

---

## Environment variables

### Required

| Variable | Description |
|----------|-------------|
| `WEBHOOK_SECRET` | Random string — same value in Jira webhook URL |
| `LLM_API_KEY` | API key for the orchestrator LLM |
| `JIRA_DOMAIN` | Subdomain only: `mycompany` for `mycompany.atlassian.net` |
| `JIRA_EMAIL` | Your Jira account email |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_PROJECT_KEY` | Project key, e.g. `MYPROJECT` |
| `GITHUB_TOKEN` | GitHub token for pipeline operations |
| `GITHUB_TOKEN_TARGET` | GitHub token for the target repo (clone + PR) |
| `GITHUB_REPO` | `owner/repo` of the target repository |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_AUTH_JSON` | — | Base64 of `~/.claude/.credentials.json` (cloud only) |
| `LLM_BASE_URL` | `https://api.deepseek.com` | Orchestrator LLM endpoint |
| `LLM_MODEL` | `deepseek-chat` | Orchestrator LLM model name |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for notifications |
| `MAX_CONCURRENT_PIPELINES` | `1` | Parallel parent tasks |
| `MAX_CONCURRENT_JOBS` | `3` | Parallel Claude Code processes |
| `JOB_TIMEOUT_MINUTES` | `60` | Max runtime per Claude Code call |
| `MAX_RETRIES` | `3` | Rate limit retry attempts |
| `RETRY_DELAY_MINUTES` | `10` | Delay between retries |
| `AUTO_TRANSITION_ON_COMPLETE` | `In Review` | Parent status when all stages done (empty = disabled) |
| `STAGE_BRANCH` | `stage` | Base branch for PRs |
| `TRIGGER_STATUS` | `In Progress` | Jira status that triggers the pipeline |

See [.env.example](.env.example) for the full list including all status name overrides.

---

## Best practice: project context files

Claudev works best when your target repository contains files that describe the project for AI. Without them, Claude Code still works — but with them, it writes code that actually fits your architecture.

### Recommended files

| File | Purpose |
|------|---------|
| **CLAUDE.md** | Rules and conventions for AI assistants: what to do, what to avoid, priorities. Think of it as onboarding docs for your AI developer. |
| **ARCHITECTURE.md** | Project structure: components, how they connect, data flows, tech stack. The more detail, the better Claude understands where to make changes. |
| **STEERING.md** | Design principles and hard constraints: things that must not be changed, invariants, boundaries between modules. |

These files are read by Claude Code **before every stage** — they're the difference between "generic code that kind of works" and "code that fits your project perfectly."

> For detailed recommendations on writing effective CLAUDE.md and ARCHITECTURE.md files, see our guide at [partenit.io](https://partenit.io).

---

## Customization

### Label taxonomy

Edit [orchestrator.py](orchestrator.py) → `suggest_labels()` to define your project's services, libraries, and domains. The orchestrator LLM auto-suggests labels from this taxonomy.

### Prompts

Edit [prompts.py](prompts.py) to customize what Claude Code does at each stage. The coding standards, post-flight checklist, and stage instructions are all configurable.

### Multi-repo support

Add `repo:bridge` label to a Jira task to route it to a secondary GitHub repo. Configure `GITHUB_REPO_BRIDGE` and `GITHUB_TOKEN_BRIDGE` in `.env`.

---

## License

MIT

---

<p align="center">
  <a href="https://partenit.io">partenit.io</a><br>
  Made with love for robots
</p>
