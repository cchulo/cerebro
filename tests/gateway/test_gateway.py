"""The gateway end to end, in process: MCP client -> httpx ASGITransport -> Starlette app -> FastMCP -> fakes.
Identity is `static` (tokens for alice: no group, bob: payments-team, ci: service with code.read only)."""
import json
from contextlib import asynccontextmanager
import httpx, pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from cerebro.core import TokenScope, registry
from cerebro.gateway.identity import build_identity
from cerebro.gateway.live import LiveRegistry
from cerebro.gateway.server import Gateway, keywords
from .conftest import ISSUER, RESOURCE
from .fakes import FakeDocs, FakeCode, FakeMemory, FakeConfluenceLive, fake_plugins

URL = "http://127.0.0.1:8090/mcp"
TOKENS = {"tok-alice": {"subject": "alice"},
          "tok-bob": {"subject": "bob", "groups": ["payments-team"]},
          "tok-ci": {"subject": "ci-bot", "kind": "service", "groups": ["sre"], "token_scopes": ["cerebro:code.read"]}}
STATIC = {"mode": "static", "tokens": TOKENS}


def make_gateway(make_config, make_ctx, *, identity=None, docs=None, code=None, memory=None, plugins=None, **overrides) -> Gateway:
    cfg = make_config(identity=identity or STATIC, **overrides)
    ctx = make_ctx(cfg, CEREBRO_TOKEN="remote-secret")
    docs = docs if docs is not None else FakeDocs(answers={"public": ("deploy with make deploy", True)})
    return Gateway(cfg, identity=build_identity(cfg, ctx), policy=registry.build("policy", "groups", {}, ctx),
                   docs=docs, code=code if code is not None else FakeCode(), memory=memory if memory is not None else FakeMemory(),
                   live=LiveRegistry(cfg, plugins=fake_plugins() if plugins is None else plugins))


@pytest.fixture
def gateway(make_config, make_ctx) -> Gateway:
    return make_gateway(make_config, make_ctx)


def http(gw: Gateway, client_host: str = "127.0.0.1", token: str | None = None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app, client=(client_host, 40000)),
                             base_url="http://127.0.0.1:8090", headers=headers)


@asynccontextmanager
async def session(gw: Gateway, token: str | None, client_host: str = "127.0.0.1"):
    """An initialised MCP ClientSession against the in-process app. Each session serves a fresh Gateway over the
    same adapters (a StreamableHTTPSessionManager runs once per instance; uvicorn's lifespan does that in
    production), so state in the fakes carries across sessions while the manager does not."""
    served = Gateway(gw.config, identity=gw.identity, policy=gw.policy, docs=gw.docs, code=gw.code, memory=gw.memory, live=gw.live)
    async with served.mcp.session_manager.run():
        async with http(served, client_host, token) as client:
            async with streamable_http_client(URL, http_client=client) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    yield s


async def call(s: ClientSession, name: str, **args):
    """(is_error, parsed result or error text)"""
    res = await s.call_tool(name, args)
    text = "".join(getattr(b, "text", "") for b in res.content)
    if res.isError:
        return True, text
    return False, res.structuredContent if res.structuredContent is not None else json.loads(text)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


# ------------------------------------------------------------------------------------------------- HTTP surface
async def test_unauthenticated_gets_401_with_challenge(gateway):
    async with http(gateway) as h:
        r = await h.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
        assert r.json()["error"] == "invalid_token"
        r = await h.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer nope"})
        assert r.status_code == 401
        assert (await h.get("/health")).status_code == 200            # health needs no identity


async def test_bearer_mode_challenge_points_at_resource_metadata(make_config, make_ctx, issuer):
    gw = make_gateway(make_config, make_ctx, identity={"mode": "external", "issuer": ISSUER, "audience": RESOURCE},
                      gateway={"public_url": RESOURCE})
    async with http(gw) as h:
        r = await h.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == 'Bearer resource_metadata="http://gateway.test/.well-known/oauth-protected-resource"'
        md = await h.get("/.well-known/oauth-protected-resource")
        assert md.status_code == 200 and md.json()["resource"] == RESOURCE and md.json()["authorization_servers"] == [ISSUER]
        assert "cerebro:docs.read" in md.json()["scopes_supported"]
        suffixed = await h.get("/.well-known/oauth-protected-resource/mcp")      # RFC 9728 path form
        assert suffixed.status_code == 200 and suffixed.json() == md.json()


