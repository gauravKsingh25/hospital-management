"""Model registry — the single import that populates `SQLModel.metadata`.

Alembic autogenerate can only see tables whose classes have been imported. Every
module's `models.py` must be listed here the moment it defines a table, or its
migrations will silently come out empty (and, worse, a later autogenerate will
try to DROP the tables it cannot see).

Phase 0 defines no tables yet — only the mixins in `core.models`, which are
abstract. The list below fills in as modules land, starting with `tenancy` and
`identity` in Phase 2.
"""

from __future__ import annotations

from sqlmodel import SQLModel

from app.modules.billing import models as billing_models  # noqa: F401
from app.modules.billing.handlers import register_handlers as _register_billing_handlers
from app.modules.clinical import models as clinical_models  # noqa: F401
from app.modules.diagnostics import models as diagnostics_models  # noqa: F401
from app.modules.diagnostics.handlers import register_handlers as _register_diagnostics_handlers
from app.modules.identity import models as identity_models  # noqa: F401
from app.modules.ipd import models as ipd_models  # noqa: F401
from app.modules.ipd.handlers import register_handlers as _register_ipd_handlers
from app.modules.notifications import models as notifications_models  # noqa: F401
from app.modules.notifications.handlers import (
    register_handlers as _register_notification_handlers,
)
from app.modules.patients import models as patients_models  # noqa: F401
from app.modules.scheduling import models as scheduling_models  # noqa: F401
from app.modules.scheduling.handlers import register_handlers as _register_scheduling_handlers
from app.modules.tenancy import models as tenancy_models  # noqa: F401

# `reporting` is deliberately absent: it owns no tables, only SQL views defined
# in its own migration (see `app/modules/reporting/models.py`). Alembic
# autogenerate does not manage views, and importing the module here would
# register nothing.


def register_event_handlers() -> None:
    """Wire every module's subscribers onto the event bus.

    Called once at import — by the app factory, by the worker and by the test
    harness — because this module is the one thing all three load. A `billing`
    that is imported but not subscribed would drop every charge in silence.

    Exposed as a function rather than left as bare import-time statements so the
    test harness can put the bus back after a test has cleared it. That is not
    hypothetical: several tests use `event_bus.clear()` to drop an ad-hoc
    subscriber, which was harmless while nothing else subscribed and became a
    live grenade the moment `billing` started capturing charges transactionally.
    The registration is idempotent for pending-item providers (keyed by source);
    the caller is responsible for clearing first if it wants exactly one
    subscription per event.
    """
    _register_diagnostics_handlers()
    _register_billing_handlers()
    _register_notification_handlers()
    _register_ipd_handlers()
    _register_scheduling_handlers()


register_event_handlers()

target_metadata = SQLModel.metadata

__all__ = ["register_event_handlers", "target_metadata"]
