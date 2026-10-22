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
"""Protected assets (agents and shares), their snapshots, and screenshot verification."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from pydantic import Field

from ..api_client import DattoClient, DattoError, segment
from ._common import READ_ONLY, AgentId, ClientFactory, SerialNumber, iso, run_tool, to_datetime

_ASSET_LIMIT = 2000


async def get_asset(client: DattoClient, serial_number: str, agent_id: str) -> dict:
    """GET .../asset/{volumeName}; the API answers with an object, a one-item list or {items: {...}}."""
    payload = await client.get(f"bcdr/device/{segment(serial_number.strip())}/asset/{segment(agent_id.strip())}")
    if isinstance(payload, dict) and "items" in payload:
        payload = payload["items"]
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        raise DattoError(404, f'No protected asset "{agent_id}" on appliance {serial_number}')
    return payload


def _snapshots(asset: dict) -> list[dict]:
    return [s for s in asset.get("backups") or [] if isinstance(s, dict)]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_list_assets(serialNumber: SerialNumber) -> str:
        """List the machines and shares an appliance protects, with last snapshot, offsite and screenshot."""

        async def action(client: DattoClient) -> dict:
            items, truncated = await client.all_items(f"bcdr/device/{segment(serialNumber.strip())}/asset", _ASSET_LIMIT)
            assets = []
            for asset in items:
                if not isinstance(asset, dict):
                    continue
                summary = {k: v for k, v in asset.items() if k != "backups"}
                summary["snapshotCount"] = len(_snapshots(asset))
                summary["lastSnapshotAt"] = iso(asset.get("lastSnapshot"))
                assets.append(summary)
            return {"serialNumber": serialNumber.strip(), "assets": assets, "count": len(assets), "truncated": truncated or None}

        return await run_tool("datto_bcdr_list_assets", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_get_asset(serialNumber: SerialNumber, agentId: AgentId) -> str:
        """Get one protected asset: OS, agent version, volumes, paused / archived, last snapshot and offsite."""

        async def action(client: DattoClient) -> dict:
            asset = await get_asset(client, serialNumber, agentId)
            summary = {k: v for k, v in asset.items() if k != "backups"}
            return summary | {
                "snapshotCount": len(_snapshots(asset)),
                "lastSnapshotAt": iso(asset.get("lastSnapshot")),
                "latestOffsiteAt": iso(asset.get("latestOffsite")),
                "lastScreenshotAttemptAt": iso(asset.get("lastScreenshotAttempt")),
            }

        return await run_tool("datto_bcdr_get_asset", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_list_backups(serialNumber: SerialNumber, agentId: AgentId) -> str:
        """List an asset's snapshots, newest first: backup status, errors, local and screenshot verification."""

        async def action(client: DattoClient) -> dict:
            asset = await get_asset(client, serialNumber, agentId)
            snaps = sorted(_snapshots(asset), key=lambda s: to_datetime(s.get("timestamp")) or to_datetime(0), reverse=True)
            failed = [s for s in snaps if str((s.get("backup") or {}).get("status", "")).lower() not in ("", "success")]
            return {
                "serialNumber": serialNumber.strip(),
                "agentId": agentId.strip(),
                "backups": snaps,
                "count": len(snaps),
                "failedCount": len(failed),
                "lastSnapshotAt": iso(asset.get("lastSnapshot")),
                "localSnapshots": asset.get("localSnapshots"),
            }

        return await run_tool("datto_bcdr_list_backups", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_list_screenshots(serialNumber: SerialNumber, agentId: AgentId) -> str:
        """Get an asset's screenshot (boot) verification: the latest attempt, plus per-snapshot results.

        latest is the most recent verification (time, status, image link). Per-snapshot results
        only cover the recent snapshots the API returns (about the last 10), so they are often empty.
        """

        async def action(client: DattoClient) -> dict:
            asset = await get_asset(client, serialNumber, agentId)
            rows = []
            for snap in _snapshots(asset):
                verification = (snap.get("advancedVerification") or {}).get("screenshotVerification")
                if isinstance(verification, dict) and (verification.get("status") or verification.get("image")):
                    rows.append({"timestamp": snap.get("timestamp"), "status": verification.get("status"), "image": verification.get("image")})
            rows.sort(key=lambda r: to_datetime(r["timestamp"]) or to_datetime(0), reverse=True)
            attempt = asset.get("lastScreenshotAttempt")
            latest = (
                {
                    "at": iso(attempt),
                    "epoch": attempt,
                    "status": asset.get("lastScreenshotAttemptStatus"),
                    "imageUrl": asset.get("lastScreenshotUrl"),
                }
                if attempt
                else None
            )
            note = None
            if latest and not rows:
                note = (
                    "None of the recent snapshots the API returns carries a screenshot result; "
                    "latest is the most recent verification. Older results are not available."
                )
            return {
                "serialNumber": serialNumber.strip(),
                "agentId": agentId.strip(),
                "latest": latest,
                "screenshots": rows,
                "count": len(rows),
                "note": note,
                "lastScreenshotAttempt": asset.get("lastScreenshotAttempt"),
                "lastScreenshotAttemptAt": iso(asset.get("lastScreenshotAttempt")),
                "lastScreenshotAttemptStatus": asset.get("lastScreenshotAttemptStatus"),
                "lastScreenshotUrl": asset.get("lastScreenshotUrl"),
            }

        return await run_tool("datto_bcdr_list_screenshots", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_get_screenshot(
        serialNumber: SerialNumber,
        agentId: AgentId,
        epoch: Annotated[
            int | None,
            Field(description="Optional: Unix time of the screenshot wanted; the API only serves the latest one, so it must match it."),
        ] = None,
    ) -> Any:
        """Fetch the latest screenshot-verification image of an asset (Datto serves it as JPEG)."""

        async def action(client: DattoClient) -> Any:
            if epoch is not None:
                asset = await get_asset(client, serialNumber, agentId)
                latest = to_datetime(asset.get("lastScreenshotAttempt"))
                wanted = to_datetime(epoch)
                if latest is None or wanted is None or abs((latest - wanted).total_seconds()) > 60:
                    raise ValueError(
                        f"Datto only serves the latest screenshot (taken at {iso(asset.get('lastScreenshotAttempt')) or 'unknown'}, "
                        f"epoch {asset.get('lastScreenshotAttempt')}); older ones are not available. Omit epoch to get it."
                    )
            path = f"bcdr/device/{segment(serialNumber.strip())}/asset/{segment(agentId.strip())}/screenshot/latest"
            data, content_type = await client.get_binary(path)
            if not data:
                raise DattoError(404, "No screenshot is available for this asset yet")
            return Image(data=data, format=content_type.removeprefix("image/") if content_type.startswith("image/") else "png")

        return await run_tool("datto_bcdr_get_screenshot", client_factory, action)
