"""`cerebro smoke` in process: the same Smoke driver the CLI runs, connected to the fake gateway of tests/gateway
through httpx's ASGI transport. Proves the persona table and expectations come out of the config + policy, and that
the checks pass against a gateway that behaves, fail against one that leaks."""
import contextlib
import httpx, pytest, yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from cerebro.core import TokenScope
from cerebro.gateway.server import Gateway
from cerebro.smoke import PARTS, Smoke, load_probes, main, personas_from_config
from .test_gateway import STATIC, TOKENS, URL, make_gateway


def connect_in_process(gw: Gateway):
    @contextlib.asynccontextmanager
    async def connect(token: str):
        served = Gateway(gw.config, identity=gw.identity, policy=gw.policy, docs=gw.docs, code=gw.code, memory=gw.memory, live=gw.live)
        async with served.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=served.app, client=("127.0.0.1", 40000)),
                                         base_url="http://127.0.0.1:8090", headers={"Authorization": f"Bearer {token}"}) as client:
                async with streamable_http_client(URL, http_client=client) as (r, w, _):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        yield s
    return connect


@pytest.fixture
def gateway(make_config, make_ctx):
    return make_gateway(make_config, make_ctx)


def test_personas_come_from_the_config_and_the_policy(gateway):
    personas = personas_from_config(gateway.config)
    assert set(personas) == {"alice", "bob", "ci-bot"}
    assert personas["alice"].token == "tok-alice" and personas["alice"].grants.scopes == ["public"]
    assert personas["bob"].grants.banks == ["user-bob", "team-payments-team"]
    ci = personas["ci-bot"]
    assert ci.kind == "service" and ci.grants.personal_bank is None and ci.has(TokenScope.CODE_READ) and not ci.has(TokenScope.DOCS_READ)


def test_non_static_identity_is_refused(make_config):
    with pytest.raises(SystemExit, match="identity.mode static"):
        personas_from_config(make_config(identity={"mode": "none"}))


async def test_smoke_passes_against_a_well_behaved_gateway(gateway):
    lines = []
    smoke = Smoke(gateway.config, personas_from_config(gateway.config), connect_in_process(gateway), out=lines.append, verbose=True)
    assert await smoke.run() == 0, [r for r in smoke.results if not r.ok]
    checks = {(r.persona, r.check) for r in smoke.results}
    assert ("alice", "query_docs scope=payments refused") in checks
    assert ("bob", "recall from foreign bank user-alice refused") in checks
    assert ("alice", "code_tool on foreign unit code-payments-jinja-github-com-pallet-737f99 refused") in checks
    assert ("ci-bot", "query_docs refused without its token scope") in checks
    assert ("alice", "search_code hits only repositories of the caller's scopes") in checks
    assert not any("branch" in r.check for r in smoke.results), "the fake engine declares no branches: branch checks are skipped"
    assert any(line.startswith("slowest calls") for line in lines) and "passed, 0 failed" in "\n".join(lines)


async def test_smoke_catches_a_leaking_gateway(gateway, monkeypatch):
    """A policy that grants everyone every scope: whoami no longer matches what the smoke test computed."""
    from cerebro.core import Grants
    real = gateway.policy.grants

    def leaky(principal):
        g = real(principal)
        return Grants(**{**g.model_dump(), "scopes": list(gateway.config.scopes)})
    monkeypatch.setattr(gateway.policy, "grants", leaky)
    smoke = Smoke(gateway.config, personas_from_config(gateway.config), connect_in_process(gateway), out=lambda s: None,
                  only=("identity",))
    assert await smoke.run(["alice"]) == 1
    failed = {r.check for r in smoke.results if not r.ok}
    assert "whoami scopes match the policy" in failed


async def test_probes_and_live_come_from_the_yaml(gateway, tmp_path):
    probes = tmp_path / "probes.yaml"
    probes.write_text(yaml.safe_dump({
        "docs": [{"name": "public deploy", "query": "how do we deploy", "marker": "make deploy", "scope": "public",
                  "visible_to": ["alice", "bob"], "hidden_from": []},
                 {"name": "never", "query": "fraud thresholds", "marker": "RESTRICTED-QX-9911", "hidden_from": ["alice", "bob"]}],
        "live": {"source": "confluence",
                 "search": [{"query": "settlement", "ref": "payments-PAY-1", "space": "PAY", "visible_to": ["bob"], "hidden_from": ["alice"]}],
                 "fetch": [{"ref": "payments-PAY-1", "marker": "page body", "visible_to": ["bob"], "hidden_from": ["alice"]}],
                 "fallback": [{"query": "settlement", "persona": "bob", "scope": "payments"}]}}))
    smoke = Smoke(gateway.config, personas_from_config(gateway.config), connect_in_process(gateway), out=lambda s: None,
                  probes=load_probes(probes), live=True, only=("probes", "live"))
    rc = await smoke.run()
    assert {r.part for r in smoke.results} == {"probes", "live"}
    assert rc == 0, [r for r in smoke.results if not r.ok]


def test_cli_validates_arguments(tmp_path, monkeypatch):
    cfg = tmp_path / "cerebro.yaml"
    cfg.write_text(yaml.safe_dump({"version": 2, "identity": STATIC, "scopes": {"public": {"groups": ["everyone"]}}}))
    with pytest.raises(SystemExit, match="unknown persona"):
        main(["-c", str(cfg), "--as", "nobody"])
    with pytest.raises(SystemExit, match="exactly one --as"):
        main(["-c", str(cfg), "--token", "t"])
    with pytest.raises(SystemExit, match="unknown part"):
        main(["-c", str(cfg), "--only", "identity,nope"])
    assert set(PARTS) == {"identity", "docs", "code", "memory", "probes", "live"} and set(TOKENS) == {"tok-alice", "tok-bob", "tok-ci"}
