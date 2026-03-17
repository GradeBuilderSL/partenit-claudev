import logging

import httpx

from config import GITHUB_TOKEN_TRUST_LAYER as GITHUB_TOKEN, GITHUB_REPO

logger = logging.getLogger("pipeline.github")


class GitHubClient:
    def __init__(self) -> None:
        self.token = GITHUB_TOKEN
        self.repo = GITHUB_REPO
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def create_pr(self, head: str, base: str, title: str, body: str) -> dict:
        r = httpx.post(
            f"https://api.github.com/repos/{self.repo}/pulls",
            headers=self.headers,
            json={"title": title, "body": body, "head": head, "base": base},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        logger.info("PR #%s: %s", data["number"], data["html_url"])
        return {"number": data["number"], "html_url": data["html_url"]}

    def add_labels(self, pr_number: int, labels: list) -> bool:
        r = httpx.post(
            f"https://api.github.com/repos/{self.repo}/issues/{pr_number}/labels",
            headers=self.headers,
            json={"labels": labels},
            timeout=10,
        )
        r.raise_for_status()
        return True

    def find_pr(self, branch: str) -> dict | None:
        """Find open PR by head branch. Returns PR dict or None."""
        r = httpx.get(
            f"https://api.github.com/repos/{self.repo}/pulls",
            headers=self.headers,
            params={"state": "open", "head": f"{self.repo.split('/')[0]}:{branch}"},
            timeout=10,
        )
        r.raise_for_status()
        prs = r.json()
        return prs[0] if prs else None

    def merge_pr(self, pr_number: int, commit_message: str) -> dict:
        """Merge PR into its base branch. Returns merge result dict."""
        r = httpx.put(
            f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}/merge",
            headers=self.headers,
            json={
                "commit_title": commit_message,
                "merge_method": "squash",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

