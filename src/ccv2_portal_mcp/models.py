"""Structured tool outputs. Unknown fields from the portal are kept (extra='allow')."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Loose(BaseModel):
    """Portal DTO: unknown fields are kept, explicit nulls fall back to the field default."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data):
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


class SubscriptionInfo(BaseModel):
    name: str
    code: str
    base_url: str
    default_permissions: list[str]
    permissions: dict[str, list[str]]
    is_default: bool


class SubscriptionsResult(BaseModel):
    subscriptions: list[SubscriptionInfo]


class EnvironmentInfo(Loose):
    code: str
    name: str | None = None
    type: str | None = None
    status: str | None = None
    deploymentStatus: str | None = None
    hibernationStatus: str | None = None
    permissions: list[str] = Field(default_factory=list)


class EnvironmentsResult(BaseModel):
    subscription: str
    environments: list[EnvironmentInfo]


class ServiceInfo(Loose):
    code: str
    name: str | None = None
    runnable: bool | None = None
    desiredReplicas: int | None = None
    availableReplicas: int | None = None


class DeploymentInfo(Loose):
    code: str
    buildCode: str | None = None
    environmentCode: str | None = None
    status: str | None = None
    strategy: str | None = None
    databaseUpdateMode: str | None = None
    createdBy: str | None = None
    createdTimestamp: str | None = None
    deployedTimestamp: str | None = None
    failedTimestamp: str | None = None
    previousDeploymentCode: str | None = None
    canaryStatus: str | None = None


class EnvironmentDetail(BaseModel):
    subscription: str
    environment: EnvironmentInfo
    services: list[ServiceInfo] = Field(default_factory=list)
    services_error: str | None = None
    latest_deployment: DeploymentInfo | None = None
    latest_build: "BuildInfo | None" = None
    scaling: dict | None = None
    scaling_error: str | None = None


class BuildInfo(Loose):
    code: str
    name: str | None = None
    branch: str | None = None
    status: str | None = None
    createdBy: str | None = None
    buildStartTimestamp: str | None = None
    buildEndTimestamp: str | None = None
    buildVersion: str | None = None
    deployed: bool | None = None


class BuildsResult(BaseModel):
    subscription: str
    builds: list[BuildInfo]


class BuildProgress(Loose):
    buildStatus: str | None = None
    percentage: int | None = None
    numberOfTasks: int | None = None
    errorMessage: str | None = None
    startedTasks: list[dict] = Field(default_factory=list)


class BuildDetail(BaseModel):
    subscription: str
    build: BuildInfo
    progress: BuildProgress | None = None
    terminal: bool
    deployments: list[DeploymentInfo] = Field(default_factory=list)


class BuildLogResult(BaseModel):
    subscription: str
    build_code: str
    entry: str
    entries: list[str]
    total_lines: int
    returned_lines: int
    truncated: bool
    mode: str
    lines: list[str]


class DeploymentsResult(BaseModel):
    subscription: str
    deployments: list[DeploymentInfo]


class StageInfo(Loose):
    name: str | None = None
    type: str | None = None
    status: str | None = None
    startTimestamp: str | None = None
    endTimestamp: str | None = None
    failed_steps: list[str] = Field(default_factory=list)


class DeploymentProgress(Loose):
    deploymentStatus: str | None = None
    percentage: int | None = None
    stages: list[StageInfo] = Field(default_factory=list)


class DeploymentDetail(BaseModel):
    subscription: str
    deployment: DeploymentInfo
    build: BuildInfo | None = None
    progress: DeploymentProgress | None = None
    terminal: bool


class WaitResult(BaseModel):
    subscription: str
    kind: str
    code: str
    status: str | None
    percentage: int | None = None
    done: bool
    succeeded: bool | None = None
    waited_sec: float
    hint: str


EnvironmentDetail.model_rebuild()


# ------------------------------------------------------------- phase 1 extras

class PropertyResult(BaseModel):
    subscription: str
    environment: str
    service: str
    key: str
    value: str | None
    masked: bool = False
    flags: list[str] = Field(default_factory=list)


