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
import functools
import os
import re
import shlex
import urllib.parse
from typing import Callable, Iterator, List, NamedTuple, Optional, Tuple

# Every Claude Code tool name a translation can produce. Parity uses this set
# to decide whether a hook's matcher can ever be reached from Codex.
CLAUDE_TOOLS = (
    "Bash", "Write", "Edit", "Read", "WebSearch", "WebFetch",
    "AskUserQuestion", "Agent", "SendMessage",
)

Call = Tuple[str, dict, str]   # (Claude tool name, tool_input, derivation tag or "")

# The longest shell command read, in characters. Reading takes time in
# proportion to the length, a few seconds per million characters, and the
# hooks' time budget runs from the moment the bridge starts; a command too
# long to read in time is refused, never let through unread.
COMMAND_MAX = 1_000_000
FOLDER_MAX = 4096   # a cd into a longer path is not followed, and the command is refused
NEST_MAX = 16       # bash -c and eval inside one another; deeper is refused
FOLDERS_MAX = 16    # folders the shell may be in after cds that may fail; more is refused


class Untranslatable(Exception):
    """A call the bridge cannot read in time; the hook refuses it rather than let it run unchecked."""


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
# body is not one: a parenthesis or backquote; a separator between commands
# (&&, ||, a background &, a pipe |, ;, a newline); or a cd at the start of a
# simple command, past its -L/-P options and `--`. A cd with no folder goes
# home; one whose folder starts with a backquote runs a command for it. An
# mkdir is read too, since a folder it makes exists for a cd after it.
FOLDER_RX = re.compile(
    r"[()`]|(?P<sep>&&|\|\||(?<![>&|])&(?![>&])|(?<![>|])\||;|\n)"
    r"|(?:^|(?<=[;&|\n(`]))[ \t]*(?:(?:!|\{|do|then|else|elif|if|while|until)[ \t]+)*"
    r"cd(?P<options>(?:[ \t]+-[LPe@]+)*)(?:[ \t]+--)?"
    r"(?:[ \t]+(?P<target>'[^']*'|\"[^\"]*\"|`|[^\s;&|()`]+)|(?=[ \t]*(?:$|[;&|)\n])))"
    r"|(?:^|(?<=[;&|\n(`]))[ \t]*(?:(?:!|\{|do|then|else|elif|if|while|until)[ \t]+)*"
    r"mkdir(?P<mkdir>[ \t][^;&|\n()`]*)", re.M)


