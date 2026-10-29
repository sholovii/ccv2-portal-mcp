# CCv2 Cloud Portal MCP server — research

Date: 2026-10-28. Scope: what the official SAP Commerce Cloud (CCv2) Cloud Portal API
allows, what already exists in the ecosystem, and what an MCP server should wrap.
The full OpenAPI spec (v2.0.12, 52 operations) is checked in at
`spec/cloud-portal-api-2.0.12.yaml` (extracted from the SAP Help page; SAP publishes it
only inside the rendered page).

## 1. Where this fits

Operating CCv2 has three surfaces: the running application (HAC, OCC), observability (logs
in OpenSearch, Dynatrace), and the **control plane** in the Cloud Portal — builds,
deployments, environments, endpoints, backups, scaling, properties. MCP servers exist for
the first surface (several HAC/OCC servers on GitHub and the registries) and the second can
be covered by the Dynatrace Managed MCP, but **no MCP server (public or registry-listed)
wraps the Cloud Portal API today**. Every SAP Commerce MCP found wraps HAC or OCC. SAP's own
announced MCP work (Oct 2026 blog) targets OCC / storefront agents, not the portal. That is
the gap this project fills.

## 2. The official Cloud Portal API

- Docs: SAP Help "Cloud Portal APIs" (OpenAPI 3.0.2, info.version 2.0.12, labelled Beta),
  https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/452dcbb0e00f47e88a69cdaeb87a925d/66abfe678b55457fab235ce8039dda71.html
- Base URL: `https://portalapi.commerce.ondemand.com/v2/subscriptions/{subscriptionCode}/…`.
  The older `portalrotapi.hana.ondemand.com` host returns 302 now (KBA 3400008); most
  blogs and `unic/sapcc-toolkit` still hard-code it.
- Pagination: hard cap 100 items per page; OData-style `$top`, `$skip`, `$orderby`, `$count`.
- Errors: RFC 7807 `application/problem+json` `{title, detail}`. POST/PUT need
  `Content-Type: application/json` (KBA 3767430: 401 / 411 / unsupported media type).
- Rate limits: not documented. KBA 3250487 reports transient 429 + empty body on
  polling (SAP says fixed). Back off on 429 anyway.
- Subscription code = the segment after `subscription/` in the portal URL.

### 2.1 Authentication — API tokens are dead

The Cloud Portal moved from BTP Neo to Kyma (completed ~March 2026, What's New 6–7 Mar
2026). **API tokens stopped working after the migration** (KBA 3285272, 3707347).
Replacement: **Technical Users** (Cloud Portal → User Management → Technical Users).

- Creating one yields: token endpoint URL (SAP IAS, `https://ycloud.accounts.ondemand.com/oauth2/token`),
  client id, client secret, **resource**, expiration date. Max 2 technical users per
  portal user. Role per technical user: `CUSTOMER_SYS_ADMIN` or `CUSTOMER_DEVELOPER`,
  plus the list of environments it may touch.
- Only the creator can edit/delete/regenerate it; if the owner is removed, their
  technical users go too (KBA 3764102). Multiple secrets per user, each with an
  expiry (~3 months reported, KBA 3789335) — plan for rotation.
- Token request: OAuth2 `client_credentials`, form-encoded, **must include `resource`**
  (Swagger Editor cannot send it). Response `{access_token, token_type: Bearer, expires_in: 3600}`.
- **Header is `x-approuter-authorization: Bearer <token>`**, not `Authorization`.
  Wrong header → HTML redirect to the IAS login page with HTTP 200, "regardless of the
  token being valid" (KBA 3770030). Detect `text/html` and fail loudly.
- Permissions follow the technical user's role + environment list.

Anything built on `SAP_CCV2_API_TOKEN` (sap-cx-actions, Seidor Compass app, 2020–2024
blogs, archived ccv2ctl with S-user certs) is broken.

### 2.2 Operation inventory (from the checked-in spec + newer help pages)

