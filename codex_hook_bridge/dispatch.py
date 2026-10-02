"""Run the matching Claude Code hooks over a Codex call and answer Codex.

The answers are read the way Claude Code reads them (exit 2 blocks with
stderr; a JSON `permissionDecision`, `decision: "block"` or `continue: false`
blocks with its reason; `additionalContext` and plain text on the context
events add context) and written back in the shapes Codex documents for each
event.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, NamedTuple, Optional, Tuple

from .settings import Route, handler_key
from .translate import matcher_fits, translate

TOOL_EVENTS = ("PreToolUse", "PostToolUse")
# Events Codex fires that Claude Code also has, with the payload field each
# event's matcher is tested against. None: the event takes no matcher.
BRIDGED_EVENTS = {
    "PreToolUse": "tool_name", "PostToolUse": "tool_name",
    "SessionStart": "source", "SessionEnd": "reason",
    "PreCompact": "trigger", "PostCompact": "trigger",
    "SubagentStart": "agent_type", "SubagentStop": "agent_type",
    "UserPromptSubmit": None, "Stop": None,
}
# Where a refusal stops something. On the other bridged events Claude Code
# shows a hook's exit-2 message to the user and carries on, so the bridge does too.
BLOCKING_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SubagentStop", "PreCompact")
# Events whose Codex output accepts hookSpecificOutput.additionalContext.
CONTEXT_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "SubagentStart")
# Events where Claude Code adds a hook's plain-text stdout to the model's context.
PLAIN_TEXT_CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit")
DEFAULT_HANDLER_TIMEOUT = 600.0   # seconds, Claude Code's default for a command hook
SESSION_END_BUDGET = 2.5          # Codex allows SessionEnd hooks three seconds at most
ASK_NOTE = "[the hook asked for approval; Codex hooks have no approval prompt, so the call is refused] "


class Job(NamedTuple):
    payload: dict
    route: Route


class Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class Answer(NamedTuple):
    stdout: str
    stderr: str
    exit_code: int


def jobs_for(event: str, payload: dict, routes: List[Route]) -> List[Job]:
    """Each (Claude-shaped payload, route) pair to run for one Codex call.

    A tool call is translated first and each translated payload meets the
    routes whose matcher selects its tool name. A handler that matches every
    tool runs once per Codex call, on the first payload, rather than once per
    file a patch touches. The same handler never runs twice on one payload,
    which is how Claude Code treats a handler defined in two settings files.
    """
    routes = [r for r in routes if r.event == event]
    field = BRIDGED_EVENTS.get(event)
    if event not in TOOL_EVENTS:
        value = str(payload.get(field) or "") if field else ""
        out, seen = [], set()
        for r in routes:
            key = handler_key(r.handler)
            if key not in seen and (field is None or matcher_fits(r.matcher, value)):
                seen.add(key)
                out.append(Job(payload, r))
        return out
    out, seen = [], set()
    for index, p in enumerate(translate(payload)):
        for r in routes:
            universal = r.matcher in ("", "*")
            key = (None if universal else index, handler_key(r.handler))
            if key in seen or not matcher_fits(r.matcher, str(p.get("tool_name") or "")):
                continue
            seen.add(key)
            out.append(Job(p, r))
    return out


def _shell() -> List[str]:
    bash = shutil.which("bash")
    return [bash, "-c"] if bash else ["/bin/sh", "-c"]


def run_handler(command: str, payload: dict, timeout: float, env: dict) -> Result:
    """Run one hook command with the payload as JSON on stdin."""
    if timeout <= 0:
        return Result(0, "", "", True)
    try:
        # Its own process group, so a timeout also stops whatever the hook started.
        proc = subprocess.Popen(_shell() + [command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    except OSError as exc:
        return Result(1, "", "could not start: %s" % exc, False)
    try:
        out, err = proc.communicate(json.dumps(payload, ensure_ascii=False), timeout=timeout)
        return Result(proc.returncode, out, err, False)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return Result(0, "", "", True)


def read_verdict(event: str, result: Result) -> Tuple[str, str]:
    """('block', reason) | ('context', text) | ('message', text) | ('', '') for one hook's answer."""
    out = (result.stdout or "").strip()
    parsed = None
    if out.startswith("{") and out.endswith("}"):
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
    if not isinstance(parsed, dict):
        parsed = None

    block_reason = ""
    context = ""
    message = ""
    if parsed is not None:
        specific = parsed.get("hookSpecificOutput") or {}
        if not isinstance(specific, dict):
            specific = {}
        decision = str(specific.get("permissionDecision") or "").lower()
        if decision in ("deny", "ask"):
            reason = specific.get("permissionDecisionReason") or parsed.get("reason") or decision
            block_reason = (ASK_NOTE if decision == "ask" else "") + str(reason)
        elif str(parsed.get("decision") or "").lower() == "block":
            block_reason = str(parsed.get("reason") or "blocked by hook")
        elif parsed.get("continue") is False:
            block_reason = str(parsed.get("stopReason") or parsed.get("reason") or "stopped by hook")
        context = str(specific.get("additionalContext") or "")
        message = str(parsed.get("systemMessage") or "")

    if result.returncode == 2:
        return "block", block_reason or (result.stderr or result.stdout or "blocked by hook").strip()
    if block_reason:
        return "block", block_reason
    if context:
        return "context", context
    if message:
        return "message", message
    if parsed is None and result.returncode == 0 and out and event in PLAIN_TEXT_CONTEXT_EVENTS:
        return "context", out
    return "", ""