def folders(command: str, cwd: str, masked: str = "") -> Callable[[int], Tuple[str, ...]]:
    """A function giving the folders the shell may be in at each offset of
    `command`, the likeliest first.

    Every `cd` moves it, except where the shell runs the cd in a subshell of
    its own: inside `( ... )`, `$( ... )` or backquotes until they close, in
    any command of a pipeline, and in a list sent to the background with `&`.
    A cd into a folder that does not exist yet, followed by `;`, a newline
    or `||`, may fail and leave the shell where it was while the next command
    still runs, so both folders count from there on. A cd whose folder is
    only known when it runs (`cd "$PWD"`, `cd -`) keeps every folder seen.
    """
    masked = masked or mask(command)
    here: Tuple[str, ...] = (cwd,)
    # one level per subshell: where the shell is now, where the current list
    # (commands joined by && and ||) and the current pipeline command began,
    # whether a pipe has been seen in this pipeline, and what opened the level
    levels = [{"now": here, "list": here, "element": here, "piped": False, "opened": ""}]
    offsets, values = [0], [here]
    seen = {cwd: None}   # every folder this command may have been in, for a cd whose folder is not known
    made = set()         # folders an mkdir earlier in the command makes
    for m in FOLDER_RX.finditer(masked):
        token, level = m.group(), levels[-1]
        if m.group("mkdir") is not None:
            for name in mkdir_operands(_words(command[m.start("mkdir"):m.end("mkdir")])):
                for path in (cd_folder(expand_home(name), f, False) for f in level["now"]):
                    while path not in made and path != os.path.dirname(path):
                        made.add(path)   # its parents exist too once it does (mkdir -p makes them)
                        path = os.path.dirname(path)
            continue
        if token == "(" or (token == "`" and level["opened"] != "`"):
            levels.append({"now": level["now"], "list": level["now"], "element": level["now"],
                           "piped": False, "opened": token})
        elif token in (")", "`"):
            if level["opened"] == ("(" if token == ")" else "`") and len(levels) > 1:
                levels.pop()   # an unmatched `)` (a case pattern) closes nothing
        elif m.group("sep"):
            if level["piped"] and token != "|":
                level["now"], level["piped"] = level["element"], False   # a pipeline's last command ran apart too
            if token == "|":
                level["now"], level["piped"] = level["element"], True    # each pipeline command runs in a subshell
            elif token == "&":
                level["now"] = level["list"]                              # the whole list ran in the background
            level["element"] = level["now"]
            if token not in ("&&", "||", "|"):
                level["list"] = level["now"]
        elif m.group("target") is None:
            level["now"] = (os.path.expanduser("~"),)
        elif unknown_folder(expand_home(command[m.start("target"):m.end("target")])):   # quotes are blank in masked
            # `cd "$PWD"`, `cd -`, `cd "$(git rev-parse --show-toplevel)"`: the folder is only
            # known when the command runs, so every folder it may have been in still counts
            level["now"] = tuple(dict.fromkeys(level["now"] + tuple(seen)))
        else:
            target = expand_home(command[m.start("target"):m.end("target")])
            options = m.group("options").replace("e", "").replace("@", "")
            physical = options.rfind("P") > options.rfind("L")   # the last of -L and -P wins, -L by default
            moved = tuple(dict.fromkeys(cd_folder(target, f, physical) for f in level["now"]))
            if any(len(f) > FOLDER_MAX for f in moved):
                # each cd deeper costs more to follow; past this the path is not a real folder
                raise Untranslatable("a cd in this command leads to a folder path over %d characters long, "
                                     "which the bridge does not follow" % FOLDER_MAX)
            after = masked[m.end():m.end() + 64].lstrip(" \t")[:2]
            # `&&` holds the next command back when the cd fails; `;`, a newline and `||` let it run
            next_runs_anyway = after[:1] in (";", "\n") or after == "||"
            may_fail = next_runs_anyway and not all(f in made or os.path.isdir(f) for f in moved)
            level["now"] = tuple(dict.fromkeys(moved + level["now"])) if may_fail else moved
        if len(levels[-1]["now"]) > FOLDERS_MAX:
            raise Untranslatable("this command's cds into folders that may not exist leave more than %d "
                                 "folders it could be in, which the bridge does not follow" % FOLDERS_MAX)
        seen.update(dict.fromkeys(levels[-1]["now"]))
        offsets.append(m.end())
        values.append(levels[-1]["now"])
    return lambda offset: values[bisect.bisect_right(offsets, offset) - 1]


def cd_folder(target: str, folder: str, physical: bool) -> str:
    """Where `cd target` from `folder` goes. By default bash's cd is logical:
    `l2/..` climbs back out of the link by name, so the path is tidied as
    text. With -P it follows the links on disk first."""
    joined = os.path.join(folder, os.path.expanduser(target.strip().strip("'\"")))
    return os.path.realpath(joined) if physical else os.path.normpath(joined)


def mkdir_operands(words: List[str]) -> List[str]:
    """The folders an mkdir's arguments name, past its options and -m's mode."""
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w == "-m":
            skip = True
        elif not w.startswith("-"):
            out.append(w)
    return out


def unknown_folder(target: str) -> bool:
    """Whether a cd's folder is only known when the command runs: `-` (the
    previous folder), or a folder holding a variable or a command's output."""
    return target == "-" or "$" in target or "`" in target


