"""Codex payloads in, Claude Code payloads out."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from codex_hook_bridge.translate import (COMMAND_MAX, Untranslatable, matcher_fits, shell_targets,
                                         translate)

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

    def test_two_hunks_in_one_file_are_two_edits_because_multiedit_is_gone(self) -> None:
        body = ("*** Begin Patch\n*** Update File: a.py\n@@\n-a\n+b\n@@ def f():\n-c\n+d\n*** End Patch")
        out = translate(codex("apply_patch", {"command": body}))
        self.assertEqual([(p["tool_name"], p["tool_input"]) for p in out], [
            ("Edit", {"file_path": "/work/app/a.py", "old_string": "a", "new_string": "b"}),
            ("Edit", {"file_path": "/work/app/a.py", "old_string": "c", "new_string": "d"}),
        ])

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

    def test_indented_file_headers_are_read_because_codex_trims_each_line(self) -> None:
        body = ("*** Begin Patch\n *** Add File: .env\n+K=1\n\t*** Add File: b/.env\n+K=2\n"
                "  *** Update File: a.py\n@@\n-x\n+y\n*** End Patch")
        out = translate(codex("apply_patch", {"command": body}))
        self.assertEqual(names(out), [("Write", "/work/app/.env"), ("Write", "/work/app/b/.env"),
                                      ("Edit", "/work/app/a.py")])

    def test_an_indented_header_inside_an_update_section_is_context_as_in_codex(self) -> None:
        body = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n *** Add File: .env\n*** End Patch"
        out = translate(codex("apply_patch", {"command": body}))
        self.assertEqual(names(out), [("Edit", "/work/app/a.py")])
        self.assertEqual(out[0]["tool_input"]["new_string"], "y\n*** Add File: .env")

    def test_absolute_paths_with_dot_dot_are_normalized_like_relative_ones(self) -> None:
        body = "*** Begin Patch\n*** Add File: /work/app/src/../.env\n+K=1\n*** End Patch"
        out = translate(codex("apply_patch", {"command": body}))
        self.assertEqual(names(out), [("Write", "/work/app/.env")])
        out = translate(codex("Bash", {"command": "echo x > /work/app/./a/../b.txt"}))
        self.assertEqual(out[1]["tool_input"]["file_path"], "/work/app/b.txt")

    def test_dot_dot_after_a_symlink_names_the_file_it_really_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.makedirs(os.path.join(tmp, "secret", "sub"))
            os.makedirs(os.path.join(tmp, "app"))
            os.symlink(os.path.join(tmp, "secret", "sub"), os.path.join(tmp, "app", "link"))
            body = "*** Begin Patch\n*** Add File: link/../k\n+x\n*** End Patch"
            out = translate(codex("apply_patch", {"command": body}, cwd=os.path.join(tmp, "app")))
            self.assertEqual(names(out), [("Write", os.path.join(tmp, "secret", "k"))])

    def test_dot_dot_with_no_symlink_in_the_way_keeps_the_path_as_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:   # on macOS tmp itself sits behind /var -> /private/var
            os.makedirs(os.path.join(tmp, "a"))
            body = "*** Begin Patch\n*** Add File: a/../k\n+x\n*** End Patch"
            out = translate(codex("apply_patch", {"command": body}, cwd=tmp))
            self.assertEqual(names(out), [("Write", os.path.join(tmp, "k"))])

    def test_lines_split_on_newline_only_as_codex_does(self) -> None:
        body = "*** Begin Patch\r\n*** Add File: x\r/../.env\r\n+A\rB\u2028C\r\n*** End Patch\r\n"
        out = translate(codex("apply_patch", {"command": body}))
        self.assertEqual(names(out), [("Write", "/work/app/.env")])
        self.assertEqual(out[0]["tool_input"]["content"], "A\rB\u2028C")

    def test_a_shell_patch_ends_only_on_a_line_that_is_the_end_marker(self) -> None:
        command = ("apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: notes.txt\n+see *** End Patch\n"
                   "*** Add File: .env\n+K=1\n*** End Patch\nEOF")
        out = translate(codex("Bash", {"command": command}))
        self.assertIn(("Write", "/work/app/.env"), names(out))

    def test_a_cd_before_a_shell_patch_moves_where_its_paths_resolve(self) -> None:
        command = "cd sub && apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: .env\n+K=1\n*** End Patch\nEOF"
        out = translate(codex("Bash", {"command": command}))
        self.assertIn(("Write", "/work/app/sub/.env"), names(out))
        self.assertNotIn(("Write", "/work/app/.env"), names(out))

    def test_a_quoted_cd_or_one_in_a_closed_subshell_does_not_move_a_shell_patch(self) -> None:
        patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: k\n+x\n*** End Patch\nEOF"
        for prefix in ("echo 'a;cd /nowhere' ; ", "(cd /nowhere) && "):
            with self.subTest(prefix=prefix):
                out = translate(codex("Bash", {"command": prefix + patch}, cwd="/work/secret"))
                self.assertIn(("Write", "/work/secret/k"), names(out))
                self.assertNotIn(("Write", "/nowhere/k"), names(out))

    def test_a_cd_inside_the_subshell_that_runs_the_patch_still_applies(self) -> None:
        command = "(cd sub && apply_patch <<'EOF')\n*** Begin Patch\n*** Add File: k\n+x\n*** End Patch\nEOF"
        self.assertIn(("Write", "/work/app/sub/k"), names(translate(codex("Bash", {"command": command}))))

    def test_every_patch_in_a_shell_command_is_read(self) -> None:
        command = ("apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: a.txt\n+a\n*** End Patch\nEOF\n"
                   "cd deep && apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: .env\n+K=1\n*** End Patch\nEOF")
        out = translate(codex("Bash", {"command": command}))
        self.assertIn(("Write", "/work/app/a.txt"), names(out))
        self.assertIn(("Write", "/work/app/deep/.env"), names(out))

    def test_a_patch_under_a_key_not_read_passes_through_so_match_all_hooks_see_it(self) -> None:
        for key in ("input", "patch"):
            with self.subTest(key=key):
                out = translate(codex("apply_patch", {key: PATCH}))
                self.assertEqual([(p["tool_name"], p["tool_input"]) for p in out], [("apply_patch", {key: PATCH})])

    def test_every_payload_keeps_the_session_fields_and_names_the_codex_tool(self) -> None:
        for p in translate(codex("apply_patch", {"command": PATCH})):
            self.assertEqual(p["session_id"], "s1")
            self.assertEqual(p["cwd"], CWD)
            self.assertEqual(p["codex_tool_name"], "apply_patch")


def targets(command: str) -> list:
    """The files a command writes, relative to CWD when under it."""
    return [p[len(CWD) + 1:] if p.startswith(CWD + "/") else p for _, p in shell_targets(command, CWD)]


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

    def test_an_argv_command_is_joined_with_shell_quoting_wherever_it_sits(self) -> None:
        argv = ["bash", "-lc", "cd /secret && echo x > .env"]
        for tool_input in ({"command": argv}, {"action": {"type": "exec", "command": argv}}):
            with self.subTest(tool_input=tool_input):
                out = translate(codex("local_shell", tool_input))
                self.assertEqual(names(out), [("Bash", "bash -lc 'cd /secret && echo x > .env'"),
                                              ("Write", "/secret/.env")])

    def test_local_shell_runs_in_its_action_working_directory(self) -> None:
        for workdir, expected in (("/secret", "/secret/.env"), ("sub", "/work/app/sub/.env")):
            with self.subTest(workdir=workdir):
                out = translate(codex("local_shell", {"action": {"type": "exec", "working_directory": workdir,
                                                                 "command": ["bash", "-lc", "echo x > .env"]}}))
                self.assertEqual([p["tool_input"]["file_path"] for p in out[1:]], [expected])

    def test_a_redirect_is_also_a_write_to_its_target(self) -> None:
        out = translate(codex("Bash", {"command": "echo hi > notes.md"}))
        self.assertEqual(names(out), [("Bash", "echo hi > notes.md"), ("Write", "/work/app/notes.md")])
        self.assertEqual(out[1]["codex_derived"], "shell-write")

    def test_a_redirect_inside_quotes_or_a_heredoc_body_is_not_a_write(self) -> None:
        self.assertEqual(targets('git commit -m "a > b"'), [])
        self.assertEqual(targets("python3 - <<'EOF'\nprint(1 > 0)\nEOF"), [])

    def test_known_writers_name_their_targets(self) -> None:
        cases = {
            "sed -i '' 's/a/b/' conf.py": ["conf.py"],
            "sed -e s/a/b/ conf.py": [],
            "perl -pi -e 's/a/b/' a.txt b.txt": ["a.txt", "b.txt"],
            "cp src.txt dest.txt": ["dest.txt"],
            "mv -t outdir a b": ["outdir/a", "outdir/b"],
            "cp --target-directory=out src/x.txt": ["out/x.txt"],
            "bash -lc 'echo x > lc.txt'": ["lc.txt"],
            "sh -ec 'tee ec.txt'": ["ec.txt"],
            "timeout 10 cp a.txt b.txt": ["b.txt"],
            "sudo -u git cp a .env": [".env"],
            "timeout -s KILL 10 tee t.txt": ["t.txt"],
            "env -u HOME X=1 cp a envd.txt": ["envd.txt"],
            "sudo -u root nice -n 5 tee nested.txt": ["nested.txt"],
            "(cd sub && echo x > paren.txt)": ["sub/paren.txt"],
            "(sed -i s/a/b/ sub.txt)": ["sub.txt"],
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
                self.assertEqual(targets(command), expected)

    def test_a_cd_moves_where_a_shell_write_lands(self) -> None:
        cases = {
            "cd /secret && echo x > .env": ["/secret/.env"],
            "cd sub; cp a.txt b.txt": ["/work/app/sub/b.txt", "/work/app/b.txt"],   # sub may not exist
            "bash -c 'cd /secret && tee .env'": ["/secret/.env"],
            "(cd sub && echo x > p.txt); echo y > q.txt": ["/work/app/sub/p.txt", "/work/app/q.txt"],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                out = translate(codex("Bash", {"command": command}))
                self.assertEqual([p["tool_input"]["file_path"] for p in out[1:]], expected)

    def test_a_cd_that_may_fail_before_a_semicolon_or_newline_reports_both_folders(self) -> None:
        patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: k\n+x\n*** End Patch\nEOF"
        for sep in (" ; ", "\n"):
            with self.subTest(sep=sep):
                out = translate(codex("Bash", {"command": "cd /nowhere" + sep + "echo x > f.txt" + sep + patch}))
                self.assertEqual(sorted(p["tool_input"]["file_path"] for p in out[1:]),
                                 ["/nowhere/f.txt", "/nowhere/k", "/work/app/f.txt", "/work/app/k"])
        # after && the next command runs only if the cd worked, and a folder that exists is entered
        out = translate(codex("Bash", {"command": "cd /nowhere && echo x > f.txt"}))
        self.assertEqual([p["tool_input"]["file_path"] for p in out[1:]], ["/nowhere/f.txt"])
        with tempfile.TemporaryDirectory() as tmp:
            out = translate(codex("Bash", {"command": "cd %s ; echo x > f.txt" % tmp}))
            self.assertEqual([p["tool_input"]["file_path"] for p in out[1:]], [os.path.join(tmp, "f.txt")])

    def test_a_cd_inside_a_substitution_leaves_the_rest_of_the_command_where_it_was(self) -> None:
        for inner in ("$(cd /x && pwd)", "`cd /x && pwd`", '"$(cd /x)"', "`cd /x`"):
            with self.subTest(inner=inner):
                out = translate(codex("Bash", {"command": "d=%s && echo y > f.txt" % inner}))
                self.assertEqual([p["tool_input"]["file_path"] for p in out[1:]], ["/work/app/f.txt"])

    def test_a_cd_to_home_is_expanded_however_it_is_spelled(self) -> None:
        patch = " && apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: p.env\n+K=1\n*** End Patch\nEOF"
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            for cd in ("cd $HOME/proj", "cd ${HOME}/proj", 'cd "$HOME/proj"', "cd ~/proj", "cd && cd proj",
                       "cd -P $HOME/proj", "cd -- $HOME/proj"):
                with self.subTest(cd=cd):
                    out = translate(codex("Bash", {"command": cd + " && echo x > .env" + patch}))
                    self.assertEqual([p["tool_input"]["file_path"] for p in out[1:]],
                                     ["/home/u/proj/.env", "/home/u/proj/p.env"])

    def test_more_writers_and_shell_forms_are_read(self) -> None:
        cases = {
            "curl -oattached.html https://x": [("Write", "attached.html")],
            "curl -sSo cluster.html https://x": [("Write", "cluster.html")],
            "curl --output long.html https://x": [("Write", "long.html")],
            "curl -d data -s https://x": [],
            "wget -qO quiet.html https://x": [("Write", "quiet.html")],
            "wget -o wget.log https://x": [("Write", "wget.log")],
            "wget -qa append.log https://x": [("Write", "append.log")],
            "wget --output-file=of.log https://x": [("Write", "of.log")],
            "curl -D headers.txt https://x": [("Write", "headers.txt")],
            "curl -sc jar.txt https://x": [("Write", "jar.txt")],
            "curl --cookie-jar jar2.txt https://x": [("Write", "jar2.txt")],
            "curl -sO https://x/files/report.pdf?v=1": [("Write", "report.pdf")],
            "curl --remote-name https://x/a/b.tar.gz": [("Write", "b.tar.gz")],
            "wget -qO- https://x": [],
            "unzip -ddir a.zip": [("Write", "dir"), ("Write", "dir/")],
            "unzip -qd qdir a.zip": [("Write", "qdir"), ("Write", "qdir/")],
            "tar -xf a.tar -C out/": [("Write", "out"), ("Write", "out/")],
            "bash -c -- 'echo x > dashdash.txt'": [("Write", "dashdash.txt")],
            "ksh -c 'echo x > ksh.txt'": [("Write", "ksh.txt")],
            "eval 'echo x > eval.txt'": [("Write", "eval.txt")],
            "for f in a; do cp a do.txt; done": [("Write", "do.txt")],
            "! cp a bang.txt": [("Write", "bang.txt")],
            "if true; then cp a then.txt; fi": [("Write", "then.txt")],
            "echo $(cp a subst.txt)": [("Write", "subst.txt")],
            'echo "$(cp a quoted-subst.txt)"': [("Write", "quoted-subst.txt")],
            "echo `cp a backquote.txt`": [("Write", "backquote.txt")],
            "stdbuf -oL cp a stdbuf.txt": [("Write", "stdbuf.txt")],
            "stdbuf -o L tee stdbuf2.txt": [("Write", "stdbuf2.txt")],
            "git restore .env": [("Write", ".env"), ("Edit", ".env")],
            "git restore --source HEAD~1 -- a.py": [("Write", "a.py"), ("Edit", "a.py")],
            "git -C sub restore .env": [("Write", "sub/.env"), ("Edit", "sub/.env")],
            "git -C /secret -C sub -c core.x=1 restore a": [("Write", "/secret/sub/a"), ("Edit", "/secret/sub/a")],
            "git -C sub checkout -- c.py": [("Write", "sub/c.py")],
            "for d in a; do cd /secret; done; echo x > .env": [("Write", "/secret/.env"), ("Write", ".env")],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                out = translate(codex("Bash", {"command": command}))
                self.assertEqual([(p["tool_name"], p["tool_input"]["file_path"]) for p in out[1:]],
                                 [(tool, os.path.join(CWD, path)) for tool, path in expected])

    def test_a_restore_reaches_write_and_edit_hooks_with_no_content_known(self) -> None:
        out = translate(codex("Bash", {"command": "git restore .env"}))
        self.assertEqual([(p["tool_name"], p["codex_derived"]) for p in out[1:]],
                         [("Write", "shell-write"), ("Edit", "shell-edit")])
        self.assertEqual(out[2]["tool_input"], {"file_path": "/work/app/.env", "old_string": "", "new_string": ""})

    def test_commands_nested_past_any_real_use_are_refused_not_passed(self) -> None:
        with self.assertRaises(Untranslatable):
            translate(codex("Bash", {"command": "eval " * 100 + "echo x > .env"}))

    def test_a_heredoc_write_carries_its_body(self) -> None:
        out = translate(codex("Bash", {"command": "cat > a.txt <<'EOF'\nhello\nEOF"}))
        self.assertEqual(out[1]["tool_input"], {"file_path": "/work/app/a.txt", "content": "hello"})

    def test_a_copy_carries_the_source_file_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "src.txt"), "w") as fh:
                fh.write("copied text")
            out = translate(codex("Bash", {"command": "cp src.txt dest.txt"}, cwd=tmp))
            self.assertEqual(out[1]["tool_input"]["content"], "copied text")


class LongCommands(unittest.TestCase):
    def test_the_longest_command_read_is_read_in_seconds_whatever_its_shape(self) -> None:
        # each of these took minutes once, when a scan restarted at every marker or quote
        units = {"unterminated heredoc": "cat <<EOF x\n", "heredoc marker in quotes": "echo '<<EOF'\n",
                 "escaped quotes": "\\'", "one long word": "a", "one line of redirects": " > a",
                 "nested parentheses": "(", "many shell patches": "apply_patch <<'EOF'\n*** Begin Patch\n"
                 "*** Add File: a\n+x\n*** End Patch\nEOF\n"}
        for name, unit in units.items():
            with self.subTest(shape=name):
                command = unit * (COMMAND_MAX // len(unit))
                started = time.monotonic()
                translate(codex("Bash", {"command": command}))
                # a few seconds at most here; the margin keeps a slow CI runner from failing at random
                self.assertLess(time.monotonic() - started, 20.0)

    def test_a_command_too_long_to_read_in_time_is_refused_not_passed(self) -> None:
        with self.assertRaises(Untranslatable):
            translate(codex("Bash", {"command": "x" * (COMMAND_MAX + 1)}))

    def test_cds_that_may_fail_leaving_too_many_possible_folders_are_refused_not_passed(self) -> None:
        with self.assertRaises(Untranslatable):
            translate(codex("Bash", {"command": "cd a ; cd .. ; " * 20 + "echo x > .env"}))

    def test_a_cd_chain_too_deep_to_follow_is_refused_not_passed(self) -> None:
        with self.assertRaises(Untranslatable):
            translate(codex("Bash", {"command": "cd a && " * 3000 + "echo x > .env"}))


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
