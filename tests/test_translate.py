"""Codex payloads in, Claude Code payloads out."""
from __future__ import annotations

import os
import tempfile
import unittest

from codex_hook_bridge.translate import matcher_fits, shell_targets, translate

CWD = "/work/app"


def codex(tool: str, tool_input: object, **extra: object) -> dict:
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": CWD,
               "tool_name": tool, "tool_input": tool_input}
    payload.update(extra)
    return payload


def names(payloads: list) -> list:
    return [(p["tool_name"], p["tool_input"].get("file_path") or p["tool_input"].get("command"))
            for p in payloads]


PATCH = """*** Begin Patch
*** Update File: src/app.py
@@
 x = 1
-y = 2
+y = 3
*** Add File: .env
+TOKEN=abc
+DEBUG=1
*** Delete File: old.txt
*** End Patch"""


class ApplyPatch(unittest.TestCase):
    def test_one_payload_per_file_in_the_order_the_patch_names_them(self) -> None:
        out = translate(codex("apply_patch", {"command": PATCH}))
        self.assertEqual(names(out), [
            ("Edit", "/work/app/src/app.py"),
            ("Write", "/work/app/.env"),
            ("Bash", "rm -- '/work/app/old.txt'"),
        ])

    def test_an_update_hunk_is_an_edit_with_context_on_both_sides(self) -> None:
        edit = translate(codex("apply_patch", {"command": PATCH}))[0]["tool_input"]
        self.assertEqual(edit["old_string"], "x = 1\ny = 2")
        self.assertEqual(edit["new_string"], "x = 1\ny = 3")

    def test_an_added_file_carries_its_content(self) -> None:
        write = translate(codex("apply_patch", {"command": PATCH}))[1]["tool_input"]
        self.assertEqual(write["content"], "TOKEN=abc\nDEBUG=1")

    def test_two_hunks_in_one_file_are_a_multiedit(self) -> None:
        body = ("*** Begin Patch\n*** Update File: a.py\n@@\n-a\n+b\n@@ def f():\n-c\n+d\n*** End Patch")
        out = translate(codex("apply_patch", {"command": body}))
        self.assertEqual(out[0]["tool_name"], "MultiEdit")
        self.assertEqual(out[0]["tool_input"]["edits"],
                         [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}])

    def test_a_move_is_mv_then_the_edit_lands_on_the_new_path(self) -> None:
        body = "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-x\n+y\n*** End Patch"
        out = translate(codex("apply_patch", {"command": body}))
        self.assertEqual(names(out), [("Bash", "mv -- '/work/app/a.py' '/work/app/b.py'"),
                                      ("Edit", "/work/app/b.py")])
        self.assertEqual(out[0]["codex_derived"], "patch-move")

    def test_a_patch_sent_through_the_shell_is_split_the_same_way(self) -> None:
        command = "apply_patch <<'EOF'\n" + PATCH + "\nEOF"
        out = translate(codex("Bash", {"command": command}))
        self.assertEqual(out[0]["tool_name"], "Bash")
        self.assertIn(("Edit", "/work/app/src/app.py"), names(out))
        self.assertIn(("Write", "/work/app/.env"), names(out))

    def test_every_payload_keeps_the_session_fields_and_names_the_codex_tool(self) -> None:
        for p in translate(codex("apply_patch", {"command": PATCH})):
            self.assertEqual(p["session_id"], "s1")
            self.assertEqual(p["cwd"], CWD)
            self.assertEqual(p["codex_tool_name"], "apply_patch")


class ShellWrites(unittest.TestCase):
    def test_a_plain_command_is_one_bash_payload(self) -> None:
        out = translate(codex("Bash", {"command": "ls -la"}))
        self.assertEqual(names(out), [("Bash", "ls -la")])
        self.assertNotIn("codex_derived", out[0])

    def test_every_codex_shell_name_becomes_bash(self) -> None:
        for tool in ("Bash", "exec_command", "shell", "local_shell"):
            with self.subTest(tool=tool):
                out = translate(codex(tool, {"cmd": "ls"} if tool == "exec_command" else {"command": "ls"}))
                self.assertEqual(names(out), [("Bash", "ls")])

    def test_a_redirect_is_also_a_write_to_its_target(self) -> None:
        out = translate(codex("Bash", {"command": "echo hi > notes.md"}))
        self.assertEqual(names(out), [("Bash", "echo hi > notes.md"), ("Write", "/work/app/notes.md")])
        self.assertEqual(out[1]["codex_derived"], "shell-write")

    def test_a_redirect_inside_quotes_or_a_heredoc_body_is_not_a_write(self) -> None:
        self.assertEqual(shell_targets('git commit -m "a > b"'), [])
        self.assertEqual(shell_targets("python3 - <<'EOF'\nprint(1 > 0)\nEOF"), [])

    def test_known_writers_name_their_targets(self) -> None:
        cases = {
            "sed -i '' 's/a/b/' conf.py": ["conf.py"],
            "sed -e s/a/b/ conf.py": [],
            "perl -pi -e 's/a/b/' a.txt b.txt": ["a.txt", "b.txt"],
            "cp src.txt dest.txt": ["dest.txt"],
            "mv -t outdir a b": ["outdir"],
            "tee -a log.txt": ["log.txt"],
            "curl -o page.html https://example.com": ["page.html"],
            "dd if=/dev/zero of=disk.img bs=1": ["disk.img"],
            "git checkout -- a.py b.py": ["a.py", "b.py"],
            "sudo tee /etc/hosts": ["/etc/hosts"],
            "bash -c 'echo x > inner.txt'": ["inner.txt"],
            "echo x 2>/dev/null >&2": [],
            "touch a.txt; ls > list.txt": ["list.txt", "a.txt"],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(shell_targets(command), expected)

    def test_a_heredoc_write_carries_its_body(self) -> None:
        out = translate(codex("Bash", {"command": "cat > a.txt <<'EOF'\nhello\nEOF"}))
        self.assertEqual(out[1]["tool_input"], {"file_path": "/work/app/a.txt", "content": "hello"})

    def test_a_copy_carries_the_source_file_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "src.txt"), "w") as fh:
                fh.write("copied text")
            out = translate(codex("Bash", {"command": "cp src.txt dest.txt"}, cwd=tmp))
            self.assertEqual(out[1]["tool_input"]["content"], "copied text")


