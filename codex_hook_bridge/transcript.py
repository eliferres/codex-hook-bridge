"""Keep a Claude Code shaped copy of a Codex session log for hooks that read it.

Some Claude Code hooks open the file at the payload's `transcript_path`: a
JSON-lines file of `user` and `assistant` rows with `message.content` and
`message.usage`. Codex writes its session log (a rollout-*.jsonl file) in a
different shape, so those hooks would read nothing useful. `mirror()` keeps a
converted copy of the Codex log and returns its path, which the bridge hands
to the hooks in place of the original.

The copy is refreshed incrementally. A sidecar file remembers how far the log
has been read; each refresh converts only the new rows. An assistant row is
final only once the token count for its response has arrived, so rows still
waiting are rewritten on the next refresh. A log that shrank or was replaced
is rebuilt from the start. Usage is mapped so that input + cache read + cache
write equals Codex's own input_tokens, which already includes the cached part.

Any error returns "" and the hooks get the original path: a problem here must
never break a tool call.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from typing import Iterator, List, Optional, Tuple

MAX_LINE = 1 << 20          # a Codex row longer than this is skipped, never parsed
MAX_LOG = 200 * (1 << 20)   # a log larger than this is not mirrored
LOCK_WAIT = 5.0             # seconds a refresh waits for a parallel refresh of the same log
SAFE_ID = re.compile(r"^(?!\.+$)[A-Za-z0-9._-]{1,128}$")   # never all dots: '..' would leave the folder
# Row types only a Claude Code transcript has: a log carrying one is already Claude's.
CLAUDE_TYPES = ("user", "assistant", "system", "summary", "attachment", "file-history-snapshot")
# A user message made only of these parts is something a person typed. Any
# other part (instructions files, environment context, hook output) is the
# harness writing, which Claude Code marks isMeta.
PERSON_KINDS = ("user.text", "user.image", "unknown")
SHELL_TOOLS = ("exec_command", "shell", "local_shell")


def default_state_dir() -> str:
    """$XDG_STATE_HOME/codex-hook-bridge/transcripts, with XDG's default of ~/.local/state."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "codex-hook-bridge", "transcripts")


