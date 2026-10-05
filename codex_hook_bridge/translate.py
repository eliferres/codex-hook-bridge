"""Turn one Codex hook payload into the Claude Code tool payloads it amounts to.

Claude Code hooks read a payload whose `tool_name` is one of Claude Code's own
tools (Bash, Write, Edit, Read, ...). Codex describes the same acts
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

import bisect
import os
import re
import shlex
from typing import Callable, Iterator, List, Optional, Tuple

# Every Claude Code tool name a translation can produce. Parity uses this set
# to decide whether a hook's matcher can ever be reached from Codex.
CLAUDE_TOOLS = (
    "Bash", "Write", "Edit", "Read", "WebSearch", "WebFetch",
    "AskUserQuestion", "Agent", "SendMessage",
)

Call = Tuple[str, dict, str]   # (Claude tool name, tool_input, derivation tag or "")


# ---------------------------------------------------------------------------
# apply_patch

PATCH_HEADERS = (("*** Add File: ", "add"), ("*** Delete File: ", "delete"), ("*** Update File: ", "update"))
PATCH_MOVE = "*** Move to: "


def absolute(path: str, cwd: str) -> str:
    """`path` made absolute against the session's cwd, the way Codex resolves it."""
    path = os.path.expanduser(str(path).strip().strip("'\""))
    if path.startswith("file://"):
        path = path[7:]
    joined = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
    # normalized either way, so `/a/b/../.env` reaches a hook as `/a/.env`
    lexical = os.path.normpath(joined)
    if ".." not in joined.split(os.sep):
        return lexical
    # A `..` after a symlink climbs out of the link's target, not out of the
    # folder the link sits in, so the tidied path can name a different file.
    # Then, and only then, the parent folder's real path is used: a path that
    # needs no resolving stays as written, so a hook comparing it with the
    # session folder still matches when that folder sits behind a symlink.
    head, tail = os.path.split(joined)
    if tail in ("", ".", ".."):
        head, tail = joined, ""
    real = os.path.realpath(head)
    if real == os.path.realpath(os.path.dirname(lexical) if tail else lexical):
        return lexical
    return os.path.join(real, tail) if tail else real


def expand_home(word: str) -> str:
    """`word` with $HOME and ${HOME} replaced by the home folder, as the shell
    expands them; a leading ~ is left for absolute() to expand."""
    home = os.path.expanduser("~")
    return word.replace("${HOME}", home).replace("$HOME", home)


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

    # Header lines are recognized the way Codex's own parser does it: with the
    # line trimmed on both sides, except inside an Update section, where only
    # trailing space is trimmed, so an indented header there is a context line.
    # Lines split on \n only, one trailing \r dropped: str.splitlines() would
    # also split on a lone \r, VT or U+2028 and read a different file name.
    for line in body.strip().split("\n"):
        line = line[:-1] if line.endswith("\r") else line
        in_update = cur is not None and cur["op"] == "update"
        marker_text = line.rstrip() if in_update else line.strip()
        header = next(((op, marker_text[len(m):]) for m, op in PATCH_HEADERS if marker_text.startswith(m)), None)
        if header:
            close_hunk()
            cur = {"op": header[0], "path": header[1], "move_to": "", "added": [], "hunks": []}
            hunk = [[], []] if cur["op"] == "update" else None
            files.append(cur)
            continue
        if marker_text in ("*** End Patch", "*** Begin Patch") or cur is None:
            continue
        if cur["op"] == "add":
            if line.startswith("+"):
                cur["added"].append(line[1:])
        elif cur["op"] == "update":
            if marker_text.startswith(PATCH_MOVE) and not cur["hunks"] and not (hunk[0] or hunk[1]):
                cur["move_to"] = marker_text[len(PATCH_MOVE):]
            elif marker_text == "*** End of File":
                continue
            elif marker_text == "@@" or marker_text.startswith("@@ "):
                close_hunk()
                hunk = [[], []]
            elif line.startswith("-"):
                hunk[0].append(line[1:])
            elif line.startswith("+"):
                hunk[1].append(line[1:])
            elif line.startswith(" ") or not line:
                hunk[0].append(line[1:])
                hunk[1].append(line[1:])
    close_hunk()
    return files