def patches_in_shell(command: str, cwd: str, depth: int = 0) -> List[Tuple[str, str]]:
    """(apply_patch body, folder its paths resolve against) for every patch
    inside a shell command (`apply_patch <<'EOF' ...`), in order, once for
    each folder the shell may be in (see folders()).

    A body ends at the first line whose trimmed text is the end marker, as
    in Codex; the marker appearing inside a line of content does not end it.
    A patch resolves in the folder its command runs in: after any `cd` before
    it (Codex's own `cd <dir> && apply_patch` form included), and for a
    heredoc, at the `<<` that opens it rather than where its body sits.
    A patch inside a `bash -c` script or an `eval` is also read within that
    script, from the folder the script starts in, so a cd before it there
    counts as well; the outer reading stays, so a patch is never dropped.
    """
    if depth > NEST_MAX:
        raise Untranslatable("this command nests bash -c or eval more than %d deep, "
                             "which the bridge does not read" % NEST_MAX)
    found: List[Tuple[str, str]] = []
    where = folders(command, cwd)
    bodies = heredocs(command)
    body_starts = [body_start for _, body_start, _ in bodies]
    done = 0
    while True:
        start = command.find("*** Begin Patch", done)
        if start < 0:
            break
        k = bisect.bisect_right(body_starts, start) - 1
        opened_at = bodies[k][0] if k >= 0 and start <= bodies[k][2] else start
        marker = END_PATCH_RX.search(command, start)
        end = marker.end() if marker else len(command)
        found += [(command[start:end], folder) for folder in where(opened_at)]
        done = end
    if found:   # a script can hold a patch only where the command does
        for words, offset in _segments(command):
            script = inner_script(unwrap(words))
            if script is not None and "*** Begin Patch" in script:
                for folder in where(offset):
                    found += patches_in_shell(script, folder, depth + 1)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# Files a shell command writes
#
# On Codex the shell is also a file-writing tool, so each file a command
# writes is reported as a Write as well. The command is scanned with quoted
# spans and heredoc bodies blanked out first, so a `>` inside a commit message
# or a script string is never read as a redirect; target names are then read
# back from the original text, quotes included.

HEREDOC_MARK_RX = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1")
DELIMITER_RX = re.compile(r"\w+")
SHELL_TOKEN_RX = re.compile(r"[\\'\"`()#\n]|\$\(|<<<?")   # what changes the lexer's state outside quotes
DOUBLE_TOKEN_RX = re.compile(r"[\\\"`]|\$\(")             # ... and inside double quotes
END_PATCH_RX = re.compile(r"^[^\S\n]*\*\*\* End Patch[^\S\n]*$", re.M)
REDIRECT_OP_RX = re.compile(r"(?:\d|&)?>>?\|?")
# The word after a redirect, quotes included, up to where the shell would end it.
REDIRECT_WORD_RX = re.compile(r"""[ \t]*((?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s'"\\;&|<>()])*)""")
SEGMENT_RX = re.compile(r"[;&|\n()`]+")   # a subshell or $( ... ) is a command of its own
SOURCE_MAX = 200_000   # bytes read from a copy's source file to show what it writes
INPLACE_PROGS = ("sed", "gsed", "perl", "ruby")
# program: (its short options that take a value, the options naming a file or folder it writes)
OUTPUT_OPTIONS = {
    "curl": ("AbcCdDeEFHKmoPQrtTuUwxXyYz", ("-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar")),
    "wget": ("aABDeiIlnoOPQRtTUwX", ("-O", "--output-document", "-o", "--output-file", "-a", "--append-output")),
    "tar": ("bCfFgHKLNTVX", ("-C", "--directory")),
    "unzip": ("dP", ("-d",)),
}
WRAPPERS = ("sudo", "env", "command", "nohup", "time", "timeout", "nice", "exec", "xargs", "doas", "stdbuf")
KEYWORDS = ("!", "{", "do", "then", "else", "elif", "if", "while", "until")   # come before a command
SHELLS = ("bash", "sh", "zsh", "dash", "ksh")


class Scan(NamedTuple):
    masked: str                              # the command, quoted text, comments and heredoc bodies blanked
    heredocs: List[Tuple[int, int, int]]     # (offset of its <<, body start, body end)
    quoted: List[Tuple[int, int]]            # (start, end) of the text inside each pair of quotes