| Group | Operations | Notes |
|---|---|---|
| **build** | getBuilds, createBuild, getBuild, deleteBuild, getBuildProgress, getBuildLogs | create: `{applicationCode ("commerce-cloud"), branch, name}` → `{code}`. Build DTO has branch, createdBy, status, buildVersion, deployed, properties[]. Progress: `{percentage, buildStatus, numberOfTasks, startedTasks[], errorMessage}`. Status: BUILDING / SUCCESS / FAIL / SCHEDULED / DELETED / UNKNOWN. Logs = **zip** via `Accept: application/octet-stream`. **No build cancel**. Undeployed builds soft-deleted after 6 months. |
| **deployment** | getDeploymentModes, getDeployments, createDeployment, getDeployment, getDeploymentProgress, getDeploymentCancellationOptions, createDeploymentCancellation, createDeploymentDecision, getDeploymentDecisions, getTrafficSplit, updateTrafficSplit, getTrafficSplitHistory | create: `{buildCode, environmentCode, databaseUpdateMode NONE/UPDATE/INITIALIZE, strategy ROLLING_UPDATE/RECREATE/GREEN}` → `{code}`; 409/412/423 when env busy/locked. getDeploymentModes says which modes each env allows. Status: SCHEDULED / DEPLOYING / DEPLOYED / UNDEPLOYED / FAIL. Progress has `stages[{name,type,status PENDING/RUNNING/DONE/FAIL,logLink,steps[...]}]`. Deployment DTO has `previousDeploymentCode`, `canaryStatus`, `cancelation{}`. Cancellation: `{rollbackDatabase, cancelReason}`; **feature-flagged — 501 unless enabled in the portal**; only after "Deploy application" stage; no DB rollback in live prod during rolling update. Decision: ACCEPT / REJECT / PREPARE_CANARY (blue-green). Traffic split: per service/endpoint percentages (canary, since Jul 2024). |
| **environment** | getEnvironments | `{code, name, type, status PROVISIONING/AVAILABLE/…, deploymentStatus, hibernationStatus}`. No get-by-code, no health endpoint. |
| **services** | getServices (Aug 2026, help page only, not in yaml) | `[{code, name, runnable, desiredReplicas, availableReplicas}]`. Help page shows the path without `/subscriptions/` — verify. No start/stop/restart. |
| **serviceProperties** | getProperty, putProperty | one property at a time per `{env}/services/{serviceCode}/properties/{key}`; `{key, value, flags[]}`; 403 for disallowed keys. KBA 3285271: a put on `hcs_common` succeeded but was not applied without "apply configuration" in the UI — verify current behaviour. No bulk import/export via API. |
| **endpoint** | getEndpoints, createEndpoint, getEndpoint, updateEndpoint (merge-patch), deleteEndpoint, updateEndpoints (bulk PATCH, Apr 2026, help page only) | DTO: domainName, protocol, access, k8sService, k8sServiceVersion BLUE/GREEN, sslCertificate, mTLS, ipFilteringSets[codes], redirectSets, staticFileSet, denyPathsSet, responseHeaderSet, **maintenanceMode**, caching{…}, wafConfig{rateLimiting}. IP filter / redirect / deny-path / header sets are **referenced by code only** — no API to create them. |
| **certificates** | getCertificates, createCertificate (PEM cert + key + CA), getCertificateDetails, deleteCertificate | no replace; rotate = create → patch endpoint → delete old. Shows expirationDate + which endpoints use it. |
| **staticfiles** | list, create (multipart zip), get, delete (Sep 2025, help page only) | assign via endpoint patch |
| **databackup / datarestore** | getDatabackups, createDatabackup, getDatabackup, deleteDatabackup, changeDatabackupState (CANCEL), getDatarestores, createDatarestore, getDatarestore, changeDatarestoreState (CANCEL) | create: `{description, databackupType QUICK/STANDARD, databackupContent{databasesBackup{included}, storagesBackup{included}}}`. **Quick backups are in the API.** Restore: `{databackupCode, sourceEnvironmentCode, restorePointTime}` posted to the **target** env → **cross-environment data refresh is possible via API**. Standard: max 3 prod / 2 non-prod, no expiry; Quick: DB only, PITR, 35-day expiry, max 4. |
| **scaling** | getEnvironmentScalingDetail, updateEnvironmentScalingDetail (PATCH), getEnvironmentScalingOption | worker VM type/count, DB size/HA/perf tier, per-service replicas/CPU/memory/memoryScaleFactor 1.0–2.99/storage, autoscaling min/max, `hibernated`. **PATCH only on Consumption Credits contracts.** |
| **scheduledactivity** | list, create, get, update (reschedule), cancel | types HIBERNATE_COMMERCE_ENVIRONMENT, WAKE_UP_COMMERCE_ENVIRONMENT, UPGRADE_PREMIUM_DR, DOWNGRADE_TO_STANDARD_DR; status SCHEDULED/QUEUED/RUNNING/SUCCESS/ERROR/CANCELED… |
| **roles / userroleassignments** | getAllRolesForSubscription, get/create/update/delete UserRoleAssignment (Nov 2025) | roles: CUSTOMER_SYS_ADMIN, DEVELOPER, LOG_VIEWER, OBSERVABILITY_ANALYST, MERCHANDISER, SERVICE_AGENT, SOLR_ADMIN. No technical-user management API. |
| SCIM `/scim/v3/users|groups` | separate; served by the Commerce app (`cloudscimwebservices` extension), not the portal | out of scope |

