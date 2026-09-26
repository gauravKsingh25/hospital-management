"""Shared pagination primitives.

CLAUDE.md §11: every list endpoint is paginated, no exceptions. Patient and
encounter tables will run to millions of rows; an unbounded `SELECT *` is a
production outage waiting for a busy Monday.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, Field, computed_field

# Hard ceiling. A caller asking for 10,000 rows gets 200 and no apology.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class PageParams(BaseModel):
    """Limit/offset paging, injected as query parameters."""

    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE
    offset: Annotated[int, Field(ge=0)] = 0


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    """FastAPI dependency producing validated `PageParams`."""
    return PageParams(limit=limit, offset=offset)


class Page[ItemT](BaseModel):
    """A slice of a result set plus enough context to render a pager."""

    items: list[ItemT]
    total: int = Field(description="Total rows matching the filter, ignoring paging.")
    limit: int
    offset: int

    @computed_field(description="True when another page follows this one.")  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        """Whether a further page exists.

        `computed_field`, not a bare `@property`. In Pydantic v2 a plain
        property is invisible to serialisation and to the OpenAPI schema, so
        this read as part of the contract for nine phases while every
        paginated response in the system quietly omitted it — and any client
        that trusted the type would have got `undefined`.
        """
        return self.offset + len(self.items) < self.total

    @classmethod
    def build(cls, items: Sequence[ItemT], *, total: int, params: PageParams) -> Page[ItemT]:
        return cls(items=list(items), total=total, limit=params.limit, offset=params.offset)
