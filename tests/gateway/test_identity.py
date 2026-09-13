"""Identity adapters through the contract mixin plus each adapter's own rules."""
import time
import httpx, pytest
from cerebro.core import TokenScope, registry
from cerebro.core.contracts import RequestInfo
from cerebro.core.types import Unauthenticated
from cerebro.gateway.identity import build_identity, identity_type, Chain
from tests.contracts.identity import IdentityProviderContract
from .conftest import ISSUER, RESOURCE

LOCAL = RequestInfo(headers={}, client_host="127.0.0.1")
REMOTE = RequestInfo(headers={}, client_host="10.0.0.9")


def bearer(token: str, host: str = "10.0.0.9", **headers) -> RequestInfo:
    return RequestInfo(headers={"authorization": f"Bearer {token}", **headers}, client_host=host)


# ------------------------------------------------------------------------------------------------- none
class TestNone(IdentityProviderContract):
    @pytest.fixture
    def adapter(self, make_config, make_ctx):
        return registry.build("identity", "none", {}, make_ctx(make_config()))

    @pytest.fixture
    def good_request(self):
        return LOCAL

    @pytest.fixture
    def bad_request(self):
        return REMOTE

    async def test_fixed_principal_has_every_scope(self, adapter):
        p = await adapter.resolve(LOCAL)
        assert p.subject == "me" and p.groups == {"everyone", "admin"} and p.token_scopes == TokenScope.all()
        assert p.issuer is None and p.kind == "user"

    async def test_allow_remote_requires_the_static_token(self, make_config, make_ctx):
        cfg = make_config(identity={"mode": "none", "allow_remote": True, "principal": {"subject": "me"}})
        a = registry.build("identity", "none", {}, make_ctx(cfg, CEREBRO_TOKEN="s3cret"))
        assert (await a.resolve(bearer("s3cret"))).subject == "me"
        with pytest.raises(Unauthenticated):
            await a.resolve(REMOTE)
        with pytest.raises(Unauthenticated):
            await a.resolve(bearer("wrong"))
        with pytest.raises(Unauthenticated):                       # loopback too: a same-host proxy cannot bypass it
            await a.resolve(LOCAL)
        unset = registry.build("identity", "none", {}, make_ctx(cfg))
        with pytest.raises(Unauthenticated, match="CEREBRO_TOKEN"):
            await unset.resolve(bearer("anything"))


# ------------------------------------------------------------------------------------------------- static
class TestStatic(IdentityProviderContract):
    @pytest.fixture
    def adapter(self, make_config, make_ctx):
        cfg = make_config(identity={"mode": "static", "tokens": {
            "tok-alice": {"subject": "alice", "groups": ["payments-team"]},
            "tok-ci": {"subject": "ci", "kind": "service", "token_scopes": ["cerebro:code.read"]}}})
        return registry.build("identity", "static", {}, make_ctx(cfg))

    @pytest.fixture
    def good_request(self):
        return bearer("tok-alice")

    async def test_seed_fields_carry_over(self, adapter):
        a = await adapter.resolve(bearer("tok-alice"))
        assert a.subject == "alice" and a.groups == {"payments-team"} and a.token_scopes == TokenScope.all()
        ci = await adapter.resolve(bearer("tok-ci"))
        assert ci.kind == "service" and ci.token_scopes == {"cerebro:code.read"} and ci.issuer == "static"
        with pytest.raises(Unauthenticated):
            await adapter.resolve(REMOTE)


# ------------------------------------------------------------------------------------------------- trusted headers
class TestTrustedHeaders(IdentityProviderContract):
    @pytest.fixture
    def adapter(self, make_config, make_ctx):
        cfg = make_config(identity={"legacy": {"type": "trusted_headers", "user_header": "X-Auth-User", "groups_header": "X-Auth-Groups"}})
        return registry.build("identity", "trusted_headers", {}, make_ctx(cfg))

    @pytest.fixture
    def good_request(self):
        return RequestInfo.from_headers({"X-Auth-User": "bob", "X-Auth-Groups": "payments-team, sre"}, client_host="10.0.0.2")

    async def test_groups_split_on_commas_and_always_groups_are_not_added(self, adapter, good_request):
        p = await adapter.resolve(good_request)
        assert p.subject == "bob" and p.groups == {"payments-team", "sre"} and p.issuer == "trusted-headers"
        solo = await adapter.resolve(RequestInfo(headers={"x-auth-user": "carol"}))
        assert solo.groups == frozenset()

    async def test_default_header_names(self, make_config, make_ctx):
        a = registry.build("identity", "trusted_headers", {}, make_ctx(make_config()))
        p = await a.resolve(RequestInfo.from_headers({"X-Forwarded-User": "dan", "X-Forwarded-Groups": "sre"}))
        assert p.subject == "dan" and p.groups == {"sre"}