### 2.3 What the portal UI does that the API does not (confirmed by TOC comparison)

| Capability | API? | Alternative |
|---|---|---|
| Environment / deployment logs (OpenSearch, ex-Kibana), Tomcat/GC logs | **No** — behind IAS SSO; `logLink` in deployment progress is just a UI link | the Azure Blob log archive (90 d prod / 30 d non-prod) read with a SAS token; one blog reverse-engineered an internal OpenSearch JSON endpoint |
| Service start/stop/restart, pod restart, manual ops history | **No** (read-only getServices) | — |
| Environment provisioning, "Transfer Properties" (sizing/infra/data copy) | No | backup + cross-env restore covers the data part |
| DB copy / bacpac for local dev, PITR restore of SAP's automated backups | No — SAP ticket | Quick backup/restore for ≤35 days |
| Initial passwords, hcs_admin credential, hot-folder / log storage account keys | No | SAS via Azure Storage Explorer (KBA 2950805) |
| IP filter / redirect / deny-path / response-header / host-alias sets, trusted CA certs, maintenance page upload | Upload UI only; API references by code | — |
| DNS, domain groups, CDN, Frontend Hosting, VPN/NAT/Private Link, env-level WAF, SQL View Gateway | No | — |
| Platform/K8s upgrade scheduling, scheduled updates | No (only hibernation + DR activities) | — |
| Dynatrace, availability reports, synthetic monitoring | No | Dynatrace REST API v2 with self-generated PAT; Dynatrace Managed MCP exists and SAP blogged a CCv2 setup (Sep 2026) |
| Webhooks / callbacks on build or deployment events | **No** | polling only |
| Prod deploy "type CONFIRM" guard | UI only | MCP server must add its own guard |
| Audit log, Cloud Ops notifications, bulk property import/export | No | — |

### 2.4 API timeline (What's New)

Oct 2024 scheduled activities · Jul 2024 canary traffic split · Jun 2025 SSL certs,
scaling, 100-item page cap · Jun 2025 endpoint management · Sep 2025 static file sets ·
Nov 2025 user role assignments · Mar 2026 Kyma migration, technical users, new header
and host · Apr 2026 bulk endpoint PATCH · Aug 2026 getServices + hibernationStatus ·
Oct 2026 bulk endpoint editing in UI. Spec is still labelled Beta; a few DTO fields are
deprecated (`cancellation*` top-level fields → `cancelation{}`, `decison` → `decision`).

## 3. Existing ecosystem

### 3.1 Portal-API clients

| Tool | Lang | Coverage | State |
|---|---|---|---|
| **sapccm** (SAP official CLI, "CX COMM CLOUD MAN CLI") | Java | build + deployment + build logs + cancel; config client-id/secret/token-url | official; "unstable for CI" (2020), "stable but heavy" (2024) |
| **eljoujat/sapcc-portal-cli** (`sccp`, npm, Sep 2026) | Node | environments, builds, deployments (+decision, modes), endpoints, backups/restore, scaling, certificates, scheduled activities, properties, roles; library API; client_credentials + `resource`, token cache, 401 retry | 3 commits, 0 stars; **best reference for the auth flow** |
| unic/sapcc-toolkit | Java | build/deploy services | legacy host |
| SAP-archive/commerce-ccv2ctl | Go | builds, deployments, properties | archived 2021, S-user cert auth (dead) |
| nicolabeghin/sap-commerce-cloud-management-api-gui | JavaFX | builds, deployments, endpoints + maintenance windows, hibernation; swagger-codegen client | active 2026 |
| sap-cx-actions/commerce-build, commerce-deploy | GitHub Actions | build/deploy + polling, retry, Slack/Teams webhooks | API-token based → broken |
| Seidor "CCv2 Link for Compass" | Atlassian app | read-only build/deploy sync | API-token based |
| mmolotov/cx_hac_ccv2_helper | Go | HAC on CCv2 via IAS SSO emulation; undocumented portal v1 API for replica routing (`ROUTE` cookie) | unsupported hacks |

