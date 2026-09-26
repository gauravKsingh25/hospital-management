"""The two clearance desks have worklists a human can actually work from.

CLAUDE.md §6 puts a visit into `PENDING_CLEARANCE` and says it closes "only when
the last pending item is cleared by the relevant staff (cashier / lab tech /
pharmacist)". Both of those people need to be able to *find* the work first, and
until these two boards existed neither could: the lab's list came from reports,
which do not exist for an order nobody has accessioned, and the counter's list
came from invoices, which do not exist for charges nobody has assembled. In both
cases the work most at risk of being forgotten was the work the screen could not
show.

So each board is built from the record that exists from the first moment — the
order, and the charge — and each carries patient identity on the row, bulk
loaded. The statement-count tests are the ones to keep: they are what stops a
later change turning a flat lookup back into one query per row on the two
screens that are refreshed most often in the building.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
QUEUE = "/api/v1/queue"
ENCOUNTERS = "/api/v1/encounters"
CATALOGUE = "/api/v1/diagnostics/catalogue"
SPECIMENS = "/api/v1/diagnostics/specimens"
REPORTS = "/api/v1/diagnostics/reports"
WORKLIST = "/api/v1/diagnostics/worklist"
RATE_CARDS = "/api/v1/billing/rate-cards"
SERVICES = "/api/v1/billing/services"
INVOICES = "/api/v1/billing/invoices"
ACCOUNTS = "/api/v1/billing/accounts"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def doctor_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)


@pytest.fixture
async def tech_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.LAB_TECH, hospital_id=hospital.id)


@pytest.fixture
async def cashier_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.CASHIER, hospital_id=hospital.id)


@pytest.fixture
async def admin_token(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def reception_token(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def doctor_token(api: AsyncClient, doctor_user: User) -> str:
    return await login(api, doctor_user)


@pytest.fixture
async def tech_token(api: AsyncClient, tech_user: User) -> str:
    return await login(api, tech_user)


@pytest.fixture
async def cashier_token(api: AsyncClient, cashier_user: User) -> str:
    return await login(api, cashier_user)


@pytest.fixture
async def doctor_id(api: AsyncClient, admin_token: str, doctor_user: User) -> str:
    created = await api.post(
        DOCTORS,
        json={"user_id": str(doctor_user.id), "specialty": "General Medicine"},
        headers=auth(admin_token),
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture
async def cbc_id(api: AsyncClient, admin_token: str) -> str:
    item = await api.post(
        CATALOGUE,
        json={
            "code": "CBC",
            "name": "Complete Blood Count",
            "discipline": "LAB",
            "specimen_type": "BLOOD",
            "container": "EDTA (purple top)",
        },
        headers=auth(admin_token),
    )
    assert item.status_code == 201, item.text
    item_id = str(item.json()["id"])

    analyte = await api.post(
        f"{CATALOGUE}/{item_id}/analytes",
        json={
            "code": "HB",
            "name": "Haemoglobin",
            "unit": "g/dL",
            "ranges": [{"low": "12.0", "high": "15.0"}],
        },
        headers=auth(admin_token),
    )
    assert analyte.status_code == 201, analyte.text
    return item_id


@pytest.fixture
async def priced(api: AsyncClient, admin_token: str) -> str:
    """A default cash rate card with the consultation priced.

    Without a price the consultation charge lands at zero and the visit owes
    nothing, which is the one case the counter's board is right to hide.
    """
    card = await api.post(
        RATE_CARDS,
        json={"code": "CASH", "name": "Cash counter", "payer_type": "CASH", "is_default": True},
        headers=auth(admin_token),
    )
    assert card.status_code == 201, card.text
    card_id = str(card.json()["id"])

    service = await api.post(
        SERVICES,
        json={"code": "OPD-CONSULT", "name": "OPD consultation", "category": "CONSULTATION"},
        headers=auth(admin_token),
    )
    assert service.status_code == 201, service.text
    price = await api.put(
        f"{SERVICES}/{service.json()['id']}/prices",
        json={"rate_card_id": card_id, "price": "500.00"},
        headers=auth(admin_token),
    )
    assert price.status_code == 200, price.text
    return card_id


async def _register(api: AsyncClient, token: str, *, name: str, phone: str, age: int = 34) -> str:
    created = await api.post(
        PATIENTS,
        json={"full_name": name, "phone": phone, "gender": "FEMALE", "age_years": age},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _quick_opd(api: AsyncClient, token: str, *, patient_id: str, doctor_id: str) -> str:
    opd = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": patient_id, "doctor_id": doctor_id},
        headers=auth(token),
    )
    assert opd.status_code == 201, opd.text
    return str(opd.json()["encounter_id"])


async def _order_cbc(api: AsyncClient, doctor_token: str, encounter_id: str) -> str:
    await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor_token))
    order = await api.post(
        f"{ENCOUNTERS}/{encounter_id}/orders",
        json={"order_type": "LAB", "item_name": "CBC"},
        headers=auth(doctor_token),
    )
    assert order.status_code == 201, order.text
    return str(order.json()["id"])


def _count_statements(fragment: str) -> Any:
    """Context manager counting SQL statements containing `fragment`."""
    import contextlib

    from sqlalchemy import event

    from app.core.database import get_engine

    @contextlib.contextmanager
    def counter() -> Any:
        seen: list[str] = []
        engine = get_engine().sync_engine

        def record(conn: object, cursor: object, statement: str, *args: object) -> None:
            if fragment in statement:
                seen.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            yield seen
        finally:
            event.remove(engine, "before_cursor_execute", record)

    return counter()


# ---------------------------------------------------------------------------
# The bench
# ---------------------------------------------------------------------------
class TestDiagnosticsWorklist:
    async def test_an_unaccessioned_order_is_on_the_list(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        tech_token: str,
        doctor_id: str,
    ) -> None:
        """The whole reason the board is built from orders rather than reports.

        Before it is accessioned an order has no report at all — so a worklist
        drawn from reports shows only the work somebody already remembered to
        start, and the forgotten request is exactly the one that stays invisible.
        """
        patient_id = await _register(api, reception_token, name="Sunita Devi", phone="9876543210")
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )
        await _order_cbc(api, doctor_token, encounter_id)

        response = await api.get(WORKLIST, headers=auth(tech_token))
        assert response.status_code == 200, response.text

        [row] = response.json()["items"]
        assert row["stage"] == "AWAITING_ACCESSION"
        assert row["item_name"] == "CBC"
        assert row["report_id"] is None

    async def test_a_row_names_the_patient(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        tech_token: str,
        doctor_id: str,
    ) -> None:
        """A technician who has to open each row to find out whose blood this
        is will batch them, and a STAT sample ends up processed fourth."""
        patient_id = await _register(
            api, reception_token, name="Kavita Sharma", phone="9811122233", age=45
        )
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )
        await _order_cbc(api, doctor_token, encounter_id)

        [row] = (await api.get(WORKLIST, headers=auth(tech_token))).json()["items"]
        assert row["patient_name"] == "Kavita Sharma"
        assert row["patient_uhid"]
        assert row["patient_age_years"] == 45
        assert row["patient_gender"] == "FEMALE"
        assert row["patient_is_deceased"] is False

    async def test_the_stage_follows_the_sample_then_the_report(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        tech_token: str,
        doctor_id: str,
        cbc_id: str,
    ) -> None:
        """One answer to "what do I do with this one?", out of three enums.

        The sample gate outranks the report's own status deliberately: a report
        sits at `REGISTERED` from the moment it is accessioned, long before
        there is anything in a tube — reading it alone would say "enter results"
        against a patient nobody has drawn blood from.
        """
        patient_id = await _register(
            api, reception_token, name="Ramesh Yadav", phone="9812345670", age=58
        )
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )
        order_id = await _order_cbc(api, doctor_token, encounter_id)

        async def stage() -> str:
            body = (await api.get(WORKLIST, headers=auth(tech_token))).json()
            return str(body["items"][0]["stage"])

        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()
        specimen_id = report["specimen"]["id"]
        assert await stage() == "AWAITING_COLLECTION"

        await api.post(f"{SPECIMENS}/{specimen_id}/collect", json={}, headers=auth(tech_token))
        assert await stage() == "AWAITING_RECEIPT"

        await api.post(f"{SPECIMENS}/{specimen_id}/receive", headers=auth(tech_token))
        assert await stage() == "AWAITING_RESULTS"

        await api.post(
            f"{REPORTS}/{report['id']}/results",
            json={"results": [{"analyte_code": "HB", "value_numeric": "13.0"}]},
            headers=auth(tech_token),
        )
        assert await stage() == "AWAITING_VERIFICATION"

    async def test_a_rejected_sample_sends_the_row_back_to_collection(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        tech_token: str,
        doctor_id: str,
        cbc_id: str,
    ) -> None:
        """A rejected tube means sticking the patient again. The row must not
        look finished, or the request quietly dies at the bench."""
        patient_id = await _register(api, reception_token, name="Anita Kumari", phone="9876500011")
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )
        order_id = await _order_cbc(api, doctor_token, encounter_id)

        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()
        specimen_id = report["specimen"]["id"]
        await api.post(f"{SPECIMENS}/{specimen_id}/collect", json={}, headers=auth(tech_token))
        rejected = await api.post(
            f"{SPECIMENS}/{specimen_id}/reject",
            json={"reason": "Haemolysed on receipt"},
            headers=auth(tech_token),
        )
        assert rejected.status_code == 200, rejected.text

        [row] = (await api.get(WORKLIST, headers=auth(tech_token))).json()["items"]
        assert row["stage"] == "AWAITING_COLLECTION"
        assert row["specimen_status"] == "REJECTED"

    async def test_a_verified_report_leaves_the_worklist(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        tech_token: str,
        doctor_id: str,
        cbc_id: str,
    ) -> None:
        patient_id = await _register(api, reception_token, name="Meena Patel", phone="9876500022")
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )
        order_id = await _order_cbc(api, doctor_token, encounter_id)

        report = (
            await api.post(
                REPORTS,
                json={"order_id": order_id, "catalogue_item_id": cbc_id},
                headers=auth(tech_token),
            )
        ).json()
        specimen_id = report["specimen"]["id"]
        await api.post(f"{SPECIMENS}/{specimen_id}/collect", json={}, headers=auth(tech_token))
        await api.post(f"{SPECIMENS}/{specimen_id}/receive", headers=auth(tech_token))
        await api.post(
            f"{REPORTS}/{report['id']}/results",
            json={"results": [{"analyte_code": "HB", "value_numeric": "13.0"}]},
            headers=auth(tech_token),
        )
        verified = await api.post(f"{REPORTS}/{report['id']}/verify", headers=auth(doctor_token))
        assert verified.status_code == 200, verified.text

        body = (await api.get(WORKLIST, headers=auth(tech_token))).json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_a_report_carries_the_age_and_sex_its_ranges_depend_on(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        tech_token: str,
        doctor_id: str,
        cbc_id: str,
    ) -> None:
        """Not decoration. A number means nothing without the band it was judged
        against, and the band is chosen by sex and age."""
        patient_id = await _register(
            api, reception_token, name="Lakshmi Nair", phone="9876500033", age=62
        )
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )
        order_id = await _order_cbc(api, doctor_token, encounter_id)

        report = await api.post(
            REPORTS,
            json={"order_id": order_id, "catalogue_item_id": cbc_id},
            headers=auth(tech_token),
        )
        assert report.status_code == 201, report.text

        body = report.json()
        assert body["patient_name"] == "Lakshmi Nair"
        assert body["patient_uhid"]
        assert body["patient_age_years"] == 62
        assert body["patient_gender"] == "FEMALE"

    async def test_identity_costs_one_lookup_regardless_of_worklist_length(
        self,
        api: AsyncClient,
        reception_token: str,
        doctor_token: str,
        tech_token: str,
        doctor_id: str,
    ) -> None:
        """A busy lab has a long list. It must not have a slow screen."""
        for index in range(4):
            patient_id = await _register(
                api, reception_token, name=f"Bench Patient{index}", phone=f"98110000{index:02d}"
            )
            encounter_id = await _quick_opd(
                api, reception_token, patient_id=patient_id, doctor_id=doctor_id
            )
            await _order_cbc(api, doctor_token, encounter_id)

        with _count_statements("FROM patients") as seen:
            response = await api.get(WORKLIST, headers=auth(tech_token))

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 4
        assert len(seen) == 1, f"expected a single patients query, got {len(seen)}"

    async def test_the_page_envelope_is_intact(self, api: AsyncClient, tech_token: str) -> None:
        body = (await api.get(WORKLIST, params={"limit": 1}, headers=auth(tech_token))).json()
        assert set(body) == {"items", "total", "limit", "offset", "has_more"}


# ---------------------------------------------------------------------------
# The counter
# ---------------------------------------------------------------------------
class TestAccountsBoard:
    async def test_a_visit_with_uninvoiced_charges_is_on_the_board(
        self,
        api: AsyncClient,
        reception_token: str,
        cashier_token: str,
        doctor_id: str,
        priced: str,
    ) -> None:
        """The whole reason the board is not the invoice list.

        A visit whose charges nobody has assembled into an invoice owes money
        just as surely as one with an issued invoice nobody has paid — and it is
        the more common way a patient walks out without paying, because there is
        no document whose absence anyone would notice.
        """
        patient_id = await _register(api, reception_token, name="Sunita Devi", phone="9876543210")
        await _quick_opd(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        response = await api.get(ACCOUNTS, headers=auth(cashier_token))
        assert response.status_code == 200, response.text

        [row] = response.json()["items"]
        assert row["patient_name"] == "Sunita Devi"
        assert row["patient_uhid"]
        assert row["balance_due"] == "500.00"
        assert row["pending_total"] == "500.00"
        assert row["invoiced_balance"] == "0.00"
        assert row["encounter_number"]

    async def test_a_paid_visit_leaves_the_board(
        self,
        api: AsyncClient,
        reception_token: str,
        cashier_token: str,
        doctor_id: str,
        priced: str,
    ) -> None:
        """A settled visit contributes a zero row, not an absent one. Without the
        HAVING clause the counter would see every paid visit of the day."""
        patient_id = await _register(api, reception_token, name="Kavita Sharma", phone="9811122233")
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )

        draft = await api.post(
            INVOICES, json={"encounter_id": encounter_id}, headers=auth(cashier_token)
        )
        assert draft.status_code == 201, draft.text
        invoice_id = draft.json()["id"]
        issued = await api.post(
            f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(cashier_token)
        )
        assert issued.status_code == 200, issued.text

        # Still owed until the money arrives — the debt moved, it did not go.
        mid = (await api.get(ACCOUNTS, headers=auth(cashier_token))).json()["items"]
        assert len(mid) == 1
        assert mid[0]["pending_total"] == "0.00"
        assert mid[0]["invoiced_balance"] == "500.00"

        paid = await api.post(
            f"{INVOICES}/{invoice_id}/payments",
            json={"amount": "500.00", "method": "UPI", "reference": "UPI-TEST-1"},
            headers=auth(cashier_token),
        )
        assert paid.status_code == 201, paid.text

        after = (await api.get(ACCOUNTS, headers=auth(cashier_token))).json()
        assert after["total"] == 0

    async def test_the_board_says_whether_the_patient_is_still_here(
        self,
        api: AsyncClient,
        reception_token: str,
        cashier_token: str,
        doctor_id: str,
        priced: str,
    ) -> None:
        """`encounter_status` is what separates "they are at the window" from
        "this bill was left behind" — same money, three different conversations
        (CLAUDE.md §6)."""
        patient_id = await _register(api, reception_token, name="Ramesh Yadav", phone="9812345670")
        await _quick_opd(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        [row] = (await api.get(ACCOUNTS, headers=auth(cashier_token))).json()["items"]
        assert row["encounter_status"] in {"REGISTERED", "IN_CONSULTATION", "PENDING_CLEARANCE"}

    async def test_a_bill_left_by_a_death_stays_on_the_board(
        self,
        api: AsyncClient,
        reception_token: str,
        cashier_token: str,
        doctor_id: str,
        doctor_user: User,
        priced: str,
        hospital_admin: User,
    ) -> None:
        """The visit is closed and the money is still owed. Filtering the board
        to live visits would hide it, and CLAUDE.md §6 requires the settlement
        flow to still happen."""
        patient_id = await _register(
            api, reception_token, name="Gopal Verma", phone="9812345699", age=81
        )
        encounter_id = await _quick_opd(
            api, reception_token, patient_id=patient_id, doctor_id=doctor_id
        )

        admin_token = await login(api, hospital_admin)
        death = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/death",
            json={
                "deceased_at": utc_now().isoformat(),
                "death_certified_by_id": str(doctor_user.id),
                "cause_of_death": "Cardiac arrest",
                "death_place": "OPD",
            },
            headers=auth(admin_token),
        )
        assert death.status_code == 200, death.text

        [row] = (await api.get(ACCOUNTS, headers=auth(cashier_token))).json()["items"]
        assert row["encounter_status"] == "DECEASED"
        assert row["patient_is_deceased"] is True
        assert row["balance_due"] == "500.00"

    async def test_identity_costs_one_lookup_regardless_of_board_length(
        self,
        api: AsyncClient,
        reception_token: str,
        cashier_token: str,
        doctor_id: str,
        priced: str,
    ) -> None:
        for index in range(4):
            patient_id = await _register(
                api, reception_token, name=f"Counter Patient{index}", phone=f"98120000{index:02d}"
            )
            await _quick_opd(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        with _count_statements("FROM patients") as seen:
            response = await api.get(ACCOUNTS, headers=auth(cashier_token))

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 4
        assert len(seen) == 1, f"expected a single patients query, got {len(seen)}"

    async def test_the_page_envelope_is_intact(self, api: AsyncClient, cashier_token: str) -> None:
        body = (await api.get(ACCOUNTS, params={"limit": 1}, headers=auth(cashier_token))).json()
        assert set(body) == {"items", "total", "limit", "offset", "has_more"}


class TestBoardsRespectRbacAndTenancy:
    async def test_a_receptionist_cannot_read_the_bench_worklist(
        self, api: AsyncClient, reception_token: str
    ) -> None:
        assert (await api.get(WORKLIST, headers=auth(reception_token))).status_code == 403

    async def test_an_unauthenticated_request_is_refused(self, api: AsyncClient) -> None:
        assert (await api.get(WORKLIST)).status_code == 401
        assert (await api.get(ACCOUNTS)).status_code == 401

    async def test_another_hospitals_debts_are_invisible(
        self,
        api: AsyncClient,
        session: AsyncSession,
        reception_token: str,
        doctor_id: str,
        priced: str,
    ) -> None:
        import uuid

        from app.core import database as db

        patient_id = await _register(api, reception_token, name="Sunita Devi", phone="9876543210")
        await _quick_opd(api, reception_token, patient_id=patient_id, doctor_id=doctor_id)

        async with db.system_context(session):
            other = Hospital(code=f"B{uuid.uuid4().hex[:5].upper()}", name="Other Hospital")
            session.add(other)
            await session.flush()
            other_id = other.id
        await session.commit()

        outsider = await _make_user(session, role_code=Roles.CASHIER, hospital_id=other_id)
        outsider_token = await login(api, outsider)

        body = (await api.get(ACCOUNTS, headers=auth(outsider_token))).json()
        assert body["total"] == 0
