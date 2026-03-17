import uuid
import threading
import time
import logging
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException
import uvicorn

import os
from config import PORT, WEBHOOK_SECRET, TRIGGER_STATUS, MAX_CONCURRENT_JOBS, JIRA_DOMAIN, STATUS_MERGE
from dependency_tracker import get_stage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("pipeline")

app = FastAPI(title="Trust Layer Dev Pipeline")

# In-memory job store
jobs: Dict[str, Dict[str, Any]] = {}
active_count = 0
lock = threading.Lock()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "active_jobs": active_count, "total_jobs": len(jobs)}


@app.get("/jobs")
def list_jobs() -> Dict[str, Any]:
    return {
        "jobs": [
            {
                "job_id": jid,
                "issue_key": j["issue_key"],
                "status": j["status"],
                "created": j.get("created"),
            }
            for jid, j in sorted(
                jobs.items(), key=lambda x: x[1].get("created", 0), reverse=True
            )
        ][:20]
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


def _run_with_tracking(job: Dict[str, Any]) -> None:
    global active_count
    from worker import run_job

    with lock:
        active_count += 1
    job["status"] = "running"
    try:
        run_job(job)
        job["status"] = "done"
    except Exception as e:  # pragma: no cover
        job["status"] = "failed"
        job["error"] = str(e)[:500]
        logger.error("Job %s failed: %s", job["job_id"], e)
    finally:
        with lock:
            active_count -= 1


@app.post("/webhook/jira")
async def webhook_jira(request: Request, secret: str = "") -> Dict[str, Any]:
    # 1. Проверить secret
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")

    body = await request.json()

    # 2. Извлечь данные
    issue = body.get("issue", {})
    fields = issue.get("fields", {})
    issue_key = issue.get("key", "")
    status_name = fields.get("status", {}).get("name", "")
    issue_type = fields.get("issuetype", {}).get("name", "")

    # 3. Фильтр — принимаем TRIGGER_STATUS (старт пайплайна) и STATUS_MERGE (авто-мердж)
    if status_name not in (TRIGGER_STATUS, STATUS_MERGE):
        return {"skipped": True, "reason": f"status={status_name}"}

    ALLOWED_TYPES = ("Task", "Bug", "Story", "Sub-task", "Задача", "Баг", "История", "Подзадача")
    if issue_type not in ALLOWED_TYPES:
        return {"skipped": True, "reason": f"type={issue_type}"}

    labels = fields.get("labels", [])
    # Detect sub-tasks by parent field (reliable across all Jira locales)
    is_subtask = bool(fields.get("parent")) or issue_type in ("Sub-task", "Подзадача")

    # Merge jobs: only parent tasks, skip sub-tasks
    if status_name == STATUS_MERGE and is_subtask:
        return {"skipped": True, "reason": "merge trigger ignored for sub-tasks"}

    if is_subtask:
        # Sub-tasks: must have a pipeline stage label to be processed
        stage = get_stage(labels)
        if not stage:
            return {"skipped": True, "reason": "sub-task without pipeline label"}
    else:
        # Parent tasks: stage=None → setup job (creates subtasks + starts first stages)
        stage = None

    # Idempotency
    for j in jobs.values():
        if j["issue_key"] == issue_key and j["status"] in ("queued", "running"):
            return {"skipped": True, "reason": f"already processing {issue_key}"}

    # Лимит конкурентности
    global active_count
    with lock:
        if active_count >= MAX_CONCURRENT_JOBS:
            return {"skipped": True, "reason": "max concurrent jobs reached"}

    # Extract parent key for sub-tasks
    parent_key = issue_key
    if is_subtask:
        parent_ref = fields.get("parent", {})
        parent_key = parent_ref.get("key", issue_key)

    # 4. Создать job
    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id": job_id,
        "issue_key": issue_key,
        "parent_key": parent_key,
        "summary": fields.get("summary", ""),
        "description": fields.get("description", {}),
        "description_text": "",  # populated by worker
        "issue_type": issue_type,
        "stage": stage,
        "trigger": status_name,
        "jira_domain": f"{JIRA_DOMAIN}.atlassian.net",
        "priority": fields.get("priority", {}).get("name", "Medium"),
        "labels": labels,
        "components": [
            c.get("name", "") if isinstance(c, dict) else c
            for c in fields.get("components", [])
        ],
        "status": "queued",
        "created": time.time(),
    }
    jobs[job_id] = job

    # 5. Запустить worker в фоне
    t = threading.Thread(target=_run_with_tracking, args=(job,), daemon=True)
    t.start()

    logger.info("Job %s queued for %s", job_id, issue_key)
    return {"accepted": True, "job_id": job_id, "issue_key": issue_key}


if __name__ == "__main__":  # pragma: no cover
    uvicorn.run(app, host="0.0.0.0", port=PORT)