Not found anywhere: Terraform provider, Azure DevOps extension, Python client, Postman
collection, any MCP server.

### 3.2 Existing SAP Commerce MCP servers (none touch the portal)

HAC: Emenowicz/hybris-mcp (16★, HAC + OCC),
nirajlonkar/sap-commerce-mcp-server, andreescocard/hybris-mcp-lite, AlexTsvetkov/commerce-mcp,
chriszhangrui/sap-commerce-mcp (binary-only, "CCv2" = release packaging, never deploys).
OCC: Dunkansdk/mcp-sap-commerce, commerce-cloud-integrations/sap-commerce-mcp-server,
nishanhegde/mcp-commerce, SAP's own prototype (Oct 2026 blog, "not production-ready").
Observability: Dynatrace Managed MCP against the SAP-managed tenant (SAP blog, Sep 2026).

### 3.3 What people automate and where it hurts (2020–2026)

The universal pipeline: `createBuild → poll → createDeployment → poll → notify`
(Bitbucket/GCP function, Jenkins, Azure DevOps, GitHub Actions, AWS Step Functions).
Nobody found uses backup, endpoint, scaling, hibernation, certificate, traffic-split or
role APIs in pipelines.

Recurring pain points:
- No webhooks → everyone polls (30 s … 10 min); progress can report 100 % while still
  running; UNKNOWN status appears.
- Transient build failures (~weekly) are not retried by SAP → clients retry.
- No "rollback to previous build" call. Practical rollback = redeploy previous build
  with NONE + ROLLING_UPDATE; `previousDeploymentCode` on the deployment DTO helps.
- Build log is a zip of plain text; JUnit XML was removed → people grep raw logs.
- putProperty may need a manual apply; env personas (dev/stage/prod) limit per-env
  properties; no sanitization flag on deployments.
- Blue-green preview endpoints only for storefront / JS storefront / API services.
- Auth friction: secret rotation, HTML login page instead of JSON, two hostnames,
  `resource` param, max 2 technical users per person.

## 4. What the MCP server should do

### 4.1 Read tools (safe by default)

| Tool | Portal API | Notes |
|---|---|---|
| `list_environments` | getEnvironments | with hibernationStatus, deploymentStatus |
| `get_environment` | getEnvironments + getServices + getDeployments(latest) + scaling | one call: what is deployed, replicas, hibernated?, running deployment? |
| `list_builds` / `get_build` | getBuilds / getBuild + progress | filter by branch, status, `statusNot` |
| `get_build_log` | getBuildLogs | **unzip, return text; support `grep`, `tail`, `first_error`** |
| `list_deployments` / `get_deployment` | getDeployments / getDeployment + progress | stages/steps flattened; include build branch |
| `get_property` | getProperty | mask secret-looking values |
| `list_endpoints` / `get_endpoint` | endpoint | maintenance mode, IP filter sets, cert expiry |
| `list_certificates` | certificates | flag expiring < 30 days |
| `list_backups` / `list_restores` | databackup / datarestore | |
| `get_scaling` / `get_scaling_options` | scaling | |
| `list_scheduled_activities` | scheduledactivity | |
| `get_deployment_modes` | getDeploymentModes | which modes an env allows — validate before create |

### 4.2 Write tools (per-environment permission + confirm)

