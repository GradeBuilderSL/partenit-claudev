"""Auto-refresh OAuth token for Claude Code in Docker/CI.

Claude Code CLI uses OAuth tokens (Max/Pro subscription) that expire
after ~24h. This script checks the token and refreshes it before it
expires, so the pipeline never gets 401 errors.

Called from:
  - entrypoint.sh (on container start)
  - worker._run_claude() (before each Claude Code invocation)
  - Background loop (every 30 min while container is alive)

Non-blocking: if refresh fails, continues with existing token.
"""
import json
import logging
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger("pipeline.token")

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
OAUTH_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
REFRESH_THRESHOLD_SEC = 3600  # refresh if token expires within 1 hour


def main() -> bool:
    """Check and refresh OAuth token if needed. Returns True if refreshed."""
    if not CREDENTIALS_PATH.exists():
        return False

    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Cannot read credentials: %s", e)
        return False

    oauth = creds.get("claudeAiOauth")
    if not oauth:
        return False

    expires_at_ms = oauth.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    remaining_sec = (expires_at_ms - now_ms) / 1000

    if remaining_sec > REFRESH_THRESHOLD_SEC:
        logger.debug("Token valid for %.0fm, no refresh needed", remaining_sec / 60)
        return False

    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        logger.warning("No refreshToken in credentials, cannot refresh")
        return False

    logger.info("Token expires in %.0fm, refreshing...", remaining_sec / 60)

    try:
        resp = httpx.post(
            OAUTH_ENDPOINT,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Token refresh request failed: %s", e)
        return False

    # Update credentials
    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token")
    new_expires = data.get("expires_at")  # might be seconds or ms

    if not new_access:
        logger.warning("No access_token in refresh response")
        return False

    oauth["accessToken"] = new_access
    if new_refresh:
        oauth["refreshToken"] = new_refresh
    if new_expires:
        # Normalize to milliseconds
        if new_expires < 1e12:
            new_expires = int(new_expires * 1000)
        oauth["expiresAt"] = new_expires

    creds["claudeAiOauth"] = oauth

    try:
        CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2))
        CREDENTIALS_PATH.chmod(0o600)
    except OSError as e:
        logger.warning("Failed to write refreshed credentials: %s", e)
        return False

    remaining_new = (oauth["expiresAt"] - int(time.time() * 1000)) / 1000 / 60
    logger.info("Token refreshed, valid for %.0fm", remaining_new)
    return True


BACKGROUND_INTERVAL_SEC = 1800  # 30 minutes
_background_started = False


def start_background_refresh() -> None:
    """Start a daemon thread that refreshes the token every 30 minutes.

    Safe to call multiple times — only starts once.
    """
    global _background_started
    if _background_started:
        return
    _background_started = True

    def _loop():
        while True:
            time.sleep(BACKGROUND_INTERVAL_SEC)
            try:
                refreshed = main()
                if refreshed:
                    logger.info("[bg] Token refreshed by background loop")
            except Exception as e:
                logger.debug("[bg] Token refresh failed: %s", e)

    t = threading.Thread(target=_loop, daemon=True, name="token-refresh")
    t.start()
    logger.info("Background token refresh started (every %dm)", BACKGROUND_INTERVAL_SEC // 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    refreshed = main()
    if refreshed:
        print("[refresh_token] Token refreshed successfully.")
    else:
        print("[refresh_token] No refresh needed or not possible.")
