"""Which Claude Code hook routes a Codex session can reach, and why not when it cannot.

Every route in the settings gets one status:

  reached      a Codex call or event arrives at it through the bridge
  unreachable  it cannot be reached, for a reason this tool knows (an event
               Codex never fires, a Claude Code tool with no Codex twin, a
               handler type the bridge does not run)
  accepted     it cannot be reached and the accept file says why
  unaccounted  none of the above: nobody has said what happens to it

An accept file entry that a Codex call can in fact reach, or that names a
route no longer in the settings, is reported too, so the file cannot quietly
go stale.
"""
from __future__ import annotations

import json
from typing import Dict, List, NamedTuple, Optional, Tuple

from .dispatch import BRIDGED_EVENTS, TOOL_EVENTS
from .settings import Route, SettingsError
from .translate import CLAUDE_TOOLS, MATCH_ALL, matcher_fits

# Claude Code events with no Codex counterpart, and why.
CLAUDE_ONLY_EVENTS: Dict[str, str] = {
    "Notification": "Codex fires no notification event",
    "PermissionRequest": "Codex has a PermissionRequest event, but the bridge does not carry approval decisions",
    "PermissionDenied": "Codex fires no event after a denied permission",
    "PostToolUseFailure": "Codex has no separate failure event; a failed shell call still runs PostToolUse",
    "PostToolBatch": "Codex fires no event after a batch of tool calls",
    "Setup": "Codex has no setup run",
    "InstructionsLoaded": "Codex fires no event when it loads instruction files",
    "UserPromptExpansion": "Codex fires no event when a slash command expands",
    "MessageDisplay": "Codex fires no event when a message is displayed",
    "StopFailure": "Codex fires no event when a turn fails",
    "TeammateIdle": "Codex has no agent teams",
    "TaskCreated": "Codex has no task list events",
    "TaskCompleted": "Codex has no task list events",
    "ConfigChange": "Codex fires no event when configuration changes",
    "CwdChanged": "Codex fires no event when the working directory changes",
    "DirectoryAdded": "Codex fires no event when a directory is added",
    "FileChanged": "Codex does not watch files for hooks",
    "WorktreeCreate": "Codex fires no worktree events",
    "WorktreeRemove": "Codex fires no worktree events",
    "PreModelSwitch": "Codex fires no event when the model changes",
    "PostModelSwitch": "Codex fires no event when the model changes",
    "Elicitation": "Codex fires no event for MCP elicitation",
    "ElicitationResult": "Codex fires no event for MCP elicitation",
}

# Claude Code tools that no Codex call is translated to, and why.
CLAUDE_ONLY_TOOLS: Dict[str, str] = {
    "Glob": "Codex searches through its shell, so a search reaches Bash hooks instead",
    "Grep": "Codex searches through its shell, so a search reaches Bash hooks instead",
    "PowerShell": "Codex reports every shell call as Bash",
    "NotebookEdit": "Codex edits notebooks with apply_patch, which reaches Write and Edit hooks",
    "ExitPlanMode": "Codex plan mode is a setting, not a tool call a hook sees",
    "Skill": "Codex reads skills as files; there is no skill tool call",
    "TodoWrite": "Codex keeps its plan with update_plan, which passes through under its own name",
    "Workflow": "Codex has no workflow tool",
}

REACHED, UNREACHABLE, ACCEPTED, UNACCOUNTED = "reached", "unreachable", "accepted", "unaccounted"
STALE, GONE = "stale-acceptance", "gone"


class Finding(NamedTuple):
    status: str
    event: str
    matcher: str
    command: str
    reason: str
    source: str


def describe(handler: dict) -> str:
    """How a route's handler reads in a report: its command, or its type for other handler kinds."""
    kind = str(handler.get("type") or "command")
    if kind == "command":
        return str(handler.get("command") or "")
    target = handler.get("url") or handler.get("tool") or ""
    return ("%s hook %s" % (kind, target)).rstrip()