| Tool | Portal API | Guard |
|---|---|---|
| `create_build` | createBuild | `build` permission |
| `create_deployment` | createDeployment | `deploy` / `deploy_prod`; INITIALIZE, RECREATE and prod require `confirm: true`; validate against getDeploymentModes; refuse if env has a running deployment |
| `cancel_deployment` | cancellationoptions + cancellation | reason mandatory; 501 → explain feature flag |
| `blue_green_decision` | createDeploymentDecision | ACCEPT / REJECT / PREPARE_CANARY |
| `set_traffic_split` | updateTrafficSplit | |
| `set_maintenance_mode` | updateEndpoints (bulk) / updateEndpoint | one call for all public endpoints of an env |
| `set_property` | putProperty | audit log; warn about apply-config caveat |
| `create_backup` / `cancel_backup` | databackup | QUICK by default |
| `restore_backup` | createDatarestore | gated like prod deploy; cross-env refresh is a legit use case |
| `update_scaling` | PATCH scaling | only Consumption Credits |
| `schedule_hibernate` / `schedule_wake_up` / `cancel_activity` | scheduledactivity | |
| `assign_endpoint_certificate` | createCertificate + patch endpoint | cert rotation helper |

### 4.3 Higher-level tools — the value over a CLI

1. **`wait_for_build` / `wait_for_deployment`** — bounded `timeout_seconds` (≤ ~60 s
   per call), backoff, normalized terminal states, so an agent can loop without
   blocking an MCP call for an hour. `build_and_deploy` = create build and return
   codes; the agent chains waits.
2. **`rollback_environment`** — resolve previous successful deployment's build,
   redeploy with NONE + ROLLING_UPDATE after confirmation.
3. **`whats_deployed`** — per env: build code, branch, built when / by whom; diff
   staging vs prod ("which branch is where").
4. **`diagnose_build_failure`** — fetch log zip, extract the first compile / test /
   Spring-context error block, return relevant lines instead of the whole log.
5. **`pre_deploy_checklist`** — env available, no running deployment, allowed modes,
   last backup age, maintenance mode state, pending blue-green decision, cert expiry.
6. **`compare_properties`** — diff a set of keys between two environments (API is
   per-key, so the server batches).
7. **`release_timeline`** — builds + deployments for a branch in a window, for release
   notes / incident timelines (pairs with log time windows).
8. **Status normalization** — HTML login page → auth error; 302 from old host; 429
   backoff; 100 %-but-BUILDING; UNKNOWN.

### 4.4 MCP resources and prompts

- Resources: `ccv2://{sub}/environments`, `ccv2://{sub}/environments/{env}`,
  `ccv2://{sub}/builds/{code}/log` (lazy, text).
- Prompts: "release checklist for <env>", "investigate failed build <code>",
  "why is staging different from prod".

### 4.5 Integration with neighbouring tools

- Logs: deployments give exact time windows and build codes (log annotation
  `ccv2_cx_sap_com/build-code`); an agent can chain `get_deployment` with whatever log
  tool it has (OpenSearch UI, a blob-archive reader).
- HAC: post-deploy smoke checks (cron job status, property check) belong to a HAC tool.
- Dynatrace: don't rebuild; run the Dynatrace Managed MCP alongside.
- Notifications: a thin script reusing the same client can post to Slack or Teams. Not an
  MCP concern.

### 4.6 Non-goals

Environment logs via OpenSearch, service restarts, DB copies for local dev, PITR of
SAP-managed backups, hot-folder keys, IP-filter-set upload, DNS/CDN, user creation.
State them in the README so nobody chases them.

## 5. Design notes

- **Auth**: technical-user OAuth2 client_credentials with `resource`; header
  `x-approuter-authorization`; cache token, refresh 30 s before expiry; retry once on
  401; treat `text/html` responses as auth failures with an actionable message.
  Multi-subscription config (one technical user per subscription); store the secret
  creation date and warn at 75 days.
- **Host**: `portalapi.commerce.ondemand.com` only.
- **Safety model**: per-environment permission flags
  (`read`, `build`, `deploy`, `deploy_prod`, `properties_write`, `endpoints_write`,
  `scaling`, `backup_restore`), read-only default, `confirm: true` for destructive
  ops, append-only audit log of every write with who/what/when.
- **Long-running ops**: never block > ~60 s inside a tool; return codes and offer
  `wait_*` tools. Builds take 20–60 min, deployments similar.
- **Pagination**: page internally, expose `limit`.
- **Spec-driven**: generate the HTTP client/DTOs from `spec/cloud-portal-api-2.0.12.yaml`;
  add the three help-page-only operations (getServices, updateEndpoints bulk,
  staticfiles) by hand.
- **Stack**: Python + the MCP Python SDK, or Node (could vendor `sapcc-portal-cli`'s
  client). Recommendation: Python; the SDK offloads sync tools to threads and the
  packaging is a single `uv`-built image.
