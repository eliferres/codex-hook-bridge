"""A stand-in Claude Code hook for the tests.

Usage: hook.py <tag> <mode>. It appends the payload it received to
$HOOK_RECORD_DIR/<tag>.jsonl, then answers the way <mode> says.
"""
import json
import os
import sys
import time

tag, mode = sys.argv[1], sys.argv[2]
raw = sys.stdin.read()
record = os.environ.get("HOOK_RECORD_DIR")
if record:
    with open(os.path.join(record, tag + ".jsonl"), "a") as fh:
        fh.write(json.dumps(json.loads(raw)) + "\n")
if mode == "exit2":
    sys.stderr.write("refused by %s\n" % tag)
    sys.exit(2)
if mode == "deny":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": "denied by %s" % tag}}))
if mode == "ask":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "ask",
                                             "permissionDecisionReason": "ask from %s" % tag}}))
if mode == "block":
    print(json.dumps({"decision": "block", "reason": "blocked by %s" % tag}))
if mode == "halt":
    print(json.dumps({"continue": False, "stopReason": "halted by %s" % tag}))
if mode == "context":
    print(json.dumps({"hookSpecificOutput": {"additionalContext": "context from %s" % tag}}))
if mode == "text":
    print("plain text from %s" % tag)
if mode == "warn":
    print(json.dumps({"systemMessage": "warning from %s" % tag}))
if mode == "env":
    print(json.dumps({"hookSpecificOutput": {"additionalContext": "%s|%s" % (
        os.environ.get("CLAUDE_PROJECT_DIR"), os.environ.get("CODEX_HOOK_BRIDGE"))}}))
if mode == "crash":
    sys.stderr.write("something broke\n")
    sys.exit(1)
if mode == "sleep":
    time.sleep(5)
