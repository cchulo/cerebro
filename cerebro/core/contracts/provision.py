"""Provisioner: turn UnitSpecs into running workloads, on compose (dev) or Kubernetes (kopf controller).

Adapters describe what they need as UnitSpec / JobSpec (an image, args, env, volumes, ports). The provisioner owns
naming, networking, storage and idle handling, and is the Locator the rest of the stack asks for endpoints.

Wire contract every provisioned engine unit must satisfy (the bridge image provides it for stdio engines):
    GET  /health
    POST /mcp                          MCP over streamable HTTP, stateless, read tools only
    GET  /.well-known/cerebro-capabilities   {engine, version, unit, capabilities: {search, graph, branches}, tools: [...]}
"""
from __future__ import annotations
from abc import abstractmethod
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field
from ..context import Adapter, Locator

UnitRole = Literal["docs", "code", "memory", "auth", "mcp", "infra", "gateway", "ingest", "inference"]


class PortSpec(BaseModel):
    name: str = "http"
    port: int
    protocol: Literal["TCP", "UDP"] = "TCP"


class VolumeSpec(BaseModel):
    name: str
    mount_path: str
    size: str = "5Gi"
    read_only: bool = False
    shared_with: list[str] = Field(default_factory=list, description="other unit/job names mounting the same volume")


class UnitSpec(BaseModel):
    name: str                                       # DNS label; also the service hostname other units use
    role: UnitRole
    image: str
    build: str | None = Field(default=None, description="path to a Dockerfile directory, for images cerebro builds")
    command: list[str] | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list, description="secret names to inject as env vars")
    ports: list[PortSpec] = Field(default_factory=lambda: [PortSpec(port=8080)])
    volumes: list[VolumeSpec] = Field(default_factory=list)
    resources: dict[str, str] = Field(default_factory=dict)
    health_path: str | None = "/health"
    scope: str | None = None
    idle_ttl: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    stateful: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    run_as_root: bool = False

    @property
    def http_port(self) -> int:
        return self.ports[0].port if self.ports else 80


class JobSpec(BaseModel):
    """A run-to-completion workload (indexer, seed), optionally on a cron schedule."""
    name: str
    image: str
    build: str | None = None
    command: list[str] | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list)
    volumes: list[VolumeSpec] = Field(default_factory=list)
    schedule: str | None = Field(default=None, description="cron; None = on demand only")
    scope: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class UnitRef(BaseModel):
    name: str


class Endpoint(BaseModel):
    url: str
    ready: bool = True


class UnitStatus(BaseModel):
    name: str
    exists: bool = False
    ready: bool = False
    replicas: int = 0
    last_used: datetime | None = None
    message: str | None = None


class Provisioner(Adapter, Locator):
    kind = "provision"

    @abstractmethod
    async def ensure(self, spec: UnitSpec) -> Endpoint:
        """Idempotent: create the workload if missing, scale it up if idled, return its endpoint."""

    @abstractmethod
    async def release(self, ref: UnitRef) -> None: ...

    @abstractmethod
    async def status(self, ref: UnitRef) -> UnitStatus: ...

    @abstractmethod
    async def run_job(self, job: JobSpec, *, wait: bool = False) -> str:
        """Start a job now (ignores schedule); returns a run id."""

    @abstractmethod
    def render(self, units: list[UnitSpec], jobs: list[JobSpec]) -> dict[str, str]:
        """Materialise manifests: {relative path: content}. compose -> one file; kubernetes -> one per unit."""

    def touch(self, ref: UnitRef) -> None:
        """Record use for idle handling. Default: no-op."""
