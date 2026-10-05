"""Read the hook routes out of Claude Code settings files.

Claude Code merges hooks across its settings files rather than letting one
file replace another: the user file, the project's shared file and the
project's local file each add their routes. The same reading applies here,
with the same default locations. Claude Code also runs hooks from managed
policy settings, plugins, and skill or subagent frontmatter; those are not
read here.
"""
from __future__ import annotations

import json
import os
from typing import List, NamedTuple, Optional


class SettingsError(Exception):
    """A settings file that exists but cannot be read as Claude Code settings."""


class Route(NamedTuple):
    event: str         # PreToolUse, Stop, ...
    matcher: str       # "" when the matcher group has none
    handler: dict      # the hook handler object as written ({"type": "command", ...})
    source: str        # the settings file it came from


def default_files(project_dir: str) -> List[str]:
    """The settings files Claude Code reads hooks from, lowest precedence first.

    The user file lives in $CLAUDE_CONFIG_DIR when that is set, as it does for
    Claude Code, else in ~/.claude.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    project = os.path.join(project_dir, ".claude")
    return [os.path.join(config_dir, "settings.json"),
            os.path.join(project, "settings.json"),
            os.path.join(project, "settings.local.json")]


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise SettingsError("cannot read %s: %s" % (path, exc.strerror or exc))
    except ValueError as exc:
        raise SettingsError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(data, dict):
        raise SettingsError("%s is not a JSON object" % path)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError("%s: \"hooks\" is not an object" % path)
    return data


def _file_routes(path: str, data: dict) -> List[Route]:
    routes: List[Route] = []
    for event, groups in data.get("hooks", {}).items():
        if not isinstance(groups, list):
            raise SettingsError("%s: hooks.%s is not a list" % (path, event))
        # A malformed entry fails its whole file, never just itself: an entry
        # dropped without a word is a guard that silently allows.
        for n, group in enumerate(groups):
            where = "%s: hooks.%s[%d]" % (path, event, n)
            if not isinstance(group, dict):
                raise SettingsError("%s is not an object" % where)
            matcher = group.get("matcher") or ""
            if not isinstance(matcher, str):
                raise SettingsError("%s.matcher is not a string" % where)
            handlers = group.get("hooks") or []
            if not isinstance(handlers, list):
                raise SettingsError("%s.hooks is not a list" % where)
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise SettingsError("%s.hooks has an entry that is not an object" % where)
                routes.append(Route(event, matcher, handler, path))
    return routes


def load_routes(explicit: Optional[List[str]] = None, project_dir: str = ".",
                skipped: Optional[List[str]] = None) -> List[Route]:
    """Every hook route across the settings files, in file order then file position.

    Explicit files must exist. Default files are read when present and skipped
    when absent, as Claude Code does. A file that exists but cannot be read as
    settings raises, unless `skipped` is given: then, as in Claude Code, that
    file alone is left out and the reason appended to `skipped`, and every
    other file's hooks still load. `disableAllHooks` takes its value from the
    highest-precedence file that sets it; true switches every route off.
    """
    if explicit:
        files = [(path, True) for path in explicit]
    else:
        files = [(path, False) for path in default_files(project_dir)]
    routes: List[Route] = []
    disabled = False
    for path, required in files:
        if not os.path.exists(path):
            if required:
                raise SettingsError("cannot read %s: no such file" % path)
            continue
        try:
            data = _load(path)
            file_routes = _file_routes(path, data)
        except SettingsError as exc:
            if skipped is None:
                raise
            skipped.append(str(exc))
            continue
        if isinstance(data.get("disableAllHooks"), bool):
            disabled = data["disableAllHooks"]   # later files take precedence, as in Claude Code
        routes += file_routes
    return [] if disabled else routes


def handler_key(handler: dict) -> str:
    """Identity of a handler for de-duplication: the same definition in two
    settings files runs once."""
    return json.dumps(handler, sort_keys=True)
