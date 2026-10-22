# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Shared argument types, time helpers and the error-handling wrapper."""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import DattoClient, DattoError

logger = logging.getLogger("datto_bcdr_mcp.tools")

ClientFactory = Callable[[], DattoClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "Datto credentials are missing for this request (X-Datto-BCDR-Public-Key, X-Datto-BCDR-Private-Key).",
    False,
)

SerialNumber = Annotated[str, Field(description="Appliance serial number, from datto_bcdr_list_devices.", min_length=1)]
AgentId = Annotated[
    str,
    Field(description="Protected asset's volume name (the name / volume field from datto_bcdr_list_assets).", min_length=1),
]
Page = Annotated[int | None, Field(description="Page number, from 1 (default 1).", ge=1)]
PerPage = Annotated[int | None, Field(description="Results per page (default 50, max 250).", ge=1, le=250)]
Offset = Annotated[
    int | None,
    Field(description="Record position to start from (0-based, overrides page); pass next_offset from the previous result.", ge=0),
]
DEFAULT_PAGE_SIZE = 50
IsoTime = Annotated[str | None, Field(description="ISO 8601 date-time, e.g. 2026-06-01T00:00:00Z.")]


def parse_time(value: str, field: str) -> datetime:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f'{field} must be an ISO 8601 date-time, e.g. "2026-06-01T00:00:00Z" (got "{value}").') from e
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def to_datetime(value: Any) -> datetime | None:
    """A Datto timestamp (Unix seconds / milliseconds or date string) as an aware datetime."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e12 else value
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.lstrip("-").isdigit():
            return to_datetime(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def iso(value: Any) -> str | None:
    moment = to_datetime(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def locate(offset: int | None, page: int | None, per_page: int | None) -> tuple[int, int, int]:
    """(page, perPage, start) for a read from record ``start``.

    Datto pages by number only, so a position is reached by asking for the page that holds it
    (same page size) and skipping the records before it. Locally paged lists slice the same way.
    """
    size = per_page or DEFAULT_PAGE_SIZE
    start = offset if offset is not None else ((page or 1) - 1) * size
    return start // size + 1, size, start


def continuation(start: int, after: int, more: bool, first: int | None = None) -> dict:
    """offset / next_offset paging fields for records read from ``start`` up to ``after``.

    next_offset continues right after the last record returned, also when the result is cut to
    fit the size limit (a page number cannot: the rest of a cut page is not on any other page).
    ``first`` is the position of the first record returned when records before it were dropped.
    """
    first = start if first is None else first
    return {
        "offset": start,
        "next_offset": after if more else None,
        "_resume": {"key": "next_offset", "start": first},
    }


def page_info(pagination: Any, page: int, per_page: int, count: int) -> dict:
    info = pagination if isinstance(pagination, dict) else {}
    total_pages = info.get("totalPages")
    more = page < total_pages if isinstance(total_pages, int) else count >= per_page
    return {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total": info.get("count"),
        "has_more": more,
    }


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[DattoClient], Awaitable[Any]],
) -> Any:
    """Resolve the caller's client, run ``action``, and return its result.

    Errors never escape as exceptions: they become the standard error envelope.
    Dicts and lists are serialized to capped JSON; other values (images) pass through.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except DattoError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Datto returned an empty response."}
    return dump_json_capped(result) if isinstance(result, (dict, list)) else result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
