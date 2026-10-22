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
"""Alerts across all appliances, and the partner portal activity log."""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import DattoClient, segment
from ._common import (
    READ_ONLY,
    ClientFactory,
    IsoTime,
    Offset,
    Page,
    PerPage,
    continuation,
    locate,
    page_info,
    parse_time,
    run_tool,
    to_datetime,
)

DEFAULT_WINDOW = timedelta(days=7)
_DEVICE_LIMIT = 1000
_ALERTS_PER_DEVICE = 1000
_CONCURRENCY = 5
# Datto's activity log rejects since >= 744 hours ("less than 744 hours"), i.e. about 31 days.
_ACTIVITY_MAX_HOURS = 743
# Entries read when clientName is matched here (Datto's own clientName filter returns nothing).
_ACTIVITY_SCAN_LIMIT = 1000


def _window(since: str | None, until: str | None) -> tuple[datetime, datetime | None]:
    end = parse_time(until, "until") if until and until.strip() else None
    start = parse_time(since, "since") if since and since.strip() else (end or datetime.now(UTC)) - DEFAULT_WINDOW
    if end is not None and end < start:
        raise ValueError("until must be after since.")
    return start, end


def _in_window(moment: datetime | None, start: datetime, end: datetime | None) -> bool:
    if moment is None:
        return True
    return moment >= start and (end is None or moment <= end)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_list_alerts(
        since: IsoTime = None,
        until: IsoTime = None,
        page: Page = None,
        perPage: PerPage = None,
        offset: Offset = None,
    ) -> str:
        """List the alerts currently open on all appliances, raised in a time window (default 7 days).

        Newest first. Datto returns only open alerts, so cleared alerts never appear (no history).
        Reads every appliance that reports alerts; each alert carries the appliance serial and name.
        """

        async def action(client: DattoClient) -> dict:
            start, end = _window(since, until)
            devices, devices_truncated = await client.all_items("bcdr/device", _DEVICE_LIMIT)
            with_alerts = [
                d
                for d in devices
                if isinstance(d, dict) and d.get("serialNumber") and d.get("alertCount") != 0
            ]
            gate = asyncio.Semaphore(_CONCURRENCY)

            async def for_device(device: dict) -> list[dict]:
                async with gate:
                    alerts, _ = await client.all_items(f"bcdr/device/{segment(device['serialNumber'])}/alert", _ALERTS_PER_DEVICE)
                return [
                    {"serialNumber": device["serialNumber"], "deviceName": device.get("name"), **a}
                    for a in alerts
                    if isinstance(a, dict) and _in_window(to_datetime(a.get("dateTriggered")), start, end)
                ]

            found = [a for group in await asyncio.gather(*(for_device(d) for d in with_alerts)) for a in group]
            found.sort(key=lambda a: to_datetime(a.get("dateTriggered")) or datetime.min.replace(tzinfo=UTC), reverse=True)
            number, size, at = locate(offset, page, perPage)
            window = found[at : number * size]
            more = number * size < len(found)
            return {
                "alerts": window,
                "count": len(window),
                "page": number,
                "per_page": size,
                "total": len(found),
                "has_more": more,
                **continuation(at, at + len(window), more),
                "since": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "until": end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else None,
                "devicesScanned": len(with_alerts),
                "devicesTruncated": devices_truncated or None,
            }

        return await run_tool("datto_bcdr_list_alerts", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_list_activity(
        since: IsoTime = None,
        until: IsoTime = None,
        page: Page = None,
        perPage: PerPage = None,
        clientName: Annotated[
            str | None, Field(description="Only activity whose client name contains this text (case-insensitive).")
        ] = None,
        offset: Offset = None,
    ) -> str:
        """List partner portal activity-log entries (who did what, on which target), default last 7 days.

        The log covers the last 31 days; an older since is moved to 31 days ago (since_clamped).
        """

        async def action(client: DattoClient) -> dict:
            start, end = _window(since, until)
            now = datetime.now(UTC)
            hours = max(1, math.ceil((now - start).total_seconds() / 3600))
            clamped = hours > _ACTIVITY_MAX_HOURS
            if clamped:
                hours = _ACTIVITY_MAX_HOURS
                start = now - timedelta(hours=_ACTIVITY_MAX_HOURS)
                if end is not None and end < start:
                    raise ValueError("until is more than 31 days ago; the Datto activity log covers only the last 31 days.")
            number, size, at = locate(offset, page, perPage)
            extra = {"since": hours, "sinceUnits": "hours"}
            name = (clientName or "").strip().lower()

            def in_window(entry: object) -> bool:
                return not isinstance(entry, dict) or _in_window(to_datetime(entry.get("timestamp")), start, end)

            if name:
                # Datto's clientName parameter returns no rows even for an exact name, so match here.
                entries, truncated = await client.all_items("report/activity-log", _ACTIVITY_SCAN_LIMIT, extra)
                matched = [
                    e for e in entries if isinstance(e, dict) and in_window(e) and name in str(e.get("clientName") or "").lower()
                ]
                items = matched[at : number * size]
                more = number * size < len(matched)
                paging = {
                    "page": number,
                    "per_page": size,
                    "total": len(matched),
                    "has_more": more,
                    "scanned": len(entries),
                    "scan_incomplete": truncated or None,
                } | continuation(at, at + len(items), more)
            else:
                result = await client.page("report/activity-log", number, size, extra)
                read = result["items"][at - (number - 1) * size :]
                # Entries outside the window (newer than until) lead the newest-first page: skip them
                # so a cut result continues from the right position.
                lead = next((k for k, entry in enumerate(read) if in_window(entry)), len(read))
                items = [i for i in read[lead:] if in_window(i)]
                paging = page_info(result["pagination"], number, size, len(result["items"]))
                paging |= continuation(at, at + len(read), paging["has_more"], at + lead)
            return {"activity": items, "count": len(items)} | paging | {
                "since": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "until": end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else None,
                "since_clamped": clamped or None,
                "note": "The activity log covers only the last 31 days, so since was moved to 31 days ago."
                if clamped
                else None,
            }

        return await run_tool("datto_bcdr_list_activity", client_factory, action)
