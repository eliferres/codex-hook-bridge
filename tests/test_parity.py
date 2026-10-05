"""Every route accounted for: reached, unreachable with a reason, or accepted."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from codex_hook_bridge.parity import check, failed, render
from codex_hook_bridge.settings import Route

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def route(event: str, matcher: str, command: str = "check.sh", **handler: object) -> Route:
    return Route(event, matcher, dict({"type": "command", "command": command}, **handler), "settings.json")


def status(r: Route, accepted: list = None) -> tuple:
    f = check([r], accepted)[0]
    return f.status, f.reason


class Reach(unittest.TestCase):
    def test_tool_matchers_reached_by_a_translation_are_reached(self) -> None:
        self.assertEqual(status(route("PreToolUse", "Bash")), ("reached", "via Bash"))
        self.assertEqual(status(route("PreToolUse", "Write|Edit|MultiEdit")),
                         ("reached", "via Write, Edit"))

    def test_a_multiedit_only_route_is_unreachable_with_the_reason(self) -> None:
        self.assertEqual(status(route("PreToolUse", "MultiEdit")),
                         ("unreachable", "MultiEdit is no longer a Claude Code tool; each patch hunk arrives as an Edit"))
        self.assertEqual(status(route("PostToolUse", "")), ("reached", "every Codex tool call"))

    def test_mcp_matchers_are_reached_because_mcp_tools_keep_their_names(self) -> None:
        self.assertEqual(status(route("PreToolUse", "mcp__github__.*"))[0], "reached")

    def test_a_claude_only_tool_is_unreachable_with_its_reason(self) -> None:
        self.assertEqual(status(route("PreToolUse", "Grep|Glob")),
                         ("unreachable", "Codex searches through its shell, so a search reaches Bash hooks instead"))

    def test_a_claude_only_event_is_unreachable_with_its_reason(self) -> None:
        self.assertEqual(status(route("Notification", "")), ("unreachable", "Codex fires no notification event"))

    def test_a_bridged_lifecycle_event_is_reached(self) -> None:
        self.assertEqual(status(route("SessionStart", "startup")), ("reached", "Codex fires SessionStart"))

    def test_a_handler_type_the_bridge_does_not_run_is_unreachable(self) -> None:
        r = Route("Stop", "", {"type": "prompt", "prompt": "check"}, "s")
        self.assertEqual(status(r)[0], "unreachable")
        self.assertIn("prompt hooks are skipped", status(r)[1])

    def test_a_matcher_nothing_explains_is_unaccounted_and_fails(self) -> None:
        findings = check([route("PreToolUse", "Deploy")])
        self.assertEqual(findings[0].status, "unaccounted")
        self.assertTrue(failed(findings))

    def test_a_matcher_python_cannot_compile_is_unaccounted_with_the_reason(self) -> None:
        state, reason = status(route("PreToolUse", "(?<tool>Bash)"))
        self.assertEqual(state, "unaccounted")
        self.assertIn("not a regular expression Python can evaluate", reason)

    def test_an_unknown_event_is_unaccounted(self) -> None:
        self.assertEqual(status(route("BeforeLunch", ""))[0], "unaccounted")


class AcceptFile(unittest.TestCase):
    def test_an_accepted_route_counts_as_accounted_with_its_reason(self) -> None:
        accepted = [{"event": "PreToolUse", "matcher": "Deploy", "command": "check.sh", "reason": "our own tool"}]
        findings = check([route("PreToolUse", "Deploy")], accepted)
        self.assertEqual((findings[0].status, findings[0].reason), ("accepted", "our own tool"))
        self.assertFalse(failed(findings))

    def test_accepting_a_reachable_route_is_reported_stale(self) -> None:
        accepted = [{"event": "PreToolUse", "matcher": "Bash", "command": "check.sh", "reason": "x"}]
        findings = check([route("PreToolUse", "Bash")], accepted)
        self.assertEqual(findings[0].status, "stale-acceptance")
        self.assertTrue(failed(findings))

    def test_accepting_a_route_that_no_longer_exists_is_reported_gone(self) -> None:
        accepted = [{"event": "Stop", "matcher": "", "command": "old.sh", "reason": "x"}]
        findings = check([], accepted)
        self.assertEqual([f.status for f in findings], ["gone"])


class Report(unittest.TestCase):
    def test_the_report_names_each_route_its_reason_and_the_counts(self) -> None:
        text = render(check([route("PreToolUse", "Bash", "guard.sh"), route("PreToolUse", "Deploy", "d.sh")]))
        self.assertEqual(text.splitlines(), [
            "reached      PreToolUse [Bash]: guard.sh",
            "             via Bash",
            "UNACCOUNTED  PreToolUse [Deploy]: d.sh",
            "             matches no tool a Codex call is translated to",
            "2 routes: 1 reached, 1 unaccounted (managed-policy and plugin hooks are not read)",
        ])

    def test_the_command_exits_1_on_an_unaccounted_route_and_0_once_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "settings.json")
            with open(settings, "w") as fh:
                json.dump({"hooks": {"PreToolUse": [{"matcher": "Deploy", "hooks": [
                    {"type": "command", "command": "d.sh"}]}]}}, fh)
            base = [sys.executable, "-m", "codex_hook_bridge", "parity", "--settings", settings]
            proc = subprocess.run(base + ["--json"], capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 1)
            report = json.loads(proc.stdout)
            self.assertFalse(report["ok"])
            self.assertEqual(report["not_read"], ["managed policy settings", "plugin hooks",
                                                  "skill and subagent frontmatter hooks"])
            accept = os.path.join(tmp, "accept.json")
            with open(accept, "w") as fh:
                json.dump([{"event": "PreToolUse", "matcher": "Deploy", "command": "d.sh", "reason": "ours"}], fh)
            proc = subprocess.run(base + ["--accept", accept], capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
