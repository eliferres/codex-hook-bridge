"""Turn one Codex hook payload into the Claude Code tool payloads it amounts to.

Claude Code hooks read a payload whose `tool_name` is one of Claude Code's own
tools (Bash, Write, Edit, MultiEdit, Read, ...). Codex describes the same acts
in its own shapes: a shell call, an apply_patch body, a code-mode script, a
subagent spawn. `translate()` returns the list of Claude-shaped payloads one
Codex call performs, so every matching Claude Code hook can judge it.

One Codex call can become several payloads. A patch that touches three files
is three Write or Edit payloads. A shell command that writes a file is a Bash
payload plus a Write payload for that file, so a hook that protects a path on
Write also sees `sed -i` and `>` aimed at it.

Every translated payload keeps the Codex payload's other keys (session_id,
cwd, transcript_path, hook_event_name, ...) and adds two of its own:
`codex_tool_name`, the name Codex used, and, on payloads derived rather than
renamed, `codex_derived`, saying how it was derived.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Iterator, List, Optional, Tuple

# Every Claude Code tool name a translation can produce. Parity uses this set
# to decide whether a hook's matcher can ever be reached from Codex.
CLAUDE_TOOLS = (
    "Bash", "Write", "Edit", "MultiEdit", "Read", "WebSearch", "WebFetch",
    "AskUserQuestion", "Agent", "SendMessage",
)

Call = Tuple[str, dict, str]   # (Claude tool name, tool_input, derivation tag or "")


# ---------------------------------------------------------------------------
# apply_patch

PATCH_FILE_RX = re.compile(r"^\*\*\* (Add|Update|Delete) File:\s*(.+?)\s*$")
PATCH_MOVE_RX = re.compile(r"^\*\*\* Move to:\s*(.+?)\s*$")


def absolute(path: str, cwd: str) -> str:
    """`path` made absolute against the session's cwd, the way Codex resolves it."""
    path = os.path.expanduser(str(path).strip().strip("'\""))
    if path.startswith("file://"):
        path = path[7:]
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(cwd or os.getcwd(), path))


