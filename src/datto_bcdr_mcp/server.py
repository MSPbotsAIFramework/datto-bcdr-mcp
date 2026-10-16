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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import DattoClient, DattoCredentials
from .config import Settings

HEADER_PUBLIC_KEY = "X-Datto-BCDR-Public-Key"
HEADER_PRIVATE_KEY = "X-Datto-BCDR-Private-Key"
REQUIRED_HEADERS = [HEADER_PUBLIC_KEY, HEADER_PRIVATE_KEY]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[DattoCredentials | None] = contextvars.ContextVar(
    "datto_bcdr_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("datto_bcdr_request_id", default="-")

INSTRUCTIONS = (
    "Datto BCDR (SIRIS / ALTO appliances) backs up MSP clients' servers and "
    "workstations locally and replicates them to the Datto cloud. This server "
    "reads the partner portal: appliances, protected assets (agents and "
    "shares), snapshot history with verification results, screenshot "
    "verification, offsite sync, alerts and the partner activity log. It is "
    "read-only.\n"
    "Usage:\n"
    "- Start with datto_bcdr_list_devices; appliances are addressed by "
    "serialNumber.\n"
    "- datto_bcdr_list_assets(serialNumber) gives each protected machine; the "
    "agentId argument of the asset tools is the asset's volume name (its "
    "name / volume field).\n"
    "- Backup health: datto_bcdr_list_backups (snapshot status, local and "
    "screenshot verification) and datto_bcdr_list_screenshots; "
    "datto_bcdr_get_screenshot returns the latest verification image.\n"
    "- Fleet issues: datto_bcdr_list_alerts (all appliances, by date), "
    "datto_bcdr_get_offsite_status (cloud replication per asset).\n"
    "- Timestamps from Datto are Unix seconds unless shown as dates."
)


def get_client_from_context() -> DattoClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return DattoClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


class GatewayCredentialsMiddleware:
    """Reads the Datto key pair on /mcp requests into the request context; 401 when missing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        public_key = _clean(request.headers.get(HEADER_PUBLIC_KEY))
        private_key = _clean(request.headers.get(HEADER_PRIVATE_KEY))
        if not (public_key and private_key):
            missing = [h for h, v in ((HEADER_PUBLIC_KEY, public_key), (HEADER_PRIVATE_KEY, private_key)) if not v]
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                },
                status_code=401,
            )(scope, receive, send)
            return

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(DattoCredentials(public_key, private_key))
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="datto-bcdr-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], DattoClient | None] = get_client_from_context

    from .tools import activity, assets, devices

    for module in (devices, assets, activity):
        module.register(mcp, client_factory)

    return mcp
