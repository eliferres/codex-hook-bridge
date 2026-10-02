"""The README's terminal picture is a record of a real session, and this test keeps it one.

Every command in demo/transcript.json is run again with bash, from the root
of a scratch copy of the repository, and must print the same combined output
and exit with the same status. demo/terminal.svg, drawn from the transcript,
must show nothing the transcript does not hold.

To record a new session after changing the demo or the output format:
    UPDATE_DEMO_TRANSCRIPT=1 python -m unittest tests.test_demo_transcript
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSCRIPT = os.path.join(ROOT, "demo", "transcript.json")
PICTURE = os.path.join(ROOT, "demo", "terminal.svg")
PLACEHOLDER = "/path/to/checkout"
SVG = "{http://www.w3.org/2000/svg}"
ELLIPSIS = "…"


def replay(entries: List[dict]) -> List[dict]:
    """Run each recorded command in a fresh copy of the repository, in order."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "codex-hook-bridge")
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.egg-info", "build", "dist", ".venv"))
        env = dict(os.environ, XDG_STATE_HOME=os.path.join(tmp, "state"))
        out = []
        for entry in entries:
            proc = subprocess.run(["bash", "-c", entry["cmd"]], cwd=copy, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            text = proc.stdout
            # macOS reports the temp dir both as /var/... and /private/var/...
            for form in (os.path.realpath(copy), copy):
                text = text.replace(form, PLACEHOLDER)
            out.append({"cmd": entry["cmd"], "out": text.rstrip("\n"), "status": proc.returncode})
        return out


def picture_rows() -> List[Tuple[str, str]]:
    """(kind, text) for each session row of the picture: 'cmd' for a prompt
    row, 'cont' for a wrapped command's continuation, 'out' for output."""
    rows = []
    for text in ET.parse(PICTURE).getroot().iter(SVG + "text"):
        if text.get("font-size"):
            continue   # the title bar
        spans = text.findall(SVG + "tspan")
        if spans:
            rows.append(("cmd", spans[-1].text or ""))
        elif text.get("class") == "cmd":
            rows.append(("cont", (text.text or "")[4:]))
        else:
            rows.append(("out", text.text or ""))
    return rows


def shown_as(shown: str, line: str) -> bool:
    """A drawn row is its line in full, or a front part of it ending in one ellipsis."""
    if shown == line:
        return True
    return shown.endswith(ELLIPSIS) and shown.count(ELLIPSIS) == 1 and line.startswith(shown[:-1])


class DemoTranscript(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(TRANSCRIPT, encoding="utf-8") as fh:
            cls.recorded = json.load(fh)
        cls.replayed = replay(cls.recorded)
        if os.environ.get("UPDATE_DEMO_TRANSCRIPT"):
            with open(TRANSCRIPT, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(cls.replayed, indent=2, ensure_ascii=False) + "\n")
            cls.recorded = cls.replayed

    def test_each_command_prints_and_exits_as_recorded(self) -> None:
        self.assertEqual(len(self.replayed), len(self.recorded))
        for i, (want, got) in enumerate(zip(self.recorded, self.replayed), 1):
            with self.subTest(entry=i, cmd=want["cmd"]):
                self.assertEqual(got["out"], want["out"], "entry %d printed something else" % i)
                self.assertEqual(got["status"], want["status"], "entry %d exited %d, recorded %d"
                                 % (i, got["status"], want["status"]))

    def test_the_session_never_shows_a_machine_path(self) -> None:
        temp = tempfile.gettempdir()
        markers = {os.path.expanduser("~"), temp, os.path.realpath(temp)}
        for entry in self.recorded:
            for marker in markers:
                self.assertNotIn(marker, entry["out"])

    def test_the_picture_draws_the_transcript_in_order_and_nothing_else(self) -> None:
        rows = picture_rows()
        self.assertTrue(rows, "the picture has no session rows")
        at = 0
        for entry in self.recorded:
            if at == len(rows):
                break   # the picture may end between commands
            kind, text = rows[at]
            self.assertEqual(kind, "cmd", "row %d should start %r" % (at + 1, entry["cmd"]))
            pieces = [text]
            at += 1
            while at < len(rows) and rows[at][0] == "cont":
                pieces.append(rows[at][1])
                at += 1
            # a wrapped command row ends in " \"; the break took one space
            rebuilt = " ".join(p[:-2] if p.endswith(" \\") else p for p in pieces)
            self.assertEqual(rebuilt, entry["cmd"])
            for line in [l for l in entry["out"].splitlines() if l.strip()]:
                self.assertLess(at, len(rows), "the picture stops inside %r" % entry["cmd"])
                self.assertEqual(rows[at][0], "out", "row %d should be output %r" % (at + 1, line))
                self.assertTrue(shown_as(rows[at][1], line), "row %d shows %r, not %r" % (at + 1, rows[at][1], line))
                at += 1
        self.assertEqual(at, len(rows), "the picture has rows the transcript does not hold")


if __name__ == "__main__":
    unittest.main()
