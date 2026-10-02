"""Deny writes to .env files with a JSON decision, Claude Code's structured way to block."""
import json
import os
import sys

path = json.load(sys.stdin).get("tool_input", {}).get("file_path", "")
if os.path.basename(path).startswith(".env"):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": path + " holds secrets; edit it by hand",
    }}))
