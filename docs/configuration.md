# Configuration

All settings are environment variables, read from `.env` for local runs and from the
`env_file` in `docker-compose.yml` for the container. [.env.example](../.env.example) lists
them.

## Technical user

The Cloud Portal API authenticates **technical users** only. API tokens stopped working
with the portal's move to Kyma in March 2026.

1. In the Cloud Portal open User Management → Technical Users → Create.
2. Give it the `CUSTOMER_DEVELOPER` role (reads, builds, deployments) or
   `CUSTOMER_SYS_ADMIN` (everything), and tick the environments it may access.
3. Copy the token endpoint URL, client id, client secret and resource. The portal shows the
   secret once. Secrets expire; only the user who created the technical user can rotate it,
   and a portal user may own at most two technical users.

The subscription code is the segment after `subscription/` in the Cloud Portal URL.

## One subscription

```
CCV2_SUBSCRIPTION_NAME=acme
CCV2_SUBSCRIPTION_CODE=<code>
CCV2_CLIENT_ID=<client id>
CCV2_CLIENT_SECRET=<client secret>
CCV2_TOKEN_URL=https://ycloud.accounts.ondemand.com/oauth2/token
CCV2_RESOURCE=<resource>
CCV2_PERMISSIONS=d1:read,build,deploy;s1:read,build,deploy;p1:read
CCV2_DEFAULT_PERMISSIONS=read
```

## Several subscriptions

Every tool takes a `subscription` argument naming one of the configured subscriptions; it
may be omitted when a default is set or only one is configured. Each subscription has its
own technical user, base URL and permissions, and gets its own token cache.

### From environment variables

List the names in `CCV2_SUBSCRIPTIONS`; each name becomes an upper-case prefix
(`acme` → `ACME_`, `globex-eu` → `GLOBEX_EU_`).

```
CCV2_SUBSCRIPTIONS=acme,globex-eu
CCV2_DEFAULT_SUBSCRIPTION=acme
CCV2_TOKEN_URL=https://ycloud.accounts.ondemand.com/oauth2/token

ACME_SUBSCRIPTION_CODE=<code>
ACME_CLIENT_ID=<client id>
ACME_CLIENT_SECRET=<client secret>
ACME_RESOURCE=<resource>
ACME_PERMISSIONS=d1:read,build,deploy;s1:read,build,deploy;p1:read

GLOBEX_EU_SUBSCRIPTION_CODE=<code>
GLOBEX_EU_CLIENT_ID=<client id>
GLOBEX_EU_CLIENT_SECRET=<client secret>
GLOBEX_EU_RESOURCE=<resource>
GLOBEX_EU_PERMISSIONS=d1:read
```

`<PREFIX>_TOKEN_URL`, `<PREFIX>_DEFAULT_PERMISSIONS` and `<PREFIX>_BASE_URL` are optional and
fall back to `CCV2_TOKEN_URL`, `CCV2_DEFAULT_PERMISSIONS` and `CCV2_BASE_URL`. With a single
name it is the default automatically.

### From a JSON file

Point `CCV2_CONFIG` at a JSON file. String values may contain `${VAR}` placeholders, resolved
from the environment, so secrets stay out of the file. See
[config/config.example.json](../config/config.example.json).

```json
{
  "default_subscription": "acme",
  "subscriptions": {
    "acme": {
      "code": "...",
      "client_id": "${ACME_CLIENT_ID}",
      "client_secret": "${ACME_CLIENT_SECRET}",
      "token_url": "https://ycloud.accounts.ondemand.com/oauth2/token",
      "resource": "${ACME_RESOURCE}",
      "default_permissions": ["read"],
      "permissions": { "d1": ["read", "build", "deploy"], "p1": ["read"] }
    }
  }
}
```

In the container, mount the file under `./config` and set `CCV2_CONFIG=/config/config.json`.

## Permissions

Each environment code maps to a set of permissions. Environments not listed get
`CCV2_DEFAULT_PERMISSIONS` (default `read`). Set the default to empty to hide unlisted
environments entirely.

| Permission | Allows |
|---|---|
| `read` | every read tool on that environment |
| `build` | `create_build` (subscription-wide; granted if any environment has it) |
| `deploy` | deployments, cancellations, blue-green decisions, traffic split, rollback; on production every such call also needs `confirm=true` |
| `properties_write` | `set_property` |
| `endpoints_write` | maintenance mode, certificate upload / rotation / deletion |
| `backup_restore` | backups, restores, `backup_first` on deployments |
| `scaling` | scaling changes and scheduled activities (hibernate, wake-up, DR) |

