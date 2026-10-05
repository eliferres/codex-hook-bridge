"""The command line, run as a subprocess the way Codex runs it."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

from codex_hook_bridge import __version__
from codex_hook_bridge.translate import translate

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

    def test_a_broken_settings_file_in_hook_mode_is_skipped_and_named_and_the_others_still_guard(self) -> None:
        broken = os.path.join(self.tmp.name, "broken.json")
        with open(broken, "w") as fh:
            fh.write("{oops")
        payload = {"hook_event_name": "PreToolUse", "cwd": self.tmp.name, "tool_name": "Bash",
                   "tool_input": {"command": "ls"}}
        proc = cli(["hook", "--settings", broken, "--settings", self.settings], json.dumps(payload))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refused by guard", proc.stderr)
        self.assertIn("broken.json is not valid JSON", proc.stderr)

    def test_a_broken_settings_file_alone_is_named_and_the_call_proceeds(self) -> None:
        with open(self.settings, "w") as fh:
            fh.write("{oops")
        proc = cli(["hook", "--settings", self.settings], '{"hook_event_name": "Stop"}')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(proc.stderr.splitlines()), 1)
        self.assertIn("is not valid JSON", proc.stderr)

    def test_translate_refuses_a_command_too_long_to_read_in_one_line_and_exit_2(self) -> None:
        from codex_hook_bridge.translate import COMMAND_MAX
        payload = {"tool_name": "Bash", "tool_input": {"command": "x" * (COMMAND_MAX + 1)}}
        proc = cli(["translate"], json.dumps(payload))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(len(proc.stderr.splitlines()), 1)
        self.assertIn("characters long", proc.stderr)
        self.assertNotIn("internal error", proc.stderr)

    def guard_on(self, prefix: str) -> str:
        """A settings file whose Write|Edit guard refuses any path under `prefix`."""
        guard = os.path.join(self.tmp.name, "guard.py")
        with open(guard, "w") as fh:
            fh.write("import json, sys\npath = json.load(sys.stdin)['tool_input'].get('file_path', '')\n"
                     "sys.exit(2 if path.startswith(%r) else 0)\n" % prefix)
        settings = os.path.join(self.tmp.name, "guarded.json")
        with open(settings, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Write|Edit", "hooks": [
                {"type": "command", "command": '"%s" "%s"' % (sys.executable, guard)}]}]}}, fh)
        return settings

    def test_a_write_through_a_symlinked_folder_meets_a_guard_on_the_real_folder(self) -> None:
        root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(root, "secret", "sub"))
        os.makedirs(os.path.join(root, "app"))
        os.symlink(os.path.join(root, "secret", "sub"), os.path.join(root, "app", "link"))
        payload = {"hook_event_name": "PreToolUse", "cwd": os.path.join(root, "app"), "tool_name": "apply_patch",
                   "tool_input": {"command": "*** Begin Patch\n*** Add File: link/k\n+x\n*** End Patch"}}
        proc = cli(["hook", "--settings", self.guard_on(os.path.join(root, "secret") + "/")], json.dumps(payload))
        self.assertEqual(proc.returncode, 2)

    def test_a_guard_on_the_written_path_still_matches_when_a_system_folder_is_a_symlink(self) -> None:
        # on macOS /etc is a symlink to /private/etc; the written path must still be sent
        payload = {"hook_event_name": "PreToolUse", "cwd": self.tmp.name, "tool_name": "Bash",
                   "tool_input": {"command": "echo x > /etc/hosts"}}
        proc = cli(["hook", "--settings", self.guard_on("/etc/")], json.dumps(payload))
        self.assertEqual(proc.returncode, 2)

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

    def test_a_bad_option_in_hook_mode_is_one_line_and_exit_1_not_a_refusal(self) -> None:
        proc = cli(["hook", "--budget", "abc"], '{"hook_event_name": "Stop"}')
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(proc.stderr.splitlines()), 1)
        self.assertIn("--budget", proc.stderr)

    def test_a_bad_option_elsewhere_is_one_line_and_exit_2(self) -> None:
        proc = cli(["parity", "--bogus"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(len(proc.stderr.splitlines()), 1)

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



class GuardedSession(unittest.TestCase):
    """A session folder app/ with a guard on app/secret/, driven the way Codex
    sends a shell call: argv ["bash", "-lc", script]."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.app = os.path.join(self.root, "app")
        for folder in ("app/secret", "app/src", "elsewhere/inner"):
            os.makedirs(os.path.join(self.root, folder))
        os.symlink(os.path.join(self.root, "elsewhere", "inner"), os.path.join(self.app, "l2"))
        guard = os.path.join(self.root, "guard.py")
        with open(guard, "w") as fh:
            fh.write("import json, sys\npath = json.load(sys.stdin)['tool_input'].get('file_path', '')\n"
                     "sys.exit(2 if path.startswith(%r) else 0)\n" % (os.path.join(self.app, "secret") + "/"))
        self.settings = os.path.join(self.root, "settings.json")
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Write|Edit", "hooks": [
                {"type": "command", "command": '"%s" "%s"' % (sys.executable, guard)}]}]}}, fh)

    def run_tool(self, tool: str, tool_input: dict) -> int:
        payload = {"hook_event_name": "PreToolUse", "cwd": self.app, "tool_name": tool, "tool_input": tool_input}
        proc = cli(["hook", "--settings", self.settings, "--state-dir", os.path.join(self.root, "state")],
                   json.dumps(payload), env={"CLAUDE_CONFIG_DIR": os.path.join(self.root, "none")})
        return proc.returncode

    def exit_code(self, script: str) -> int:
        # a patch is read from the command text itself, so patches go as exec_command's string
        tool_input = {"cmd": script} if "apply_patch" in script else {"command": ["bash", "-lc", script]}
        payload = {"hook_event_name": "PreToolUse", "cwd": self.app,
                   "tool_name": "exec_command" if "cmd" in tool_input else "shell", "tool_input": tool_input}
        proc = cli(["hook", "--settings", self.settings, "--state-dir", os.path.join(self.root, "state")],
                   json.dumps(payload), env={"CLAUDE_CONFIG_DIR": os.path.join(self.root, "none")})
        return proc.returncode

    def assert_refused(self, *scripts: str) -> None:
        for script in scripts:
            with self.subTest(script=script):
                self.assertEqual(self.exit_code(script), 2)

    def test_a_cd_to_a_folder_known_only_when_it_runs_keeps_every_folder_seen(self) -> None:
        self.assert_refused('cd "$(git rev-parse --show-toplevel)" && echo x > secret/k',
                            "cd $(git rev-parse --show-toplevel) && echo x > secret/k",
                            'cd "$PWD" && echo x > secret/k',
                            'cd src && cd "$OLDPWD" && echo x > secret/k',
                            "cd /tmp && cd - && echo x > secret/k",
                            "cd `pwd` && echo x > secret/k")


    def test_a_cd_in_a_background_job_or_a_pipeline_leaves_the_shell_where_it_was(self) -> None:
        patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: secret/k\n+x\n*** End Patch\nEOF"
        self.assert_refused("cd /tmp & echo x > secret/k",
                            "cd /tmp | true; echo x > secret/k",
                            "true | cd /tmp; echo x > secret/k",
                            "cd /tmp && true & echo x > secret/k",
                            "cd /tmp & " + patch,
                            "cd /tmp | true; " + patch)


    def test_a_cd_through_a_symlink_climbs_back_by_name_as_bash_does_unless_dash_p(self) -> None:
        self.assert_refused("cd l2/.. && echo x > secret/k", "cd -L l2/.. && echo x > secret/k")
        out = translate({"tool_name": "Bash", "cwd": self.app, "tool_input": {"command": "cd -P l2/.. && echo x > k"}})
        self.assertEqual(out[1]["tool_input"]["file_path"], os.path.join(self.root, "elsewhere", "k"))


    def test_a_folder_made_earlier_in_the_command_is_entered_for_certain(self) -> None:
        builds = "".join("mkdir -p d%d; cd d%d; make; cd ..; " % (n, n) for n in range(9))
        self.assertEqual(self.exit_code(builds + "echo done"), 0)
        script = "mkdir -p out; cd out; echo x > f; cd ..; echo y > g"
        self.assertEqual(self.exit_code(script), 0)
        out = translate({"tool_name": "Bash", "cwd": self.app, "tool_input": {"command": script}})
        self.assertEqual([p["tool_input"]["file_path"] for p in out[1:]],
                         [os.path.join(self.app, "out", "f"), os.path.join(self.app, "g")])
        made = translate({"tool_name": "Bash", "cwd": self.app,
                          "tool_input": {"command": "mkdir -m 755 -p a/b; cd a; cd b; echo x > f"}})
        self.assertEqual([p["tool_input"]["file_path"] for p in made[1:]], [os.path.join(self.app, "a", "b", "f")])


    def test_ordinary_commands_pass(self) -> None:
        two_files = ("apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: src/a.py\n+a\n"
                     "*** Add File: README.md\n+r\n*** End Patch\nEOF")
        script = "\n".join(["set -e", "cd src", "echo a > a.txt", "ls -la", "cd ..", "git status",
                            "cd src && make", "cd ..", "mkdir -p build", "cd build", "cmake ..", "cd ..",
                            "for f in src/*.py; do python3 -m py_compile \"$f\"; done", "cd src",
                            "sed -i.bak 's/a/b/' a.txt", "cd ..", "npm run lint | tee lint.log",
                            "git diff --stat", "cd src; echo done > status.txt; cd ..", "echo ok"])
        for command in ("npm test", "git status && git diff", "cd src && echo x > out.txt",
                        "curl -s https://example.com/api | jq .", "tar -czf out.tgz src", two_files, script):
            with self.subTest(command=command):
                self.assertEqual(self.exit_code(command), 0)


    def test_a_patch_inside_a_bash_script_follows_the_cd_before_it_there(self) -> None:
        patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: k\n+x\n*** End Patch\nEOF"
        for tool, key, wrap in (("shell", "command", lambda c: ["bash", "-lc", c]),
                                ("local_shell", "action", lambda c: {"type": "exec", "command": ["bash", "-lc", c]}),
                                ("exec_command", "cmd", lambda c: "bash -lc %s" % shlex.quote(c))):
            with self.subTest(tool=tool):
                self.assertEqual(self.run_tool(tool, {key: wrap("cd secret && " + patch)}), 2)
                self.assertEqual(self.run_tool(tool, {key: wrap(patch)}), 0)


if __name__ == "__main__":
    unittest.main()