@functools.lru_cache(maxsize=4)
def scan(command: str) -> Scan:
    """Read `command` once, left to right, the way the shell splits it.

    Quoted text, comments and heredoc bodies are blanked in `masked`, every
    offset kept, so a `>` in a commit message or a script string is never a
    redirect. A `$( ... )` or backquoted command inside double quotes stays
    visible, because the shell runs it. A heredoc whose end line never comes,
    and a quote that never closes, are left visible rather than hiding the
    rest of the command. One pass, so the time grows with the length alone.
    """
    starts = [0] + [m.end() for m in re.finditer("\n", command)]
    end_lines: dict = {}   # heredoc delimiter -> numbers of the lines that hold it alone
    for n, line in enumerate(command.split("\n")):
        if DELIMITER_RX.fullmatch(line.strip()):
            end_lines.setdefault(line.strip(), []).append(n)
    blank: List[Tuple[int, int, bool]] = []   # (start, end, keep newlines)
    bodies: List[Tuple[int, int, int]] = []
    quoted: List[Tuple[int, int]] = []
    stack: List[list] = []     # open contexts [kind, offset, where blanking resumes]: ' " ` $( (
    pending: List[Tuple[str, int]] = []       # heredocs whose bodies start after this line
    i, size = 0, len(command)
    while i < size:
        top = stack[-1] if stack else ["", 0, 0]
        if top[0] == "'":
            close = command.find("'", i)
            if close < 0:
                break
            stack.pop()
            blank.append((i, close, False))
            quoted.append((i, close))
            i = close + 1
            continue
        if top[0] == '"':
            m = DOUBLE_TOKEN_RX.search(command, i)
            if not m:
                break
            if m.group() == "\\":
                i = m.end() + 1
                continue
            blank.append((top[2], m.start(), False))
            if m.group() == '"':
                stack.pop()
                quoted.append((top[1] + 1, m.start()))
            else:   # $( or ` inside double quotes: the shell runs it, so it stays visible
                stack.append([m.group(), m.start(), 0])
            i = m.end()
            continue
        m = SHELL_TOKEN_RX.search(command, i)
        if not m:
            break
        token, i = m.group(), m.end()
        if token == "\\":
            i += 1
        elif token in ("'", '"'):
            stack.append([token, m.start(), i])
        elif token == "#":
            before = command[m.start() - 1] if m.start() else " "
            if before.isspace() or before in ";&|()":   # a comment only where a word could start
                line_end = command.find("\n", i)
                i = size if line_end < 0 else line_end
                blank.append((m.start(), i, False))
        elif token in ("$(", "("):
            stack.append([token, m.start(), 0])
        elif token in (")", "`"):
            closes = ("$(", "(") if token == ")" else ("`",)
            if top[0] in closes:
                stack.pop()
                if stack and stack[-1][0] == '"':
                    stack[-1][2] = i   # back inside double quotes: blanking resumes here
            elif token == "`":
                stack.append([token, m.start(), 0])
            # an unmatched `)` (a case pattern) closes nothing
        elif token == "<<":
            mark = HEREDOC_MARK_RX.match(command, m.start())
            if mark:
                pending.append((mark.group(2), m.start()))
                i = mark.end()
        elif token == "\n" and pending:
            line = bisect.bisect_right(starts, m.start()) - 1
            for word, marker in pending:
                ends = end_lines.get(word, [])
                k = bisect.bisect_right(ends, line)
                if k == len(ends):
                    continue   # never ended: left visible
                body_start = starts[line + 1]
                body_end = max(body_start, starts[ends[k]] - 1)
                bodies.append((marker, body_start, body_end))
                blank.append((body_start, body_end, True))
                line = ends[k]
            pending = []
            i = starts[line + 1] if line + 1 < len(starts) else size
    open_quotes = [offset for kind, offset, _ in stack if kind in ("'", '"')]
    if open_quotes:
        blank = [b for b in blank if b[0] < min(open_quotes)]   # nothing after an unclosed quote is hidden
    out, at = [], 0
    for start, end, keep_newlines in sorted(blank):
        start = max(start, at)
        if start >= end:
            continue
        part = command[start:end]
        out += [command[at:start], re.sub(r"[^\n]", " ", part) if keep_newlines else " " * len(part)]
        at = end
    out.append(command[at:])
    return Scan("".join(out), bodies, quoted)


