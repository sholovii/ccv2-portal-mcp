# ccv2-portal-mcp

An [MCP](https://modelcontextprotocol.io/) server for the **SAP Commerce Cloud (CCv2) Cloud
Portal**. It lets an AI assistant such as Claude see and operate the CCv2 control plane the
way a release engineer does: check what is deployed where, trigger builds, deploy with the
right guard rails, read build logs, manage backups, endpoints, certificates, scaling and
hibernation.

Everything goes through SAP's official Cloud Portal API. Nothing is scraped from the UI.

```
"What is deployed on staging and prod right now, and are they on the same branch?"
"Why did build 20260101.1 fail?"
"Run the pre-deploy checklist for p1 with build 20260101.1."
"Deploy it to s1 with a database update, take a quick backup first."
"Put p1 into maintenance mode."
"Roll d1 back to the previous build."
```

## Features

- **Full picture of an environment in one call**: status, services and replicas, the live
  build and branch, scaling, hibernation.
- **Builds and deployments**: list, inspect, trigger, wait. Deployment progress comes back by
  stage with the failed steps pulled out.
- **Build logs that fit in a context window**: the portal's log zip is unpacked and you ask
  for the first error, a grep, a head or a tail.
- **Guard rails for writes**: every environment has its own permission set, read-only by
  default. Production, data-wiping and downtime-causing actions require an explicit
  `confirm=true`, mirroring the portal's own CONFIRM prompt. A deployment is refused when the
  build did not succeed, another deployment is running, or the portal does not allow the
  chosen mode.
- **Backup before deploy**: one flag takes a quick database backup and deploys only once it
  is restorable.
- **Blue-green and canary**: accept or reject a green deployment, set the traffic split.
- **Rollback**: redeploys the previous build; shows the plan first, executes on confirm.
- **Pre-deploy checklist**: availability, permissions, running deployment, build status and
  branch, allowed modes, backup age, maintenance mode, certificate expiry, pending
  blue-green decision.
- **Build failure diagnosis**: classifies the first error (compile, tests, Spring context,
  missing branch, dependency, out of memory, transient) with the relevant log lines.
- **Maintenance mode** for all endpoints of an environment in one call.
- **Certificates**: upload, rotate across endpoints with a domain check, delete when unused.
- **Backups, restores and cross-environment data refresh**, scaling, scheduled hibernation.
- **Property diff** between environments and a **release timeline** of builds and
  deployments.
- **Bounded waits**: no tool call blocks for more than a minute; long operations are polled
  in short, repeatable calls.
- **Audit log**: every write attempt, including refused ones, is recorded as JSON with
  secrets masked.
- **Several subscriptions** from one server, each with its own technical user and
  permissions, configured from variables or a JSON file; every tool takes a `subscription`
  argument.
- **Status page**: a read-only page showing the server, the registered tools, each
  subscription's configuration and authentication state, every environment with its
  status, deployed build and the permissions the agent holds there, and the latest write
  calls from the audit log.

## Quick start

You need a **technical user** on the Cloud Portal (User Management → Technical Users) with
the `CUSTOMER_DEVELOPER` or `CUSTOMER_SYS_ADMIN` role and the environments it may access.
The portal shows its token endpoint URL, client id, client secret and resource once.

```bash
cp .env.example .env        # fill in the technical-user values and the subscription code
docker compose up -d --build
```

The server now listens on `http://localhost:8849/mcp` (streamable HTTP) and serves the
status page at `http://localhost:8849/`. Register it with Claude Code:

```bash
claude mcp add --transport http ccv2-portal http://localhost:8849/mcp
```

Without Docker, for local use over stdio:

```bash
uv run ccv2-portal-mcp
```

```bash
claude mcp add ccv2-portal -- uv --directory /path/to/ccv2-portal-mcp run ccv2-portal-mcp
```

Over stdio there is no HTTP port, so the status page runs as its own process:

```bash
uv run ccv2-portal-mcp ui          # http://127.0.0.1:8850/
```

By default every environment is read-only. Grant more per environment in `.env`:

```
CCV2_PERMISSIONS=d1:read,build,deploy,properties_write;s1:read,build,deploy;p1:read
```

## Documentation

- [docs/tools.md](docs/tools.md): every tool, what it does, which permission it needs and
  when it asks for confirmation.
- [docs/configuration.md](docs/configuration.md): environment variables, multi-subscription
  config, permissions, audit log, running modes, and the portal API gotchas the server
  handles.
- [docs/research.md](docs/research.md): the Cloud Portal API inventory, what the API cannot
  do, the tooling ecosystem and the design decisions behind this project.
- `spec/cloud-portal-api-2.0.12.yaml`: the official OpenAPI spec, extracted from SAP Help.

## Status

Read tools validated against a live subscription with `scripts/smoke.py`, which runs every
read tool in one command. Builds and rolling deployments with data migration have been run
live through the write tools. The remaining write tools (properties, endpoints,
certificates, backups, restores, scaling, scheduled activities, blue-green, cancellation)
are unit-tested against the API contract but not yet exercised live.

## Development

```bash
uv sync
uv run pytest
```

Tests use an in-memory fake portal; nothing talks to SAP.

## License

MIT