def reach(event: str, matcher: str, handler: dict) -> Tuple[str, str]:
    """(status, reason) for one route, before any accept file is applied."""
    kind = str(handler.get("type") or "command")
    if event in CLAUDE_ONLY_EVENTS:
        return UNREACHABLE, CLAUDE_ONLY_EVENTS[event]
    if event not in BRIDGED_EVENTS:
        return UNACCOUNTED, "%s is not a hook event this tool knows" % event
    if kind != "command":
        return UNREACHABLE, "the bridge runs command hooks only; %s hooks are skipped" % kind
    if event not in TOOL_EVENTS:
        return REACHED, "Codex fires %s" % event
    if matcher in MATCH_ALL:
        return REACHED, "every Codex tool call"
    tools = [t for t in CLAUDE_TOOLS if matcher_fits(matcher, t)]
    if tools:
        return REACHED, "via " + ", ".join(tools)
    if matcher.startswith("mcp__"):
        return REACHED, "MCP tools keep their names under Codex, when the same server is configured there"
    blocked = [t for t in CLAUDE_ONLY_TOOLS if matcher_fits(matcher, t)]
    if blocked:
        reasons = list(dict.fromkeys(CLAUDE_ONLY_TOOLS[t] for t in blocked))
        return UNREACHABLE, "; ".join(reasons)
    return UNACCOUNTED, "matches no tool a Codex call is translated to"


def load_accepted(path: str) -> List[dict]:
    """The accept file: a JSON list of {event, matcher, command, reason}."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise SettingsError("cannot read %s: %s" % (path, exc.strerror or exc))
    except ValueError as exc:
        raise SettingsError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(data, list) or not all(isinstance(e, dict) and e.get("reason") for e in data):
        raise SettingsError("%s must be a JSON list of objects with event, matcher, command and reason" % path)
    return data


def _key(event: str, matcher: str, command: str) -> Tuple[str, str, str]:
    return (event, matcher or "", command)


def check(routes: List[Route], accepted: Optional[List[dict]] = None) -> List[Finding]:
    """One finding per route, in settings order, then one per stale or gone acceptance."""
    reasons = {_key(str(e.get("event", "")), str(e.get("matcher") or ""), str(e.get("command", ""))): str(e["reason"])
               for e in accepted or []}
    findings, present = [], set()
    for r in routes:
        command = describe(r.handler)
        key = _key(r.event, r.matcher, command)
        present.add(key)
        status, reason = reach(r.event, r.matcher, r.handler)
        if key in reasons:
            if status == REACHED:
                findings.append(Finding(STALE, r.event, r.matcher, command,
                                        "accepted as unreachable, but Codex reaches it (%s)" % reason, r.source))
                continue
            status, reason = ACCEPTED, reasons[key]
        findings.append(Finding(status, r.event, r.matcher, command, reason, r.source))
    for key in reasons:
        if key not in present:
            findings.append(Finding(GONE, key[0], key[1], key[2], "in the accept file but not in the settings", ""))
    return findings


def failed(findings: List[Finding]) -> bool:
    """Whether any route is unaccounted or the accept file has drifted."""
    return any(f.status in (UNACCOUNTED, STALE, GONE) for f in findings)


def render(findings: List[Finding]) -> str:
    """The human report: one block per route, then a count line."""
    lines = []
    for f in findings:
        label = f.status.upper() if f.status in (UNACCOUNTED, STALE, GONE) else f.status
        matcher = " [%s]" % f.matcher if f.matcher else ""
        lines.append("%-12s %s%s: %s" % (label, f.event, matcher, f.command))
        lines.append("%-12s %s" % ("", f.reason))
    counts = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    order = (REACHED, UNREACHABLE, ACCEPTED, UNACCOUNTED, STALE, GONE)
    summary = ", ".join("%d %s" % (counts[s], s) for s in order if s in counts)
    total = sum(f.status != GONE for f in findings)
    lines.append("%d route%s: %s" % (total, "" if total == 1 else "s", summary or "none"))
    return "\n".join(lines)


def as_json(findings: List[Finding]) -> str:
    return json.dumps({"routes": [f._asdict() for f in findings], "ok": not failed(findings)}, indent=2)
