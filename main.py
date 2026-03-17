import uuid
import threading
import time
import logging
from typing import Dict, Any
from collections import deque

from fastapi import FastAPI, Request, HTTPException
import uvicorn

from config import (
    PORT, WEBHOOK_SECRET, TRIGGER_STATUS, MAX_CONCURRENT_JOBS,
    MAX_CONCURRENT_PIPELINES, JIRA_DOMAIN,
    STATUS_MERGE, STATUS_CANCELLED, STATUS_IN_REVIEW, STATUS_DONE,
)
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

# Pipeline-level queue: tracks active parent pipelines and queued ones
# active_pipelines: set of parent_keys currently running through the pipeline
# pipeline_queue: FIFO of (job_dict) waiting to start
active_pipelines: set[str] = set()
pipeline_queue: deque[Dict[str, Any]] = deque()


def _get_active_pipeline_count() -> int:
    """Count parent tasks with active (queued/running) subtask jobs."""
    parents = set()
    for j in jobs.values():
        if j["status"] in ("queued", "running"):
            parents.add(j.get("parent_key", j["issue_key"]))
    return len(parents)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "active_jobs": active_count,
        "total_jobs": len(jobs),
        "active_pipelines": list(active_pipelines),
        "queued_pipelines": [q["issue_key"] for q in pipeline_queue],
    }


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


