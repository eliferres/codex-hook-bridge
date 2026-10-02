"""Refuse a manual package publish: exit 2 with the reason on stderr, Claude Code's plain way to block."""
import json
import re
import sys

command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
if re.search(r"\b(npm|pnpm|yarn)\s+publish\b", command):
    sys.stderr.write("publishing is done by the release workflow, not by hand\n")
    sys.exit(2)
