"""cerebro.yaml: the one file an operator edits.

Secrets are never in this file. String values may reference environment variables as ${NAME} or ${NAME:-default};
the loader interpolates them so endpoints can differ between compose and Kubernetes without two files.
Run `cerebro schema` for the JSON schema (editor completion) and `cerebro validate` to check a file.
"""
from __future__ import annotations
import os, pathlib, re
from typing import Any, Literal
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from .principal import repo_name

UnitKind = Literal["scope", "repo"]
_SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")   # scope names become part of DNS labels
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# ----------------------------------------------------------------------------------------------- scopes
class RepoSpec(BaseModel):
    url: str
    branches: list[str] = Field(default_factory=list, description="tracked branches (globs allowed); empty = default branch only")

    @property
    def name(self) -> str:
        return repo_name(self.url) or self.url

    @property
    def key(self) -> str:
        return self.url.rstrip("/").removesuffix(".git").lower()


class ScopeCode(BaseModel):
    unit: UnitKind | None = Field(default=None, description="override engines.code.unit for this scope")
    repos: list[RepoSpec] = Field(default_factory=list)

    @field_validator("repos", mode="before")
    @classmethod
    def _coerce(cls, v):
        return [{"url": r} if isinstance(r, str) else r for r in (v or [])]


class ScopeConfig(BaseModel):
    groups: list[str] = Field(default_factory=list, description="IdP groups allowed to read this scope")
    code: ScopeCode = Field(default_factory=ScopeCode)
    docs: dict[str, dict[str, Any]] = Field(default_factory=dict, description="source plugin name -> what of it belongs here")

    @field_validator("docs", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return {k: (c or {}) for k, c in (v or {}).items()}

    @field_validator("code", mode="before")
    @classmethod
    def _code_none(cls, v):
        return v or {}


# ----------------------------------------------------------------------------------------------- identity
class PrincipalSeed(BaseModel):
    subject: str
    groups: list[str] = Field(default_factory=list)
    kind: Literal["user", "service"] = "user"
    token_scopes: list[str] | None = Field(default=None, description="None = every scope")


class UserSeed(BaseModel):
    name: str
    groups: list[str] = Field(default_factory=list)
    email: str | None = None


class IntrospectionConfig(BaseModel):
    url: str | None = None                     # default: from the issuer's RFC 8414 metadata
    client_id: str
    client_secret_env: str


class AuthServerConfig(BaseModel):
    type: str = "keycloak"
    realm: str = "cerebro"
    admin_user_env: str = "CEREBRO_AUTH_ADMIN_USER"
    admin_password_env: str = "CEREBRO_AUTH_ADMIN_PASSWORD"
    public_url: str | None = Field(default=None, description="URL browsers reach the server at; issuer derives from it")
    options: dict[str, Any] = Field(default_factory=dict)


class LegacyIdentity(BaseModel):
    type: Literal["trusted_headers"] = "trusted_headers"
    user_header: str = "X-Forwarded-User"
    groups_header: str = "X-Forwarded-Groups"


class IdentityConfig(BaseModel):
    """mode none: one principal, no tokens; static: a token map (tests, demo); builtin: the stack runs an
    authorization server; external: any OAuth 2.1 / OIDC issuer. builtin and external share the bearer adapters."""
    mode: Literal["none", "static", "builtin", "external"] = "none"
    # none
    principal: PrincipalSeed = Field(default_factory=lambda: PrincipalSeed(subject="local", groups=["everyone", "admin"]))
    allow_remote: bool = Field(default=False, description="mode none: listen beyond loopback (then static_token_env is required)")
    static_token_env: str = "CEREBRO_TOKEN"
    # static
    tokens: dict[str, PrincipalSeed] = Field(default_factory=dict, description="mode static: token -> principal")
    # builtin / external
    issuer: str | None = None
    audience: str | None = None
    groups_claim: str = "groups"
    scope_claim: str = "scope"
    token_validation: Literal["jwks", "introspection"] = "jwks"
    jwks_url: str | None = None
    introspection: IntrospectionConfig | None = None
    server: AuthServerConfig | None = None
    users: list[UserSeed] = Field(default_factory=list)
    # optional alternative provider in front of / instead of bearer tokens
    legacy: LegacyIdentity | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.mode == "external" and not (self.issuer and self.audience):
            raise ValueError("identity.mode external requires issuer and audience")
        if self.mode == "builtin":
            self.server = self.server or AuthServerConfig()
        if self.mode == "static" and not self.tokens:
            raise ValueError("identity.mode static requires a tokens: map")
        if self.token_validation == "introspection" and self.mode in ("builtin", "external") and not self.introspection:
            raise ValueError("token_validation introspection requires an introspection: block")
        return self


class PolicyConfig(BaseModel):
    type: str = "groups"
    always_groups: list[str] = Field(default_factory=lambda: ["everyone"])
    team_banks_from_groups: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------------------------------------- inference
class ModelEndpoint(BaseModel):
    provider: Literal["ollama", "openai"] = "ollama"
    base_url: str = "http://ollama:11434"
    api_key_env: str | None = Field(default=None, description="secret name holding the API key (openai provider)")
    model: str
    options: dict[str, Any] = Field(default_factory=dict)


class EmbedEndpoint(ModelEndpoint):
    dim: int


class InferenceConfig(BaseModel):
    """The only outbound path for document and memory text. Point it at something the organisation controls."""
    type: str = Field(default="openai_compat", description="Inference adapter used by cerebro itself (not by engines)")
    llm: ModelEndpoint = Field(default_factory=lambda: ModelEndpoint(model="gpt-oss:20b"))
    embed: EmbedEndpoint = Field(default_factory=lambda: EmbedEndpoint(model="bge-m3", dim=1024))


# ----------------------------------------------------------------------------------------------- engines
class EngineConfig(BaseModel):
    type: str
    unit: UnitKind = "scope"
    idle_ttl: str | None = Field(default=None, description="e.g. 2h; None = never scale to zero")
    resources: dict[str, str] = Field(default_factory=dict, description="cpu / memory / storage requests")
    options: dict[str, Any] = Field(default_factory=dict)


class EnginesConfig(BaseModel):
    docs: EngineConfig | None = Field(default_factory=lambda: EngineConfig(type="lightrag"))
    code: EngineConfig | None = Field(default_factory=lambda: EngineConfig(type="tokensave"))
    memory: EngineConfig | None = Field(default_factory=lambda: EngineConfig(type="hindsight"))


class ProvisioningConfig(BaseModel):
    target: Literal["compose", "kubernetes"] = "compose"
    project: str = Field(default="cerebro", description="compose project name / label value")
    namespace: str = "cerebro"
    storage_class: str | None = None
    image_registry: str | None = Field(default=None, description="prefix for images cerebro builds itself")
    options: dict[str, Any] = Field(default_factory=dict)


class SecretsConfig(BaseModel):
    source: Literal["env", "kubernetes", "vault"] = "env"
    keys: list[str] = Field(default_factory=list, description="secret names the stack expects to exist")
    options: dict[str, Any] = Field(default_factory=dict)


class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8090
    path: str = "/mcp"
    public_url: str | None = Field(default=None, description="URL clients use; the RFC 8707 resource identifier")
    state_type: str = Field(default="json_file", description="SyncState adapter for the ingest engine")
    plugins_dir: str = "plugins"
    concurrency: int = Field(default=8, description="max parallel unit calls per request")


class Config(BaseModel):
    version: Literal[2] = 2
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    provisioning: ProvisioningConfig = Field(default_factory=ProvisioningConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict, description="non-secret options per source plugin")
    scopes: dict[str, ScopeConfig] = Field(default_factory=dict)

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_none(cls, v):
        return {k: (c or {}) for k, c in (v or {}).items()}

    @model_validator(mode="after")
    def _check(self):
        seen: dict[tuple, str] = {}
        for name, sc in self.scopes.items():
            if not _SAFE.match(name):
                raise ValueError(f"scope name '{name}' must be a lowercase DNS label (a-z, 0-9, '-')")
            for r in sc.code.repos:
                k = ("repo", r.key)
                if k in seen and seen[k] != name:
                    raise ValueError(f"repo {r.url} is listed in scopes {seen[k]} and {name}")
                seen[k] = name
            for sp in (sc.docs.get("confluence") or {}).get("spaces", []) or []:
                k = ("space", sp)
                if k in seen and seen[k] != name:
                    raise ValueError(f"Confluence space {sp} is listed in scopes {seen[k]} and {name}")
                seen[k] = name
        return self

    # ---- convenience
    def scope_unit(self, scope: str) -> UnitKind:
        override = self.scopes[scope].code.unit
        return override or (self.engines.code.unit if self.engines.code else "scope")

    def source_names(self) -> list[str]:
        out: list[str] = []
        for sc in self.scopes.values():
            for n in sc.docs:
                if n not in out:
                    out.append(n)
        return out

    def resource_id(self) -> str:
        return self.gateway.public_url or f"http://{self.gateway.host}:{self.gateway.port}{self.gateway.path}"


# ----------------------------------------------------------------------------------------------- loading
def interpolate(value, env: dict[str, str] | None = None):
    """${NAME} / ${NAME:-default} in any string, recursively through dicts and lists."""
    env = os.environ if env is None else env
    if isinstance(value, str):
        def sub(m):
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise KeyError(f"cerebro.yaml references ${{{name}}} but it is not set")
        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    return value


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    p = pathlib.Path(path or os.environ.get("CEREBRO_CONFIG", "cerebro.yaml"))
    raw = yaml.safe_load(p.read_text()) or {}
    return Config.model_validate(interpolate(raw, env))


def json_schema() -> dict:
    return Config.model_json_schema()
