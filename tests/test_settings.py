"""Hook routes read and merged from Claude Code settings files."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from codex_hook_bridge.settings import SettingsError, default_files, load_routes


def write(path: str, data: object) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))
    return path


def hooks(event: str, matcher: str, command: str) -> dict:
    return {"hooks": {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]}}


class Merging(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = os.path.join(self.tmp.name, "config")
        self.project = os.path.join(self.tmp.name, "project")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.user})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_user_project_and_local_files_all_add_routes(self) -> None:
        write(os.path.join(self.user, "settings.json"), hooks("PreToolUse", "Bash", "user.sh"))
        write(os.path.join(self.project, ".claude", "settings.json"), hooks("PreToolUse", "Write", "project.sh"))
        write(os.path.join(self.project, ".claude", "settings.local.json"), hooks("Stop", "", "local.sh"))
        routes = load_routes(None, self.project)
        self.assertEqual([(r.event, r.matcher, r.handler["command"]) for r in routes], [
            ("PreToolUse", "Bash", "user.sh"),
            ("PreToolUse", "Write", "project.sh"),
            ("Stop", "", "local.sh"),
        ])

    def test_missing_default_files_are_skipped(self) -> None:
        self.assertEqual(load_routes(None, self.project), [])

    def test_the_user_file_follows_claude_config_dir(self) -> None:
        self.assertEqual(default_files("/p")[0], os.path.join(self.user, "settings.json"))

    def test_disable_all_hooks_true_in_the_local_file_switches_every_route_off(self) -> None:
        write(os.path.join(self.user, "settings.json"), hooks("PreToolUse", "Bash", "user.sh"))
        write(os.path.join(self.project, ".claude", "settings.local.json"), {"disableAllHooks": True})
        self.assertEqual(load_routes(None, self.project), [])

    def test_a_project_false_overrides_a_user_true_for_disable_all_hooks(self) -> None:
        write(os.path.join(self.user, "settings.json"),
              dict(hooks("PreToolUse", "Bash", "user.sh"), disableAllHooks=True))
        write(os.path.join(self.project, ".claude", "settings.json"), {"disableAllHooks": False})
        self.assertEqual([r.handler["command"] for r in load_routes(None, self.project)], ["user.sh"])

    def test_an_explicit_file_that_is_missing_is_an_error(self) -> None:
        with self.assertRaises(SettingsError) as caught:
            load_routes([os.path.join(self.tmp.name, "nope.json")])
        self.assertIn("nope.json", str(caught.exception))

    def test_a_file_that_is_not_json_is_an_error_naming_the_file(self) -> None:
        path = write(os.path.join(self.tmp.name, "bad.json"), "{not json")
        with self.assertRaises(SettingsError) as caught:
            load_routes([path])
        self.assertIn("bad.json is not valid JSON", str(caught.exception))

    def test_a_malformed_hooks_value_is_an_error_not_silence(self) -> None:
        cases = [
            {"hooks": "exit 2"},
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": "exit 2"}]}},
            {"hooks": {"PreToolUse": ["exit 2"]}},
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": ["exit 2"]}]}},
            {"hooks": {"PreToolUse": [{"matcher": 5, "hooks": []}]}},
        ]
        for i, data in enumerate(cases):
            with self.subTest(data=data):
                path = write(os.path.join(self.tmp.name, "m%d.json" % i), data)
                with self.assertRaises(SettingsError) as caught:
                    load_routes([path])
                self.assertIn("m%d.json" % i, str(caught.exception))
                self.assertEqual(len(str(caught.exception).splitlines()), 1)

    def test_a_group_without_a_matcher_matches_with_an_empty_string(self) -> None:
        path = write(os.path.join(self.tmp.name, "s.json"),
                     {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}})
        self.assertEqual(load_routes([path])[0].matcher, "")


if __name__ == "__main__":
    unittest.main()