def heredocs(command: str) -> List[Tuple[int, int, int]]:
    """(offset of its `<<`, body start, body end) for each heredoc in `command`."""
    return scan(command).heredocs


def mask(command: str) -> str:
    """`command` with quoted text, comments and heredoc bodies replaced by spaces of the same length."""
    return scan(command).masked


# A shell word: quoted spans, escaped characters and plain characters run
# together; a quote with no partner is kept as a plain character.
SHELL_WORD_RX = re.compile(r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.?|[^\s\\])+""", re.S)
QUOTING_RX = re.compile(r"""'([^']*)'|"((?:[^"\\]|\\.)*)"|\\(.)""", re.S)
DOUBLE_ESCAPE_RX = re.compile(r"""\\([\\"$`\n])""")


def _unquote_word(m: "re.Match[str]") -> str:
    single, double, escaped = m.groups()
    if single is not None:
        return single
    if double is not None:
        return DOUBLE_ESCAPE_RX.sub(lambda e: "" if e.group(1) == "\n" else e.group(1), double)
    return "" if escaped == "\n" else escaped


def _words(text: str) -> List[str]:
    """`text` split into words with their quotes removed, as the shell reads
    them. A regular expression rather than shlex, whose time grows with the
    square of a word's length."""
    return [QUOTING_RX.sub(_unquote_word, m.group()) for m in SHELL_WORD_RX.finditer(text)]


def _segments(command: str, masked: str = "") -> Iterator[Tuple[List[str], int]]:
    """(words, offset) of each simple command in the line, split where the
    masked text has ; & | or a newline."""
    masked = masked or mask(command)
    cuts = [0] + [m.end() for m in SEGMENT_RX.finditer(masked)] + [len(command)]
    for i in range(len(cuts) - 1):
        words = _words(command[cuts[i]:cuts[i + 1]].rstrip(";&|\n()`"))
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
        word = REDIRECT_WORD_RX.match(command, m.end()).group(1)
        if not word:
            continue   # >&2, a descriptor duplication
        found.append(((_words(word) or [""])[0], m.start()))
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
    "stdbuf": ("-i", "-o", "-e"),
}


def unwrap(words: List[str]) -> List[str]:
    """`words` with any leading keywords (do, then, !, ...), wrappers (sudo,
    env, timeout, ...) and the wrappers' own options and values removed, so
    the first word is the program run."""
    while words and (words[0] in KEYWORDS or os.path.basename(words[0]) in WRAPPERS):
        if words[0] in KEYWORDS:
            words = words[1:]
            continue
        wrapper = os.path.basename(words[0])
        rest = words[1:]
        while rest and (rest[0].startswith("-") or (wrapper == "env" and "=" in rest[0])):
            takes_value = rest[0] in WRAPPER_VALUE_OPTIONS.get(wrapper, ())
            rest = rest[2:] if takes_value else rest[1:]
        if wrapper == "timeout" and rest:
            rest = rest[1:]   # the duration
        words = rest
    return words


