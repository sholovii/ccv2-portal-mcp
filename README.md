# ccv2-portal-mcp

MCP server for the **SAP Commerce Cloud (CCv2) Cloud Portal API**: builds, deployments,
environments, and later endpoints, backups, scaling and properties. Python + MCP SDK.

Background, API inventory and the roadmap live in [docs/research.md](docs/research.md). The
official OpenAPI spec is checked in at `spec/cloud-portal-api-2.0.12.yaml`.

## Status

Phase 1, step 1: read-only tools and bounded waits. No write tools yet.

| Tool | What it does |
|---|---|
| `ccv2portal_list_subscriptions` | configured subscriptions and per-environment permissions |
| `ccv2portal_list_environments` | environments with status, deployment and hibernation state |
| `ccv2portal_get_environment` | services/replicas, latest deployment + build, scaling, in one call |
| `ccv2portal_list_builds` | recent builds, filter by branch / status |
| `ccv2portal_get_build` | build details, progress, deployments made from it |
| `ccv2portal_get_build_log` | unpacks the log zip; `errors` / `grep` / `tail` / `head` modes |
| `ccv2portal_list_deployments` | deployments by environment / build / status |
| `ccv2portal_get_deployment` | details, build, progress by stage with failed steps |
| `ccv2portal_get_deployment_modes` | allowed DB modes and strategies per environment |
| `ccv2portal_wait_for_build` / `ccv2portal_wait_for_deployment` | bounded polling (≤ `WAIT_MAX_SEC` per call) |

## Requirements

- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/) for local runs; Docker for the service.
- A **technical user** on the Cloud Portal (User Management → Technical Users). API tokens
  stopped working with the March 2026 Kyma migration. The portal shows token endpoint URL,
  client id, client secret and `resource` once; secrets expire, only the creator can rotate them.

## Configuration

All settings are environment variables (`.env` locally, `env_file` in compose). See
[.env.example](.env.example).

Single subscription: the flat `CCV2_*` variables. Several subscriptions: point `CCV2_CONFIG`
at a JSON file like [config/config.example.json](config/config.example.json); `${VAR}`
placeholders in it are resolved from the environment so secrets never sit in the file.

### Permissions

Every environment has a permission set; unlisted environments get the default (`read`).

```
CCV2_PERMISSIONS=d1:read,build,deploy;s1:read,build,deploy;p1:read
```

Permissions: `read`, `build`, `deploy`, `deploy_prod`, `properties_write`, `endpoints_write`,
`scaling`, `backup_restore`. Read tools refuse environments without `read`, so a production
environment can be hidden from an agent entirely by leaving it out and setting the default to
nothing. Write tools (next step) will additionally require `confirm=true` for production,
`INITIALIZE` and `RECREATE`, mirroring the portal's own CONFIRM prompt.

## Run

Local, stdio:

```bash
uv run ccv2-portal-mcp
```

Docker (streamable HTTP on `http://localhost:8849/mcp`):

```bash
docker compose up -d --build
```

Claude Code, stdio:

```bash
claude mcp add ccv2-portal -- uv --directory /path/to/ccv2-portal-mcp run ccv2-portal-mcp
```

Claude Code, HTTP:

```bash
claude mcp add --transport http ccv2-portal http://localhost:8849/mcp
```

## Tests

```bash
uv run pytest
```

Tests use an in-memory `httpx.MockTransport`; nothing talks to SAP.

## Gotchas the server handles for you

- Host is `portalapi.commerce.ondemand.com`; the old `portalrotapi` host answers 302 and is
  refused in configuration.
- Auth header is `x-approuter-authorization`. An HTML login page in a response is reported as
  an auth error instead of a JSON parse failure.
- Listings are paged at 100; tools page internally.
- 429 responses are retried with backoff; 501 means the feature is disabled on the
  subscription (e.g. deployment cancellation).