def _first_rows(path: str, n: int = 5) -> List[dict]:
    rows = []
    with open(path, "rb") as fh:
        for _ in range(n):
            raw = fh.readline(MAX_LINE + 1)
            if not raw:
                break
            try:
                rows.append(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
    return [r for r in rows if isinstance(r, dict)]


def log_kind(path: str) -> Tuple[str, Optional[dict]]:
    """("codex", session_meta payload) | ("claude", None) | ("", None)."""
    rows = _first_rows(path)
    if rows and rows[0].get("type") == "session_meta" and isinstance(rows[0].get("payload"), dict):
        return "codex", rows[0]["payload"]
    for r in rows:
        if "payload" not in r and ("sessionId" in r or r.get("type") in CLAUDE_TYPES):
            return "claude", None
    return "", None


def _ids(meta: dict) -> Tuple[str, str, bool]:
    """(root session id, own id, is_subagent), or ("", "", False) when an id is unsafe as a file name.
    A subagent's session_id is its root's; its own id is `id`."""
    own = str(meta.get("id") or "")
    sub = bool(meta.get("parent_thread_id"))
    root = str(meta.get("session_id") or (meta.get("parent_thread_id") if sub else own) or "")
    if not (SAFE_ID.match(root) and SAFE_ID.match(own)):
        return "", "", False
    return root, own, sub and own != root


def _paths(state_dir: str, root: str, own: str, sub: bool) -> Tuple[str, str, str]:
    """(mirror file, sidecar, lock file). A subagent's copy sits where Claude Code puts a subagent's log."""
    if sub:
        dest = os.path.join(state_dir, root, "subagents", "agent-%s.jsonl" % own)
        key = "%s.agent-%s" % (root, own)
    else:
        dest = os.path.join(state_dir, "%s.jsonl" % root)
        key = root
    side = os.path.join(state_dir, ".state", key + ".json")
    return dest, side, side[:-5] + ".lock"


def _usage(u: object) -> Optional[dict]:
    if not isinstance(u, dict):
        return None
    total = int(u.get("input_tokens") or 0)
    cached = min(int(u.get("cached_input_tokens") or 0), total)
    return {"input_tokens": total - cached, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0, "output_tokens": int(u.get("output_tokens") or 0)}


def _texts(content: object, kinds: Tuple[str, ...]) -> List[str]:
    return [c["text"] for c in content or [] if isinstance(c, dict) and c.get("type") in kinds
            and isinstance(c.get("text"), str) and c["text"].strip()]


def _tool_name(p: dict) -> str:
    """The tool name as a hook payload gives it: an MCP namespace joined with '__', none as the bare name."""
    namespace, name = str(p.get("namespace") or ""), str(p.get("name") or "")
    return "%s__%s" % (namespace, name) if namespace.startswith("mcp__") else namespace + name


def _output_text(out: object) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return "\n".join(_texts(out, ("input_text", "output_text", "text")))
    return ""


def convert(rec: dict, st: dict) -> Tuple[List[dict], Optional[dict]]:
    """(Claude rows, usage or None) for one Codex log row. `st` carries the
    session id, model and cwd from row to row."""
    kind, p, ts = rec.get("type"), rec.get("payload"), rec.get("timestamp")
    if not isinstance(p, dict):
        return [], None
    if kind == "turn_context":
        st.update({k: p[k] for k in ("model", "cwd") if isinstance(p.get(k), str)})
        return [], None
    if kind == "token_usage_record":
        return [], _usage(p.get("usage"))
    if kind == "event_msg" and p.get("type") == "token_count":
        return [], _usage((p.get("info") or {}).get("last_token_usage"))
    if kind != "response_item":
        return [], None

    def base(row_type: str, meta: bool = False) -> dict:
        row = {"type": row_type, "isSidechain": False, "sessionId": st["root"], "cwd": st.get("cwd", ""),
               "timestamp": ts, "uuid": str(p.get("id") or p.get("call_id") or "")}
        if meta:
            row["isMeta"] = True
        return row

    ptype, role = p.get("type"), p.get("role")
    if ptype in ("function_call_output", "custom_tool_call_output"):
        call_id = str(p.get("call_id") or "")
        row = base("user")
        row["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": _output_text(p.get("output"))}]}
        row["toolUseResult"] = {"call_id": call_id}
        return [row], None
    if (ptype == "message" and role == "user") or ptype == "agent_message":
        texts = _texts(p.get("content"), ("input_text",))
        if not texts:
            return [], None
        if ptype == "agent_message":
            # In a subagent's log this is its brief or a follow-up, so a prompt;
            # in the root log it is a subagent's result delivered by the harness.
            meta = not st["sub"]
        else:
            kinds = (p.get("internal_chat_message_metadata_passthrough") or {}).get("content_item_kinds") or []
            meta = bool(kinds) and not all(k in PERSON_KINDS for k in kinds)
        row = base("user", meta)
        row["message"] = {"role": "user", "content": "\n".join(texts)}
        return [row], None
    if ptype == "message" and role == "assistant":
        content = [{"type": "text", "text": t} for t in _texts(p.get("content"), ("output_text",))]
    elif ptype in ("function_call", "custom_tool_call"):
        raw = p.get("arguments") if ptype == "function_call" else p.get("input")
        try:
            args = json.loads(raw) if ptype == "function_call" else None
        except (TypeError, ValueError):
            args = None
        tool_input = args if isinstance(args, dict) else {"input": raw if isinstance(raw, str) else ""}
        name = _tool_name(p)
        if name in SHELL_TOOLS:
            # read back as Claude Code's Bash, so a hook looking for an earlier shell call finds it
            name = "Bash"
            tool_input = dict(tool_input, command=tool_input.get("cmd") or tool_input.get("command") or "")
        content = [{"type": "tool_use", "id": str(p.get("call_id") or ""), "name": name, "input": tool_input}]
    else:
        content = []   # developer text, encrypted reasoning, compaction markers
    if not content:
        return [], None
    row = base("assistant")
    row["message"] = {"role": "assistant", "model": st.get("model", ""), "content": content}
    return [row], None


