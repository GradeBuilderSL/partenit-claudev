import base64
import logging

import httpx

from config import JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN

logger = logging.getLogger("pipeline.jira")


class JiraClient:
    def __init__(self) -> None:
        self.domain = JIRA_DOMAIN
        self.base_url = f"https://{self.domain}.atlassian.net"
        email = JIRA_EMAIL
        token = JIRA_API_TOKEN
        creds = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
        }

    def get_issue(self, key: str) -> dict:
        r = httpx.get(
            f"{self.base_url}/rest/api/3/issue/{key}",
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def transition(self, key: str, target: str) -> bool:
        r = httpx.get(
            f"{self.base_url}/rest/api/3/issue/{key}/transitions",
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        transitions = r.json().get("transitions", [])
        # Match by target status name (robust across locales) or transition name
        for t in transitions:
            to_name = t.get("to", {}).get("name", "")
            if to_name.lower() == target.lower() or t["name"].lower() == target.lower():
                httpx.post(
                    f"{self.base_url}/rest/api/3/issue/{key}/transitions",
                    headers=self.headers,
                    json={"transition": {"id": t["id"]}},
                    timeout=10,
                )
                logger.info("%s → %s (via transition '%s')", key, target, t["name"])
                return True
        logger.warning("Transition to '%s' not found for %s, available: %s",
                       target, key, [t.get("to", {}).get("name") for t in transitions])
        return False

    def add_comment(self, key: str, text: str) -> bool:
        body = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
            }
        }
        r = httpx.post(
            f"{self.base_url}/rest/api/3/issue/{key}/comment",
            headers=self.headers,
            json=body,
            timeout=10,
        )
        r.raise_for_status()
        return True

    def get_subtask_issue_type(self, project_key: str) -> str:
        """Return the best issue type name for creating child tasks in this project.

        Classic projects have a dedicated Sub-task type (subtask=True).
        Next-gen (team-managed) projects use regular Task with a parent link.
        """
        r = httpx.get(
            f"{self.base_url}/rest/api/3/project/{project_key}",
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        issue_types = r.json().get("issueTypes", [])
        logger.info("Project %s issue types: %s", project_key,
                    [it["name"] for it in issue_types])
        # Classic project: dedicated sub-task type
        for it in issue_types:
            if it.get("subtask") is True:
                logger.info("Using sub-task issue type: %s", it["name"])
                return it["name"]
        # Next-gen project: use Task (supports parent link)
        for it in issue_types:
            if it["name"].lower() in ("task", "задача"):
                return it["name"]
        # Last resort: first non-Epic type
        for it in issue_types:
            if it["name"].lower() != "epic":
                return it["name"]
        return "Task"

    def create_subtask(
        self,
        parent_key: str,
        summary: str,
        labels: list[str],
        project_key: str,
    ) -> str:
        """Create a sub-task under parent_key. Returns the new issue key."""
        issuetype_name = self.get_subtask_issue_type(project_key)
        body = {
            "fields": {
                "project": {"key": project_key},
                "parent": {"key": parent_key},
                "summary": summary,
                "issuetype": {"name": issuetype_name},
                "labels": labels,
            }
        }
        r = httpx.post(
            f"{self.base_url}/rest/api/3/issue",
            headers=self.headers,
            json=body,
            timeout=10,
        )
        if not r.is_success:
            logger.error("create_subtask error %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        key = r.json()["key"]
        logger.info("Created sub-task %s under %s", key, parent_key)
        return key

    def add_labels(self, key: str, new_labels: list[str]) -> bool:
        """Append labels to an issue without removing existing ones."""
        if not new_labels:
            return True
        # Fetch current labels first (Jira replaces, not appends)
        issue = self.get_issue(key)
        current = issue.get("fields", {}).get("labels", [])
        merged = list(dict.fromkeys(current + new_labels))  # deduplicate, preserve order
        r = httpx.put(
            f"{self.base_url}/rest/api/3/issue/{key}",
            headers=self.headers,
            json={"fields": {"labels": merged}},
            timeout=10,
        )
        r.raise_for_status()
        logger.info("%s labels → %s", key, merged)
        return True

    def get_subtasks(self, parent_key: str) -> list[dict]:
        """Return list of subtasks for a parent issue.

        Each item: {"key", "summary", "status", "labels"}
        """
        issue = self.get_issue(parent_key)
        subtasks_raw = issue.get("fields", {}).get("subtasks", [])
        result = []
        for sub in subtasks_raw:
            sub_fields = sub.get("fields", {})
            # subtasks in the parent response have minimal fields; fetch each
            try:
                full = self.get_issue(sub["key"])
                full_fields = full.get("fields", {})
                result.append({
                    "key": sub["key"],
                    "summary": full_fields.get("summary", ""),
                    "status": full_fields.get("status", {}).get("name", ""),
                    "labels": full_fields.get("labels", []),
                })
            except Exception:
                result.append({
                    "key": sub["key"],
                    "summary": sub_fields.get("summary", ""),
                    "status": sub_fields.get("status", {}).get("name", ""),
                    "labels": [],
                })
        return result

    def get_comments(self, key: str) -> list[str]:
        """Return list of comment body strings (plain text, ADF stripped)."""
        r = httpx.get(
            f"{self.base_url}/rest/api/3/issue/{key}/comment",
            headers=self.headers,
            timeout=10,
        )
        r.raise_for_status()
        comments = []
        for item in r.json().get("comments", []):
            body = item.get("body", {})
            comments.append(_adf_to_text(body))
        return comments

    def update_description(self, key: str, markdown: str) -> bool:
        """Update issue description with plain markdown text."""
        body = {
            "fields": {
                "description": {
                    "version": 1,
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": markdown}],
                        }
                    ],
                }
            }
        }
        r = httpx.put(
            f"{self.base_url}/rest/api/3/issue/{key}",
            headers=self.headers,
            json=body,
            timeout=10,
        )
        r.raise_for_status()
        return True


def _adf_to_text(adf_json) -> str:
    """Convert Atlassian Document Format to plain text (no external calls)."""
    if isinstance(adf_json, str):
        return adf_json
    if not adf_json:
        return ""

    def _extract(node) -> str:
        if not isinstance(node, dict):
            return ""
        if node.get("type") == "text":
            return node.get("text", "")
        parts = []
        for child in node.get("content", []):
            parts.append(_extract(child))
        sep = "\n" if node.get("type") in ("paragraph", "heading", "listItem", "bulletList") else ""
        return sep.join(parts)

    return _extract(adf_json).strip()
