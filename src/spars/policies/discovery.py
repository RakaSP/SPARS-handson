"""Find policies by class name, so a new policy needs no catalog entry.

A scheduler or power manager is any top-level class in a file under
policies/<kind>/ whose ``__init__`` takes ``config``, or that has no
``__init__`` and subclasses another class (names starting with ``_`` or
``Base`` are skipped). The files are parsed with ``ast``; nothing is
imported. A name resolves to a catalog alias first, then to a class name
(case-insensitive, catalog classes included); a class name used twice is ambiguous and must be given as
its full ``module:Class`` path.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .catalog import POWER_MANAGERS, SCHEDULERS

CATALOGS = {"scheduler": SCHEDULERS, "power_management": POWER_MANAGERS}
_ROOT = Path(__file__).resolve().parent
_PARSED: dict[tuple[Path, int], list[str]] = {}  # (file, mtime) -> class names


def _takes_config(node: ast.ClassDef) -> bool:
    """A policy writes ``__init__(..., config)``, or subclasses one and inherits its constructor."""
    init = next(
        (item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == "__init__"),
        None,
    )
    if init is None:
        return bool(node.bases)
    return any(a.arg == "config" for a in init.args.args + init.args.kwonlyargs)


def _classes(path: Path) -> list[str]:
    key = (path, path.stat().st_mtime_ns)
    if key not in _PARSED:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return []
        _PARSED[key] = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and not node.name.startswith(("_", "Base"))
            and _takes_config(node)
        ]
    return _PARSED[key]


def discover(kind: str, unlisted_only: bool = False) -> dict[str, list[str]]:
    """Lower-case class name -> ``module:Class`` paths of the classes under policies/<kind>/."""
    listed = set(CATALOGS[kind].values()) if unlisted_only else set()
    found: dict[str, list[str]] = {}
    if not unlisted_only:  # a catalog class may not show ``config`` in its own __init__
        for path in dict.fromkeys(CATALOGS[kind].values()):
            found.setdefault(path.split(":")[1].lower(), []).append(path)
    for path in sorted((_ROOT / kind).rglob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        module = ".".join(("spars", "policies", *path.relative_to(_ROOT).with_suffix("").parts))
        for name in _classes(path):
            full = f"{module}:{name}"
            if full not in listed and full not in found.get(name.lower(), []):
                found.setdefault(name.lower(), []).append(full)
    return found


def resolve(kind: str, name: str) -> str:
    """The ``module:Class`` path behind a catalog alias, a class name or a path."""
    if ":" in name:
        return name
    catalog = CATALOGS[kind]
    if name.lower() in catalog:
        return catalog[name.lower()]
    paths = discover(kind).get(name.lower(), [])
    if len(paths) == 1:
        return paths[0]
    label = "power manager" if kind == "power_management" else kind
    if paths:
        raise ValueError(
            f"{label} name {name!r} is ambiguous; use one of: {', '.join(paths)}"
        )
    known = sorted([*catalog, *(p.split(":")[1] for ps in discover(kind).values() for p in ps)])
    raise ValueError(f"Unknown {label}: {name!r}. Available {label}s: {known}")


def options(kind: str) -> list[tuple[str, str, str]]:
    """``(value, title, path)`` for every choosable policy.

    ``value`` is what goes into the definition: the alias or class name, or the
    full path when a class name is ambiguous.
    """
    rows = [(alias, alias, path) for alias, path in CATALOGS[kind].items()]
    for paths in discover(kind, unlisted_only=True).values():
        for path in paths:
            title = path.split(":")[1]
            rows.append((title if len(paths) == 1 else path, title, path))
    return rows