def read_options(words: List[str], takes_value: str, long_values: Tuple[str, ...] = ()) -> List[Tuple[str, Optional[str]]]:
    """(option, value or None for a flag) for every option before `--`, however
    it is spelled: `-o F`, `-oF`, `-sSo F` (a cluster of short options, the
    last taking a value), `--output F` and `--output=F`. `takes_value` names
    the program's short options that take a value, so that in `-dfoo` the `o`
    is part of -d's value; `long_values` the long ones whose value is the next word."""
    out: List[Tuple[str, Optional[str]]] = []
    i = 1
    while i < len(words) and words[i] != "--":
        w = words[i]
        if w.startswith("--"):
            name, eq, value = w.partition("=")
            if not eq and name in long_values and i + 1 < len(words):
                i += 1
                value, eq = words[i], "="
            out.append((name, value if eq else None))
        elif w.startswith("-") and len(w) > 1:
            for k, letter in enumerate(w[1:], 2):
                if letter not in takes_value:
                    out.append(("-" + letter, None))
                    continue
                value = w[k:]
                if not value and i + 1 < len(words):
                    i += 1
                    value = words[i]
                out.append(("-" + letter, value))
                break
        i += 1
    return out


def restored_paths(words: List[str]) -> List[str]:
    """The paths `git restore` puts back: its operands, past its options and -s's value."""
    paths, i = [], 2
    while i < len(words):
        w = words[i]
        if w == "--":
            return paths + words[i + 1:]
        if w in ("-s", "--source"):
            i += 1
        elif not w.startswith("-"):
            paths.append(w)
        i += 1
    return paths


def inner_script(words: List[str]) -> Optional[str]:
    """The script a simple command hands to a shell of its own, `bash -c
    SCRIPT` (or -lc, -ec, past any --) or `eval ...`, or None. `words` is
    already unwrapped."""
    prog = os.path.basename(words[0]) if words else ""
    if prog == "eval":
        return " ".join(words[1:])
    if prog in SHELLS:
        flag = next((i for i, w in enumerate(words[1:], 1)
                     if w.startswith("-") and not w.startswith("--") and "c" in w[1:]), None)
        script = words[flag + 1:] if flag is not None else []
        script = script[1:] if script[:1] == ["--"] else script
        if script:
            return script[0]
    return None


def segment_targets(words: List[str], folder: str, depth: int = 0) -> List[Tuple[str, str]]:
    """(Write or Edit, file) for each file one simple command run in `folder`
    writes, by what its program is known to write, the file as written in
    the command. `depth` counts the bash -c and eval levels around it."""
    words = unwrap(words)
    if not words:
        return []
    prog = os.path.basename(words[0])
    script = inner_script(words)
    if script is not None:
        return shell_targets(script, folder, depth + 1)   # absolute already, so they resolve to themselves
    if prog == "git":
        words, git_folder = git_command(words)
        if len(words) > 2 and words[1] == "restore":
            # both, so a guard on either tool sees a restore; it rewrites the file like a Write
            return [(tool, os.path.join(git_folder, p)) for p in restored_paths(words) for tool in ("Write", "Edit")]
        if len(words) > 2 and words[1] == "checkout" and "--" in words:
            return [("Write", os.path.join(git_folder, p)) for p in words[words.index("--") + 1:]]
        return []
    return [("Write", p) for p in _written(prog, words)]


def git_command(words: List[str]) -> Tuple[List[str], str]:
    """`git ...` with git's own options before the subcommand removed, and the
    folder its `-C` options move it to ("" when none), against which the
    subcommand's paths resolve."""
    rest, folder = words[1:], ""
    while rest and rest[0].startswith("-"):
        if rest[0] in ("-C", "-c") and len(rest) > 1:
            if rest[0] == "-C":
                folder = os.path.join(folder, rest[1])   # each -C is relative to the one before
            rest = rest[2:]
        else:
            rest = rest[1:]
    return words[:1] + rest, folder


def _written(prog: str, words: List[str]) -> List[str]:
    """The files a program that is not a shell writes, read from its arguments."""
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
        takes_value, outputs = OUTPUT_OPTIONS[prog]
        options = read_options(words, takes_value, tuple(o for o in outputs if o.startswith("--")))
        values = [value for name, value in options if name in outputs and value is not None]
        if prog == "curl" and any(name in ("-O", "--remote-name") for name, _ in options):
            # each URL is saved in the folder under the last segment of its path
            values += [os.path.basename(urllib.parse.urlsplit(w).path) for w in words[1:]
                       if "://" in w and not w.startswith("-")]
        if prog in ("tar", "unzip"):
            # a folder the archive's files land in: with and without the slash,
            # so a guard written either way (`secret`, `secret/`) matches
            return [v for folder in values for v in (folder.rstrip("/") or "/", folder.rstrip("/") + "/")]
        return values
    return []


