"""Plugin discovery loader — "drop a .py file in, it appears".

Imports every module under `backend/tool_plugins/` so each `@tool` decorator
fires and registers its tool into the live `_TOOLS` registry. Called once from
the lifespan startup in `main.py`, right after the other loaders.

`reload()` re-scans the folder so a freshly dropped plugin can appear without a
full server restart (wired to the Tools-page "Reload" button). Each module is
imported in isolation: a single broken plugin is logged and skipped, never
crashing startup or hiding the other plugins (same failure-isolation style the
Phase 6e loaders use).
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from types import ModuleType

from . import tool_plugins

log = logging.getLogger("tools.loader")

_PKG = tool_plugins
_PREFIX = _PKG.__name__ + "."


def _iter_plugin_module_names() -> list[str]:
    names: list[str] = []
    for mod in pkgutil.iter_modules(_PKG.__path__):
        if mod.name.startswith("_"):
            continue
        names.append(_PREFIX + mod.name)
    return sorted(names)


def load(*, reload: bool = False) -> int:
    """Import (or re-import) every plugin module. Returns the count loaded.

    With reload=True, modules already imported are re-imported so edits to an
    existing plugin file take effect; `register()` is idempotent on tool id so
    re-running this never duplicates a tool.
    """
    loaded = 0
    for name in _iter_plugin_module_names():
        try:
            existing: ModuleType | None = sys.modules.get(name)
            if existing is not None and reload:
                importlib.reload(existing)
            elif existing is not None:
                pass  # already imported, nothing to do
            else:
                importlib.import_module(name)
            loaded += 1
        except Exception as e:  # noqa: BLE001 — one bad plugin must not kill the rest
            log.error("Tool plugin '%s' failed to load (skipping): %s", name, e)
    log.info("Tool plugins loaded: %d", loaded)
    return loaded


def reload() -> int:
    """Re-scan the plugin folder (picks up new + edited files)."""
    # importlib caches the package's submodule list lazily; invalidate so a
    # brand-new file dropped after startup is discovered.
    importlib.invalidate_caches()
    return load(reload=True)
