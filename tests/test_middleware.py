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
"""Credential middleware: 401 on missing keys, per-request isolation."""

import asyncio
import base64

import pytest
from starlette.testclient import TestClient

from datto_bcdr_mcp.__main__ import build_http_app
from datto_bcdr_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
KEYS = {"X-Datto-BCDR-Public-Key": "public", "X-Datto-BCDR-Private-Key": "private"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Datto-BCDR-Public-Key": "public"},
        {"X-Datto-BCDR-Private-Key": "private"},
        {"X-Datto-BCDR-Public-Key": "public", "X-Datto-BCDR-Private-Key": "${DATTO_PRIVATE_KEY}"},
    ],
)
def test_missing_keys_return_401(headers):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required_headers"] == list(KEYS)


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | KEYS)
    assert resp.status_code == 200
    for name in ("datto_bcdr_list_devices", "datto_bcdr_get_screenshot", "datto_bcdr_list_activity"):
        assert name in resp.text


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def test_keys_reach_request_context_as_basic_auth_and_are_reset():
    seen = _run_middleware([(b"x-datto-bcdr-public-key", b" pub "), (b"x-datto-bcdr-private-key", b"priv")])
    headers = seen["client"]._headers("application/json")
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pub:priv").decode()
    assert seen["client"].base_url == "https://api.datto.com/v1"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().public_key
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().public_key))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(key: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": [(b"x-datto-bcdr-public-key", key), (b"x-datto-bcdr-private-key", b"p")]}

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a"), receive, send), middleware(scope(b"tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_keys():
    seen = _run_middleware([(b"x-datto-bcdr-public-key", b"pub-value"), (b"x-datto-bcdr-private-key", b"very-secret")])
    text = repr(seen["credentials"])
    assert "very-secret" not in text and "pub-value" not in text
