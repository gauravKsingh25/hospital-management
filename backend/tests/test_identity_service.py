"""Service-layer behaviour for `identity`: auth, RBAC, audit."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.identity import service
from app.modules.identity.models import AuditLog, RefreshToken, User
from app.modules.identity.rbac import Permissions, Roles
from app.modules.identity.schemas import UserCreate
from app.modules.tenancy.models import Hospital
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.integration

STRONG_PASSWORD = "a-perfectly-reasonable-passphrase"


class TestAuthentication:
    async def test_correct_credentials_authenticate(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        user = await service.authenticate(session, email=receptionist.email, password=TEST_PASSWORD)
        assert user.id == receptionist.id

    async def test_wrong_password_is_rejected(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        with pytest.raises(AuthenticationError):
            await service.authenticate(
                session, email=receptionist.email, password="not-the-password"
            )

    async def test_unknown_account_and_wrong_password_are_indistinguishable(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        """Otherwise the login form doubles as a staff directory."""
        with pytest.raises(AuthenticationError) as unknown:
            await service.authenticate(
                session, email="nobody@example.com", password="whatever-at-all"
            )
        with pytest.raises(AuthenticationError) as wrong:
            await service.authenticate(
                session, email=receptionist.email, password="whatever-at-all"
            )
        assert unknown.value.message == wrong.value.message
        assert unknown.value.code == wrong.value.code

    async def test_account_locks_after_repeated_failures(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        for _ in range(service.MAX_FAILED_LOGINS):
            with pytest.raises(AuthenticationError):
                await service.authenticate(
                    session, email=receptionist.email, password="wrong-password-here"
                )

        # Even the right password is refused while the lockout holds.
        with pytest.raises(AuthenticationError) as excinfo:
            await service.authenticate(session, email=receptionist.email, password=TEST_PASSWORD)
        assert excinfo.value.code == "account_locked"

    async def test_successful_login_clears_the_failure_counter(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        with pytest.raises(AuthenticationError):
            await service.authenticate(
                session, email=receptionist.email, password="wrong-password-here"
            )
        user = await service.authenticate(session, email=receptionist.email, password=TEST_PASSWORD)
        assert user.failed_login_attempts == 0
        assert user.last_login_at is not None

    async def test_deactivated_account_cannot_authenticate(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        await service.set_user_active(session, receptionist, active=False)
        await session.commit()

        with pytest.raises(AuthenticationError) as excinfo:
            await service.authenticate(session, email=receptionist.email, password=TEST_PASSWORD)
        assert excinfo.value.code == "account_inactive"


class TestRefreshRotation:
    async def test_refresh_returns_a_new_pair_and_retires_the_old_one(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        _, _, refresh, _ = await service.login(
            session, email=receptionist.email, password=TEST_PASSWORD
        )
        await session.commit()

        _, _, rotated, _ = await service.rotate_refresh_token(session, refresh)
        await session.commit()
        assert rotated != refresh

        with pytest.raises(AuthenticationError):
            await service.rotate_refresh_token(session, refresh)

    async def test_reusing_a_rotated_token_revokes_the_whole_family(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        """Replay is the signature of theft; we cannot tell victim from thief."""
        _, _, first, _ = await service.login(
            session, email=receptionist.email, password=TEST_PASSWORD
        )
        await session.commit()
        _, _, second, _ = await service.rotate_refresh_token(session, first)
        await session.commit()

        with pytest.raises(AuthenticationError) as excinfo:
            await service.rotate_refresh_token(session, first)
        await session.commit()
        assert excinfo.value.code == "token_reused"

        # The legitimate client's current token is dead too.
        with pytest.raises(AuthenticationError):
            await service.rotate_refresh_token(session, second)

    async def test_logout_revokes_only_that_session(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        _, _, session_a, _ = await service.login(
            session, email=receptionist.email, password=TEST_PASSWORD
        )
        _, _, session_b, _ = await service.login(
            session, email=receptionist.email, password=TEST_PASSWORD
        )
        await session.commit()

        await service.revoke_refresh_token(session, session_a)
        await session.commit()

        # Retrying a logged-out token is not evidence of theft, so it must not
        # take the user's other sessions down with it.
        with pytest.raises(AuthenticationError) as excinfo:
            await service.rotate_refresh_token(session, session_a)
        assert excinfo.value.code == "token_revoked"

        user, _, _, _ = await service.rotate_refresh_token(session, session_b)
        assert user.id == receptionist.id

    async def test_changing_password_ends_every_session(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        _, _, refresh, _ = await service.login(
            session, email=receptionist.email, password=TEST_PASSWORD
        )
        await session.commit()

        await service.change_password(
            session,
            receptionist,
            current_password=TEST_PASSWORD,
            new_password=STRONG_PASSWORD,
        )
        await session.commit()

        with pytest.raises(AuthenticationError):
            await service.rotate_refresh_token(session, refresh)

    async def test_deactivating_a_user_ends_every_session(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        _, _, refresh, _ = await service.login(
            session, email=receptionist.email, password=TEST_PASSWORD
        )
        await session.commit()

        await service.set_user_active(session, receptionist, active=False)
        await session.commit()

        with pytest.raises(AuthenticationError):
            await service.rotate_refresh_token(session, refresh)

    async def test_stored_refresh_tokens_are_hashed(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        """A database read must not be enough to mint a session."""
        _, _, refresh, _ = await service.login(
            session, email=receptionist.email, password=TEST_PASSWORD
        )
        await session.commit()

        async with db.system_context(session):
            stored = (await session.execute(sa.select(RefreshToken))).scalars().all()
        assert stored
        assert all(row.token_hash != refresh for row in stored)


class TestPasswordChange:
    async def test_wrong_current_password_is_refused(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        with pytest.raises(AuthenticationError):
            await service.change_password(
                session,
                receptionist,
                current_password="not-my-password",
                new_password=STRONG_PASSWORD,
            )

    async def test_reusing_the_same_password_is_refused(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        with pytest.raises(ValidationError):
            await service.change_password(
                session,
                receptionist,
                current_password=TEST_PASSWORD,
                new_password=TEST_PASSWORD,
            )

    async def test_weak_new_password_is_refused(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        with pytest.raises(ValidationError):
            await service.change_password(
                session, receptionist, current_password=TEST_PASSWORD, new_password="short"
            )

    async def test_admin_reset_forces_a_change_at_next_login(
        self, session: AsyncSession, receptionist: User, hospital_admin: User
    ) -> None:
        await service.reset_password(
            session, receptionist, new_password=STRONG_PASSWORD, actor=hospital_admin
        )
        await session.commit()
        assert receptionist.must_change_password is True


class TestUsers:
    async def test_duplicate_email_is_rejected(
        self, session: AsyncSession, hospital: Hospital, receptionist: User
    ) -> None:
        with pytest.raises(ConflictError):
            await service.create_user(
                session,
                UserCreate(
                    email=receptionist.email,
                    full_name="Someone Else",
                    password=STRONG_PASSWORD,
                ),
                hospital_id=hospital.id,
            )

    async def test_created_user_gets_its_roles_and_permissions(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        user = await service.create_user(
            session,
            UserCreate(
                email="nurse@example.com",
                full_name="A Nurse",
                password=STRONG_PASSWORD,
                role_codes=[Roles.NURSE],
            ),
            hospital_id=hospital.id,
        )
        await session.commit()

        roles, permissions = await service.resolve_permissions(session, user)
        assert Roles.NURSE in roles
        assert Permissions.HOSPITAL_READ in permissions

    async def test_new_accounts_must_change_password(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        user = await service.create_user(
            session,
            UserCreate(email="new@example.com", full_name="New Staff", password=STRONG_PASSWORD),
            hospital_id=hospital.id,
        )
        assert user.must_change_password is True

    async def test_weak_password_is_refused_at_creation(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        with pytest.raises(ValidationError):
            await service.create_user(
                session,
                UserCreate(email="weak@example.com", full_name="Weak", password="short"),
                hospital_id=hospital.id,
            )

    async def test_listing_is_scoped_to_one_hospital(
        self, session: AsyncSession, hospital: Hospital, receptionist: User
    ) -> None:
        # Listing is a tenant-scoped read, so it runs bound to a tenant exactly
        # as a real request does — RLS applies on top of the service filter.
        await db.set_tenant_context(session, hospital.id)
        users, total = await service.list_users(session, PageParams(), hospital_id=hospital.id)
        assert total >= 1
        assert all(user.hospital_id == hospital.id for user in users)

    async def test_listing_is_paginated(self, session: AsyncSession, hospital: Hospital) -> None:
        for index in range(5):
            await service.create_user(
                session,
                UserCreate(
                    email=f"staff{index}@example.com",
                    full_name=f"Staff {index}",
                    password=STRONG_PASSWORD,
                ),
                hospital_id=hospital.id,
            )
        await session.commit()

        await db.set_tenant_context(session, hospital.id)
        page, total = await service.list_users(
            session, PageParams(limit=2, offset=0), hospital_id=hospital.id
        )
        assert len(page) == 2
        assert total >= 5


class TestRoleAssignment:
    async def test_assignment_replaces_rather_than_appends(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        await service.assign_roles(session, receptionist, [Roles.NURSE])
        await session.commit()
        roles, _ = await service.resolve_permissions(session, receptionist)
        assert roles == {Roles.NURSE}

    async def test_unknown_role_is_refused(self, session: AsyncSession, receptionist: User) -> None:
        with pytest.raises(ValidationError):
            await service.assign_roles(session, receptionist, ["WARD_WIZARD"])

    async def test_platform_admin_cannot_be_granted_to_a_tenant_user(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        """It is the only cross-tenant role; a tenant user holding it is a breach."""
        with pytest.raises(ValidationError) as excinfo:
            await service.assign_roles(session, receptionist, [Roles.PLATFORM_ADMIN])
        assert excinfo.value.code == "invalid_role_assignment"

    async def test_permission_check_helper(
        self, session: AsyncSession, hospital_admin: User
    ) -> None:
        context = await service.build_auth_context(session, hospital_admin)
        assert context.has_permission(Permissions.USER_CREATE)
        assert not context.has_permission(Permissions.HOSPITAL_CREATE)

        with pytest.raises(PermissionDeniedError):
            service.require_permission(context, Permissions.HOSPITAL_CREATE)


class TestAudit:
    async def test_successful_login_is_recorded(
        self, session: AsyncSession, hospital: Hospital, receptionist: User
    ) -> None:
        await service.login(session, email=receptionist.email, password=TEST_PASSWORD)
        await session.commit()

        await db.set_tenant_context(session, hospital.id)
        entries, _ = await service.list_audit_logs(
            session, PageParams(), hospital_id=receptionist.hospital_id, action="auth.login"
        )
        assert any(entry.succeeded and entry.actor_user_id == receptionist.id for entry in entries)

    async def test_failed_login_is_recorded_with_a_reason(
        self, session: AsyncSession, hospital: Hospital, receptionist: User
    ) -> None:
        with pytest.raises(AuthenticationError):
            await service.authenticate(
                session, email=receptionist.email, password="wrong-password-here"
            )
        await session.commit()

        await db.set_tenant_context(session, hospital.id)
        entries, _ = await service.list_audit_logs(
            session, PageParams(), hospital_id=receptionist.hospital_id, action="auth.login"
        )
        failures = [entry for entry in entries if not entry.succeeded]
        assert failures
        assert failures[0].changes is not None
        assert failures[0].changes["reason"] == "bad_password"

    async def test_actor_email_is_snapshotted_not_only_referenced(
        self, session: AsyncSession, hospital: Hospital, receptionist: User
    ) -> None:
        """The trail must still read correctly after the account is renamed."""
        await service.login(session, email=receptionist.email, password=TEST_PASSWORD)
        await session.commit()

        await db.set_tenant_context(session, hospital.id)
        entries, _ = await service.list_audit_logs(
            session, PageParams(), hospital_id=receptionist.hospital_id
        )
        assert any(entry.actor_email == receptionist.email for entry in entries)

    async def test_audit_rows_cannot_be_updated_or_deleted(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        """Append-only is enforced by the database, not by convention."""
        await service.record_audit(
            session, action="test.action", actor=receptionist, resource_type="user"
        )
        await session.commit()

        async with db.system_context(session):
            entry_id = (await session.execute(sa.select(AuditLog.id).limit(1))).scalar_one()

        # Bypass is on so the row is visible: what refuses the write below is
        # the trigger, not row-level security filtering it out of reach.
        await db.set_bypass_rls(session, enabled=True)
        with pytest.raises(sa.exc.DBAPIError):
            await session.execute(
                sa.text("UPDATE audit_logs SET action = 'tampered' WHERE id = :id"),
                {"id": entry_id},
            )
        await session.rollback()

        await db.set_bypass_rls(session, enabled=True)
        with pytest.raises(sa.exc.DBAPIError):
            await session.execute(
                sa.text("DELETE FROM audit_logs WHERE id = :id"), {"id": entry_id}
            )
        await session.rollback()

    async def test_a_user_with_audit_history_cannot_be_deleted(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        """The foreign key must agree with the append-only trigger.

        This column was `ON DELETE SET NULL`, which contradicted that trigger:
        `SET NULL` is implemented as an UPDATE of the referencing row, and the
        trigger refuses UPDATE. So the declared behaviour was unreachable —
        deleting a staff account would not have anonymised the trail, it would
        have failed with a confusing "audit_logs is append-only" error during
        offboarding.

        It is `ON DELETE RESTRICT` now, which states the real rule: an account
        that has done anything cannot be erased, because an audit trail whose
        actor can be deleted is not an audit trail. Offboarding sets
        `users.deleted_at` instead.
        """
        await service.record_audit(
            session, action="test.action", actor=receptionist, resource_type="user"
        )
        await session.commit()

        await db.set_bypass_rls(session, enabled=True)
        with pytest.raises(sa.exc.IntegrityError) as exc:
            await session.execute(
                sa.text("DELETE FROM users WHERE id = :id"), {"id": receptionist.id}
            )
        await session.rollback()

        # A foreign-key refusal, not the trigger firing — which is the whole
        # point of the correction. The old behaviour failed here too, but with
        # "audit_logs is append-only; UPDATE is not permitted".
        message = str(exc.value)
        assert "fk_audit_logs_actor_user_id_users" in message
        assert "append-only" not in message

    async def test_soft_deleting_a_user_leaves_the_trail_readable(
        self, session: AsyncSession, receptionist: User
    ) -> None:
        """Offboarding is `deleted_at`, and the snapshot is what makes it work.

        `actor_email` is written onto every audit row precisely so the history
        still names who did what after the account is retired or renamed.
        """
        await service.record_audit(
            session, action="test.action", actor=receptionist, resource_type="user"
        )
        async with db.system_context(session):
            receptionist.deleted_at = utc_now()
            receptionist.is_active = False
            session.add(receptionist)
            await session.flush()
        await session.commit()

        async with db.system_context(session):
            row = (
                (
                    await session.execute(
                        sa.select(AuditLog)
                        .where(AuditLog.actor_user_id == receptionist.id)
                        .limit(1)
                    )
                )
                .scalars()
                .one()
            )

        assert row.actor_email == receptionist.email
        assert row.actor_user_id == receptionist.id


class TestTenantIsolation:
    async def test_a_bound_session_cannot_see_another_hospitals_users(
        self, session: AsyncSession, hospital: Hospital, receptionist: User
    ) -> None:
        """The database backstop, not the service filter, is what is under test."""
        async with db.system_context(session):
            other = Hospital(code=f"O{uuid.uuid4().hex[:6].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            outsider = User(
                hospital_id=other.id,
                email="outsider@example.com",
                full_name="Outsider",
                password_hash="x",
                is_active=True,
            )
            session.add(outsider)
            await session.flush()
        await session.commit()

        await db.set_tenant_context(session, hospital.id)
        visible = (await session.execute(sa.select(User.email))).scalars().all()

        assert receptionist.email in visible
        assert "outsider@example.com" not in visible

    async def test_an_unbound_session_sees_no_tenant_rows(
        self, session: AsyncSession, hospital: Hospital, receptionist: User
    ) -> None:
        """No tenant set means no rows — the policy fails closed, not open."""
        count = (await session.execute(sa.select(sa.func.count()).select_from(User))).scalar_one()
        assert count == 0

    async def test_writing_into_another_tenant_is_refused(
        self, session: AsyncSession, hospital: Hospital
    ) -> None:
        async with db.system_context(session):
            other = Hospital(code=f"X{uuid.uuid4().hex[:6].upper()}", name="Third Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        await db.set_tenant_context(session, hospital.id)
        session.add(
            User(
                hospital_id=other_id,
                email="smuggled@example.com",
                full_name="Smuggled",
                password_hash="x",
                is_active=True,
            )
        )
        with pytest.raises(sa.exc.DBAPIError):
            await session.flush()
        await session.rollback()