# ------------------------------------------------------------------------------------------------- bearer_jwt
EXTERNAL = {"mode": "external", "issuer": ISSUER, "audience": RESOURCE}


class TestBearerJwt(IdentityProviderContract):
    @pytest.fixture
    def adapter(self, make_config, make_ctx, issuer):
        cfg = make_config(identity=EXTERNAL, gateway={"public_url": RESOURCE})
        return registry.build("identity", "bearer_jwt", {}, make_ctx(cfg))

    @pytest.fixture
    def good_request(self, sign):
        return bearer(sign(groups=["payments-team"], scope="openid cerebro:docs.read"))

    async def test_claims_map_to_principal(self, adapter, sign):
        p = await adapter.resolve(bearer(sign(groups=["payments-team", "sre"], scope="openid cerebro:docs.read cerebro:memory.read",
                                              email="alice@example.com", name="Alice")))
        assert p.subject == "alice" and p.groups == {"payments-team", "sre"} and p.kind == "user"
        assert p.token_scopes == {"cerebro:docs.read", "cerebro:memory.read"}
        assert p.display_name == "Alice" and p.issuer == ISSUER

    async def test_plain_oidc_token_gets_every_scope(self, adapter, sign):
        p = await adapter.resolve(bearer(sign(scope="openid profile email", groups="payments-team sre")))
        assert p.token_scopes == TokenScope.all() and p.groups == {"payments-team", "sre"}

    async def test_client_credentials_token_is_a_service(self, adapter, sign):
        p = await adapter.resolve(bearer(sign(sub="svc-ci", client_id="svc-ci", scope="cerebro:code.read")))
        assert p.kind == "service" and p.subject == "svc-ci"
        q = await adapter.resolve(bearer(sign(sub="u1", azp="cerebro-cli", preferred_username="alice")))
        assert q.kind == "user"

    @pytest.mark.parametrize("claims", [
        {"aud": "https://something-else/mcp"},                    # RFC 8707: minted for another resource
        {"iss": "https://evil.test"},                             # wrong issuer
        {"exp": int(time.time()) - 600},                          # expired (beyond leeway)
    ])
    async def test_rejects_bad_claims(self, adapter, sign, claims):
        with pytest.raises(Unauthenticated):
            await adapter.resolve(bearer(sign(**claims)))

    async def test_audience_may_be_a_list_containing_the_resource(self, adapter, sign):
        p = await adapter.resolve(bearer(sign(aud=["account", RESOURCE])))
        assert p.subject == "alice"

    async def test_rejects_unknown_key_after_one_refetch(self, adapter, sign, issuer):
        with pytest.raises(Unauthenticated, match="key"):
            await adapter.resolve(bearer(sign(kid="rotated")))
        assert issuer.jwks_route.call_count == 2

    async def test_rejects_garbage_and_missing_token(self, adapter):
        with pytest.raises(Unauthenticated):
            await adapter.resolve(bearer("not.a.jwt"))
        with pytest.raises(Unauthenticated):
            await adapter.resolve(REMOTE)

    async def test_jwks_is_cached(self, adapter, sign, issuer):
        await adapter.resolve(bearer(sign()))
        await adapter.resolve(bearer(sign()))
        assert issuer.jwks_route.call_count == 1

    def test_rfc9728_metadata_and_challenge(self, adapter):
        md = adapter.protected_resource_metadata()
        assert md == {"resource": RESOURCE, "authorization_servers": [ISSUER],
                      "scopes_supported": [s.value for s in TokenScope], "bearer_methods_supported": ["header"]}
        assert adapter.challenge() == {"WWW-Authenticate": 'Bearer resource_metadata="http://gateway.test/.well-known/oauth-protected-resource"'}

    async def test_explicit_jwks_url_skips_discovery(self, make_config, make_ctx, sign, jwks):
        import respx
        with respx.mock() as mock:
            route = mock.get("https://keys.test/jwks.json").mock(return_value=httpx.Response(200, json=jwks))
            cfg = make_config(identity={**EXTERNAL, "jwks_url": "https://keys.test/jwks.json"}, gateway={"public_url": RESOURCE})
            a = registry.build("identity", "bearer_jwt", {}, make_ctx(cfg))
            assert (await a.resolve(bearer(sign()))).subject == "alice"
            assert route.called

    async def test_resource_id_without_public_url(self, make_config, make_ctx, sign, issuer):
        cfg = make_config(identity={**EXTERNAL, "audience": "http://127.0.0.1:8090/mcp"})
        a = registry.build("identity", "bearer_jwt", {}, make_ctx(cfg))
        assert (await a.resolve(bearer(sign(aud="http://127.0.0.1:8090/mcp")))).subject == "alice"
        assert a.challenge()["WWW-Authenticate"].endswith('"http://127.0.0.1:8090/.well-known/oauth-protected-resource"')


