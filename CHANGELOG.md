# Changelog

All notable changes to this project are documented in this file, in the format of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-10-02

### Added

- `codex-hook-bridge hook`, run by Codex as a hook, which runs your Claude Code hooks over each Codex call and returns their verdicts in the shape Codex expects for that event; a `continue: false` from a `Stop` or `SubagentStop` hook passes through as itself.
- Codex shell calls reach `Bash` hooks, and every file a shell command writes also reaches `Write` hooks, including writes behind `bash -lc`, `timeout`, subshells and `cp -t`.
- An `apply_patch` reaches your file hooks once per file, with headers read the way Codex reads them (indented ones included): `Write` for an added file, `Edit` or `MultiEdit` for an updated one, `rm` and `mv` as `Bash` for deletes and renames.
- Every matching hook, a `*` or empty matcher included, runs once on each payload a Codex call becomes, all in parallel within one time budget.
- Code-mode scripts, image views, web searches, user questions, subagent spawns, subagent messages and MCP tools are translated to their Claude Code counterparts.
- Hooks are read from the user, project and local Claude Code settings files, merged the way Claude Code merges them, with `--settings` to name files instead and `--project-dir` to set the project folder hooks see as `CLAUDE_PROJECT_DIR`. A malformed hooks entry is an error, never skipped.
- Hooks that read `transcript_path` get a Claude Code shaped copy of the Codex session log; copies not written for 30 days are pruned.
- In hook mode the bridge's own problems (a bad option, unreadable settings, an internal error) are one line on stderr with exit 1, so they never read to Codex as a refusal; a hook that cannot start is named on stderr.
- `codex-hook-bridge translate` prints the Claude Code payloads a Codex payload becomes, without running anything.
- `codex-hook-bridge parity` lists every hook route with whether Codex can reach it and why not, and exits 1 on any route nobody accounted for; `--accept` takes a file of known exceptions, `--json` prints the report as JSON, and the report states that managed-policy and plugin hooks are not read.
