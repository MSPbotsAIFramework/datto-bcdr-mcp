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
"""Request shapes, paging, retries and tool behaviour against a mocked Datto API."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from datto_bcdr_mcp import api_client
from datto_bcdr_mcp.api_client import DattoClient, DattoCredentials
from datto_bcdr_mcp.server import _credentials_var, create_mcp_server

CREDS = DattoCredentials("pub", "priv")
PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


def paged(items, page=1, total_pages=1):
    return {"pagination": {"page": page, "perPage": 250, "totalPages": total_pages, "count": len(items)}, "items": items}


class Recorder:
    """Mock transport: replies by path below /v1/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else paged([])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?")[0].removeprefix("/v1/")
        reply = self.routes.get(path, self.default)
        if callable(reply):
            return reply(request)
        if isinstance(reply, bytes):
            return httpx.Response(200, content=reply, headers={"content-type": "image/png"})
        return httpx.Response(200, json=reply)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.raw_path.decode().split("?")[0] == f"/v1/{path}"]


async def _call_raw(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        result = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    # Tools without an output schema (the image tool) return the content list alone.
    return result[0] if isinstance(result, tuple) else result


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    return json.loads((await _call_raw(monkeypatch, recorder, tool, args))[0].text)


def _client(recorder: Recorder) -> DattoClient:
    return DattoClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


def ts(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


ASSET = {
    "name": "SRV01",
    "volume": "SRV01",
    "lastSnapshot": 1780272000,
    "latestOffsite": 1780268400,
    "lastScreenshotAttempt": 1780272300,
    "lastScreenshotAttemptStatus": True,
    "backups": [
        {
            "timestamp": "2026-06-01T00:00:00+00:00",
            "backup": {"status": "success"},
            "advancedVerification": {"screenshotVerification": {"status": "success", "image": "https://img/1"}},
        },
        {"timestamp": "2026-06-02T00:00:00+00:00", "backup": {"status": "failed", "errorMessage": "VSS"}},
    ],
}


# ---- client ----------------------------------------------------------------


async def test_basic_auth_and_paging_params():
    rec = Recorder({"bcdr/device": paged([{"serialNumber": "S1"}], total_pages=3)})
    result = await _client(rec).page("bcdr/device", 2, 100)
    request = rec.requests[0]
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"pub:priv").decode()
    assert (request.url.params["_page"], request.url.params["_perPage"]) == ("2", "100")
    assert result["items"] == [{"serialNumber": "S1"}]


async def test_all_items_follows_total_pages():
    pages = {"1": paged([{"i": 1}], 1, 2), "2": paged([{"i": 2}], 2, 2)}
    rec = Recorder({"bcdr/device": lambda r: httpx.Response(200, json=pages[r.url.params["_page"]])})
    items, truncated = await _client(rec).all_items("bcdr/device", 100)
    assert items == [{"i": 1}, {"i": 2}] and truncated is False


async def test_reads_retry_429_and_5xx():
    replies = iter([httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(503), httpx.Response(200, json=paged([]))])
    rec = Recorder({"bcdr/device": lambda r: next(replies)})
    await _client(rec).page("bcdr/device")
    assert len(rec.requests) == 3


async def test_bad_keys_are_unauthorized(monkeypatch):
    rec = Recorder({"bcdr/device": lambda r: httpx.Response(401, json={"code": "401", "message": "Unauthorized"})})
    result = await _call(monkeypatch, rec, "datto_bcdr_list_devices", {})
    assert result["error"]["code"] == "unauthorized"
    assert "priv" not in json.dumps(result)


# ---- devices ---------------------------------------------------------------


async def test_list_devices_reports_paging(monkeypatch):
    rec = Recorder({"bcdr/device": paged([{"serialNumber": "S1"}], total_pages=2)})
    result = await _call(monkeypatch, rec, "datto_bcdr_list_devices", {"page": 1, "perPage": 1})
    assert result["devices"] == [{"serialNumber": "S1"}]
    assert (result["has_more"], result["total_pages"]) == (True, 2)


async def test_offsite_status_combines_device_and_assets(monkeypatch):
    rec = Recorder(
        {
            "bcdr/device/S1": {"serialNumber": "S1", "name": "Office", "offsiteStorageUsed": {"size": 5, "units": "TB"}},
            "bcdr/device/S1/asset": paged([ASSET, {"name": "PC02", "latestOffsite": None, "isArchived": False}]),
        }
    )
    result = await _call(monkeypatch, rec, "datto_bcdr_get_offsite_status", {"serialNumber": "S1"})
    assert result["offsiteStorageUsed"] == {"size": 5, "units": "TB"}
    assert result["assets"][0]["latestOffsiteAt"] == "2026-05-31T23:00:00Z"
    assert result["assetsWithoutOffsite"] == ["PC02"]


# ---- assets ----------------------------------------------------------------


async def test_list_assets_drops_snapshot_arrays(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset": paged([ASSET])})
    result = await _call(monkeypatch, rec, "datto_bcdr_list_assets", {"serialNumber": "S1"})
    asset = result["assets"][0]
    assert "backups" not in asset and asset["snapshotCount"] == 2


async def test_asset_volume_names_are_path_encoded(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset/C%3A%2F%20data": [ASSET]})
    await _call(monkeypatch, rec, "datto_bcdr_get_asset", {"serialNumber": "S1", "agentId": "C:/ data"})
    assert rec.calls("bcdr/device/S1/asset/C%3A%2F%20data")


async def test_list_backups_newest_first_and_counts_failures(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset/SRV01": {"items": ASSET}})
    result = await _call(monkeypatch, rec, "datto_bcdr_list_backups", {"serialNumber": "S1", "agentId": "SRV01"})
    assert [b["backup"]["status"] for b in result["backups"]] == ["failed", "success"]
    assert result["failedCount"] == 1


async def test_list_screenshots_from_verification(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset/SRV01": [ASSET]})
    result = await _call(monkeypatch, rec, "datto_bcdr_list_screenshots", {"serialNumber": "S1", "agentId": "SRV01"})
    assert result["screenshots"] == [{"timestamp": "2026-06-01T00:00:00+00:00", "status": "success", "image": "https://img/1"}]
    assert result["lastScreenshotAttemptStatus"] is True


async def test_get_screenshot_returns_png(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset/SRV01/screenshot/latest": PNG})
    content = await _call_raw(monkeypatch, rec, "datto_bcdr_get_screenshot", {"serialNumber": "S1", "agentId": "SRV01"})
    assert content[0].type == "image"
    assert content[0].mimeType == "image/png"
    assert base64.b64decode(content[0].data) == PNG
    assert rec.requests[0].headers["Accept"].startswith("image/png")


async def test_get_screenshot_with_old_epoch_explains(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset/SRV01": [ASSET]})
    result = await _call(monkeypatch, rec, "datto_bcdr_get_screenshot", {"serialNumber": "S1", "agentId": "SRV01", "epoch": 1700000000})
    assert result["error"]["code"] == "invalid_argument"
    assert "1780272300" in result["error"]["message"]
    assert not rec.calls("bcdr/device/S1/asset/SRV01/screenshot/latest")


async def test_get_screenshot_with_matching_epoch(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset/SRV01": [ASSET], "bcdr/device/S1/asset/SRV01/screenshot/latest": PNG})
    content = await _call_raw(monkeypatch, rec, "datto_bcdr_get_screenshot", {"serialNumber": "S1", "agentId": "SRV01", "epoch": 1780272300})
    assert content[0].type == "image"


async def test_missing_asset_is_not_found(monkeypatch):
    rec = Recorder({"bcdr/device/S1/asset/NOPE": []})
    result = await _call(monkeypatch, rec, "datto_bcdr_get_asset", {"serialNumber": "S1", "agentId": "NOPE"})
    assert result["error"]["code"] == "not_found"


# ---- alerts / activity -----------------------------------------------------


async def test_alerts_scan_devices_with_alerts_and_filter_by_date(monkeypatch):
    rec = Recorder(
        {
            "bcdr/device": paged([{"serialNumber": "S1", "name": "A", "alertCount": 2}, {"serialNumber": "S2", "alertCount": 0}]),
            "bcdr/device/S1/alert": paged([{"type": "offsite", "dateTriggered": ts(1)}, {"type": "old", "dateTriggered": ts(30)}]),
        }
    )
    result = await _call(monkeypatch, rec, "datto_bcdr_list_alerts", {})
    assert [a["type"] for a in result["alerts"]] == ["offsite"]
    assert result["alerts"][0]["serialNumber"] == "S1" and result["alerts"][0]["deviceName"] == "A"
    assert result["devicesScanned"] == 1
    assert not rec.calls("bcdr/device/S2/alert")


async def test_alerts_window_validation(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "datto_bcdr_list_alerts", {"since": "2026-06-02T00:00:00Z", "until": "2026-06-01T00:00:00Z"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_activity_sends_since_in_hours(monkeypatch):
    rec = Recorder({"report/activity-log": paged([{"timestamp": ts(0.2), "action": "login"}])})
    since = (datetime.now(UTC) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = await _call(monkeypatch, rec, "datto_bcdr_list_activity", {"since": since})
    params = rec.requests[0].url.params
    assert params["sinceUnits"] == "hours" and "clientName" not in params
    assert params["since"] in ("10", "11")
    assert result["activity"][0]["action"] == "login"



# ---- PRD-20992: clientName is matched here ------------------------------------------------


def _activity_log(entries):
    def reply(request):
        number, size = int(request.url.params["_page"]), int(request.url.params["_perPage"])
        pages = max(1, -(-len(entries) // size))
        return httpx.Response(200, json=paged(entries[(number - 1) * size : number * size], number, pages))

    return reply


async def test_activity_client_name_is_matched_locally(monkeypatch):
    entries = [{"timestamp": ts(1), "clientName": "Winston Insurance", "action": f"a{i}"} for i in range(11)]
    entries += [{"timestamp": ts(1), "clientName": "Other Co", "action": "x"}]
    rec = Recorder({"report/activity-log": _activity_log(entries)})
    for name in ("Winston Insurance", "winston insurance", "Winston"):
        result = await _call(monkeypatch, rec, "datto_bcdr_list_activity", {"clientName": name})
        assert result["total"] == 11 and result["count"] == 11 and result["has_more"] is False
        assert {e["clientName"] for e in result["activity"]} == {"Winston Insurance"}
    assert all("clientName" not in r.url.params for r in rec.requests)
    none = await _call(monkeypatch, rec, "datto_bcdr_list_activity", {"clientName": "Nobody"})
    assert (none["total"], none["count"]) == (0, 0)


async def test_activity_client_name_pages_the_matches(monkeypatch):
    entries = [{"timestamp": ts(1), "clientName": "Winston Insurance", "action": f"a{i}"} for i in range(5)]
    rec = Recorder({"report/activity-log": _activity_log(entries)})
    second = await _call(monkeypatch, rec, "datto_bcdr_list_activity", {"clientName": "winston", "page": 2, "perPage": 2})
    assert [e["action"] for e in second["activity"]] == ["a2", "a3"]
    assert (second["total"], second["has_more"]) == (5, True)


# ---- PRD-20993 (1): the activity log covers 31 days -----------------------------------------


async def test_activity_since_beyond_31_days_is_clamped(monkeypatch):
    rec = Recorder({"report/activity-log": paged([])})
    result = await _call(monkeypatch, rec, "datto_bcdr_list_activity", {"since": "2026-01-01T00:00:00Z"})
    assert rec.requests[0].url.params["since"] == "743"
    assert result["since_clamped"] is True and "31 days" in result["note"]


async def test_activity_until_beyond_31_days_is_explained(monkeypatch):
    rec = Recorder()
    result = await _call(
        monkeypatch, rec, "datto_bcdr_list_activity", {"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"}
    )
    assert result["error"]["code"] == "invalid_argument" and "31 days" in result["error"]["message"]
    assert rec.requests == []


# ---- PRD-20991: the latest screenshot attempt is the main result -----------------------------


async def test_list_screenshots_reports_latest_when_snapshots_have_none(monkeypatch):
    asset = dict(ASSET, lastScreenshotUrl="https://img/latest",
                 backups=[{"timestamp": ts(0.01 * i), "backup": {"status": "success"}} for i in range(10)])
    rec = Recorder({"bcdr/device/S1/asset/SRV01": [asset]})
    result = await _call(monkeypatch, rec, "datto_bcdr_list_screenshots", {"serialNumber": "S1", "agentId": "SRV01"})
    assert result["count"] == 0
    assert result["latest"] == {
        "at": "2026-06-01T00:05:00Z", "epoch": 1780272300, "status": True, "imageUrl": "https://img/latest"
    }
    assert "latest" in result["note"]
