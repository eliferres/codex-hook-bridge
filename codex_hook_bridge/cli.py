"""Command line: `hook` (installed in Codex), `translate` (inspect), `parity` (audit)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from . import parity
from .dispatch import run_hook
from .settings import SettingsError, load_routes
from .transcript import default_state_dir, mirror
from .translate import Untranslatable, translate

PROG = "codex-hook-bridge"


class UsageError(Exception):
    """Bad input on the command line or stdin."""


class _Parser(argparse.ArgumentParser):
    """An argument parser that raises instead of exiting, so hook mode can
    choose its own exit code: argparse's exit 2 reads to Codex as a refusal."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def _read_payload() -> dict:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise UsageError("stdin is not JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise UsageError("stdin is not a JSON object")
    return payload


def _project_dir(args: argparse.Namespace, payload: Optional[dict] = None) -> str:
    return args.project_dir or str((payload or {}).get("cwd") or "") or os.getcwd()


def cmd_hook(args: argparse.Namespace) -> int:
    payload = _read_payload()
    project = _project_dir(args, payload)
    skipped: List[str] = []
    routes = load_routes(args.settings, project, skipped)
    for problem in skipped:
        # named, never silent, but the other files' guards still run
        sys.stderr.write("%s: %s; that file's hooks did not run\n" % (PROG, problem))
    transcript = mirror(payload, args.state_dir)
    reply = run_hook(payload, routes, event=args.event, budget=args.budget, transcript_path=transcript,
                     project_dir=project)
    if reply.stdout:
        print(reply.stdout)
    if reply.stderr:
        sys.stderr.write(reply.stderr)
    return reply.exit_code


def summarize(payload: dict) -> str:
    """One readable line for a translated payload."""
    name = str(payload.get("tool_name") or payload.get("hook_event_name") or "?")
    tool_input = payload.get("tool_input") or {}
    if name == "Bash":
        detail = str(tool_input.get("command") or "")
    elif "file_path" in tool_input:
        detail = str(tool_input["file_path"])
    elif name == "WebSearch":
        detail = str(tool_input.get("query") or "")
    elif name == "WebFetch":
        detail = str(tool_input.get("url") or "")
    elif name == "Agent":
        detail = str(tool_input.get("subagent_type") or "")
    else:
        detail = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    tag = payload.get("codex_derived")
    return "%-10s %s%s" % (name, detail, "  [%s]" % tag if tag else "")


def cmd_translate(args: argparse.Namespace) -> int:
    payloads = translate(_read_payload())
    if args.json:
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
    else:
        for p in payloads:
            print(summarize(p))
    return 0


def cmd_parity(args: argparse.Namespace) -> int:
    routes = load_routes(args.settings, _project_dir(args))
    accepted = parity.load_accepted(args.accept) if args.accept else None
    findings = parity.check(routes, accepted)
    print(parity.as_json(findings) if args.json else parity.render(findings))
    return 1 if parity.failed(findings) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG, description="Run Claude Code hooks under the Codex CLI without rewriting them.")
    parser.add_argument("--version", action="version", version="%s %s" % (PROG, __version__))
    sub = parser.add_subparsers(dest="command", metavar="command")

    def settings_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--settings", action="append", metavar="FILE",
                       help="a Claude Code settings file to read hooks from (repeatable); default: "
                            "the user, project and local settings files, merged")
        p.add_argument("--project-dir", metavar="DIR",
                       help="project whose .claude/ settings apply; default: the payload's cwd "
                            "(hook) or the current directory (parity)")

    hook = sub.add_parser("hook", help="run as a Codex hook: payload on stdin, reply on stdout and exit code")
    settings_options(hook)
    hook.add_argument("--event", help="event name when the payload lacks hook_event_name")
    hook.add_argument("--budget", type=float, default=25.0, metavar="SECONDS",
                      help="time for all hooks of one call together (default 25; keep it under "
                           "the timeout set on the Codex hook)")
    hook.add_argument("--state-dir", default=default_state_dir(), metavar="DIR",
                      help="where Claude-shaped copies of Codex session logs are kept "
                           "(default: $XDG_STATE_HOME/codex-hook-bridge/transcripts)")
    hook.set_defaults(func=cmd_hook)

    tr = sub.add_parser("translate", help="print the Claude Code payloads a Codex payload on stdin becomes")
    tr.add_argument("--json", action="store_true", help="print the full payloads as JSON")
    tr.set_defaults(func=cmd_translate)

    par = sub.add_parser("parity", help="list every hook route and whether a Codex action can reach it")
    settings_options(par)
    par.add_argument("--accept", metavar="FILE",
                     help="JSON list of {event, matcher, command, reason} for routes you know Codex cannot reach")
    par.add_argument("--json", action="store_true", help="print the report as JSON")
    par.set_defaults(func=cmd_parity)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        sys.stderr.write("%s: %s\n" % (PROG, exc))
        return 1 if argv[:1] == ["hook"] else 2
    if not getattr(args, "func", None):
        sys.stderr.write("%s: a command is required (hook, translate, parity)\n" % PROG)
        return 2
    try:
        return args.func(args)
    except (SettingsError, UsageError, OSError, Untranslatable) as exc:
        sys.stderr.write("%s: %s\n" % (PROG, exc))
        # Under Codex, exit 2 means "refuse" (and on Stop, "keep going"), so a
        # broken setup must not look like a hook's decision: hook mode exits 1,
        # which Codex reports as a failed hook and does not act on.
        return 1 if args.command == "hook" else 2
    except Exception as exc:   # never a traceback: under Codex one line is all that is read
        sys.stderr.write("%s: internal error: %s: %s\n" % (PROG, type(exc).__name__, exc))
        return 1 if args.command == "hook" else 2
