import pytest
from cerebro.core.contracts import Provisioner, UnitSpec, JobSpec, PortSpec, VolumeSpec


class ProvisionerContract:
    @pytest.fixture
    def adapter(self, ctx) -> Provisioner:
        raise NotImplementedError

    @pytest.fixture
    def units(self) -> list[UnitSpec]:
        return [UnitSpec(name="docs-public", role="docs", image="ghcr.io/hkuds/lightrag:v1.5.7", scope="public",
                         ports=[PortSpec(port=9621)], env={"WORKSPACE": "scope_public"}, secret_env=["LIGHTRAG_API_KEY"],
                         volumes=[VolumeSpec(name="data", mount_path="/app/data")]),
                UnitSpec(name="code-public", role="code", image="cerebro/code-bridge:dev", build="images/code-bridge",
                         scope="public", ports=[PortSpec(port=8045)], idle_ttl="2h")]

    @pytest.fixture
    def jobs(self) -> list[JobSpec]:
        return [JobSpec(name="index-code-public", image="cerebro/code-bridge:dev", schedule="0 3 * * *", scope="public",
                        args=["index", "all"])]

    def test_is_provisioner(self, adapter):
        assert isinstance(adapter, Provisioner) and adapter.kind == "provision"

    def test_endpoint_uses_unit_name(self, adapter, units):
        url = adapter.endpoint(units[0].name)
        assert url.startswith("http") and units[0].name in url

    def test_render_covers_every_unit_and_job(self, adapter, units, jobs):
        files = adapter.render(units, jobs)
        assert isinstance(files, dict) and files
        text = "\n".join(files.values())
        for u in units:
            assert u.name in text
        for j in jobs:
            assert j.name in text
        assert "LIGHTRAG_API_KEY" in text, "secret names must be injected, never values"
