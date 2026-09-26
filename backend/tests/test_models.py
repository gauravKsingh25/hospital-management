"""Mixins are inherited by every table in the system — a defect here is a
defect in all of them."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.ids import new_id
from app.core.models import AuditedTenantModel, IdentifiedModel, utc_now


class _Widget(AuditedTenantModel, table=True):
    """Throwaway concrete model used to inspect generated columns."""

    __tablename__ = "test_widgets"


class _PlatformWidget(IdentifiedModel, table=True):
    __tablename__ = "test_platform_widgets"


def test_ids_are_uuid_version_7() -> None:
    assert new_id().version == 7


def test_ids_are_time_ordered() -> None:
    """The whole point of v7 over v4: sequential inserts stay index-adjacent."""
    ids = [new_id() for _ in range(50)]
    assert ids == sorted(ids, key=lambda value: value.bytes)


def test_ids_are_unique() -> None:
    assert len({new_id() for _ in range(5_000)}) == 5_000


def test_utc_now_is_timezone_aware() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)


def test_tenant_table_has_every_required_column() -> None:
    columns = _Widget.__table__.columns
    for name in ("id", "hospital_id", "created_at", "updated_at", "deleted_at"):
        assert name in columns, f"{name} missing from a tenant-scoped table"


def test_platform_table_has_no_tenant_column() -> None:
    assert "hospital_id" not in _PlatformWidget.__table__.columns


def test_primary_key_is_uuid_and_generated() -> None:
    widget = _Widget(hospital_id=uuid.uuid4())
    assert isinstance(widget.id, uuid.UUID)
    assert _Widget.__table__.columns["id"].primary_key


def test_timestamps_are_timezone_aware_with_server_defaults() -> None:
    columns = _Widget.__table__.columns
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True
    assert columns["created_at"].server_default is not None
    assert columns["updated_at"].onupdate is not None


def test_tenant_and_soft_delete_columns_are_indexed() -> None:
    """Both appear in the WHERE clause of nearly every query."""
    columns = _Widget.__table__.columns
    assert columns["hospital_id"].index is True
    assert columns["deleted_at"].index is True


def test_hospital_fk_restricts_deletion() -> None:
    """Deleting a tenant must never cascade patient records away."""
    (fk,) = list(_Widget.__table__.columns["hospital_id"].foreign_keys)
    assert fk.target_fullname == "hospitals.id"
    assert fk.ondelete == "RESTRICT"


def test_soft_delete_marks_rather_than_removes() -> None:
    widget = _Widget(hospital_id=uuid.uuid4())
    assert widget.is_deleted is False

    widget.mark_deleted()
    assert widget.is_deleted is True
    assert isinstance(widget.deleted_at, datetime)


def test_constraint_naming_convention_is_applied() -> None:
    """Deterministic names keep Alembic autogenerate diffs honest."""
    assert _Widget.__table__.primary_key.name == "pk_test_widgets"
