"""Print a notification's message. Claude Code fires this event; Codex has none like it."""
import json
import sys

print(json.load(sys.stdin).get("message", ""))
