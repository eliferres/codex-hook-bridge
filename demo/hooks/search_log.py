"""Record each search pattern. Matched on Claude Code's Grep tool, which Codex has no twin for."""
import json
import sys

print(json.load(sys.stdin).get("tool_input", {}).get("pattern", ""))