class EndpointInfo(Loose):
    code: str
    name: str | None = None
    url: str | None = None
    service: str | None = None
    webProxy: str | None = None
    maintenanceMode: bool | None = None


class EndpointsResult(BaseModel):
    subscription: str
    environment: str
    endpoints: list[EndpointInfo]


class CertificateInfo(Loose):
    code: str
    name: str | None = None
    domains: list[str] = Field(default_factory=list)
    issuer: str | None = None
    expirationDate: str | None = None
    endpoints: list[dict] = Field(default_factory=list)
    days_to_expiry: int | None = None


class CertificatesResult(BaseModel):
    subscription: str
    certificates: list[CertificateInfo]


class BackupInfo(Loose):
    databackupCode: str
    name: str | None = None
    status: str | None = None
    databackupType: str | None = None
    description: str | None = None
    buildCode: str | None = None
    createdBy: str | None = None
    createdTimestamp: str | None = None
    canBeRestored: bool | None = None
    canBeDeleted: bool | None = None
    canBeCanceled: bool | None = None


class BackupsResult(BaseModel):
    subscription: str
    environment: str
    backups: list[BackupInfo]


class RestoreInfo(Loose):
    datarestoreCode: str
    name: str | None = None
    status: str | None = None
    databackupCode: str | None = None
    createdBy: str | None = None
    createdTimestamp: str | None = None
    canBeCanceled: bool | None = None


class RestoresResult(BaseModel):
    subscription: str
    environment: str
    restores: list[RestoreInfo]


class ActivityInfo(Loose):
    activityCode: str | None = None
    activityType: str | None = None
    status: str | None = None
    scheduledTimestamp: str | None = None
    cancelable: bool | None = None
    updatable: bool | None = None


class ActivitiesResult(BaseModel):
    subscription: str
    environment: str
    activities: list[ActivityInfo]


class WriteResult(BaseModel):
    """Uniform answer of every write tool."""

    subscription: str
    action: str
    environment: str | None = None
    code: str | None = None
    executed: bool
    response: dict | list | None = None
    hint: str = ""


class Check(BaseModel):
    name: str
    level: str  # ok | warn | fail | unknown
    detail: str


class ChecklistResult(BaseModel):
    subscription: str
    environment: str
    build_code: str | None
    ready: bool
    checks: list[Check]


class DeployedState(BaseModel):
    environment: str
    type: str | None = None
    deployment_code: str | None = None
    deployed_at: str | None = None
    deployed_by: str | None = None
    strategy: str | None = None
    database_update_mode: str | None = None
    build_code: str | None = None
    branch: str | None = None
    build_name: str | None = None
    build_version: str | None = None
    build_created_by: str | None = None
    active_deployment: str | None = None
    error: str | None = None


class WhatsDeployedResult(BaseModel):
    subscription: str
    environments: list[DeployedState]
    comparison: list[str]


class RollbackPlan(BaseModel):
    subscription: str
    environment: str
    current_deployment: str | None
    current_build: str | None
    current_branch: str | None = None
    target_deployment: str | None
    target_build: str | None
    target_branch: str | None = None
    strategy: str
    database_update_mode: str
    executed: bool
    new_deployment_code: str | None = None
    hint: str


class Diagnosis(BaseModel):
    subscription: str
    build_code: str
    status: str | None
    branch: str | None
    category: str
    summary: str
    portal_error: str | None = None
    evidence: list[str]
    log_entry: str | None = None
    suggestion: str


class PropertyDiff(BaseModel):
    key: str
    a: str | None
    b: str | None
    same: bool
    masked: bool = False
    error_a: str | None = None
    error_b: str | None = None


class PropertyDiffResult(BaseModel):
    subscription: str
    service: str
    environment_a: str
    environment_b: str
    diffs: list[PropertyDiff]
    different: int


class TimelineEvent(BaseModel):
    at: str | None
    kind: str  # build | deployment
    code: str
    status: str | None
    environment: str | None = None
    branch: str | None = None
    build_code: str | None = None
    by: str | None = None
    detail: str | None = None


class TimelineResult(BaseModel):
    subscription: str
    since: str
    events: list[TimelineEvent]
