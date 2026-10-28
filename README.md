# ccv2-portal-mcp

MCP server for the **SAP Commerce Cloud (CCv2) Cloud Portal API**: builds, deployments,
environments, and later endpoints, backups, scaling and properties. Python + MCP SDK.

Background, API inventory and the roadmap live in [docs/research.md](docs/research.md). The
official OpenAPI spec is checked in at `spec/cloud-portal-api-2.0.12.yaml`.

## Status

Phase 1 complete against the API contract; **not yet exercised against a real subscription**
(needs a technical user). 40 tools.

### Read

| Tool | What it does |
|---|---|
| `ccv2portal_list_subscriptions` | configured subscriptions and per-environment permissions |
| `ccv2portal_list_environments` | environments with status, deployment and hibernation state |
| `ccv2portal_get_environment` | services/replicas, latest deployment + build, scaling, in one call |
| `ccv2portal_list_builds` / `ccv2portal_get_build` | builds, filter by branch / status; progress and deployments of one build |
| `ccv2portal_get_build_log` | unpacks the log zip; `errors` / `grep` / `tail` / `head` modes |
| `ccv2portal_list_deployments` / `ccv2portal_get_deployment` | deployments; progress by stage with failed steps |
| `ccv2portal_get_deployment_modes` | allowed DB modes and strategies per environment |
| `ccv2portal_get_deployment_decisions` | blue-green decisions, traffic split, cancellation options |
| `ccv2portal_get_property` | one customer property (secret-looking values masked) |
| `ccv2portal_list_endpoints` | endpoints with maintenance mode; `detail=true` for full config |
| `ccv2portal_list_certificates` | SSL certificates with days to expiry |
| `ccv2portal_list_backups` / `ccv2portal_list_restores` | data backups and restores |
| `ccv2portal_get_scaling_options` | current scaling and allowed values |
| `ccv2portal_list_scheduled_activities` | hibernation / wake-up / DR schedules |
| `ccv2portal_wait_for_build` / `ccv2portal_wait_for_deployment` | bounded polling (≤ `WAIT_MAX_SEC` per call) |

### Write (permission-gated, audited)

| Tool | Permission | `confirm=true` needed |
|---|---|---|
| `ccv2portal_create_build` | `build` on any env | – |
| `ccv2portal_create_deployment` | `deploy` (+ `deploy_prod`) | production, `INITIALIZE`, `RECREATE` |
| `ccv2portal_cancel_deployment` | `deploy` (+ `deploy_prod`) | always |
| `ccv2portal_blue_green_decision` | `deploy` (+ `deploy_prod`) | production |
| `ccv2portal_set_traffic_split` | `deploy` (+ `deploy_prod`) | production |
| `ccv2portal_set_property` | `properties_write` | production |
| `ccv2portal_create_backup` / `ccv2portal_cancel_backup` | `backup_restore` | – |
| `ccv2portal_delete_backup` | `backup_restore` | always |
| `ccv2portal_restore_backup` | `backup_restore` (+ `deploy_prod` on prod target) | always |
| `ccv2portal_cancel_restore` | `backup_restore` | always |
| `ccv2portal_update_scaling` | `scaling` | always |
| `ccv2portal_schedule_activity` / `reschedule` / `cancel` | `scaling` | hibernating production |

`create_deployment` additionally refuses non-SUCCESS builds, a running deployment on the
environment, and modes the portal does not allow for it.

### Helpers (what every pipeline re-implements)

| Tool | What it does |
|---|---|
| `ccv2portal_whats_deployed` | which build/branch is live where, with a plain-language comparison |
| `ccv2portal_rollback_environment` | plan (default) or execute a redeploy of the previous build with `NONE` |
| `ccv2portal_diagnose_build_failure` | classifies the first error in a failed build's log and returns evidence |
| `ccv2portal_pre_deploy_checklist` | status, permissions, running deployment, build, modes, backup age, maintenance mode, cert expiry, pending blue-green |
| `ccv2portal_compare_properties` | diff property keys between two environments |
| `ccv2portal_release_timeline` | builds and deployments of the last N days in one list |

### Audit

Every write (including refused ones) is appended as one JSON line to `AUDIT_LOG_PATH`
(`/data/audit.jsonl` in the container) and logged to stderr: timestamp, subscription,
tool, environment, arguments, portal response or error. Secret-looking keys and the values
of secret-looking properties are masked.

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
nothing. Write tools require `confirm=true` for production, `INITIALIZE`, `RECREATE`,
cancellations, restores and scaling, mirroring the portal's own CONFIRM prompt.

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
