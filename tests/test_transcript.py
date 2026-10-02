"""A Codex session log read back as a Claude Code transcript."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from codex_hook_bridge.transcript import mirror

TS = "2026-01-01T10:00:00.000Z"


def row(kind: str, payload: dict) -> dict:
    return {"timestamp": TS, "type": kind, "payload": payload}


def meta(own: str, root: str = "", parent: str = "") -> dict:
    p = {"id": own, "session_id": root or own, "cwd": "/work/app"}
    if parent:
        p["parent_thread_id"] = parent
    return row("session_meta", p)


def user(text: str, kinds: tuple = ("user.text",)) -> dict:
    return row("response_item", {"type": "message", "role": "user", "id": "u1",
                                 "content": [{"type": "input_text", "text": text}],
                                 "internal_chat_message_metadata_passthrough": {"content_item_kinds": list(kinds)}})


def say(text: str) -> dict:
    return row("response_item", {"type": "message", "role": "assistant", "id": "m1",
                                 "content": [{"type": "output_text", "text": text}]})


def call(name: str, args: dict, call_id: str) -> dict:
    return row("response_item", {"type": "function_call", "name": name, "arguments": json.dumps(args),
                                 "call_id": call_id})


def output(call_id: str, text: str) -> dict:
    return row("response_item", {"type": "function_call_output", "call_id": call_id, "output": text})


def usage(total: int, cached: int) -> dict:
    return row("token_usage_record", {"usage": {"input_tokens": total, "cached_input_tokens": cached,
                                                "output_tokens": 100}})


def brief(text: str) -> dict:
    return row("response_item", {"type": "agent_message", "content": [{"type": "input_text", "text": text}]})


class Mirror(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.log = os.path.join(self.tmp.name, "rollout.jsonl")

    def write(self, rows: list, mode: str = "w", path: str = "") -> None:
        with open(path or self.log, mode) as fh:
            for r in rows:
                fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def read(self, path: str) -> list:
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def text(self, path: str) -> str:
        with open(path) as fh:
            return fh.read()

    def mirror(self, path: str = "") -> str:
        return mirror({"transcript_path": path or self.log}, self.state)

    def test_rows_come_out_in_the_claude_code_shape(self) -> None:
        self.write([meta("root-1"), user("instructions", ("agents_md.instructions",)), user("Fix the parser."),
                    call("exec_command", {"cmd": "cat parser.py"}, "c1"), usage(40000, 30000),
                    output("c1", "exit 0"), say("Fixed."), usage(42000, 40000)])
        path = self.mirror()
        self.assertEqual(path, os.path.join(self.state, "root-1.jsonl"))
        rows = self.read(path)
        self.assertEqual([(r["type"], r.get("isMeta", False)) for r in rows],
                         [("user", True), ("user", False), ("assistant", False), ("user", False), ("assistant", False)])
        tool_use = rows[2]["message"]["content"][0]
        self.assertEqual((tool_use["name"], tool_use["input"]["command"]), ("Bash", "cat parser.py"))
        self.assertEqual(rows[3]["message"]["content"][0]["tool_use_id"], "c1")
        self.assertEqual(rows[4]["message"]["content"], [{"type": "text", "text": "Fixed."}])

    def test_usage_sums_to_codex_input_with_the_cache_split_out(self) -> None:
        self.write([meta("root-1"), say("Hi."), usage(40000, 30000)])
        u = self.read(self.mirror())[0]["message"]["usage"]
        self.assertEqual(u, {"input_tokens": 10000, "cache_read_input_tokens": 30000,
                             "cache_creation_input_tokens": 0, "output_tokens": 100})

    def test_a_subagent_log_lands_under_its_root_and_its_brief_is_its_prompt(self) -> None:
        self.write([meta("sub-2", "root-1", "root-1"), brief("Check the tests."), say("Green."), usage(10, 0)])
        path = self.mirror()
        self.assertEqual(path, os.path.join(self.state, "root-1", "subagents", "agent-sub-2.jsonl"))
        first = self.read(path)[0]
        self.assertEqual((first["message"]["content"], first.get("isMeta", False)), ("Check the tests.", False))

    def test_appended_rows_are_added_without_rewriting_earlier_ones(self) -> None:
        self.write([meta("root-1"), user("One."), say("A."), usage(10, 0)])
        before = self.text(self.mirror())
        self.write([user("Two."), say("B.")], "a")
        after = self.read(self.mirror())
        self.assertTrue(self.text(self.mirror()).startswith(before))
        self.assertEqual(after[-1]["message"]["content"][0]["text"], "B.")
        # waiting for its own count, it carries the last one known
        self.assertEqual(after[-1]["message"]["usage"]["input_tokens"], 10)
        self.write([usage(50, 0)], "a")
        self.assertEqual(self.read(self.mirror())[-1]["message"]["usage"]["input_tokens"], 50)
        self.assertEqual([r["message"]["content"] if r["type"] == "user" else r["message"]["content"][0]["text"]
                          for r in self.read(self.mirror())], ["One.", "A.", "Two.", "B."])

    def test_a_row_refreshed_twice_while_waiting_is_not_written_twice(self) -> None:
        self.write([meta("root-1"), user("One."), say("A."), usage(10, 0)])
        self.mirror()
        self.write([user("Two."), say("B.")], "a")
        self.mirror()
        self.mirror()
        self.write([usage(50, 0)], "a")
        texts = [str(r["message"]["content"]) for r in self.read(self.mirror())]
        self.assertEqual(sum("'B.'" in t for t in texts), 1)

    def test_a_replaced_log_is_rebuilt_from_the_start(self) -> None:
        self.write([meta("root-1"), user("Old."), say("A."), usage(10, 0)])
        self.mirror()
        self.write([meta("root-1"), user("New.")])
        rows = self.read(self.mirror())
        self.assertEqual([r["message"]["content"] for r in rows], ["New."])

    def test_a_claude_code_transcript_is_returned_as_given(self) -> None:
        self.write([{"type": "user", "sessionId": "x", "message": {"role": "user", "content": "hi"}}])
        self.assertEqual(self.mirror(), self.log)

    def test_no_log_or_an_unknown_file_gives_an_empty_path(self) -> None:
        self.assertEqual(self.mirror(os.path.join(self.tmp.name, "missing.jsonl")), "")
        self.write(["not json", "{}"])
        self.assertEqual(self.mirror(), "")

    def test_an_unsafe_session_id_is_not_used_as_a_file_name(self) -> None:
        self.write([meta(".."), user("x")])
        self.assertEqual(self.mirror(), "")


if __name__ == "__main__":
    unittest.main()