- **Transport**: stdio + streamable HTTP so it can run in the same compose stack.

## 6. Open questions to verify against a real subscription

Answered by the first live run of `scripts/smoke.py` (2026-10-29, read tools only):

- getServices lives at `/subscriptions/{code}/environments/{env}/services` (the help page
  path without `/subscriptions/` is wrong).
- Ten back-to-back progress calls produced no 429.
- List DTOs carry explicit `null` for empty arrays (`endpoints: null` on certificates);
  models treat null as absent.
- `GET /deploymentmodes` is subscription-wide and answers 403 ("Not allowed to read
  environment 'p1'") when the technical user lacks any one environment, even though
  environments and deployments of that environment are readable. The server treats the
  modes as unknown in that case.
- `GET .../scaling` and `.../scalingOptions` answer 403 for a technical user with the
  `CUSTOMER_DEVELOPER` role ("Not authorized to access environment scaling details").
- Deployment progress stage names are i18n keys such as
  `DEPLOYMENT_INSIGHT.ROLLING_DEPLOYMENT.NAME`.
- Build logs are one `releaselog-<code>.txt` entry, ~12k lines for a full build.

Still open:

1. Does putProperty apply configuration now, or is a UI apply still needed (KBA 3285271)?
2. Does createDatarestore accept a prod backup into a staging target without a ticket?
3. Is deployment cancellation enabled on the subscription (501 otherwise)?
4. Secret expiry length on a freshly created technical user.
5. Whether scaling needs `CUSTOMER_SYS_ADMIN` on the technical user.

## Sources

- API overview + embedded OpenAPI: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/452dcbb0e00f47e88a69cdaeb87a925d/66abfe678b55457fab235ce8039dda71.html
- Technical users: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/0c2050f6d31f49ddb6eba18509060ae5/57bef96f18034193af93d2cc36f6d526.html
- getServices: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/452dcbb0e00f47e88a69cdaeb87a925d/747c7acffb7646aba48fbd623ee7a276.html
- updateEndpoints (bulk): https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/452dcbb0e00f47e88a69cdaeb87a925d/11388e3edfc4484a8b6662b66c4df3c0.html
- Static file sets: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/452dcbb0e00f47e88a69cdaeb87a925d/56e99b885b494f718a57ed7c9881d57c.html
- createDeployment: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/452dcbb0e00f47e88a69cdaeb87a925d/d80fd1dbefff4b8bbbbac66822d4a038.html
- Deployment cancellation: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/452dcbb0e00f47e88a69cdaeb87a925d/33fc3a6273274b769497b3b45ca24456.html
- Deployment rollback (UI doc): https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/0fa6bcf4736c46f78c248512391eb467/7615c7c868364806a54230ae6a871136.html
- Kyma migration: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/0fa6bcf4736c46f78c248512391eb467/edcfd89aa5154be59910ebb7081030e3.html
- What's New viewer: https://help.sap.com/whats-new/3d7190d2aab14c7d92f918cc3fa9a024?locale=en-US
- sapccm CLI: https://help.sap.com/docs/SAP_COMMERCE_CLOUD_PUBLIC_CLOUD/9116f1cfd16049c3a531bfb6a681ff77/8acde53272c64efb908b9f0745498015.html
- Cloud Portal updates (SAP Learning): https://learning.sap.com/courses/sap-commerce-cloud-developer/cloud-portal-updates
- KBAs (preview only): 3285272, 3707347, 3764102, 3770030, 3767430, 3400008, 3250487, 3789335, 3285271 — https://userapps.support.sap.com/sap/support/knowledge/en/<id>
- SAP blog, MCP for SAP Commerce (Oct 2026): https://community.sap.com/t5/crm-and-cx-blog-posts-by-sap/connecting-ai-agents-to-sap-commerce-cloud-with-mcp/ba-p/14504916
- SAP blog, Dynatrace MCP for CCv2 (Sep 2026): https://community.sap.com/t5/crm-and-cx-blog-posts-by-sap/how-to-create-mcp-server-for-sap-commerce-cloud-dynatrace/ba-p/14484748
- Blue-green intro: https://community.sap.com/t5/crm-and-cx-blog-posts-by-sap/an-introduction-to-blue-green-deployment-for-sap-commerce-cloud/ba-p/13548232
- Quick Data Backup: https://community.sap.com/t5/crm-and-cx-blog-posts-by-sap/quick-data-backup-for-sap-commerce-cloud/ba-p/14325384
- Backup options Q&A: https://community.sap.com/t5/crm-and-cx-q-a/what-backup-options-are-available-in-sap-commerce-cloud-and-how-to-restore/qaq-p/14384056
- sapcc-portal-cli: https://github.com/eljoujat/sapcc-portal-cli
- unic/sapcc-toolkit: https://github.com/unic/sapcc-toolkit
- commerce-ccv2ctl (archived): https://github.com/SAP-archive/commerce-ccv2ctl
- Management API GUI: https://github.com/nicolabeghin/sap-commerce-cloud-management-api-gui
- sap-cx-actions: https://github.com/sap-cx-actions/commerce-build , https://github.com/sap-cx-actions/commerce-deploy
- Acorel GitHub Actions (2024): https://blog.acorel.nl/2024/04/automating-sap-commerce-cloud-deployments-using-github-actions/
- Acorel nightly builds (2020): https://blog.acorel.nl/2020/10/sap-ccv2-how-to-do-nightly-builds-and-deploys-ci-cd/
- hybrismart lessons learned: https://hybrismart.com/2022/07/25/lessons-learned-migrating-to-ccv2/
- dev.to deployment guide: https://dev.to/aliaksandr_tsviatkou/deploying-sap-commerce-to-commerce-cloud-v2-ccv2-the-complete-guide-lpi
- OpenSearch log analyzer (unofficial): https://medium.com/@andreilescu/from-noise-to-clarity-how-i-built-an-opensearch-log-analyzer-for-sap-commerce-cloud-ca06cfbd4417
- Dynatrace token Q&A: https://community.sap.com/t5/crm-and-cx-q-a/how-to-generate-an-access-token-to-access-dynatrace-data-through-api/qaq-p/13967633
- Existing MCP servers: https://github.com/Emenowicz/hybris-mcp , https://glama.ai/mcp/servers?query=sap%20commerce

## 7. Decisions (2026-10-28)

- Project name and folder: `ccv2-portal-mcp`. Remote repo to be added later.
- Phase 1 = sections 4.1–4.3 (read tools, bounded waits, gated writes, pipeline helpers).
  Phase 2 = maintenance-mode across endpoints, backup-before-deploy, certificate rotation.
- Deliverable: Docker image + `docker-compose.yml` running streamable HTTP
  (`MCP_TRANSPORT`, `MCP_PORT`), multi-subscription config
  from a gitignored env/config file, audit log as JSONL on a small volume, no database.
  Plus a stdio entry point via `uv run` / `uvx` for local development.
- Stack: Python + MCP Python SDK 2.x.

## 8. Phase 3 (planned, after live testing): local UI and database-backed config

Intended use is a personal, local tool: Claude Code on the developer's machine talking to
the server, nothing shared or deployed. Under that model the configuration should not live
in `.env` or JSON files but be managed through a small web UI and stored locally.

- **Storage**: one SQLite file in the user's data directory for subscriptions,
  per-environment permissions, client secrets and the audit log, created with owner-only
  (0600) permissions. No OS keychain: developers are on macOS, Linux and Windows, and
  keychain backends are unreliable on headless Linux and WSL. The protection is the same
  as today's `.env` file (filesystem permissions), which is acceptable for a personal
  local tool.
- **Process model**: the MCP server (stdio or HTTP) only *reads* the database, re-reading it
  when the file's modification time changes, so permission changes apply to the next tool
  call without a restart. A separate `ccv2-portal-mcp ui` command serves the management
  page on localhost against the same database. This keeps one-process-per-session stdio
  working and avoids port conflicts.
- **UI scope**: add/edit/remove a subscription (name, code, token URL, client id, secret,
  resource), toggle permissions per environment, show token health (last success, last
  auth failure), secret age with expiry warning, and the recent audit entries.
- **Bootstrap**: on first start, an existing `.env` / `CCV2_CONFIG` is imported into the
  database; the file loaders stay as an alternative input for headless runs.
- **Not a rewrite**: tools read configuration through `Registry.configure` / `load_config`
  only, so this is a new loader plus a reload hook.

Deferred until the server has been validated against a real subscription.
