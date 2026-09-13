"""Fake adapters and a canary Secrets source for provisioning tests.

The engine adapters are written by other people; these stand-ins return the shapes the provisioner must handle: a
LightRAG-like docs unit per scope with a volume, a code unit with idle_ttl plus an index job sharing its volume, a
memory unit whose env embeds a secret in a URL, an auth unit, and a source plugin that declares an McpUpstream.
"""
from __future__ import annotations
import pathlib
from cerebro.core import AdapterContext, StaticLocator
from cerebro.core.context import Adapter
from cerebro.core.contracts.provision import JobSpec, PortSpec, UnitSpec, VolumeSpec

CANARY = "CANARY-SECRET-VALUE-9f3a"


class CanarySecrets:
    """Every secret resolves to a recognisable value; no rendered file may ever contain it."""
    def get(self, name, default=None):
        return f"{CANARY}-{name}"


def canary_ctx(config) -> AdapterContext:
    return AdapterContext(config, secrets=CanarySecrets(), locator=StaticLocator(template="http://{unit}:8080"))


class FakeDocs(Adapter):
    kind, name = "docs", "fakerag"

    def units(self):
        return [UnitSpec(name=f"docs-{s}", role="docs", image="ghcr.io/hkuds/lightrag:v1.5.7", scope=s,
                         ports=[PortSpec(port=9621)],
                         env={"WORKSPACE": f"scope_{s}", "POSTGRES_HOST": "postgres", "POSTGRES_USER": "cerebro",
                              "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}", "LIGHTRAG_API_KEY": "${LIGHTRAG_API_KEY}",
                              "MAX_ASYNC": "${LIGHTRAG_MAX_ASYNC:-2}"},
                         volumes=[VolumeSpec(name="data", mount_path="/app/data")], depends_on=["postgres"])
                for s in self.ctx.config.scopes]


class FakeCode(Adapter):
    kind, name = "code", "fakesave"

    def units(self):
        return [UnitSpec(name="code-public", role="code", image="cerebro/code-unit", build="images/code-unit", scope="public",
                         ports=[PortSpec(port=8045)], idle_ttl="2h", resources={"cpu": "1", "memory": "2Gi"},
                         volumes=[VolumeSpec(name="repos", mount_path="/repos", size="10Gi", shared_with=["index-code-public"])])]

    def jobs(self):
        vol = VolumeSpec(name="repos", mount_path="/repos", size="10Gi", shared_with=["code-public"])
        return [JobSpec(name="index-code-public", image="cerebro/code-unit", build="images/code-unit", scope="public",
                        schedule="0 3 * * *", args=["index", "all"], env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"}, volumes=[vol]),
                JobSpec(name="reindex-code-public", image="cerebro/code-unit", build="images/code-unit", scope="public",
                        args=["index", "--force"], volumes=[vol.model_copy()])]


class FakeMemory(Adapter):
    kind, name = "memory", "fakesight"

    def units(self):
        return [UnitSpec(name="memory", role="memory", image="ghcr.io/vectorize-io/hindsight:0.9.2", ports=[PortSpec(port=8888)],
                         env={"HINDSIGHT_API_DATABASE_URL": "postgresql://cerebro:${POSTGRES_PASSWORD}@postgres:5432/hindsight",
                              "HINDSIGHT_API_LLM_BASE_URL": self.ctx.config.inference.llm.base_url},
                         secret_env=["HINDSIGHT_API_KEY"], depends_on=["postgres"])]


class FakeAuth(Adapter):
    kind, name = "auth", "fakecloak"

    def units(self):
        return [UnitSpec(name="auth", role="auth", image="quay.io/keycloak/keycloak:26.0", args=["start-dev"],
                         ports=[PortSpec(port=8080)], health_path="/health/ready",
                         env={"KC_DB": "postgres", "KC_DB_URL": "jdbc:postgresql://postgres:5432/keycloak",
                              "KC_DB_USERNAME": "cerebro", "KC_DB_PASSWORD": "${POSTGRES_PASSWORD}",
                              "KC_BOOTSTRAP_ADMIN_PASSWORD": "${CEREBRO_AUTH_ADMIN_PASSWORD}"},
                         depends_on=["postgres"])]


def fake_adapters(ctx) -> list[Adapter]:
    return [FakeDocs({}, ctx), FakeCode({}, ctx), FakeMemory({}, ctx), FakeAuth({}, ctx)]


PLUGIN_SOURCE = '''
from cerebro.sdk import Plugin, LiveSource, McpUpstream

class FakeLive(LiveSource):
    name = "fakemcp"

PLUGIN = Plugin(name="fakemcp", live=FakeLive, description="test plugin with an MCP upstream",
                mcp=McpUpstream(image="ghcr.io/example/mcp:1.0", port=9000, path="/mcp", args=["--stateless"],
                                env={"FAKE_URL": "${FAKE_URL:-http://fake.internal}", "FAKE_TOKEN": "${FAKE_TOKEN}"}))
'''


def write_plugins(directory: pathlib.Path) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fakemcp.py").write_text(PLUGIN_SOURCE)
    return directory