@app.get("/queue")
def list_queue() -> Dict[str, Any]:
    """Show pipeline queue status."""
    return {
        "active_pipelines": list(active_pipelines),
        "max_concurrent": MAX_CONCURRENT_PIPELINES,
        "queued": [
            {"issue_key": q["issue_key"], "summary": q["summary"],
             "queued_at": q.get("created")}
            for q in pipeline_queue
        ],
    }


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] not in ("queued", "running"):
        return {"cancelled": False, "reason": f"job is already {job['status']}"}
    job["cancelled"] = True
    job["status"] = "cancelled"
    proc = job.get("process")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    logger.info("Job %s cancelled via API", job_id)
    return {"cancelled": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


def _cancel_jobs_for_issue(issue_key: str) -> list:
    """Cancel all running/queued jobs for the given issue_key."""
    cancelled = []
    for job_id, job in jobs.items():
        if job["issue_key"] == issue_key and job["status"] in ("queued", "running"):
            job["cancelled"] = True
            job["status"] = "cancelled"
            proc = job.get("process")
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            cancelled.append(job_id)
            logger.info("Cancelled job %s for %s", job_id, issue_key)
    # Also remove from queue
    _remove_from_queue(issue_key)
    return cancelled


def _remove_from_queue(issue_key: str) -> bool:
    """Remove a parent task from the pipeline queue."""
    for i, q in enumerate(pipeline_queue):
        if q["issue_key"] == issue_key:
            del pipeline_queue[i]
            logger.info("Removed %s from pipeline queue", issue_key)
            return True
    return False


def _try_start_queued_pipeline() -> None:
    """Check if a queued pipeline can start now."""
    with lock:
        while pipeline_queue and len(active_pipelines) < MAX_CONCURRENT_PIPELINES:
            queued_job = pipeline_queue.popleft()
            parent_key = queued_job["issue_key"]
            active_pipelines.add(parent_key)
            logger.info("Dequeued pipeline %s (queue: %d remaining)",
                        parent_key, len(pipeline_queue))

            # Notify via Telegram
            try:
                from telegram_notifier import _send
                wait_time = int(time.time() - queued_job.get("created", time.time()))
                _send(f"🚀 {parent_key} стартует из очереди "
                      f"(ждал {wait_time // 60}м {wait_time % 60}с)")
            except Exception:
                pass

            _launch_job(queued_job)


def _pipeline_finished(parent_key: str) -> None:
    """Called when a pipeline reaches a human-review stage or finishes.
    Frees the slot and starts the next queued pipeline."""
    with lock:
        active_pipelines.discard(parent_key)
    logger.info("Pipeline %s released slot (active: %d, queued: %d)",
                parent_key, len(active_pipelines), len(pipeline_queue))
    _try_start_queued_pipeline()


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
        # Check if this was the last active job for this pipeline
        # and the pipeline has reached human review (In Review / Done)
        parent_key = job.get("parent_key", job["issue_key"])
        _check_pipeline_slot_release(parent_key)


def _check_pipeline_slot_release(parent_key: str) -> None:
    """Release pipeline slot if no more active jobs for this parent."""
    if parent_key not in active_pipelines:
        return
    # Check if any jobs for this parent are still running
    for j in jobs.values():
        if (j.get("parent_key", j["issue_key"]) == parent_key
                and j["status"] in ("queued", "running")):
            return  # still has active work
    # No more active jobs — release the slot
    _pipeline_finished(parent_key)


def _launch_job(job: Dict[str, Any]) -> None:
    """Store job and start worker thread."""
    jobs[job["job_id"]] = job
    t = threading.Thread(target=_run_with_tracking, args=(job,), daemon=True)
    t.start()
    logger.info("Job %s launched for %s", job["job_id"], job["issue_key"])


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

    logger.info("Webhook: key=%s type=%r status=%r", issue_key, issue_type, status_name)

    # 3. Фильтр — принимаем TRIGGER_STATUS, STATUS_MERGE, STATUS_CANCELLED
    if status_name not in (TRIGGER_STATUS, STATUS_MERGE, STATUS_CANCELLED):
        return {"skipped": True, "reason": f"status={status_name}"}

    # Отмена: убиваем все активные джобы по этой задаче
    if status_name == STATUS_CANCELLED:
        cancelled = _cancel_jobs_for_issue(issue_key)
        logger.info("Webhook cancel: %s → cancelled jobs: %s", issue_key, cancelled)
        return {"cancelled": True, "issue_key": issue_key, "jobs": cancelled}

    ALLOWED_TYPES = ("Task", "Bug", "Story", "Sub-task",
                     "Задача", "Баг", "История", "Подзадача")
    if issue_type not in ALLOWED_TYPES:
        return {"skipped": True, "reason": f"type={issue_type}"}

    labels = fields.get("labels", [])
    is_subtask = issue_type.lower() in ("sub-task", "subtask", "подзадача")

    # Merge jobs: only parent tasks, skip sub-tasks
    if status_name == STATUS_MERGE and is_subtask:
        return {"skipped": True, "reason": "merge trigger ignored for sub-tasks"}

    if is_subtask:
        stage = get_stage(labels)
        if not stage:
            return {"skipped": True, "reason": "sub-task without pipeline label"}
    else:
        stage = None

    # Idempotency
    for j in jobs.values():
        if j["issue_key"] == issue_key and j["status"] in ("queued", "running"):
            return {"skipped": True, "reason": f"already processing {issue_key}"}

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
        "key": issue_key,
        "parent_key": parent_key,
        "summary": fields.get("summary", ""),
        "description": fields.get("description", {}),
        "description_text": "",
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

    # 5. Pipeline-level concurrency: only for parent (setup) tasks
    if not is_subtask and stage is None and status_name == TRIGGER_STATUS:
        with lock:
            if len(active_pipelines) >= MAX_CONCURRENT_PIPELINES:
                # Queue it — will start when current pipeline finishes
                pipeline_queue.append(job)
                jobs[job_id] = job
                logger.info("Pipeline %s queued (position %d), active: %s",
                            issue_key, len(pipeline_queue), list(active_pipelines))
                # Notify user via Jira comment
                try:
                    from jira_client import JiraClient
                    jira = JiraClient()
                    active_list = ", ".join(active_pipelines)
                    jira.add_comment(
                        issue_key,
                        f"⏳ Задача поставлена в очередь (позиция {len(pipeline_queue)}).\n"
                        f"Активные пайплайны: {active_list}\n"
                        "Задача стартует автоматически, когда освободится слот.",
                    )
                except Exception:
                    pass
                return {
                    "queued_pipeline": True,
                    "job_id": job_id,
                    "issue_key": issue_key,
                    "position": len(pipeline_queue),
                    "active_pipelines": list(active_pipelines),
                }
            else:
                active_pipelines.add(issue_key)

    # Subtask jobs: check that parent pipeline is active
    # (prevents orphan subtask processing)
    if is_subtask and parent_key not in active_pipelines:
        # Parent isn't in active pipelines — register it
        # (handles restart/redeploy where state was lost)
        with lock:
            active_pipelines.add(parent_key)

    # 6. Запустить worker
    _launch_job(job)
    return {"accepted": True, "job_id": job_id, "issue_key": issue_key}


if __name__ == "__main__":  # pragma: no cover
    uvicorn.run(app, host="0.0.0.0", port=PORT)