# ------------------------------------------------------------------------------------------------- bearer_introspect
INTROSPECT = {**EXTERNAL, "token_validation": "introspection",
              "introspection": {"client_id": "cerebro-gateway", "client_secret_env": "CEREBRO_OAUTH_CLIENT_SECRET"}}


class TestBearerIntrospect(IdentityProviderContract):
    @pytest.fixture
    def adapter(self, make_config, make_ctx, issuer):
        issuer.introspection["opaque-alice"] = {"active": True, "sub": "alice", "username": "alice", "iss": ISSUER,
                                                "aud": RESOURCE, "groups": ["sre"], "scope": "cerebro:docs.read"}
        issuer.introspection["opaque-ci"] = {"active": True, "sub": "ci-bot", "client_id": "ci-bot", "scope": "cerebro:code.read"}
        issuer.introspection["opaque-foreign"] = {"active": True, "sub": "x", "aud": "https://other/mcp"}
        issuer.introspection["opaque-dead"] = {"active": False}
        cfg = make_config(identity=INTROSPECT, gateway={"public_url": RESOURCE})
        return registry.build("identity", "bearer_introspect", {}, make_ctx(cfg, CEREBRO_OAUTH_CLIENT_SECRET="pw"))

    @pytest.fixture
    def good_request(self):
        return bearer("opaque-alice")

    async def test_active_token_maps_like_a_jwt(self, adapter, issuer):
        p = await adapter.resolve(bearer("opaque-alice"))
        assert p.subject == "alice" and p.groups == {"sre"} and p.token_scopes == {"cerebro:docs.read"} and p.issuer == ISSUER
        assert issuer.last_introspection_auth.startswith("Basic ")
        svc = await adapter.resolve(bearer("opaque-ci"))
        assert svc.kind == "service"

    @pytest.mark.parametrize("token", ["opaque-dead", "opaque-foreign", "never-seen"])
    async def test_inactive_or_foreign_tokens_are_rejected(self, adapter, token):
        with pytest.raises(Unauthenticated):
            await adapter.resolve(bearer(token))

    async def test_endpoint_from_config_overrides_discovery(self, make_config, make_ctx, issuer):
        import respx
        route = issuer.post("https://custom.test/introspect").mock(return_value=httpx.Response(200, json={"active": True, "sub": "zed"}))
        cfg = make_config(identity={**INTROSPECT, "introspection": {**INTROSPECT["introspection"], "url": "https://custom.test/introspect"}},
                          gateway={"public_url": RESOURCE})
        a = registry.build("identity", "bearer_introspect", {}, make_ctx(cfg, CEREBRO_OAUTH_CLIENT_SECRET="pw"))
        assert (await a.resolve(bearer("whatever"))).subject == "zed" and route.called


# ------------------------------------------------------------------------------------------------- factory / chain
def test_identity_type_follows_mode(make_config):
    assert identity_type(make_config()) == "none"
    assert identity_type(make_config(identity={"mode": "static", "tokens": {"t": {"subject": "a"}}})) == "static"
    assert identity_type(make_config(identity=EXTERNAL)) == "bearer_jwt"
    assert identity_type(make_config(identity=INTROSPECT)) == "bearer_introspect"
    assert identity_type(make_config(identity={"mode": "builtin"})) == "bearer_jwt"


async def test_build_identity_chains_legacy_headers_behind_bearer(make_config, make_ctx, issuer, sign):
    cfg = make_config(identity={**EXTERNAL, "legacy": {"type": "trusted_headers"}}, gateway={"public_url": RESOURCE})
    ident = build_identity(cfg, make_ctx(cfg))
    assert isinstance(ident, Chain) and [p.name for p in ident.providers] == ["bearer_jwt", "trusted_headers"]
    assert (await ident.resolve(bearer(sign()))).issuer == ISSUER
    via_proxy = await ident.resolve(RequestInfo.from_headers({"X-Forwarded-User": "eve", "X-Forwarded-Groups": "sre"}))
    assert via_proxy.subject == "eve" and via_proxy.issuer == "trusted-headers"
    with pytest.raises(Unauthenticated, match="bearer_jwt.*trusted_headers"):
        await ident.resolve(REMOTE)
    assert "resource_metadata=" in ident.challenge()["WWW-Authenticate"]
    assert ident.protected_resource_metadata()["resource"] == RESOURCE


def test_build_identity_plain(make_config, make_ctx):
    assert build_identity(make_config(), make_ctx(make_config())).name == "none"
