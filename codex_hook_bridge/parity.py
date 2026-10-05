"""Which Claude Code hook routes a Codex session can reach, and why not when it cannot.

Every route in the settings gets one status:

  reached      a Codex call or event arrives at it through the bridge
  unreachable  it cannot be reached, for a reason this tool knows (an event
               Codex never fires, a Claude Code tool with no Codex twin, a
               handler type the bridge does not run)
  over-budget  it is reached, but its timeout is longer than the bridge's time
               budget, so a run that long is stopped and the call proceeds
  accepted     it cannot be reached, or is over budget, and the accept file says why
  unaccounted  none of the above: nobody has said what happens to it

An accept file entry that a Codex call can in fact reach, or that names a
route no longer in the settings, is reported too, so the file cannot quietly
go stale.
"""
from __future__ import annotations

import json
from typing import Dict, List, NamedTuple, Optional, Tuple

from .dispatch import BRIDGED_EVENTS, DEFAULT_BUDGET, SESSION_END_BUDGET, TOOL_EVENTS
from .settings import Route, SettingsError
from .translate import CLAUDE_TOOLS, MATCH_ALL, matcher_error, matcher_fits

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
    "MultiEdit": "MultiEdit is no longer a Claude Code tool; each patch hunk arrives as an Edit",
}

# Hook sources Claude Code also runs that the bridge does not read.
NOT_READ = ["managed policy settings", "plugin hooks", "skill and subagent frontmatter hooks"]

REACHED, UNREACHABLE, ACCEPTED, UNACCOUNTED = "reached", "unreachable", "accepted", "unaccounted"
OVER_BUDGET = "over-budget"
STALE, GONE = "stale-acceptance", "gone"
FAILING = (UNACCOUNTED, OVER_BUDGET, STALE, GONE)


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
    if BRIDGED_EVENTS[event] and matcher_error(matcher):
        return UNACCOUNTED, ("matcher is not a regular expression Python can evaluate (%s), "
                             "so its hooks never run under the bridge" % matcher_error(matcher))
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


def over_budget(event: str, handler: dict, budget: float) -> str:
    """Why a reached hook's own timeout outlasts the bridge's budget, or "".
    A hook with no timeout set is not flagged: it runs inside the budget
    like any other, and only one that needs longer is cut short."""
    try:
        timeout = float(handler.get("timeout") or 0)
    except (TypeError, ValueError):
        return ""
    allowed = min(budget, SESSION_END_BUDGET) if event == "SessionEnd" else budget
    if timeout <= allowed:
        return ""
    return ("its timeout is %gs but the bridge gives all hooks of one call %gs; a run that long is "
            "stopped and the call proceeds as if the hook had allowed it" % (timeout, allowed))


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


def check(routes: List[Route], accepted: Optional[List[dict]] = None,
          budget: float = DEFAULT_BUDGET) -> List[Finding]:
    """One finding per route, in settings order, then one per stale or gone
    acceptance. `budget` is the hook command's --budget."""
    reasons = {_key(str(e.get("event", "")), str(e.get("matcher") or ""), str(e.get("command", ""))): str(e["reason"])
               for e in accepted or []}
    findings, present = [], set()
    for r in routes:
        command = describe(r.handler)
        key = _key(r.event, r.matcher, command)
        present.add(key)
        status, reason = reach(r.event, r.matcher, r.handler)
        late = over_budget(r.event, r.handler, budget) if status == REACHED else ""
        if late:
            status, reason = OVER_BUDGET, late
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
    """Whether any route is unaccounted or over budget, or the accept file has drifted."""
    return any(f.status in FAILING for f in findings)


def render(findings: List[Finding]) -> str:
    """The human report: one block per route, then a count line."""
    lines = []
    for f in findings:
        label = f.status.upper() if f.status in FAILING else f.status
        matcher = " [%s]" % f.matcher if f.matcher else ""
        lines.append("%-12s %s%s: %s" % (label, f.event, matcher, f.command))
        lines.append("%-12s %s" % ("", f.reason))
    counts = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    order = (REACHED, UNREACHABLE, ACCEPTED, OVER_BUDGET, UNACCOUNTED, STALE, GONE)
    summary = ", ".join("%d %s" % (counts[s], s) for s in order if s in counts)
    total = sum(f.status != GONE for f in findings)
    lines.append("%d route%s: %s (managed-policy and plugin hooks are not read)"
                 % (total, "" if total == 1 else "s", summary or "none"))
    return "\n".join(lines)


def as_json(findings: List[Finding]) -> str:
    return json.dumps({"routes": [f._asdict() for f in findings], "ok": not failed(findings),
                       "not_read": NOT_READ}, indent=2)