def shell_quote(text: str) -> str:
    """Single-quote `text` for a POSIX shell, independent of what it contains."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def parse_patch(body: str) -> List[dict]:
    """One dict per file an apply_patch body touches:
    {op: add|update|delete, path, move_to, added: [lines], hunks: [(old, new)]}.

    Context lines belong to both sides of a hunk, so each (old, new) pair is a
    literal search-and-replace that Claude Code's Edit tool would perform.
    """
    files: List[dict] = []
    cur: Optional[dict] = None
    hunk: Optional[list] = None

    def close_hunk() -> None:
        if cur is not None and hunk is not None and (hunk[0] or hunk[1]):
            cur["hunks"].append(("\n".join(hunk[0]), "\n".join(hunk[1])))

    for line in body.splitlines():
        m = PATCH_FILE_RX.match(line)
        if m:
            close_hunk()
            cur = {"op": m.group(1).lower(), "path": m.group(2), "move_to": "",
                   "added": [], "hunks": []}
            hunk = [[], []] if cur["op"] == "update" else None
            files.append(cur)
            continue
        if cur is None or line.startswith(("*** End Patch", "*** Begin Patch")):
            continue
        m = PATCH_MOVE_RX.match(line)
        if m:
            cur["move_to"] = m.group(1)
            continue
        if line.startswith("*** End of File"):
            continue
        if cur["op"] == "add":
            if line.startswith("+"):
                cur["added"].append(line[1:])
        elif cur["op"] == "update":
            if line.startswith("@@"):
                close_hunk()
                hunk = [[], []]
            elif line.startswith("-"):
                hunk[0].append(line[1:])
            elif line.startswith("+"):
                hunk[1].append(line[1:])
            else:
                context = line[1:] if line.startswith(" ") else line
                hunk[0].append(context)
                hunk[1].append(context)
    close_hunk()
    return files


def patch_calls(body: str, cwd: str) -> List[Call]:
    """The Claude Code calls an apply_patch body performs, one per file.

    Add is a Write with the new content. Update is an Edit (one hunk) or a
    MultiEdit (several). Delete is `rm` and a rename is `mv`, both as Bash,
    because Claude Code has no delete or rename tool and its hooks see those
    acts as shell commands.
    """
    out: List[Call] = []
    for f in parse_patch(body):
        path = absolute(f["path"], cwd)
        if f["op"] == "delete":
            out.append(("Bash", {"command": "rm -- " + shell_quote(path)}, "patch-delete"))
        elif f["op"] == "add":
            out.append(("Write", {"file_path": path, "content": "\n".join(f["added"])}, ""))
        else:
            if f["move_to"]:
                dest = absolute(f["move_to"], cwd)
                command = "mv -- %s %s" % (shell_quote(path), shell_quote(dest))
                out.append(("Bash", {"command": command}, "patch-move"))
                path = dest
            edits = [{"old_string": o, "new_string": n} for o, n in f["hunks"]]
            if len(edits) == 1:
                out.append(("Edit", dict({"file_path": path}, **edits[0]), ""))
            elif edits:
                out.append(("MultiEdit", {"file_path": path, "edits": edits}, ""))
            else:
                # a bare rename still writes the destination path
                out.append(("Write", {"file_path": path, "content": ""}, "patch-empty"))
    return out


def patch_in_shell(command: str) -> str:
    """The apply_patch body inside a shell command (`apply_patch <<'EOF' ...`), or ""."""
    start = command.find("*** Begin Patch")
    if start < 0:
        return ""
    end = command.find("*** End Patch", start)
    return command[start:end + len("*** End Patch")] if end > 0 else command[start:]


# ---------------------------------------------------------------------------
# Files a shell command writes
#
# On Codex the shell is also a file-writing tool, so each file a command
# writes is reported as a Write as well. The command is scanned with quoted
# spans and heredoc bodies blanked out first, so a `>` inside a commit message
# or a script string is never read as a redirect; target names are then read
# back from the original text, quotes included.

HEREDOC_RX = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*$", re.S | re.M)
QUOTED_RX = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
REDIRECT_OP_RX = re.compile(r"(?:\d|&)?>>?\|?")
SEGMENT_RX = re.compile(r"[;&|\n]+")
SOURCE_MAX = 200_000   # bytes read from a copy's source file to show what it writes
INPLACE_PROGS = ("sed", "gsed", "perl", "ruby")
OUTPUT_OPTIONS = {"curl": ("-o", "--output"), "wget": ("-O", "--output-document"),
                  "tar": ("-C", "--directory"), "unzip": ("-d",)}
WRAPPERS = ("sudo", "env", "command", "nohup", "time", "nice", "exec", "xargs", "doas")
WRITERS = ("cp", "mv", "install", "rsync", "ditto", "ln", "tee", "truncate", "touch", "dd",
           "curl", "wget", "tar", "unzip", "git", "sed", "gsed", "perl", "ruby",
           "bash", "sh", "zsh", "dash")
SHELLS = ("bash", "sh", "zsh", "dash")


def mask(command: str) -> str:
    """`command` with every quoted span and heredoc body replaced by spaces of the same length."""
    out = list(command)
    for m in HEREDOC_RX.finditer(command):
        for i in range(m.start(3), m.end(3)):
            if command[i] != "\n":
                out[i] = " "
    masked = "".join(out)
    for m in QUOTED_RX.finditer(masked):
        for i in range(m.start() + 1, m.end() - 1):
            out[i] = " "
    return "".join(out)


def _words(text: str) -> List[str]:
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:   # an unbalanced quote: fall back to whitespace
        return text.split()


def _segments(command: str) -> Iterator[List[str]]:
    """The words of each simple command in the line, split where the masked text has ; & | or a newline."""
    masked = mask(command)
    cuts = [0] + [m.end() for m in SEGMENT_RX.finditer(masked)] + [len(command)]
    for i in range(len(cuts) - 1):
        words = _words(command[cuts[i]:cuts[i + 1]].rstrip(";&|\n"))
        while words and "=" in words[0] and words[0].split("=")[0].isidentifier():
            words = words[1:]   # leading VAR=value assignments
        if words:
            yield words


def redirect_targets(command: str) -> List[str]:
    """Files named after >, >>, >|, 2>, &> outside quotes and heredoc bodies."""
    masked = mask(command)
    found = []
    for m in REDIRECT_OP_RX.finditer(masked):
        rest = command[m.end():].lstrip()
        if not rest or rest[0] in "&|;<>" or masked[m.start():m.end()].endswith("<"):
            continue   # >&2, a descriptor duplication, or part of <<
        first_line = rest[:rest.find("\n")] if "\n" in rest else rest
        word = (_words(first_line) or [""])[0]
        found.append(re.split(r"[;&|]", word)[0])   # `> f; ls`: the separator is not the name
    return found


def inplace_files(words: List[str]) -> List[str]:
    """The files `sed -i` / `perl -i` / `ruby -i` rewrite, or [] when -i is absent."""
    options = [w for w in words[1:] if w.startswith("-")]
    inplace = any(w.startswith("--in-place")
                  or (not w.startswith("--") and "i" in w[1:].split("=")[0]) for w in options)
    if not inplace:
        return []
    operands, skip = [], False
    for w in words[1:]:
        if skip:
            skip = False
            continue
        if w in ("-e", "-f", "--expression", "--file"):
            skip = True
            operands.append("<program>")
        elif w.startswith(("--expression=", "--file=")):
            operands.append("<program>")
        elif w and not w.startswith("-"):
            operands.append(w)
    # without -e the first operand is the program text itself
    return [f for f in operands[1:] if f != "<program>"]


def segment_targets(words: List[str]) -> List[str]:
    """The files one simple command writes, by what its program is known to write."""
    if words and os.path.basename(words[0]) in WRAPPERS:
        index = next((i for i, w in enumerate(words[1:], 1) if os.path.basename(w) in WRITERS), None)
        if index is None:
            return []
        words = words[index:]
    prog = os.path.basename(words[0])
    if prog in SHELLS and "-c" in words[1:-1]:
        return shell_targets(words[words.index("-c") + 1])
    operands = [w for w in words[1:] if not w.startswith("-")]
    if prog in INPLACE_PROGS:
        return inplace_files(words)
    if prog == "tee":
        return operands
    if prog in ("cp", "mv", "install", "rsync", "ditto", "ln"):
        for i, w in enumerate(words):
            if w in ("-t", "--target-directory") and i + 1 < len(words):
                return [words[i + 1]]
            if w.startswith("--target-directory="):
                return [w.split("=", 1)[1]]
        return [operands[-1]] if len(operands) >= 2 else []
    if prog in ("truncate", "touch"):
        return [w for w in operands if not w[:1].isdigit()]
    if prog == "dd":
        return [w[3:] for w in words[1:] if w.startswith("of=")]
    if prog in OUTPUT_OPTIONS:
        out = []
        for i, w in enumerate(words):
            for opt in OUTPUT_OPTIONS[prog]:
                if w == opt and i + 1 < len(words):
                    out.append(words[i + 1])
                elif w.startswith(opt + "="):
                    out.append(w.split("=", 1)[1])
        return out
    if prog == "git" and len(words) > 2 and words[1] in ("checkout", "restore") and "--" in words:
        return words[words.index("--") + 1:]
    return []


def shell_targets(command: str) -> List[str]:
    """Every path a shell command writes, in first-seen order. /dev/* is not a file."""
    found = redirect_targets(command)
    for words in _segments(command):
        found += segment_targets(words)
    found = [p.replace("${HOME}", "~").replace("$HOME", "~") for p in found]
    return [p for p in dict.fromkeys(found) if p and not p.startswith(("/dev/", "&"))]


def _read_small(path: str) -> str:
    try:
        if os.path.isfile(path) and os.path.getsize(path) <= SOURCE_MAX:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
    except OSError:
        pass
    return ""


def shell_write_calls(command: str, cwd: str) -> List[Call]:
    """One derived Write per file the command writes.

    The content is the text the command visibly writes when it can be seen (a
    heredoc body, the source file of a copy, the quoted arguments), else the
    best available approximation. A hook that judges the path alone gets the
    path exactly; a hook that judges content gets the closest text there is.
    """
    targets = shell_targets(command)
    if not targets:
        return []
    bodies = [m.group(3) for m in HEREDOC_RX.finditer(command)]
    if not bodies:
        for words in _segments(command):
            if os.path.basename(words[0]) in ("cp", "cat", "install", "ditto"):
                operands = [w for w in words[1:] if not w.startswith("-")]
                sources = operands[:-1] or operands
                bodies += [t for t in (_read_small(absolute(o, cwd)) for o in sources) if t]
    if not bodies:
        quoted = [a or b for a, b in QUOTED_RX.findall(command)]
        if quoted:
            bodies = ["\n".join(quoted).replace("\\n", "\n")]
    content = "\n".join(bodies)
    return [("Write", {"file_path": absolute(p, cwd), "content": content}, "shell-write")
            for p in targets]


# ---------------------------------------------------------------------------
# Code mode: a JavaScript cell that calls tools.exec_command and tools.apply_patch

JS_SHELL_RX = re.compile(r"tools\.exec_command\s*\(")
JS_PATCH_RX = re.compile(r"tools\.apply_patch\s*\(")
JS_FS_WRITE_RX = re.compile(
    r"(?:^|[^\w$])(?:fs\.|fsp\.|promises\.)?"
    r"(writeFileSync|writeFile|appendFileSync|appendFile|createWriteStream)\s*\(")
JS_CMD_RX = re.compile(
    r"[\"']?cmd[\"']?\s*:\s*(`(?:[^`\\]|\\.)*`|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')")


def _unquote(token: str) -> Optional[str]:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "`'\"":
        return token[1:-1].replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
    return None


def js_args(code: str, start: int) -> List[str]:
    """The top-level arguments of the call whose '(' ends at `start`, split on
    commas outside strings, template literals and brackets."""
    args, cur, depth, i, quote = [], [], 0, start, ""
    while i < len(code):
        c = code[i]
        if quote:
            cur.append(c)
            if c == "\\" and i + 1 < len(code):
                cur.append(code[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    args.append("".join(cur).strip())
    return [a for a in args if a]


def code_mode_calls(code: str, cwd: str) -> List[Call]:
    """The shell commands, patches and file writes a code-mode script contains.

    A nested command that is not a string literal is passed as written, so a
    shell hook still reads the call. A file write whose path is not a literal
    is reported with a placeholder path and the whole script as its content.
    """
    out: List[Call] = []
    for m in JS_SHELL_RX.finditer(code):
        arg = (js_args(code, m.end()) or [""])[0]
        cm = JS_CMD_RX.search(arg)
        command = _unquote(cm.group(1)) if cm else None
        out += calls("Bash", {"command": command if command is not None else arg}, cwd)
    for m in JS_PATCH_RX.finditer(code):
        body = _unquote((js_args(code, m.end()) or [""])[0]) or ""
        out += patch_calls(body, cwd) if body else []
    for m in JS_FS_WRITE_RX.finditer(code):
        args = js_args(code, m.end())
        path = _unquote(args[0]) if args else None
        body = _unquote(args[1]) if len(args) > 1 else None
        out.append(("Write", {"file_path": absolute(path, cwd) if path else "<unresolved path in script>",
                              "content": body if body is not None else code}, "code-write"))
    return out


# ---------------------------------------------------------------------------
# The tool table

def _text(value: object) -> str:
    """A message or brief as text, whatever Codex wrapped it in (a list of parts, a dict)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("content") or value.get("message") or "")
    return "" if value is None else str(value)


def _list(value: object) -> list:
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def _command(tool_input: dict) -> str:
    for key in ("command", "cmd", "script"):
        value = tool_input.get(key)
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        if isinstance(value, str) and value:
            return value
    return ""


def _questions(tool_input: dict) -> dict:
    out = []
    for q in tool_input.get("questions") or []:
        if not isinstance(q, dict):
            continue
        options = []
        for o in q.get("options") or []:
            if isinstance(o, str):
                options.append({"label": o, "description": ""})
            elif isinstance(o, dict):
                options.append({"label": str(o.get("label", "")),
                                "description": str(o.get("description", ""))})
        out.append({"question": str(q.get("question") or q.get("title") or ""),
                    "header": str(q.get("header") or q.get("id") or "")[:12], "options": options})
    return {"questions": out}


def connector_name(tool: str) -> str:
    """Codex app connectors appear as `mcp__codex_apps__<app>__<leaf>` in hook
    payloads and as `mcp__codex_apps__<app>_<leaf>` in session logs. Both come
    out in the `__` form, the shape Claude Code gives MCP tools,
    so one matcher covers both. The app is the longest known prefix."""
    prefix = "mcp__codex_apps__"
    rest = tool[len(prefix):]
    if "__" in rest:
        return tool
    apps = ("google_drive", "gmail", "slack", "github", "figma", "docusign", "sites")
    app = next((a for a in apps if rest.startswith(a + "_")), "")
    return "%s%s__%s" % (prefix, app, rest[len(app) + 1:]) if app else tool


def calls(tool: str, tool_input: dict, cwd: str) -> List[Call]:
    """The Claude Code calls one Codex tool call amounts to."""
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read"):
        return [(tool, tool_input, "")]
    if tool in ("Bash", "exec_command", "shell", "local_shell", "apply_patch", "write_stdin"):
        # write_stdin types into a running shell: its text is a command like any other
        command = _text(tool_input.get("chars")) if tool == "write_stdin" else _command(tool_input)
        if not command:
            return [] if tool in ("apply_patch", "write_stdin") else [("Bash", tool_input, "")]
        if command.lstrip().startswith("*** Begin Patch"):
            return patch_calls(command, cwd)
        body = patch_in_shell(command)
        return ([("Bash", {"command": command}, "")] + shell_write_calls(command, cwd)
                + (patch_calls(body, cwd) if body else []))
    if tool == "exec":
        code = _text(tool_input.get("code") or tool_input.get("input") or _command(tool_input))
        return code_mode_calls(code, cwd) or [(tool, tool_input, "")]
    if tool == "view_image":
        return [("Read", {"file_path": absolute(tool_input.get("path") or "", cwd)}, "")]
    if tool in ("web_search", "webrun", "web__run"):
        out: List[Call] = []
        queries = [q.get("q") for q in _list(tool_input.get("search_query")) + _list(tool_input.get("image_query"))
                   if isinstance(q, dict) and q.get("q")]
        if queries:
            out.append(("WebSearch", {"query": " | ".join(map(str, queries))}, ""))
        for o in _list(tool_input.get("open")):
            ref = str(o.get("ref_id") or "")
            if ref.startswith("http"):
                out.append(("WebFetch", {"url": ref, "prompt": ""}, ""))
        return out or [("WebSearch", {"query": ""}, "")]
    if tool in ("request_user_input", "request_user_input_async"):
        return [("AskUserQuestion", _questions(tool_input), "")]
    if tool in ("spawn_agent", "collaborationspawn_agent"):
        agent = {"subagent_type": str(tool_input.get("agent_type") or "general-purpose"),
                 "model": str(tool_input.get("model") or ""),
                 "prompt": _text(tool_input.get("message") or tool_input.get("prompt") or ""),
                 "description": str(tool_input.get("task_name") or "")}
        return [("Agent", agent, "")]
    if tool in ("send_message", "followup_task", "collaborationsend_message", "collaborationfollowup_task"):
        return [("SendMessage", {"to": str(tool_input.get("target") or ""),
                                 "message": _text(tool_input.get("message"))}, "")]
    if tool.startswith("mcp__codex_apps__"):
        return [(connector_name(tool), tool_input, "")]
    # MCP tools keep their names; anything else passes through as itself
    return [(tool, tool_input, "")]


def translate(payload: dict) -> List[dict]:
    """The Claude-shaped payloads one Codex hook payload amounts to.

    Keys other than tool_name and tool_input carry over unchanged. A payload
    with no tool (SessionStart, Stop, ...) comes back as the only element.
    """
    if not isinstance(payload, dict):
        return []
    tool = str(payload.get("tool_name") or "")
    if not tool:
        return [payload]
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, str):
        tool_input = {"command": tool_input}
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = str(payload.get("cwd") or tool_input.get("workdir") or tool_input.get("cwd") or "")
    base = {k: v for k, v in payload.items() if k not in ("tool_name", "tool_input")}
    out = []
    for name, translated_input, tag in calls(tool, tool_input, cwd):
        p = dict(base, tool_name=name, tool_input=translated_input, codex_tool_name=tool)
        if tag:
            p["codex_derived"] = tag
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Claude Code's matcher rule

EXACT_MATCHER_RX = re.compile(r"^[A-Za-z0-9_\- ,|]+$")
MATCH_ALL = ("", "*", None)


def matcher_fits(matcher: Optional[str], value: str) -> bool:
    """Whether a Claude Code matcher selects `value` (a tool name, a session
    source, an agent type, ...).

    Empty, "*" or absent matches everything. A matcher of only letters,
    digits, `_`, `-`, spaces, commas and bars is an exact name or a list of
    exact names split on `|` or `,`. Anything else is a regular expression
    searched anywhere in the value, so `^` and `$` do the anchoring. Python's
    `re` stands in for JavaScript's RegExp; the two agree on the patterns
    matchers use in practice.
    """
    if matcher in MATCH_ALL:
        return True
    if EXACT_MATCHER_RX.match(matcher):
        return value in [part.strip() for part in re.split(r"[|,]", matcher)]
    try:
        return re.search(matcher, value) is not None
    except re.error:
        return False