async def test_metadata_is_404_without_a_bearer_identity(gateway):
    async with http(gateway) as h:
        assert (await h.get("/.well-known/oauth-protected-resource")).status_code == 404
        health = (await h.get("/health")).json()
        assert health["ok"] and health["identity"] == "static" and health["engines"]["docs"] == "fake-docs"


async def test_mode_none_binds_loopback_and_refuses_remote_peers(make_config, make_ctx):
    gw = make_gateway(make_config, make_ctx, identity={"mode": "none", "principal": {"subject": "me", "groups": ["sre"]}},
                      gateway={"host": "0.0.0.0"})
    assert gw.host == "127.0.0.1" and gw.mcp.settings.host == "127.0.0.1"
    async with http(gw, client_host="10.0.0.9") as h:
        assert (await h.post("/mcp", json=INIT, headers=MCP_HEADERS)).status_code == 401
    async with session(gw, None) as s:
        err, me = await call(s, "whoami")
        assert not err and me["subject"] == "me" and me["scopes"] == ["public", "infra"]
    remote = make_gateway(make_config, make_ctx, identity={"mode": "none", "allow_remote": True}, gateway={"host": "0.0.0.0"})
    assert remote.host == "0.0.0.0"
    async with session(remote, "remote-secret", client_host="10.0.0.9") as s:
        err, me = await call(s, "whoami")
        assert not err and me["subject"] == "me"


# ------------------------------------------------------------------------------------------------- grants
async def test_whoami_and_list_scopes_follow_the_policy(gateway):
    async with session(gateway, "tok-alice") as s:
        err, me = await call(s, "whoami")
        assert not err and me["subject"] == "alice" and me["scopes"] == ["public"] and me["banks"] == ["user-alice"]
        assert me["kind"] == "user" and me["token_scopes"] == sorted(TokenScope.all()) and me["issuer"] == "static"
        err, ls = await call(s, "list_scopes")
        assert not err and ls == {"user": "alice", "scopes": ["public"], "banks": ["user-alice"],
                                  "repos": ["https://github.com/pallets/click.git", "https://github.com/pallets/flask.git"]}
    async with session(gateway, "tok-bob") as s:
        err, ls = await call(s, "list_scopes")
        assert not err and ls["scopes"] == ["public", "payments"] and "team-payments-team" in ls["banks"]
        assert "https://github.com/pallets/jinja.git" in ls["repos"]


async def test_instructions_and_prompts_are_served(gateway):
    async with session(gateway, "tok-alice") as s:
        prompts = {p.name for p in (await s.list_prompts()).prompts}
        assert prompts == {"start_task", "wrap_up"}
        got = await s.get_prompt("start_task", {"task": "rotate keys"})
        assert "rotate keys" in got.messages[0].content.text and "code_tool" in got.messages[0].content.text


# ------------------------------------------------------------------------------------------------- docs
async def test_query_docs_enforces_scopes_and_falls_back_to_live_sources(gateway):
    FakeConfluenceLive.calls.clear()
    async with session(gateway, "tok-alice") as s:
        err, text = await call(s, "query_docs", query="x", scopes=["payments"])
        assert err and "not allowed" in text
        err, out = await call(s, "query_docs", query="how do we deploy the checkout service?")
        assert not err and out["mode"] == "mix" and [r["scope"] for r in out["results"]] == ["public"]
        assert out["results"][0]["answered"] and "fallback" not in out
        assert gateway.docs.calls[-1] == ("public", "how do we deploy the checkout service?", "mix")
        err, text = await call(s, "query_docs", query="x", mode="no-such-mode")
        assert err and "mode must be one of" in text
    async with session(gateway, "tok-bob") as s:
        err, out = await call(s, "query_docs", query="When does the daily settlement batch close?", mode="naive")
        assert not err and {r["scope"]: r["answered"] for r in out["results"]} == {"public": True, "payments": False}
        fb = out["fallback"]
        assert set(fb) == {"confluence", "jama"}                       # payments lists both plugins under docs:
        assert fb["confluence"]["query"] == "daily settlement batch close"
        assert [r["space"] for r in fb["confluence"]["results"]] == ["PAY"]   # only the missed scope's spaces
        assert FakeConfluenceLive.calls[-1]["allowed"] == [{"scope": "payments", "spaces": ["PAY"]}]
        err, out = await call(s, "query_docs", query="x", fallback=False)
        assert not err and "fallback" not in out


