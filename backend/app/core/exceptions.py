"""Typed domain exceptions and their central HTTP mapping (CLAUDE.md §11).

Services raise domain exceptions; they never build HTTP responses. The mapping
to status codes happens once, here, so a service stays transport-agnostic and
survives being lifted into its own process later.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base class for all expected, business-meaningful failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "domain_error"
    message: str = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class NotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested record does not exist."


class ConflictError(DomainError):
    """Concurrent or duplicate write (duplicate UHID, double check-in, …)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The record conflicts with an existing one."


class ValidationError(DomainError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "The submitted data is not valid."


class AuthenticationError(DomainError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "not_authenticated"
    message = "Authentication is required."


class PermissionDeniedError(DomainError):
    """RBAC refusal (CLAUDE.md §8)."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class TenantIsolationError(DomainError):
    """A request touched data belonging to another hospital.

    Deliberately reported as 404, not 403: confirming that a record exists in
    another tenant is itself a cross-tenant leak.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested record does not exist."


class IllegalStateTransitionError(DomainError):
    """An Encounter transition the state machine forbids (CLAUDE.md §6)."""

    status_code = status.HTTP_409_CONFLICT
    code = "illegal_state_transition"
    message = "That status change is not allowed from the current state."


class ServiceUnavailableError(DomainError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "A dependency is temporarily unavailable."


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the domain -> HTTP mapping onto the application."""

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        # Expected failures: logged at INFO, not as errors — they are not bugs.
        logger.info("domain error code=%s message=%s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw `errors()` carries the original exception object under
        # `ctx`, which is not JSON-serialisable — returning it directly turns a
        # 422 into a 500. Project it onto the flat shape the frontend actually
        # binds to form fields.
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body")
                or "body",
                "message": error.get("msg", "Invalid value."),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The submitted data is not valid.",
                    "details": {"fields": fields},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to a hospital workstation.
        logger.exception("unhandled exception: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong. The incident has been logged.",
                }
            },
        )
