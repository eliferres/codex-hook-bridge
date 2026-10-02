"""The command line, run as a subprocess the way Codex runs it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from codex_hook_bridge import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "hook.py")


def cli(args: list, stdin: str = "", env: dict = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "codex_hook_bridge"] + args, input=stdin,
                          capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, **(env or {})))


class CommandLine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "settings.json")
        command = '"%s" "%s" guard exit2' % (sys.executable, FIXTURE)
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": command}]}]}}, fh)

    def test_version_prints_the_command_and_version(self) -> None:
        proc = cli(["--version"])
        self.assertEqual((proc.returncode, proc.stdout), (0, "codex-hook-bridge %s\n" % __version__))

    def test_no_command_is_a_usage_error(self) -> None:
        self.assertEqual(cli([]).returncode, 2)

    def test_hook_refuses_with_exit_2_and_the_hooks_reason(self) -> None:
        payload = {"hook_event_name": "PreToolUse", "cwd": self.tmp.name, "tool_name": "Bash",
                   "tool_input": {"command": "rm -rf build"}}
        proc = cli(["hook", "--settings", self.settings], json.dumps(payload))
        self.assertEqual((proc.returncode, proc.stderr), (2, "refused by guard\n"))

    def test_hook_reads_the_project_settings_from_the_payload_cwd(self) -> None:
        os.makedirs(os.path.join(self.tmp.name, ".claude"))
        os.rename(self.settings, os.path.join(self.tmp.name, ".claude", "settings.json"))
        payload = {"hook_event_name": "PreToolUse", "cwd": self.tmp.name, "tool_name": "Bash",
                   "tool_input": {"command": "ls"}}
        proc = cli(["hook"], json.dumps(payload), env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "none")})
        self.assertEqual(proc.returncode, 2)

    def test_hooks_get_the_claude_shaped_copy_of_a_codex_session_log(self) -> None:
        log = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(log, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {"id": "sess-1"}}) + "\n")
        command = '"%s" "%s" seen allow' % (sys.executable, FIXTURE)
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}, fh)
        state = os.path.join(self.tmp.name, "state")
        payload = {"hook_event_name": "Stop", "transcript_path": log}
        cli(["hook", "--settings", self.settings, "--state-dir", state], json.dumps(payload),
            env={"HOOK_RECORD_DIR": self.tmp.name})
        with open(os.path.join(self.tmp.name, "seen.jsonl")) as fh:
            seen = json.loads(fh.readline())
        self.assertEqual(seen["transcript_path"], os.path.join(state, "sess-1.jsonl"))

    def test_project_dir_is_the_claude_project_dir_hooks_see(self) -> None:
        project = os.path.join(self.tmp.name, "project")
        os.makedirs(os.path.join(project, ".claude"))
        hook = os.path.join(project, ".claude", "guard.py")
        with open(hook, "w") as fh:
            fh.write("import sys\nsys.stderr.write('guarded\\n')\nsys.exit(2)\n")
        with open(os.path.join(project, ".claude", "settings.json"), "w") as fh:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": '"%s" "$CLAUDE_PROJECT_DIR/.claude/guard.py"' % sys.executable}]}]}}, fh)
        payload = {"hook_event_name": "PreToolUse", "cwd": os.path.join(project, "src"), "tool_name": "Bash",
                   "tool_input": {"command": "ls"}}
        proc = cli(["hook", "--project-dir", project], json.dumps(payload),
                   env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "none")})
        self.assertEqual((proc.returncode, proc.stderr), (2, "guarded\n"))

    def test_a_broken_settings_file_in_hook_mode_is_one_line_and_exit_1(self) -> None:
        with open(self.settings, "w") as fh:
            fh.write("{oops")
        proc = cli(["hook", "--settings", self.settings], '{"hook_event_name": "Stop"}')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(proc.stderr.splitlines()), 1)
        self.assertIn("is not valid JSON", proc.stderr)

    def test_odd_tool_inputs_never_produce_a_traceback(self) -> None:
        for tool_input in ({"open": ["https://example.com"]}, {"search_query": "plain"}):
            payload = {"hook_event_name": "PreToolUse", "tool_name": "web_search", "tool_input": tool_input}
            proc = cli(["hook", "--settings", self.settings], json.dumps(payload))
            self.assertNotIn("Traceback", proc.stderr)
            self.assertEqual(proc.returncode, 0)
        payload = {"hook_event_name": "PreToolUse", "tool_name": "request_user_input",
                   "tool_input": {"questions": [{"question": "q", "options": 5}]}}
        proc = cli(["hook", "--settings", self.settings], json.dumps(payload))
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))

    def test_an_unexpected_error_in_hook_mode_is_one_line_and_exit_1(self) -> None:
        import io
        from unittest import mock
        from codex_hook_bridge import cli as cli_module
        err = io.StringIO()
        with mock.patch.object(cli_module, "run_hook", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stdin", io.StringIO('{"hook_event_name": "Stop"}')), mock.patch("sys.stderr", err):
            code = cli_module.main(["hook", "--settings", self.settings])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "codex-hook-bridge: internal error: RuntimeError: boom\n")

    def test_translate_prints_one_line_per_payload(self) -> None:
        payload = {"tool_name": "Bash", "cwd": "/work/app", "tool_input": {"command": "echo hi > a.txt"}}
        proc = cli(["translate"], json.dumps(payload))
        self.assertEqual(proc.stdout.splitlines(), ["Bash       echo hi > a.txt",
                                                    "Write      /work/app/a.txt  [shell-write]"])

    def test_translate_json_prints_the_full_payloads(self) -> None:
        payload = {"tool_name": "view_image", "cwd": "/w", "tool_input": {"path": "x.png"}}
        out = json.loads(cli(["translate", "--json"], json.dumps(payload)).stdout)
        self.assertEqual(out[0]["tool_input"], {"file_path": "/w/x.png"})

    def test_input_that_is_not_json_is_a_usage_error_without_a_traceback(self) -> None:
        proc = cli(["translate"], "not json")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertTrue(proc.stderr.startswith("codex-hook-bridge: stdin is not JSON"))


if __name__ == "__main__":
    unittest.main()
