"""The seeded RBAC catalogue must match the code that enforces it.

Every module declares its permissions twice: once in `rbac.py`, where route
handlers import the codes, and once in a migration, which is what actually
lands in the database. Those two are the same claim written in two places, and
two places is one more than it takes for them to disagree.

They have disagreed before. Phase 6 found four role/permission pairs in
`clinical/rbac.py` naming roles as order fulfillers that did not hold
`order:fulfil` — so the route's permission gate refused before the role check
ran, and a doctor who ordered a procedure could not mark it done, stranding the
visit in `PENDING_CLEARANCE`. Nothing failed loudly; it simply did not work.

These tests compare the two sides directly, so the next disagreement is a red
test rather than a support call. They are worth more than they look: an
unenforced permission is a lie in an access review, and a permission enforced
but never granted is a role that silently cannot do its job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules
from app.core import database as db
from app.core.rbac_registry import (
    RBAC_GRANT_MAP_NAMES,
    iter_rbac_modules,
    permission_codes,
    role_grants,
)
from app.modules.identity.rbac import SYSTEM_ROLES, Roles

pytestmark = pytest.mark.integration


def _declared_in_code() -> dict[str, set[str]]:
    """Union every module's role -> permissions map into one map.

    The module list used to live here as a hand-maintained tuple, and it went
    stale exactly the way a hand-maintained anything does: `reporting` shipped
    with three permissions of its own and was never added, so the newest
    module in the system was the one module this guard did not check. A test
    that silently stops covering new code is worse than no test, because it
    still reports green.

    `iter_rbac_modules` walks the package instead, so a module is covered from
    the moment it has an `rbac.py`.
    """
    return role_grants()


def _declared_permission_codes() -> set[str]:
    return permission_codes()


async def _seeded_in_database(session: AsyncSession) -> dict[str, set[str]]:
    async with db.system_context(session):
        rows = (
            await session.execute(
                sa.text(
                    """
                    SELECT r.code AS role_code, p.code AS permission_code
                      FROM role_permissions rp
                      JOIN roles r ON r.id = rp.role_id
                      JOIN permissions p ON p.id = rp.permission_id
                     WHERE r.hospital_id IS NULL
                    """
                )
            )
        ).all()
    seeded: dict[str, set[str]] = {}
    for role_code, permission_code in rows:
        seeded.setdefault(role_code, set()).add(permission_code)
    return seeded


# ---------------------------------------------------------------------------
# Guarding the guard
# ---------------------------------------------------------------------------
def test_discovery_finds_every_module_that_declares_permissions() -> None:
    """The checks below are only worth their runtime if this finds everything.

    Coverage that quietly shrinks is the failure mode being defended against
    here: `reporting` declared three permissions and sat outside this file for
    a whole phase, because the module list was a tuple somebody had to
    remember to edit. Comparing the walk against the filesystem means the next
    module that declares permissions is either discovered or names itself in
    this assertion's message.
    """
    on_disk = {
        path.parent.name
        for path in (Path(app.modules.__file__).parent).glob("*/rbac.py")
        if any(name in path.read_text(encoding="utf-8") for name in RBAC_GRANT_MAP_NAMES)
    }
    discovered = {module.__name__.rsplit(".", 2)[-2] for module in iter_rbac_modules()}

    assert discovered == on_disk, f"rbac modules not discovered: {sorted(on_disk - discovered)}"


def test_no_two_modules_define_the_same_permission_code() -> None:
    """A code owned twice is a permission whose meaning depends on import order.

    Each module owns its own namespace by convention (`bed:*` is `ipd`'s,
    `invoice:*` is `billing`'s). Nothing enforces that, so a copy-pasted
    catalogue entry could quietly give one code two definitions and two
    descriptions, of which the seed would land whichever it saw last.
    """
    owners: dict[str, list[str]] = {}
    for module in iter_rbac_modules():
        name = module.__name__.rsplit(".", 2)[-2]
        for permission in getattr(module, "PERMISSION_CATALOGUE", ()):
            owners.setdefault(permission.code, []).append(name)

    duplicates = {code: modules for code, modules in owners.items() if len(modules) > 1}
    assert not duplicates, f"permission codes defined in more than one module: {duplicates}"


# ---------------------------------------------------------------------------
# Code vs database
# ---------------------------------------------------------------------------
async def test_every_permission_the_code_grants_is_seeded(session: AsyncSession) -> None:
    """A grant in `rbac.py` that no migration seeds is a role that cannot work.

    This is the direction that bites in production: the code says a pathologist
    may verify a result, the database has never heard of it, and the refusal
    looks like a bug in the lab rather than a missing INSERT.
    """
    declared = _declared_in_code()
    seeded = await _seeded_in_database(session)

    missing: dict[str, list[str]] = {}
    for role, permissions in declared.items():
        gap = permissions - seeded.get(role, set())
        if gap:
            missing[role] = sorted(gap)

    assert not missing, f"declared in code but never seeded: {missing}"


async def test_every_seeded_permission_is_declared_in_code(session: AsyncSession) -> None:
    """And the reverse: a grant nobody enforces is a lie in an access review.

    A role listed as holding `invoice:write_off` had better be a role the code
    actually consults, or the permission matrix a hospital signs off is fiction.
    """
    declared = _declared_in_code()
    seeded = await _seeded_in_database(session)
    known = _declared_permission_codes()

    extra: dict[str, list[str]] = {}
    for role, permissions in seeded.items():
        # Only compare permissions owned by the modules under test; identity
        # seeds a few of its own that no module map claims.
        gap = (permissions & known) - declared.get(role, set())
        if gap:
            extra[role] = sorted(gap)

    assert not extra, f"seeded in the database but not declared in code: {extra}"


async def test_every_system_role_in_code_exists_in_the_database(session: AsyncSession) -> None:
    async with db.system_context(session):
        rows = (
            (await session.execute(sa.text("SELECT code FROM roles WHERE hospital_id IS NULL")))
            .scalars()
            .all()
        )

    assert {role.code for role in SYSTEM_ROLES} <= set(rows)


# ---------------------------------------------------------------------------
# The two roles added after CLAUDE.md §8's original twelve
# ---------------------------------------------------------------------------
async def test_a_pathologist_can_verify_and_correct_a_lab_report(session: AsyncSession) -> None:
    """The role exists because entering a result and signing it are separate acts.

    Whoever signs must also be able to amend: a correction that needs an
    administrator is a correction that waits.
    """
    seeded = await _seeded_in_database(session)
    held = seeded.get(Roles.PATHOLOGIST, set())

    assert {"result:verify", "result:amend", "report:cancel"} <= held
    # Reporting, not the pre-analytical bench — the same second-pair-of-eyes
    # argument that separates entering a result from verifying one.
    assert "specimen:collect" not in held
    assert "specimen:receive" not in held


async def test_housekeeping_can_turn_a_bed_around_and_read_nothing_clinical(
    session: AsyncSession,
) -> None:
    """A porter who can read a diagnosis is a data-protection finding (§12).

    Asserted as an absence rather than a presence, because the risk here is a
    convenience grant added later by somebody who wanted the bed board to show
    a name.
    """
    seeded = await _seeded_in_database(session)
    held = seeded.get(Roles.HOUSEKEEPING, set())

    assert held == {"ward:read", "bed:read", "bed:clean"}

    forbidden = {
        "patient:read",
        "admission:read",
        "summary:read",
        "encounter:read",
        "medication:read",
        "note:read",
        "diagnosis:read",
        "report:read",
    }
    assert not (held & forbidden)


async def test_the_lab_bench_still_cannot_sign_its_own_work(session: AsyncSession) -> None:
    """Adding a pathologist must not have loosened the technician's role."""
    seeded = await _seeded_in_database(session)
    held = seeded.get(Roles.LAB_TECH, set())

    assert "result:enter" in held
    assert "result:verify" not in held