class OtherTools(unittest.TestCase):
    def test_code_mode_nested_shell_and_patch_calls_are_judged_as_themselves(self) -> None:
        code = ('await tools.exec_command({cmd: "rm -rf build"});\n'
                'await tools.apply_patch("*** Begin Patch\\n*** Add File: x.txt\\n+hi\\n*** End Patch");')
        out = translate(codex("exec", {"code": code}))
        self.assertEqual(names(out), [("Bash", "rm -rf build"), ("Write", "/work/app/x.txt")])

    def test_a_code_mode_file_write_is_a_write(self) -> None:
        out = translate(codex("exec", {"code": "fs.writeFileSync('out.json', '{}')"}))
        self.assertEqual(names(out), [("Write", "/work/app/out.json")])
        self.assertEqual(out[0]["codex_derived"], "code-write")

    def test_view_image_is_a_read(self) -> None:
        out = translate(codex("view_image", {"path": "file:///tmp/shot.png"}))
        self.assertEqual(names(out), [("Read", "/tmp/shot.png")])

    def test_web_search_is_websearch_and_each_opened_url_is_webfetch(self) -> None:
        out = translate(codex("web_search", {"search_query": [{"q": "codex hooks"}],
                                             "open": [{"ref_id": "https://example.com/a"}]}))
        self.assertEqual([p["tool_name"] for p in out], ["WebSearch", "WebFetch"])
        self.assertEqual(out[0]["tool_input"]["query"], "codex hooks")
        self.assertEqual(out[1]["tool_input"]["url"], "https://example.com/a")

    def test_spawn_agent_is_agent_with_its_brief_as_the_prompt(self) -> None:
        out = translate(codex("spawn_agent", {"agent_type": "reviewer", "message": [{"text": "check it"}]}))
        self.assertEqual(out[0]["tool_name"], "Agent")
        self.assertEqual(out[0]["tool_input"]["subagent_type"], "reviewer")
        self.assertEqual(out[0]["tool_input"]["prompt"], "check it")

    def test_user_input_request_is_askuserquestion(self) -> None:
        out = translate(codex("request_user_input", {"questions": [{"question": "Which?", "options": ["a", "b"]}]}))
        self.assertEqual(out[0]["tool_name"], "AskUserQuestion")
        self.assertEqual([o["label"] for o in out[0]["tool_input"]["questions"][0]["options"]], ["a", "b"])

    def test_mcp_tools_keep_their_names(self) -> None:
        out = translate(codex("mcp__github__create_issue", {"title": "x"}))
        self.assertEqual(out[0]["tool_name"], "mcp__github__create_issue")

    def test_both_connector_spellings_come_out_the_same(self) -> None:
        a = translate(codex("mcp__codex_apps__gmail_send_email", {}))[0]["tool_name"]
        b = translate(codex("mcp__codex_apps__gmail__send_email", {}))[0]["tool_name"]
        self.assertEqual(a, "mcp__codex_apps__gmail__send_email")
        self.assertEqual(a, b)

    def test_an_unknown_tool_passes_through_under_its_own_name(self) -> None:
        out = translate(codex("update_plan", {"plan": []}))
        self.assertEqual(out[0]["tool_name"], "update_plan")

    def test_a_payload_without_a_tool_is_returned_as_is(self) -> None:
        payload = {"hook_event_name": "Stop", "session_id": "s1"}
        self.assertEqual(translate(payload), [payload])


class Matchers(unittest.TestCase):
    def test_empty_star_and_absent_match_everything(self) -> None:
        for matcher in ("", "*", None):
            self.assertTrue(matcher_fits(matcher, "Bash"))

    def test_plain_names_are_exact_lists_split_on_bars_or_commas(self) -> None:
        self.assertTrue(matcher_fits("Edit|Write", "Write"))
        self.assertTrue(matcher_fits("Edit, Write", "Write"))
        self.assertFalse(matcher_fits("Edit", "NotebookEdit"))
        self.assertTrue(matcher_fits("code-reviewer", "code-reviewer"))

    def test_anything_else_is_an_unanchored_regular_expression(self) -> None:
        self.assertTrue(matcher_fits("Edit.*", "NotebookEdit"))
        self.assertFalse(matcher_fits("^Edit$", "NotebookEdit"))
        self.assertTrue(matcher_fits("mcp__github__.*", "mcp__github__create_issue"))

    def test_a_broken_regular_expression_matches_nothing(self) -> None:
        self.assertFalse(matcher_fits("Bash(", "Bash"))


if __name__ == "__main__":
    unittest.main()