def _lines(fh, start: int) -> Iterator[Tuple[Optional[bytes], int]]:
    """(line bytes, or None for a line over the cap; the offset after it) for each complete line."""
    fh.seek(start)
    while True:
        raw = fh.readline(MAX_LINE + 1)
        if not raw:
            return
        if raw.endswith(b"\n"):
            yield raw, fh.tell()
            continue
        if len(raw) <= MAX_LINE:
            return   # the row Codex is still writing: read it whole next time
        while not raw.endswith(b"\n"):
            raw = fh.readline(MAX_LINE + 1)
            if not raw:
                return
        yield None, fh.tell()


def _dump(rows: List[dict]) -> bytes:
    return b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in rows)


def _head_hash(path: str, length: int) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read(length)).hexdigest()


def _refresh(src: str, meta: dict, dest: str, side: str) -> None:
    root, _, sub = _ids(meta)
    info = os.stat(src)
    try:
        with open(side) as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    # Continue only if this is the same log grown: same file, same opening bytes, not shorter.
    head_len = min(int(saved.get("headlen") or 4096), 4096)
    mirror_size = os.path.getsize(dest) if os.path.exists(dest) else -1
    keep = (saved.get("src") == src and saved.get("ino") == info.st_ino
            and saved.get("head") == _head_hash(src, head_len)
            and 0 <= saved.get("offset", -1) <= info.st_size and 0 <= saved.get("size", -1) <= mirror_size)
    if keep:
        state = saved
    else:
        head_len = min(info.st_size, 4096)
        state = {"src": src, "ino": info.st_ino, "head": _head_hash(src, head_len), "headlen": head_len,
                 "offset": 0, "size": 0, "st": {"root": root, "sub": sub, "cwd": str(meta.get("cwd") or "")}}
    st = dict(state["st"])

    done, pending, commit = [], [], (state["offset"], dict(st))
    with open(src, "rb") as fh:
        for raw, end in _lines(fh, state["offset"]):
            rec = None
            if raw is not None:
                try:
                    rec = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    rec = None
            if isinstance(rec, dict):
                rows, usage = convert(rec, st)
                pending.extend(rows)
                if usage is not None:
                    st["usage"] = usage
                    for r in pending:
                        if r["type"] == "assistant":
                            r["message"].setdefault("usage", usage)
            if not any(r["type"] == "assistant" and "usage" not in r["message"] for r in pending):
                done.extend(pending)
                pending = []
                commit = (end, dict(st))
    # Rows still waiting for their count carry the last one known for now, and
    # are cut off and written again, final, on the next refresh.
    for r in pending:
        if r["type"] == "assistant" and st.get("usage"):
            r["message"]["usage"] = st["usage"]

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # r+b, not append mode: after a truncate, append mode's tell() still counts
    # from the old end, which recorded a wrong size and duplicated rows that
    # were refreshed twice while waiting for their token count.
    if not os.path.exists(dest):
        open(dest, "wb").close()
    with open(dest, "r+b") as out:
        out.seek(state["size"])
        out.truncate()
        out.write(_dump(done))
        out.flush()
        committed = out.tell()
        out.write(_dump(pending))
    state.update({"offset": commit[0], "size": committed, "st": commit[1]})
    tmp = "%s.%d" % (side, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, side)


def mirror(payload: dict, state_dir: Optional[str] = None) -> str:
    """The path of the refreshed Claude-shaped copy of the Codex log at
    payload["transcript_path"]; a Claude Code transcript comes back as given;
    "" when there is no log, it is not a Codex log, or anything fails."""
    try:
        state_dir = state_dir or default_state_dir()
        given = (payload or {}).get("transcript_path")
        src = os.path.abspath(os.path.expanduser(given)) if isinstance(given, str) and given else ""
        if not os.path.isfile(src):
            return ""
        kind, meta = log_kind(src)
        if kind == "claude":
            return given
        if kind != "codex" or os.path.getsize(src) > MAX_LOG:
            return ""
        root, own, sub = _ids(meta)
        if not root:
            return ""
        dest, side, lock = _paths(state_dir, root, own, sub)
        os.makedirs(os.path.dirname(side), exist_ok=True)
        with open(lock, "a") as lock_fh:
            deadline = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        return dest if os.path.exists(dest) else ""
                    time.sleep(0.05)
            _refresh(src, meta, dest, side)
        return dest
    except Exception:   # never let the copy break a tool call
        return ""
