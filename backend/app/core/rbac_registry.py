"""Discovery of every module's RBAC declarations.

Permissions are declared per module (CLAUDE.md §10: each module owns an
optional `rbac.py`), which is the right boundary — `billing` should not have
to know what `ipd` can do. But several things need the *union* of those
declarations:

* `tests/test_rbac_seed.py`, which checks the code's grants against the rows a
  migration actually seeded;
* `scripts/export_rbac.py`, which generates the frontend's permission union so
  a typo in a permission string is a TypeScript error rather than a control
  that never appears.

Both used to hold their own list of modules, and a hand-maintained list is the
same drift this whole file exists to prevent — `reporting` shipped with three
permissions and sat outside the guard because nobody remembered to add it.
Walking the package means a module is covered from the moment it has an
`rbac.py`, with nothing to remember.

Imports happen inside the function, not at module scope: every module's
`rbac.py` imports `Roles` from `identity`, so importing them from `app.core`
at import time would be a cycle.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Final

__all__ = ["RBAC_GRANT_MAP_NAMES", "iter_rbac_modules", "permission_codes", "role_grants"]

# The name each module uses for its role -> permissions map. `identity` calls
# its own `SYSTEM_ROLE_PERMISSIONS` because those grants are not tenant-scoped;
# the shape is identical.
RBAC_GRANT_MAP_NAMES: Final[tuple[str, ...]] = (
    "MODULE_ROLE_PERMISSIONS",
    "SYSTEM_ROLE_PERMISSIONS",
)


def iter_rbac_modules() -> list[ModuleType]:
    """Every `app.modules.*.rbac` that declares grants, in a stable order."""
    import importlib

    import app.modules

    modules_dir = Path(app.modules.__file__).parent
    discovered: list[ModuleType] = []

    for rbac_path in sorted(modules_dir.glob("*/rbac.py")):
        module = importlib.import_module(f"app.modules.{rbac_path.parent.name}.rbac")
        if any(hasattr(module, name) for name in RBAC_GRANT_MAP_NAMES):
            discovered.append(module)

    if not discovered:
        # Callers use this to verify things. Silently finding nothing would
        # make every check that depends on it pass vacuously.
        raise RuntimeError("No RBAC modules discovered — the package walk is broken.")

    return discovered


def role_grants() -> dict[str, set[str]]:
    """Union of every module's role -> permission-codes map."""
    grants: dict[str, set[str]] = {}
    for module in iter_rbac_modules():
        for name in RBAC_GRANT_MAP_NAMES:
            declared: dict[str, tuple[str, ...]] | None = getattr(module, name, None)
            if declared is None:
                continue
            for role, permissions in declared.items():
                grants.setdefault(role, set()).update(permissions)
            break
    return grants


def permission_codes() -> set[str]:
    """Every permission code any module defines."""
    codes: set[str] = set()
    for module in iter_rbac_modules():
        codes.update(perm.code for perm in getattr(module, "PERMISSION_CATALOGUE", ()))
    return codes
