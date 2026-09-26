"""`reset_demo_data.py` must not reach configuration through a foreign key.

The script clears the day's work — patients, visits, orders, invoices — and
promises to leave the hospital's *setup* alone: staff, wards, beds, prices, the
test catalogue. It does that with `TRUNCATE ... CASCADE`, and CASCADE is the
trap: PostgreSQL follows every foreign key that *points at* a truncated table,
whether or not any row uses it.

That is not hypothetical. `beds.reserved_for_patient_id` references `patients`,
so `TRUNCATE patients CASCADE` silently took every bed in the hospital with it
while the script printed "configuration untouched". The ward board came back
empty, the seed's guard saw the ward still there and skipped, and the whole
inpatient module was unreachable — for a reason nothing on screen explained.

These tests read the real foreign keys out of SQLModel's metadata, so a new FK
from a configuration table to an operational one fails here rather than in
somebody's development database a month later.

Offline: they need the model metadata, not a database.
"""

from __future__ import annotations

import pytest
from scripts.reset_demo_data import OPERATIONAL_TABLES

from app.registry import target_metadata as metadata

# Tables the script deliberately leaves standing. Everything the hospital
# configures once, plus the append-only audit trail and the RBAC tables.
CONFIGURATION_TABLES = frozenset(
    {
        "hospitals",
        "departments",
        "users",
        "roles",
        "permissions",
        "role_permissions",
        "user_roles",
        "refresh_tokens",
        "doctors",
        "doctor_availabilities",
        "availability_exceptions",
        "wards",
        "beds",
        "rate_cards",
        "service_items",
        "service_prices",
        "test_catalogue_items",
        "test_analytes",
        "reference_ranges",
        "note_templates",
        "notification_templates",
        "audit_logs",
    }
)


def _referencing_tables(target: str) -> set[str]:
    """Every table with a foreign key pointing at `target`.

    These are exactly the tables `TRUNCATE target CASCADE` would also empty.
    """
    referencing: set[str] = set()
    for table in metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table.name == target:
                referencing.add(table.name)
    return referencing


class TestTheTruncateStaysInsideItsLane:
    def test_no_operational_truncate_can_cascade_into_configuration(self) -> None:
        """The invariant the script's promise rests on.

        If a configuration table gains a foreign key to an operational one,
        this fails — and the fix is to delete that operational table rather
        than truncate it, the way `patients` is now handled.
        """
        leaks: dict[str, set[str]] = {}
        for table in OPERATIONAL_TABLES:
            caught = _referencing_tables(table) & CONFIGURATION_TABLES
            if caught:
                leaks[table] = caught

        assert not leaks, (
            "TRUNCATE ... CASCADE would empty configuration tables:\n"
            + "\n".join(f"  {table} -> {sorted(caught)}" for table, caught in leaks.items())
            + "\nDelete that table instead of truncating it (see how `patients` is done)."
        )

    def test_patients_is_deleted_rather_than_truncated(self) -> None:
        """The specific case that caused the outage, pinned by name.

        `beds` references `patients`, so this table can never join the
        truncate list — the general test above would catch it, and this one
        says why in the failure message.
        """
        assert "patients" not in OPERATIONAL_TABLES
        assert "beds" in _referencing_tables("patients"), (
            "beds no longer references patients — if that FK is really gone, "
            "this test and the DELETE in the script can both be simplified."
        )

    @pytest.mark.parametrize("table", sorted(CONFIGURATION_TABLES))
    def test_configuration_is_never_in_the_truncate_list(self, table: str) -> None:
        assert table not in OPERATIONAL_TABLES

    def test_every_listed_table_actually_exists(self) -> None:
        """A typo in the list is a table that silently never gets cleared."""
        unknown = sorted(set(OPERATIONAL_TABLES) - set(metadata.tables))
        assert not unknown, f"not real tables: {unknown}"
