"""Matching hooks run over a translated Codex call; their answers go back in Codex's shapes."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from typing import List
from unittest import mock

from codex_hook_bridge.dispatch import run_hook
from codex_hook_bridge.settings import Route

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "hook.py")


def route(event: str, matcher: str, tag: str, mode: str = "allow", **handler: object) -> Route:
    command = '"%s" "%s" %s %s' % (sys.executable, FIXTURE, tag, mode)
    return Route(event, matcher, dict({"type": "command", "command": command}, **handler), "test")


def pre(tool: str, tool_input: dict) -> dict:
    return {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/work/app",
            "tool_name": tool, "tool_input": tool_input}


PATCH_TWO_FILES = ("*** Begin Patch\n*** Add File: a.txt\n+a\n*** Add File: secrets/.env\n+K=1\n"
                   "*** End Patch")


class Dispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOOK_RECORD_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def seen(self, tag: str) -> List[dict]:
        path = os.path.join(self.tmp.name, tag + ".jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def test_a_shell_call_reaches_bash_hooks_with_the_claude_shaped_payload(self) -> None:
        reply = run_hook(pre("exec_command", {"cmd": "ls"}), [route("PreToolUse", "Bash", "bash")])
        self.assertEqual(reply.exit_code, 0)
        self.assertEqual(self.seen("bash")[0]["tool_name"], "Bash")
        self.assertEqual(self.seen("bash")[0]["tool_input"], {"command": "ls"})

    def test_hooks_whose_matcher_does_not_fit_do_not_run(self) -> None:
        run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "Write|Edit", "write")])
        self.assertEqual(self.seen("write"), [])

    def test_exit_2_refuses_the_call_with_the_hooks_stderr(self) -> None:
        reply = run_hook(pre("Bash", {"command": "rm -rf /"}), [route("PreToolUse", "Bash", "guard", "exit2")])
        self.assertEqual(reply.exit_code, 2)
        self.assertEqual(reply.stderr, "refused by guard\n")

    def test_a_json_deny_refuses_with_its_reason(self) -> None:
        reply = run_hook(pre("Bash", {"command": "x"}), [route("PreToolUse", "Bash", "guard", "deny")])
        self.assertEqual((reply.exit_code, reply.stderr), (2, "denied by guard\n"))

    def test_ask_is_refused_because_codex_has_no_approval_prompt_for_hooks(self) -> None:
        reply = run_hook(pre("Bash", {"command": "x"}), [route("PreToolUse", "Bash", "guard", "ask")])
        self.assertEqual(reply.exit_code, 2)
        self.assertIn("ask from guard", reply.stderr)
        self.assertIn("no approval prompt", reply.stderr)

    def test_a_patch_is_judged_file_by_file(self) -> None:
        guard = route("PreToolUse", "Write", "write", "allow")
        run_hook(pre("apply_patch", {"command": PATCH_TWO_FILES}), [guard])
        # the hooks run in parallel, so they record in no fixed order
        self.assertEqual(sorted(p["tool_input"]["file_path"] for p in self.seen("write")),
                         ["/work/app/a.txt", "/work/app/secrets/.env"])

    def test_a_match_all_hook_judges_every_file_of_a_patch(self) -> None:
        for matcher in ("*", ""):
            with self.subTest(matcher=matcher):
                tag = "every" + ("star" if matcher else "empty")
                run_hook(pre("apply_patch", {"command": PATCH_TWO_FILES}), [route("PreToolUse", matcher, tag)])
                self.assertEqual(sorted(p["tool_input"]["file_path"] for p in self.seen(tag)),
                                 ["/work/app/a.txt", "/work/app/secrets/.env"])

    def test_the_same_handler_from_two_files_runs_once(self) -> None:
        twin = route("PreToolUse", "Bash", "twin")
        run_hook(pre("Bash", {"command": "ls"}), [twin, twin._replace(source="other")])
        self.assertEqual(len(self.seen("twin")), 1)

    def test_context_comes_back_as_additional_context(self) -> None:
        reply = run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "Bash", "ctx", "context")])
        self.assertEqual(reply.exit_code, 0)
        self.assertEqual(json.loads(reply.stdout)["hookSpecificOutput"],
                         {"hookEventName": "PreToolUse", "additionalContext": "context from ctx"})

    def test_a_system_message_is_passed_through(self) -> None:
        reply = run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "Bash", "w", "warn")])
        self.assertEqual(json.loads(reply.stdout), {"systemMessage": "warning from w"})

    def test_plain_text_is_context_on_session_start_and_ignored_on_tool_events(self) -> None:
        start = {"hook_event_name": "SessionStart", "source": "startup", "cwd": "/w"}
        reply = run_hook(start, [route("SessionStart", "", "s", "text")])
        self.assertEqual(json.loads(reply.stdout)["hookSpecificOutput"]["additionalContext"], "plain text from s")
        reply = run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "Bash", "b", "text")])
        self.assertEqual(reply, (("", "", 0)))

    def test_a_stop_block_is_json_on_stdout_because_codex_wants_json_from_stop(self) -> None:
        reply = run_hook({"hook_event_name": "Stop"}, [route("Stop", "", "stop", "exit2")])
        self.assertEqual(reply.exit_code, 0)
        self.assertEqual(json.loads(reply.stdout), {"decision": "block", "reason": "refused by stop"})

    def test_continue_false_on_stop_passes_through_instead_of_keeping_codex_going(self) -> None:
        for event in ("Stop", "SubagentStop"):
            with self.subTest(event=event):
                routes = [route(event, "", "a", "block"), route(event, "", "h", "halt")]
                reply = run_hook({"hook_event_name": event, "agent_type": "x"}, routes)
                self.assertEqual(reply.exit_code, 0)
                self.assertEqual(json.loads(reply.stdout), {"continue": False, "stopReason": "halted by h"})

    def test_a_refusal_on_session_start_is_shown_not_enforced(self) -> None:
        reply = run_hook({"hook_event_name": "SessionStart", "source": "startup"},
                         [route("SessionStart", "", "s", "exit2")])
        self.assertEqual(reply.exit_code, 0)
        self.assertEqual(json.loads(reply.stdout), {"systemMessage": "refused by s"})

    def test_non_tool_matchers_are_tested_against_the_events_own_field(self) -> None:
        routes = [route("SessionStart", "resume", "resume"), route("SessionStart", "startup|clear", "fresh")]
        run_hook({"hook_event_name": "SessionStart", "source": "startup"}, routes)
        self.assertEqual((len(self.seen("resume")), len(self.seen("fresh"))), (0, 1))

    def test_a_crash_is_a_non_blocking_error_reported_on_stderr(self) -> None:
        reply = run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "Bash", "c", "crash")])
        self.assertEqual(reply.exit_code, 0)
        self.assertIn("exited 1: something broke", reply.stderr)

    def test_a_hook_command_that_cannot_start_is_reported_not_silent(self) -> None:
        missing = Route("PreToolUse", "Bash", {"type": "command", "command": "/no/such/hook.sh"}, "test")
        reply = run_hook(pre("Bash", {"command": "ls"}), [missing])
        self.assertEqual(reply.exit_code, 0)
        self.assertIn("/no/such/hook.sh could not start", reply.stderr)

    def test_a_hook_past_its_timeout_does_not_hold_the_call(self) -> None:
        started = time.monotonic()
        reply = run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "Bash", "slow", "sleep", timeout=1)])
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(reply.exit_code, 0)
        self.assertIn("timed out", reply.stderr)

    def test_many_slow_hooks_do_not_starve_a_fast_guard_of_its_turn(self) -> None:
        slow = [route("PreToolUse", "Bash", "slow%d" % i, "sleep") for i in range(20)]
        guard = route("PreToolUse", "Bash", "guard", "exit2")
        reply = run_hook(pre("Bash", {"command": "ls"}), slow + [guard], budget=2)
        self.assertEqual(reply.exit_code, 2)
        self.assertIn("refused by guard", reply.stderr)

    def test_a_hook_printing_bytes_that_are_not_utf8_does_not_lose_a_refusal(self) -> None:
        routes = [route("PreToolUse", "Bash", "noisy", "badbytes"), route("PreToolUse", "Bash", "guard", "exit2")]
        reply = run_hook(pre("Bash", {"command": "ls"}), routes)
        self.assertEqual((reply.exit_code, reply.stderr), (2, "refused by guard\n"))

    def test_a_matcher_python_cannot_compile_is_reported_not_silently_skipped(self) -> None:
        reply = run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "(?<tool>Bash)", "g", "exit2")])
        self.assertEqual(reply.exit_code, 0)
        self.assertEqual(len(reply.stderr.splitlines()), 1)
        self.assertIn("matcher '(?<tool>Bash)' is not a regular expression Python can evaluate", reply.stderr)

    def test_one_refusal_among_several_hooks_refuses_the_call(self) -> None:
        routes = [route("PreToolUse", "Bash", "a", "context"), route("PreToolUse", "Bash", "b", "exit2")]
        reply = run_hook(pre("Bash", {"command": "ls"}), routes)
        self.assertEqual((reply.exit_code, reply.stderr), (2, "refused by b\n"))

    def test_hooks_see_the_project_dir_and_a_bridge_marker(self) -> None:
        reply = run_hook(pre("Bash", {"command": "ls"}), [route("PreToolUse", "Bash", "e", "env")])
        self.assertEqual(json.loads(reply.stdout)["hookSpecificOutput"]["additionalContext"], "/work/app|1")

    def test_an_event_codex_fires_that_the_bridge_does_not_carry_does_nothing(self) -> None:
        reply = run_hook({"hook_event_name": "Interrupt"}, [route("Interrupt", "", "i", "exit2")])
        self.assertEqual(reply, ("", "", 0))


if __name__ == "__main__":
    unittest.main()
