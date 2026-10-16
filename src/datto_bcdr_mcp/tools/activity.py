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
from ._common import READ_ONLY, ClientFactory, IsoTime, Page, PerPage, page_info, parse_time, run_tool, to_datetime

DEFAULT_WINDOW = timedelta(days=7)
_DEVICE_LIMIT = 1000
_ALERTS_PER_DEVICE = 1000
_CONCURRENCY = 5


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
    ) -> str:
        """List alerts raised on all appliances in a time window (default: the last 7 days), newest first.

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
            number, size = page or 1, perPage or 50
            window = found[(number - 1) * size : number * size]
            return {
                "alerts": window,
                "count": len(window),
                "page": number,
                "per_page": size,
                "total": len(found),
                "has_more": number * size < len(found),
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
        clientName: Annotated[str | None, Field(description="Only activity for this client (organization) name.")] = None,
    ) -> str:
        """List partner portal activity-log entries (who did what, on which target), default last 7 days."""

        async def action(client: DattoClient) -> dict:
            start, end = _window(since, until)
            hours = max(1, math.ceil((datetime.now(UTC) - start).total_seconds() / 3600))
            number, size = page or 1, perPage or 50
            extra = {"since": hours, "sinceUnits": "hours", "clientName": (clientName or "").strip() or None}
            result = await client.page("report/activity-log", number, size, extra)
            items = [i for i in result["items"] if not isinstance(i, dict) or _in_window(to_datetime(i.get("timestamp")), start, end)]
            return {"activity": items, "count": len(items)} | page_info(result["pagination"], number, size, len(result["items"])) | {
                "since": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "until": end.strftime("%Y-%m-%dT%H:%M:%SZ") if end else None,
            }

        return await run_tool("datto_bcdr_list_activity", client_factory, action)