async def test_live_search_and_fetch_are_confined_to_the_callers_scopes(gateway):
    async with session(gateway, "tok-alice") as s:
        err, out = await call(s, "live_search", source="confluence", query="settlement")
        assert not err and out["allowed"] == [{"scope": "public", "spaces": ["ENG", "DOCS"]}]
        assert {r["space"] for r in out["results"]} == {"ENG", "DOCS"}
        err, text = await call(s, "live_search", source="confluence", query="x", scopes=["payments"])
        assert err and "not allowed" in text
        err, out = await call(s, "live_search", source="jama", query="x")
        assert not err and out["results"] == [] and "none of your scopes" in out["note"]
        err, text = await call(s, "live_fetch", source="confluence", ref="payments-PAY-1")
        assert err and "outside your scopes" in text
        err, page = await call(s, "live_fetch", source="confluence", ref="public-ENG-1")
        assert not err and page["text"] == "page body"
        err, text = await call(s, "live_search", source="backstage", query="x")
        assert err and "no enabled live source" in text


async def test_live_overrides_come_from_sources_plugin_live(make_config, make_ctx):
    FakeConfluenceLive.calls.clear()
    gw = make_gateway(make_config, make_ctx, docs=FakeDocs(),                              # nothing answers
                      sources={"confluence": {"rest_url": "https://wiki", "live": {"via": "rest", "fallback": False}},
                               "jama": {"live": {"enabled": False}}})
    async with session(gw, "tok-bob") as s:
        err, out = await call(s, "query_docs", query="anything at all")
        assert not err and out.get("fallback") == {}                    # confluence: fallback off, jama: disabled
        err, out = await call(s, "live_search", source="confluence", query="q")    # still callable directly
        assert not err and FakeConfluenceLive.calls[-1]["options"] == {"rest_url": "https://wiki", "via": "rest"}
        err, text = await call(s, "live_search", source="jama", query="q")
        assert err and "no enabled live source" in text


def test_keywords_reduction():
    assert keywords("How do we deploy the checkout service to prod?") == "deploy checkout service prod"
    assert keywords("the") == "the"


# ------------------------------------------------------------------------------------------------- code
async def test_search_code_fans_out_per_unit_and_drops_foreign_hits(gateway):
    async with session(gateway, "tok-alice") as s:
        err, out = await call(s, "search_code", query="def main")
        assert not err and out["units"] == ["code-public"]
        assert {h["repository"] for h in out["hits"]} == {"github.com/pallets/click", "github.com/pallets/flask"}
        assert all(h["unit"] == "code-public" for h in out["hits"])
        assert gateway.code.searches[-1]["repos"] == ["github.com/pallets/click", "github.com/pallets/flask"]
        err, text = await call(s, "search_code", query="x", scopes=["payments"])
        assert err and "not allowed" in text
        err, text = await call(s, "search_code", query="x", branch="stable")
        assert err and "code-public: fake engine keeps no per-branch index" in text
    async with session(gateway, "tok-bob") as s:
        err, out = await call(s, "search_code", query="render", max_results=2)
        assert not err and len(out["units"]) == 2 and out["units"][0] == "code-public" and out["units"][1].startswith("code-payments-")
        assert FakeCode.LEAK not in {h["repository"] for h in out["hits"]} and len(out["hits"]) == 2
        err, out = await call(s, "search_code", query="render", scopes=["payments"])
        assert not err and [h["repository"] for h in out["hits"]] == ["github.com/pallets/jinja"]


async def test_code_units_and_code_tool_are_limited_to_the_callers_scopes(gateway):
    async with session(gateway, "tok-alice") as s:
        err, out = await call(s, "list_code_units")
        assert not err and [u["name"] for u in out["units"]] == ["code-public"]
        assert out["units"][0]["capabilities"]["tools"][0]["name"] == "stats"
        err, res = await call(s, "code_tool", unit="code-public", tool="stats")
        assert not err and res["unit"] == "code-public" and res["structured"] == {"repos": 2} and not res["is_error"]
        err, text = await call(s, "code_tool", unit="code-infra", tool="stats")
        assert err and "not allowed" in text
        err, text = await call(s, "code_tool", unit="code-public", tool="delete_everything")
        assert err and "not allowed" in text
        assert ("code-infra", "stats", {}) not in gateway.code.calls