Independently of permissions, production, `INITIALIZE`, `RECREATE`, cancellations, restores,
deletions and scaling changes require `confirm=true` on the call.

## Other settings

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` for local use, `streamable-http` for the service (the container forces it) |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8849` | HTTP bind address |
| `HTTP_TIMEOUT_SEC` | `30` | per-request timeout against the portal |
| `WAIT_MAX_SEC` | `60` | upper bound of any `wait_*` call and of `backup_first` |
| `WAIT_POLL_SEC` | `15` | polling interval inside a wait |
| `BUILD_LOG_MAX_LINES` | `400` | lines returned by `get_build_log` at most |
| `RETRY_429_MAX` | `3` | retries on HTTP 429 |
| `AUDIT_LOG_PATH` | empty | JSONL audit file; empty means stderr only |
| `UI_HOST` / `UI_PORT` | `127.0.0.1` / `8850` | bind address of the standalone status page (`ccv2-portal-mcp ui`) |
| `UI_CACHE_SEC` | `30` | how long the status page caches live portal data |
| `UI_AUDIT_LINES` | `25` | audit entries shown on the status page |

## Running

Docker, streamable HTTP on port 8849:

```bash
docker compose up -d --build
```

Local, stdio:

```bash
uv run ccv2-portal-mcp
```

Claude Code registration:

```bash
claude mcp add --transport http ccv2-portal http://localhost:8849/mcp
```

```bash
claude mcp add ccv2-portal -- uv --directory /path/to/ccv2-portal-mcp run ccv2-portal-mcp
```

## Status page

A read-only page with the server facts (version, transport, uptime, settings, registered
tools), each subscription's configuration (code, masked client id, whether secret and
resource are set, configured permissions) and live state (token check, every environment
the portal returns with status, deployment and hibernation state, the deployed build and
branch, any running deployment, the permissions the agent holds there), plus the newest
entries of the audit log. It never changes anything and shows no secrets.

- Under `MCP_TRANSPORT=streamable-http` (the container) it is served by the MCP server
  itself at `/` on the MCP port: `http://localhost:8849/`. `/api/status` returns the same
  data as JSON and `/healthz` answers `{"ok": true}`.
- Under stdio run `ccv2-portal-mcp ui` (options `--host`, `--port`); it reads the same
  configuration and serves `http://127.0.0.1:8850/`. It cannot tell whether the stdio
  server inside the AI client is running, and says so.

Live data is cached for `UI_CACHE_SEC`; the Refresh button bypasses the cache. The page
reloads itself every minute. The page has no authentication: it shows subscription codes,
environment names and deployed builds, so keep it bound to localhost or a private network.
The page only displays configuration; `.env` or the JSON file remain the source of truth.

## Portal API behaviour the server handles

- The API host is `portalapi.commerce.ondemand.com`. The older `portalrotapi` host answers a
  redirect; the configuration refuses it.
- The token goes in the `x-approuter-authorization` header, not `Authorization`. With the
  wrong header, a missing role or an expired secret the portal returns an HTML login page
  with HTTP 200; the server reports that as an authentication error.
- Tokens are cached and refreshed 30 seconds before expiry; a 401 triggers one refresh.
- Listings are paged at 100 items; tools page internally.
- HTTP 403 means the technical user's role or environment list does not cover the call.
  Two cases seen live: scaling reads are refused for a `CUSTOMER_DEVELOPER` technical
  user, and the deployment-modes listing is refused when the technical user lacks *any*
  environment of the subscription, since that call is subscription-wide. Tools that depend
  on it (deployment validation, the pre-deploy checklist) then report the modes as unknown
  instead of failing.
- HTTP 429 is retried with backoff. HTTP 501 means a feature such as deployment cancellation
  is not enabled for the subscription. Other errors carry the portal's RFC 7807 title and
  detail plus a plain-language hint.
- The portal has no webhooks, no build cancellation and no rollback primitive. Waits are
  bounded polling; rollback is a redeploy of the previous build.

## What the API cannot do

Environment and deployment logs (OpenSearch), service restarts, database copies for local
development, point-in-time restores of SAP's own automated backups, hot-folder storage keys,
IP filter set uploads, DNS and CDN, user creation. These stay in the portal UI or go through
SAP support.