def _resolve(name: str, folder: str) -> str:
    """A written file's absolute path, or "" for a target that is not a file (/dev/*, &2, - for stdout)."""
    if not name or name == "-" or name.startswith(("/dev/", "&")):
        return ""
    path = absolute(expand_home(name), folder)
    return path + "/" if name.endswith("/") and not path.endswith("/") else path   # a folder stays one


def shell_targets(command: str, cwd: str, depth: int = 0) -> List[Tuple[str, str]]:
    """(Write or Edit, absolute path) for every file a shell command writes,
    in first-seen order, each resolved in the folder its own part of the
    command runs in."""
    if depth > NEST_MAX:
        raise Untranslatable("this command nests bash -c or eval more than %d deep, "
                             "which the bridge does not read" % NEST_MAX)
    masked = mask(command)
    where = folders(command, cwd, masked)
    found = [("Write", _resolve(name, folder))
             for name, offset in redirect_targets(command, masked) for folder in where(offset)]
    for words, offset in _segments(command, masked):
        found += [(tool, _resolve(name, folder))
                  for folder in where(offset) for tool, name in segment_targets(words, folder, depth)]
    return [t for t in dict.fromkeys(found) if t[1]]


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
                bodies += [t for t in (_read_small(absolute(o, where(offset)[0])) for o in sources) if t]
    if not bodies:
        quoted = [command[start:end] for start, end in scan(command).quoted]
        if quoted:
            bodies = ["\n".join(quoted).replace("\\n", "\n")]
    content = "\n".join(bodies)
    # a restore's new content is in git, not in the command, so its Edit carries none
    return [("Write", {"file_path": p, "content": content}, "shell-write") if tool == "Write" else
            ("Edit", {"file_path": p, "old_string": "", "new_string": ""}, "shell-edit")
            for tool, p in targets]


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
    """The command text of a shell call; local_shell may nest it in `action`.
    An argv list is joined with shell quoting, so `["bash", "-lc", "a && b"]`
    reads as one script, not as two commands."""
    action = tool_input.get("action")
    for source in (tool_input, action if isinstance(action, dict) else {}):
        for key in ("command", "cmd", "script"):
            value = source.get(key)
            if isinstance(value, list):
                value = shlex.join(str(v) for v in value)
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
        if len(command) > COMMAND_MAX:
            raise Untranslatable("this command is %d characters long; the bridge reads up to %d in its time "
                                 "budget, so it was refused unread. Split it into shorter commands."
                                 % (len(command), COMMAND_MAX))
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
    action = tool_input.get("action")
    if isinstance(action, dict) and action.get("working_directory"):
        # local_shell runs its command there, not necessarily in the session folder
        cwd = absolute(str(action["working_directory"]), cwd)
    base = {k: v for k, v in payload.items() if k not in ("tool_name", "tool_input")}
    out = []
    for name, translated_input, tag in calls(tool, tool_input, cwd):
        p = dict(base, tool_name=name, tool_input=translated_input, codex_tool_name=tool)
        if tag:
            p["codex_derived"] = tag
        out.append(p)
        real = real_path(str(translated_input.get("file_path") or ""))
        if real:
            out.append(dict(p, tool_input=dict(translated_input, file_path=real), codex_derived="real-path"))
    return out


def real_path(path: str) -> str:
    """Where `path` really lands when a symlinked folder is on its way there,
    or "" when that is the path itself. Both are sent: the real one so a guard
    on the link's target sees the write, the written one so a guard on the
    path as written (`/etc/` where /etc is a symlink, as on macOS) still does."""
    if not os.path.isabs(path):
        return ""   # a placeholder such as <unresolved path in script>
    head, tail = os.path.split(path)
    real = os.path.realpath(head)
    return os.path.join(real, tail) if real != head else ""


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