# ------------------------------------------------------------------------------------------------- memory
async def test_memory_banks_are_enforced(gateway):
    async with session(gateway, "tok-alice") as s:
        err, text = await call(s, "recall", query="x", bank="team-payments-team")
        assert err and "not allowed" in text
        err, out = await call(s, "retain", content="deployed 2.3.1; rollback flag in runbook was wrong", tags=["deploy"])
        assert not err and out["bank"] == "user-alice" and out["accepted"]
        err, out = await call(s, "recall", query="rollback")
        assert not err and out["bank"] == "user-alice" and out["results"][0]["tags"] == ["deploy"]
        err, out = await call(s, "reflect", query="what do we know?")
        assert not err and out["bank"] == "user-alice" and "1 memories" in out["text"]
        err, text = await call(s, "recall", query="x", budget="huge")
        assert err and "budget must be one of" in text
    async with session(gateway, "tok-bob") as s:
        err, out = await call(s, "retain", content="settlement closes at 23:00 UTC", bank="team-payments-team")
        assert not err and out["bank"] == "team-payments-team"
    assert set(gateway.memory.banks) == {"user-alice", "team-payments-team"}


async def test_reflect_is_hidden_when_the_store_cannot(make_config, make_ctx):
    gw = make_gateway(make_config, make_ctx, memory=FakeMemory(supports_reflect=False))
    async with session(gw, "tok-alice") as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert "reflect" not in names and "recall" in names


# ------------------------------------------------------------------------------------------------- token scopes
async def test_token_scopes_hide_and_block_tools(gateway):
    async with session(gateway, "tok-ci") as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert names == {"whoami", "list_scopes", "search_code", "list_code_units", "code_tool"}
        err, me = await call(s, "whoami")
        assert not err and me["kind"] == "service" and me["personal_bank"] is None and me["banks"] == ["team-sre"]
        assert me["token_scopes"] == ["cerebro:code.read"] and me["scopes"] == ["public", "infra"]
        err, text = await call(s, "query_docs", query="x")
        assert err and "token lacks scope cerebro:docs.read" in text
        err, text = await call(s, "retain", content="x")
        assert err and "token lacks scope cerebro:memory.write" in text
        err, out = await call(s, "search_code", query="x")
        assert not err and set(out["units"]) == {"code-public", "code-infra"}
    async with session(gateway, "tok-alice") as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert names == {"whoami", "list_scopes", "query_docs", "live_search", "live_fetch", "search_code",
                         "list_code_units", "code_tool", "recall", "retain", "reflect"}


async def test_service_principal_without_bank_must_name_a_team_bank(make_config, make_ctx):
    tokens = {"tok-svc": {"subject": "svc", "kind": "service", "groups": ["sre"]}}
    gw = make_gateway(make_config, make_ctx, identity={"mode": "static", "tokens": tokens})
    async with session(gw, "tok-svc") as s:
        err, text = await call(s, "retain", content="x")
        assert err and "no personal bank" in text
        err, out = await call(s, "retain", content="x", bank="team-sre")
        assert not err and out["bank"] == "team-sre"


# ------------------------------------------------------------------------------------------------- startup
def test_from_config_builds_adapters_and_tolerates_missing_engine_modules(make_config):
    import importlib.util
    cfg = make_config(identity=STATIC)
    gw = Gateway.from_config(cfg)
    assert gw.identity.name == "static" and gw.policy.name == "groups"
    assert set(gw.tool_scopes) >= {"whoami", "query_docs", "search_code", "recall", "retain"}
    for kind, type_ in (("provision", cfg.provisioning.target), ("docs", "lightrag"), ("code", "tokensave"), ("memory", "hindsight")):
        exists = importlib.util.find_spec(f"cerebro.adapters.{kind}.{type_}") is not None
        assert exists or any(f"'{type_}'" in n for n in gw.notes), f"missing {kind} adapter must be reported in notes"
    if importlib.util.find_spec("cerebro.adapters.provision.compose") is None:
        assert any("StaticLocator" in n for n in gw.notes)
