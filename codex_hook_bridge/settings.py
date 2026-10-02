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


def load_routes(explicit: Optional[List[str]] = None, project_dir: str = ".") -> List[Route]:
    """Every hook route across the settings files, in file order then file position.

    Explicit files must exist. Default files are read when present and skipped
    when absent, as Claude Code does. `disableAllHooks` takes its value from
    the highest-precedence file that sets it; true switches every route off.
    """
    if explicit:
        files = [(path, True) for path in explicit]
    else:
        files = [(path, False) for path in default_files(project_dir)]
    routes: List[Route] = []
    disabled = False
    for path, required in files:
        if not required and not os.path.exists(path):
            continue
        data = _load(path)
        if isinstance(data.get("disableAllHooks"), bool):
            disabled = data["disableAllHooks"]   # later files take precedence, as in Claude Code
        for event, groups in data.get("hooks", {}).items():
            if not isinstance(groups, list):
                raise SettingsError("%s: hooks.%s is not a list" % (path, event))
            for group in groups:
                if not isinstance(group, dict):
                    continue
                matcher = group.get("matcher") or ""
                for handler in group.get("hooks") or []:
                    if isinstance(handler, dict):
                        routes.append(Route(event, str(matcher), handler, path))
    return [] if disabled else routes


def handler_key(handler: dict) -> str:
    """Identity of a handler for de-duplication: the same definition in two
    settings files runs once."""
    return json.dumps(handler, sort_keys=True)
