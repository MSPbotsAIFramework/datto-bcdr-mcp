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
"""BCDR appliances (SIRIS / ALTO devices) and their offsite replication."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..api_client import DattoClient, segment
from ._common import READ_ONLY, ClientFactory, Page, PerPage, SerialNumber, iso, page_info, run_tool

_ASSET_LIMIT = 2000


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_list_devices(page: Page = None, perPage: PerPage = None) -> str:
        """List BCDR appliances: serial, name, model, client, last seen, storage, agent / alert counts."""

        async def action(client: DattoClient) -> dict:
            number, size = page or 1, perPage or 50
            result = await client.page("bcdr/device", number, size)
            return {"devices": result["items"], "count": len(result["items"])} | page_info(
                result["pagination"], number, size, len(result["items"])
            )

        return await run_tool("datto_bcdr_list_devices", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_get_device(serialNumber: SerialNumber) -> str:
        """Get one appliance by serial number, with its agents, shares and current alerts."""

        async def action(client: DattoClient) -> Any:
            return await client.get(f"bcdr/device/{segment(serialNumber.strip())}")

        return await run_tool("datto_bcdr_get_device", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def datto_bcdr_get_offsite_status(serialNumber: SerialNumber) -> str:
        """Get an appliance's offsite (cloud) replication: storage used and each asset's latest offsite point."""

        async def action(client: DattoClient) -> dict:
            serial = segment(serialNumber.strip())
            device = await client.get(f"bcdr/device/{serial}") or {}
            assets, truncated = await client.all_items(f"bcdr/device/{serial}/asset", _ASSET_LIMIT)
            rows = []
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                rows.append(
                    {
                        "name": asset.get("name"),
                        "volume": asset.get("volume"),
                        "isPaused": asset.get("isPaused"),
                        "isArchived": asset.get("isArchived"),
                        "latestOffsite": asset.get("latestOffsite"),
                        "latestOffsiteAt": iso(asset.get("latestOffsite")),
                        "lastSnapshotAt": iso(asset.get("lastSnapshot")),
                    }
                )
            never = [r["name"] or r["volume"] for r in rows if not r["latestOffsite"] and not r["isArchived"]]
            return {
                "serialNumber": serialNumber.strip(),
                "name": device.get("name") if isinstance(device, dict) else None,
                "offsiteStorageUsed": device.get("offsiteStorageUsed") if isinstance(device, dict) else None,
                "lastSeenDate": device.get("lastSeenDate") if isinstance(device, dict) else None,
                "assets": rows,
                "assetsWithoutOffsite": never,
                "truncated": truncated or None,
            }

        return await run_tool("datto_bcdr_get_offsite_status", client_factory, action)
