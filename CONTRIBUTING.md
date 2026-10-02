# Contributing

Useful changes:

- A Codex tool or payload shape the bridge does not translate yet. Include
  the payload as Codex sent it (redact what you must) and the Claude Code
  tool call it amounts to.
- A shell command that writes a file without the bridge reporting a `Write`
  for it, or one reported as a write that writes nothing.
- A Claude Code hook that behaves differently under the bridge than under
  Claude Code, with the hook's output and what each side did with it.
- Anything the README claims that turns out not to be true.

Ground rules: the package stays standard-library only and runs on Python
3.9. Every translation ships with a test that feeds a Codex payload in and
asserts the exact Claude Code payloads out, and every hook a test or the demo
runs is a small script in the repository. Keep
`python3 -m unittest discover -s tests` green. If you change what the demo
prints, regenerate the record with
`UPDATE_DEMO_TRANSCRIPT=1 python3 -m unittest tests.test_demo_transcript`
rather than editing `demo/transcript.json` by hand.
