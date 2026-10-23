# Datto BCDR MCP Server

An [MCP](https://modelcontextprotocol.io) server for the BCDR part of the
[Datto](https://www.datto.com) partner REST API (SIRIS and ALTO backup appliances). It lets an
AI agent read appliances, the machines and shares they protect, snapshot history with local
and screenshot verification, the latest verification screenshot, offsite (cloud) replication,
alerts across the fleet and the partner portal activity log. It is read-only.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: the key pair arrives on every request as HTTP headers and is
  never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Datto-BCDR-Public-Key` | Yes | string | none | - | Datto partner portal REST API public key | `your-public-key` |
| `X-Datto-BCDR-Private-Key` | Yes | string | none | - | The secret key paired with it | `your-private-key` |

Where to get them: in the Datto partner portal open **Admin -> Integrations**, choose the REST
API option and create an API key; the portal shows the public key and the secret key.

How the keys are sent to Datto: every call is a GET to `https://api.datto.com/v1/...` with
HTTP Basic authentication (public key as user name, secret key as password).

A request to `/mcp` without both headers gets HTTP 401 naming the missing one.

## Tools

All tools return JSON except `datto_bcdr_get_screenshot`, which returns an image (Datto serves
it as JPEG). Appliances
are addressed by `serialNumber`; the `agentId` of the asset tools is the asset's volume name
(the `name` / `volume` field from `datto_bcdr_list_assets`). Datto timestamps are Unix seconds;
the tools add ISO 8601 `...At` fields next to them.

The list tools with `page` / `perPage` also take `offset` (record position, overrides `page`) and
return `offset` / `next_offset`: a page cut to fit the response size limit has no other page holding
its rest, so `next_offset` starts right after the last record returned; pass it as `offset` to
continue without gaps.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `datto_bcdr_list_devices` | Appliances: serial, name, model, client, last seen, storage, agent / alert counts | page, perPage (max 250), offset |
| `datto_bcdr_get_device` | One appliance with its agents, shares and current alerts | **serialNumber** |
| `datto_bcdr_list_assets` | Machines and shares an appliance protects, with last snapshot, offsite and screenshot state | **serialNumber** |
| `datto_bcdr_get_asset` | One protected asset: OS, agent version, volumes, paused / archived, last snapshot, offsite | **serialNumber**, **agentId** |
| `datto_bcdr_list_backups` | An asset's snapshots, newest first: backup status, errors, local and screenshot verification | **serialNumber**, **agentId** |
| `datto_bcdr_list_screenshots` | Screenshot (boot) verification: the latest attempt (`latest`), plus per-snapshot results of the recent snapshots the API returns (about the last 10) | **serialNumber**, **agentId** |
| `datto_bcdr_get_screenshot` | The latest screenshot-verification image (JPEG) | **serialNumber**, **agentId**, epoch |
| `datto_bcdr_get_offsite_status` | Offsite storage used and each asset's latest offsite point; lists assets never sent offsite | **serialNumber** |
| `datto_bcdr_list_alerts` | Alerts currently open on all appliances, raised in a window (default last 7 days), newest first; cleared alerts are not returned (no history) | since, until, page, perPage, offset |
| `datto_bcdr_list_activity` | Partner portal activity log (who did what, on which target), default last 7 days, at most the last 31 days | since, until, page, perPage, offset, clientName |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Datto returned HTTP 401 for bcdr/device", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Every call is a read, so 429, 5xx and network errors are
retried with backoff (honouring `Retry-After`). Datto's documented limit is 10,000 requests per
hour per organization.

## Run locally

```bash
uv sync
uv run python -m datto_bcdr_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t datto-bcdr-mcp:dev .
docker run --rm -p 8080:8080 datto-bcdr-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). The keys are never read from
the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Datto-BCDR-Public-Key: your-public-key" \
  -H "X-Datto-BCDR-Private-Key: your-private-key" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# snapshots of one protected server
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Datto-BCDR-Public-Key: your-public-key" \
  -H "X-Datto-BCDR-Private-Key: your-private-key" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"datto_bcdr_list_backups","arguments":{"serialNumber":"your-serial","agentId":"SRV01"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (10).** Kept as is because the tool names are an existing contract.
- **Built on the documented endpoints.** Older versions called routes that are not in Datto's
  API contract (`.../asset/{id}/backup`, `.../screenshot`, `.../screenshot/{epoch}`,
  `.../offsite`). This version uses only documented routes: snapshots and screenshot results
  come from the asset record's `backups`, the image from `.../screenshot/latest`, offsite state
  from the device and asset records.
- **`datto_bcdr_get_screenshot`.** The API serves only the latest screenshot, so `epoch` is now
  optional; when given, it must match the latest screenshot's time or the tool says which time
  is available.
- **No interactive prompts.** Older versions asked the user for a serial number or date window
  mid-call, which a stateless gateway cannot relay. `datto_bcdr_list_assets` now requires
  `serialNumber`, and the alert / activity tools default to the last 7 days.
- **Activity log.** Datto keeps 31 days of activity (`since` must be under 744 hours); an older
  `since` is moved to 31 days ago and the result says `since_clamped`. Datto's `clientName`
  parameter returns no rows even for an exact name, so `clientName` is matched here
  (case-insensitive, contains) over up to 1,000 entries of the window, then paged.
- **Screenshot history.** The API has no screenshot history: per-snapshot results come from the
  recent snapshots in the asset record (about the last 10), so `datto_bcdr_list_screenshots`
  returns the latest attempt as `latest` and says so when the per-snapshot list is empty.
- **Fleet alerts.** `datto_bcdr_list_alerts` returns the alerts currently open (Datto has no
  alert history). It reads the alerts of every appliance whose alert count is not zero (up to 1,000 appliances, 5 at a time) and pages the combined result.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