def patch_calls(body: str, cwd: str) -> List[Call]:
    """The Claude Code calls an apply_patch body performs, file by file.

    Add is a Write with the new content. Update is one Edit per hunk, since
    current Claude Code has no MultiEdit tool. Delete is `rm` and a rename is
    `mv`, both as Bash, because Claude Code has no delete or rename tool and
    its hooks see those acts as shell commands.
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
            for old, new in f["hunks"]:
                out.append(("Edit", {"file_path": path, "old_string": old, "new_string": new}, ""))
            if not f["hunks"]:
                # a bare rename still writes the destination path
                out.append(("Write", {"file_path": path, "content": ""}, "patch-empty"))
    return out


# On the masked command (see mask()), so a `cd` inside quotes or a heredoc
# body is not one: a parenthesis, or a cd at the start of a simple command,
# past its -L/-P options and `--`. A cd with no folder goes home.
FOLDER_RX = re.compile(
    r"[()]|(?:^|(?<=[;&|\n(]))[ \t]*cd(?:[ \t]+-[LPe@]+)*(?:[ \t]+--)?"
    r"(?:[ \t]+('[^']*'|\"[^\"]*\"|[^\s;&|()]+)|(?=[ \t]*(?:$|[;&|)\n])))", re.M)


def folders(command: str, cwd: str, masked: str = "") -> Callable[[int], str]:
    """A function giving the folder the shell is in at each offset of `command`.

    Every `cd` moves it; a cd inside a subshell, `( ... )`, holds only until
    the subshell's closing parenthesis.
    """
    masked = masked or mask(command)
    stack, offsets, values = [cwd], [0], [cwd]
    for m in FOLDER_RX.finditer(masked):
        if m.group() == "(":
            stack.append(stack[-1])
        elif m.group() == ")":
            if len(stack) > 1:   # an unmatched `)` (a case pattern) closes nothing
                stack.pop()
        elif m.group(1) is None:
            stack[-1] = os.path.expanduser("~")
        elif command[m.start(1):m.end(1)] != "-":   # `cd -`: the previous folder, not known here
            stack[-1] = absolute(expand_home(command[m.start(1):m.end(1)]), stack[-1])
        offsets.append(m.end())
        values.append(stack[-1])
    return lambda offset: values[bisect.bisect_right(offsets, offset) - 1]


def patches_in_shell(command: str, cwd: str) -> List[Tuple[str, str]]:
    """(apply_patch body, folder its paths resolve against) for every patch
    inside a shell command (`apply_patch <<'EOF' ...`), in order.

    A body ends at the first line whose trimmed text is the end marker, as
    in Codex; the marker appearing inside a line of content does not end it.
    A patch resolves in the folder its command runs in: after any `cd` before
    it (Codex's own `cd <dir> && apply_patch` form included), and for a
    heredoc, at the `<<` that opens it rather than where its body sits.
    """
    found: List[Tuple[str, str]] = []
    where = folders(command, cwd)
    bodies = heredocs(command)
    body_starts = [body_start for _, body_start, _ in bodies]
    done = 0
    while True:
        start = command.find("*** Begin Patch", done)
        if start < 0:
            return found
        k = bisect.bisect_right(body_starts, start) - 1
        opened_at = bodies[k][0] if k >= 0 and start <= bodies[k][2] else start
        end = len(command)
        offset = start
        for line in command[start:].split("\n"):
            offset += len(line) + 1
            if line.strip() == "*** End Patch":
                end = offset - 1
                break
        found.append((command[start:end], where(opened_at)))
        done = end


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
WRAPPERS = ("sudo", "env", "command", "nohup", "time", "timeout", "nice", "exec", "xargs", "doas")
SHELLS = ("bash", "sh", "zsh", "dash")


def heredocs(command: str) -> List[Tuple[int, int, int]]:
    """(offset of its `<<`, body start, body end) for each heredoc in `command`."""
    return [(m.start(), m.start(3), m.end(3)) for m in HEREDOC_RX.finditer(command)]


def mask(command: str) -> str:
    """`command` with every quoted span and heredoc body replaced by spaces of the same length."""
    out = list(command)
    for _, body_start, body_end in heredocs(command):
        for i in range(body_start, body_end):
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


def _segments(command: str, masked: str = "") -> Iterator[Tuple[List[str], int]]:
    """(words, offset) of each simple command in the line, split where the
    masked text has ; & | or a newline."""
    masked = masked or mask(command)
    cuts = [0] + [m.end() for m in SEGMENT_RX.finditer(masked)] + [len(command)]
    for i in range(len(cuts) - 1):
        words = _words(command[cuts[i]:cuts[i + 1]].rstrip(";&|\n"))
        # a subshell's parentheses are not part of the program or file names
        words = [w for w in (w.lstrip("(") if j == 0 else w for j, w in enumerate(words)) if w]
        words = [w.rstrip(")") or w for w in words]
        while words and "=" in words[0] and words[0].split("=")[0].isidentifier():
            words = words[1:]   # leading VAR=value assignments
        if words:
            yield words, cuts[i]


def redirect_targets(command: str, masked: str = "") -> List[Tuple[str, int]]:
    """(file, offset) for each file named after >, >>, >|, 2>, &> outside
    quotes and heredoc bodies."""
    masked = masked or mask(command)
    found = []
    for m in REDIRECT_OP_RX.finditer(masked):
        rest = command[m.end():].lstrip()
        if not rest or rest[0] in "&|;<>" or masked[m.start():m.end()].endswith("<"):
            continue   # >&2, a descriptor duplication, or part of <<
        first_line = rest[:rest.find("\n")] if "\n" in rest else rest
        word = (_words(first_line) or [""])[0]
        found.append((re.split(r"[;&|)]", word)[0], m.start()))   # `> f; ls`, `(... > f)`: not part of the name
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


# Wrapper options that take a separate value, so the value is not mistaken
# for the program (`sudo -u git cp ...` runs cp, not git).
WRAPPER_VALUE_OPTIONS = {
    "sudo": ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"),
    "doas": ("-u", "-C"),
    "env": ("-u", "-C", "-S", "--unset", "--chdir", "--split-string"),
    "timeout": ("-s", "-k", "--signal", "--kill-after"),
    "nice": ("-n", "--adjustment"),
    "time": ("-f", "-o", "--format", "--output"),
    "xargs": ("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "--max-args", "--max-procs",
              "--delimiter", "--arg-file"),
}


def unwrap(words: List[str]) -> List[str]:
    """`words` with any leading wrappers (sudo, env, timeout, ...) and their
    own options and values removed, so the first word is the program run."""
    while words and os.path.basename(words[0]) in WRAPPERS:
        wrapper = os.path.basename(words[0])
        rest = words[1:]
        while rest and (rest[0].startswith("-") or (wrapper == "env" and "=" in rest[0])):
            takes_value = rest[0] in WRAPPER_VALUE_OPTIONS.get(wrapper, ())
            rest = rest[2:] if takes_value else rest[1:]
        if wrapper == "timeout" and rest:
            rest = rest[1:]   # the duration
        words = rest
    return words


def segment_targets(words: List[str], folder: str) -> List[str]:
    """The files one simple command run in `folder` writes, by what its
    program is known to write, as written in the command."""
    words = unwrap(words)
    if not words:
        return []
    prog = os.path.basename(words[0])
    if prog in SHELLS:
        # -c alone or combined with other flags (-lc, -ec): the next word is the command
        flag = next((i for i, w in enumerate(words[1:-1], 1)
                     if w.startswith("-") and not w.startswith("--") and "c" in w[1:]), None)
        if flag is not None:
            return shell_targets(words[flag + 1], folder)   # absolute already, so they resolve to themselves
    operands = [w for w in words[1:] if not w.startswith("-")]
    if prog in INPLACE_PROGS:
        return inplace_files(words)
    if prog == "tee":
        return operands
    if prog in ("cp", "mv", "install", "rsync", "ditto", "ln"):
        for i, w in enumerate(words):
            target, value_index = None, None
            if w in ("-t", "--target-directory") and i + 1 < len(words):
                target, value_index = words[i + 1], i + 1
            elif w.startswith("--target-directory="):
                target = w.split("=", 1)[1]
            if target is not None:
                # each source lands under the target directory under its own name
                sources = [s for j, s in enumerate(words[1:], 1) if j != value_index and not s.startswith("-")]
                return [os.path.join(target, os.path.basename(s.rstrip("/"))) for s in sources] or [target]
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


def _resolve(name: str, folder: str) -> str:
    """A written file's absolute path, or "" for a target that is not a file (/dev/*, &2)."""
    if not name or name.startswith(("/dev/", "&")):
        return ""
    return absolute(expand_home(name), folder)


def shell_targets(command: str, cwd: str) -> List[str]:
    """Every file a shell command writes, as absolute paths in first-seen
    order, each resolved in the folder its own part of the command runs in."""
    masked = mask(command)
    where = folders(command, cwd, masked)
    found = [_resolve(name, where(offset)) for name, offset in redirect_targets(command, masked)]
    for words, offset in _segments(command, masked):
        found += [_resolve(name, where(offset)) for name in segment_targets(words, where(offset))]
    return [p for p in dict.fromkeys(found) if p]


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
    targets = shell_targets(command, cwd)
    if not targets:
        return []
    bodies = [command[start:end] for _, start, end in heredocs(command)]
    if not bodies:
        where = folders(command, cwd)
        for words, offset in _segments(command):
            if os.path.basename(words[0]) in ("cp", "cat", "install", "ditto"):
                operands = [w for w in words[1:] if not w.startswith("-")]
                sources = operands[:-1] or operands
                bodies += [t for t in (_read_small(absolute(o, where(offset))) for o in sources) if t]
    if not bodies:
        quoted = [a or b for a, b in QUOTED_RX.findall(command)]
        if quoted:
            bodies = ["\n".join(quoted).replace("\\n", "\n")]
    content = "\n".join(bodies)
    return [("Write", {"file_path": p, "content": content}, "shell-write") for p in targets]


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
    """A list of the dicts and strings in `value`; anything else is dropped, never raised on."""
    items = value if isinstance(value, list) else [value]
    return [v for v in items if isinstance(v, (dict, str))]


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
    for q in _list(tool_input.get("questions")):
        if not isinstance(q, dict):
            continue
        options = []
        for o in _list(q.get("options")):
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
    if tool in ("Write", "Edit", "NotebookEdit", "Read"):
        return [(tool, tool_input, "")]
    if tool in ("Bash", "exec_command", "shell", "local_shell", "apply_patch", "write_stdin"):
        # write_stdin types into a running shell: its text is a command like any other
        command = _text(tool_input.get("chars")) if tool == "write_stdin" else _command(tool_input)
        if not command:
            if tool == "write_stdin":
                return []   # nothing typed
            # a patch under a key not read here passes through as itself, so a
            # hook matching every tool still judges it
            return [(tool if tool == "apply_patch" else "Bash", tool_input, "")]
        if command.lstrip().startswith("*** Begin Patch"):
            return patch_calls(command, cwd)
        out = [("Bash", {"command": command}, "")] + shell_write_calls(command, cwd)
        for body, folder in patches_in_shell(command, cwd):
            out += patch_calls(body, folder)
        return out
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
            ref = o if isinstance(o, str) else str(o.get("ref_id") or "")
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


def matcher_error(matcher: Optional[str]) -> str:
    """Why Python's `re` cannot evaluate this matcher, or "" when it can.
    Some patterns JavaScript accepts (named groups as `(?<name>...)`, `\\p{...}`)
    fail here, and their hooks would otherwise never run without a word."""
    if matcher in MATCH_ALL or EXACT_MATCHER_RX.match(matcher):
        return ""
    try:
        re.compile(matcher)
    except re.error as exc:
        return str(exc)
    return ""


def matcher_fits(matcher: Optional[str], value: str) -> bool:
    """Whether a Claude Code matcher selects `value` (a tool name, a session
    source, an agent type, ...).

    Empty, "*" or absent matches everything. A matcher of only letters,
    digits, `_`, `-`, spaces, commas and bars is an exact name or a list of
    exact names split on `|` or `,`. Anything else is a regular expression
    searched anywhere in the value, so `^` and `$` do the anchoring. It is
    evaluated with Python's `re` in place of JavaScript's RegExp, and the
    two differ on some patterns.
    """
    if matcher in MATCH_ALL:
        return True
    if EXACT_MATCHER_RX.match(matcher):
        return value in [part.strip() for part in re.split(r"[|,]", matcher)]
    try:
        return re.search(matcher, value) is not None
    except re.error:
        return False

