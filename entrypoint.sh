#!/bin/bash
set -e

# ── Claude Code authentication ────────────────────────────────────────────────
# Option A (Railway/cloud): set ANTHROPIC_API_KEY — Claude CLI picks it up automatically
# Option B (local Docker): mount ~/.claude:/root/.claude:ro in docker-compose.yml
# Option C (any server): set CLAUDE_AUTH_JSON=<base64 of ~/.claude dir tarball>
#   Generate with: tar -czf - ~/.claude | base64 -w0

# Env var always wins — it may be newer than what's on the volume
if [ -n "$CLAUDE_AUTH_JSON" ]; then
    echo "[entrypoint] Restoring credentials from CLAUDE_AUTH_JSON env var..."
    mkdir -p /root/.claude
    CLEAN_JSON=$(echo "$CLAUDE_AUTH_JSON" | tr -d '"'"'" )
    if echo "$CLEAN_JSON" | base64 -d > /root/.claude/.credentials.json 2>/dev/null; then
        chmod 600 /root/.claude/.credentials.json
        echo "[entrypoint] Credentials restored from env var."
    else
        echo "[entrypoint] WARNING: CLAUDE_AUTH_JSON decode failed."
    fi
elif [ -f /root/.claude/.credentials.json ]; then
    echo "[entrypoint] Using existing ~/.claude/.credentials.json from volume."
else
    echo "[entrypoint] WARNING: No credentials found. Claude Code will not work."
fi

# ── Fix .claude.json if missing (restore from backup if available) ────────────
if [ ! -f /root/.claude.json ]; then
    BACKUP=$(find /root/.claude/backups/ -name '.claude.json.backup.*' 2>/dev/null | sort -r | head -1)
    if [ -n "$BACKUP" ]; then
        cp "$BACKUP" /root/.claude.json
        echo "[entrypoint] Restored .claude.json from backup: $BACKUP"
    else
        echo '{}' > /root/.claude.json
        echo "[entrypoint] Created empty .claude.json"
    fi
fi

# ── Claude Code permissions: auto-approve all tools (runs as root, can't use --dangerously-skip-permissions)
mkdir -p /root/.claude
cat > /root/.claude/settings.json << 'SETTINGS'
{
  "permissions": {
    "allow": [
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "Glob(*)",
      "Grep(*)",
      "WebFetch(*)",
      "WebSearch(*)"
    ],
    "deny": []
  }
}
SETTINGS
echo "[entrypoint] Claude Code permissions configured (auto-approve all tools)."

# ── Refresh OAuth token if needed ─────────────────────────────────────────────
python refresh_token.py || echo "[entrypoint] Token refresh skipped or failed, continuing."

# ── Start pipeline ────────────────────────────────────────────────────────────
exec python main.py
