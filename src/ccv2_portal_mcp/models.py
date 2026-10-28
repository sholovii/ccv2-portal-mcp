"""Structured tool outputs. Unknown fields from the portal are kept (extra='allow')."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


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