def answer(event: str, blocks: List[str], contexts: List[str], messages: List[str]) -> Answer:
    """The bridge's own reply to Codex, in the shape Codex documents for `event`."""
    if blocks and event not in BLOCKING_EVENTS:
        messages = messages + blocks   # shown to the user; nothing to stop
        blocks = []
    if blocks:
        reason = "\n\n".join(blocks)
        if event in ("Stop", "SubagentStop"):
            # Codex wants JSON from these events; block means "keep going with this prompt"
            return Answer(json.dumps({"decision": "block", "reason": reason}), "", 0)
        if event == "PreCompact":
            return Answer(json.dumps({"continue": False, "stopReason": reason}), "", 0)
        return Answer("", reason + "\n", 2)
    reply: dict = {}
    if contexts and event in CONTEXT_EVENTS:
        reply["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": "\n\n".join(contexts)}
    elif contexts:
        messages = messages + contexts
    if messages:
        reply["systemMessage"] = "\n\n".join(messages)
    return Answer(json.dumps(reply) if reply else "", "", 0)


def hook_env(payload: dict, project_dir: str = "") -> dict:
    """The environment a hook runs in: the bridge's own, plus CLAUDE_PROJECT_DIR
    (the project folder whose settings were read, else the payload's cwd) and
    a marker a hook can test to know it is running under Codex."""
    env = dict(os.environ, CODEX_HOOK_BRIDGE="1")
    project = project_dir or str(payload.get("cwd") or "")
    if project:
        env["CLAUDE_PROJECT_DIR"] = project
    return env


def run_hook(payload: dict, routes: List[Route], event: Optional[str] = None,
             budget: float = 25.0, transcript_path: str = "", project_dir: str = "") -> Answer:
    """Translate one Codex hook payload, run every matching Claude Code hook in
    parallel inside `budget` seconds, and return the reply for Codex.

    A hook that times out, crashes or exits with a code other than 0 and 2 is
    a non-blocking error, as in Claude Code: it is reported on stderr and the
    call proceeds.
    """
    event = event or str(payload.get("hook_event_name") or "")
    if event not in BRIDGED_EVENTS:
        return Answer("", "", 0)
    started = time.monotonic()
    if event == "SessionEnd":
        budget = min(budget, SESSION_END_BUDGET)
    jobs = jobs_for(event, payload, routes)
    if not jobs:
        return Answer("", "", 0)
    env = hook_env(payload, project_dir)

    def run(job: Job) -> Result:
        handler = job.route.handler
        try:
            limit = float(handler.get("timeout") or DEFAULT_HANDLER_TIMEOUT)
        except (TypeError, ValueError):
            limit = DEFAULT_HANDLER_TIMEOUT
        left = min(limit, budget - (time.monotonic() - started))
        hook_payload = dict(job.payload, transcript_path=transcript_path) if transcript_path else job.payload
        return run_handler(str(handler.get("command") or ""), hook_payload, left, env)

    runnable = [j for j in jobs if j.route.handler.get("type", "command") == "command"
                and j.route.handler.get("command")]
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(runnable)))) as pool:
        results = list(pool.map(run, runnable))

    blocks, contexts, messages, notices = [], [], [], []
    for job, result in zip(runnable, results):
        command = str(job.route.handler.get("command"))
        if result.timed_out:
            notices.append("codex-hook-bridge: %s timed out; treated as a non-blocking error" % command)
            continue
        kind, text = read_verdict(event, result)
        if kind == "block":
            blocks.append(text)
        elif kind == "context":
            contexts.append(text)
        elif kind == "message":
            messages.append(text)
        elif result.returncode not in (0, 2):
            # Non-blocking, as in Claude Code, but never silent: a hook that
            # could not start (126, 127) is named as such.
            detail = (result.stderr or "").strip().splitlines()
            what = "could not start" if result.returncode in (126, 127) or result.stderr.startswith(
                "could not start") else "exited %d" % result.returncode
            notices.append("codex-hook-bridge: %s %s%s; the call was not blocked" % (
                command, what, (": " + detail[-1]) if detail else ""))
    reply = answer(event, blocks, contexts, messages)
    if notices:
        reply = reply._replace(stderr=reply.stderr + "\n".join(notices) + "\n")
    return reply
