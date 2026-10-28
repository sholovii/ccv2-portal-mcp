# Tools

All tools carry the `ccv2portal_` prefix. Every tool takes an optional `subscription`
argument; it can be omitted when one subscription is configured or a default is set.

Permissions are granted per environment in the configuration (see
[configuration.md](configuration.md)). Read tools need `read`; write tools need the
permission in the table and, where noted, `confirm=true`.

## Read

| Tool | What it does |
|---|---|
| `list_subscriptions` | configured subscriptions and the per-environment permissions granted to the agent |
| `list_environments` | environments with type, status, deployment status, hibernation status and granted permissions |
| `get_environment` | one call: status, services with desired/available replicas, latest deployment and the build it carries, scaling. Partial failures land in `*_error` fields |
| `list_builds` | recent builds, newest first; filter by branch substring and status |
| `get_build` | build details, progress (percentage, tasks, error message), deployments made from it |
| `get_build_log` | unpacks the log zip. `mode=errors` (default) returns a window around the first error; `grep` with `pattern` and `context`; `tail` / `head` with `lines` |
| `list_deployments` | deployments, newest first; filter by environment, build code, status |
| `get_deployment` | deployment details, the build, progress by stage with failed steps extracted |
| `get_deployment_modes` | database update modes and strategies the portal allows per environment |
| `get_deployment_decisions` | blue-green decisions, traffic split and cancellation options of a deployment |
| `get_property` | one customer property of a service aspect; secret-looking values are masked unless `reveal=true` |
| `list_endpoints` | endpoints with URL, service, web proxy and maintenance-mode state; `detail=true` returns the full configuration |
| `list_certificates` | SSL certificates with issuer, domains, endpoints using them and computed days to expiry |
| `list_backups` / `list_restores` | data backups and restores of an environment with status flags |
| `get_scaling_options` | current scaling and the allowed values |
| `list_scheduled_activities` | hibernation, wake-up and DR package activities |
| `wait_for_build` / `wait_for_deployment` / `wait_for_backup` | poll until a terminal state or the bounded timeout (`WAIT_MAX_SEC`, default 60 s). Return `done=false` with a hint to call again |

## Write

| Tool | Permission | Confirmation required |
|---|---|---|
| `create_build` | `build` on any environment | – |
| `create_deployment` | `deploy`, plus `deploy_prod` on production | production, `INITIALIZE`, `RECREATE` |
| `cancel_deployment` | `deploy`, plus `deploy_prod` on production | always |
| `blue_green_decision` | `deploy`, plus `deploy_prod` on production | production |
| `set_traffic_split` | `deploy`, plus `deploy_prod` on production | production |
| `set_property` | `properties_write` | production |
| `set_maintenance_mode` | `endpoints_write` | production |
| `upload_certificate` | `endpoints_write` on any environment | – |
| `rotate_certificate` | `endpoints_write` | production |
| `delete_certificate` | `endpoints_write` on any environment | always |
| `create_backup` / `cancel_backup` | `backup_restore` | – |
| `delete_backup` | `backup_restore` | always |
| `restore_backup` | `backup_restore`, plus `deploy_prod` for a production target | always |
| `cancel_restore` | `backup_restore` | always |
| `update_scaling` | `scaling` | always |
| `schedule_activity` / `reschedule_activity` / `cancel_activity` | `scaling` | hibernating production |

Notes on behaviour:

- `create_deployment` refuses a build whose status is not `SUCCESS`, an environment with a
  deployment already running, and a mode or strategy the portal does not list as allowed.
  With `backup_first=true` it takes a QUICK database backup first and deploys only once the
  backup is restorable; if the backup is still running after the bounded wait, the
  deployment is not started and the backup code is returned for `wait_for_backup`.
- `cancel_deployment` only works once the portal's "Deploy application" stage is reached,
  causes downtime, and answers HTTP 501 when cancellation is not enabled for the
  subscription. `rollback_database=true` reverts the database to the restore point the
  portal offers.
- `set_maintenance_mode` changes every endpoint of the environment, or the listed ones,
  skipping endpoints already in the requested state. It uses the bulk endpoint patch and
  falls back to one patch per endpoint.
- `rotate_certificate` re-points endpoints at a new certificate after checking that the
  certificate's domains cover each endpoint's domain name. `delete_old=true` removes the
  previous certificate once no endpoint uses it.
- `restore_backup` accepts a `source_environment` different from the target, which is how a
  data refresh (for example production into staging) is done.
- `set_property` stores the value through the API. Depending on the aspect, the portal may
  still need "apply configuration" or a service restart before it is live.

## Helpers

| Tool | What it does |
|---|---|
| `whats_deployed` | which build, branch and version is live on each environment, who deployed it and when, with a plain-language comparison and any deployment in progress |
| `rollback_environment` | finds the previous successful deployment's build and redeploys it with `NONE`. Returns the plan by default; `confirm=true` executes. Needs `deploy` (and `deploy_prod`). Database schema changes are not reverted |
| `diagnose_build_failure` | classifies the first error of a failed build (branch-not-found, compile-error, test-failure, spring-context, out-of-memory, manifest, dependency, transient) and returns the evidence lines and a suggestion |
| `pre_deploy_checklist` | environment status and hibernation, agent permissions, running deployment, build status and branch, allowed modes, last backup age, maintenance mode, certificate expiry, pending blue-green decision. `ready=true` when nothing failed |
| `compare_properties` | values of the given keys in two environments, with differences counted; secret-looking values compared but masked |
| `release_timeline` | builds and deployments of the last N days in one chronological list, filterable by environment and branch |

## Audit

Every write tool call, including ones refused by a guard or by the portal, appends one JSON
line to `AUDIT_LOG_PATH` and logs it to stderr: timestamp, subscription, tool, environment,
arguments, result or error. Keys that look like secrets (`secret`, `password`, `token`,
`private_key`, `api_key`) and the values of secret-looking properties are masked.
