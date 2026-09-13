"""Keycloak adapter: unit shape, issuer format, and seed() against a stateful respx fake of the admin REST API.
Nothing here talks to a real Keycloak; the fake records what was created so idempotency can be asserted by running
seed() twice."""
import json, re
import httpx, pytest, respx
from cerebro.core import registry, TokenScope, load_config
from cerebro.core.config import UserSeed
from cerebro.core.contracts import AuthorizationServer
from cerebro.adapters.auth import keycloak
from cerebro.adapters.auth.keycloak import SeedError
from tests.conftest import ROOT

BASE = "http://auth:8080"
REALM = "cerebro"
RESOURCE = "http://127.0.0.1:8090/mcp"


def _json(request):
    return json.loads(request.content) if request.content else {}


class FakeAdmin:
    """Enough of the Keycloak admin API for seed(): realms, groups, users, client scopes, clients, components,
    client policies. Pre-seeded state models a realm that already has some of what seed() wants."""

    def __init__(self, mock: respx.MockRouter, *, realm_exists=True, passkeys=True):
        self.realm_exists = realm_exists
        self.groups = [{"id": "g-sre", "name": "sre", "path": "/sre"}] if realm_exists else []
        self.users = [{"id": "u-alice", "username": "alice"}] if realm_exists else []
        self.memberships = {"u-alice": []}
        self.scopes = [{"id": "s-docs", "name": TokenScope.DOCS_READ.value, "protocol": "openid-connect",
                        "protocolMappers": [{"id": "pm-1", "name": keycloak.AUDIENCE_MAPPER, "protocolMapper": "oidc-audience-mapper",
                                             "config": {"included.custom.audience": "http://old/mcp"}}]},
                       {"id": "s-code", "name": TokenScope.CODE_READ.value, "protocol": "openid-connect", "protocolMappers": []},
                       {"id": "s-profile", "name": "profile", "protocol": "openid-connect"}] if realm_exists else []
        self.default_scopes = ["s-profile", "s-docs"] if realm_exists else []
        self.clients = []
        self.components = [{"id": "c-th", "name": "Trusted Hosts", "providerId": "trusted-hosts", "providerType": keycloak.CRP_TYPE,
                            "parentId": "realm-id", "subType": "anonymous",
                            "config": {"trusted-hosts": [], "host-sending-registration-request-must-match": ["true"], "client-uris-must-match": ["true"]}},
                           {"id": "c-th-auth", "name": "Trusted Hosts", "providerId": "trusted-hosts", "providerType": keycloak.CRP_TYPE,
                            "parentId": "realm-id", "subType": "authenticated", "config": {"trusted-hosts": []}}] if realm_exists else []
        self.required_actions = [{"alias": "UPDATE_PASSWORD", "enabled": True},
                                 {"alias": keycloak.PASSKEY_ACTION, "enabled": passkeys}]
        self.profiles, self.policies = {"profiles": [], "globalProfiles": [{"name": "fapi-1-baseline"}]}, {"policies": []}
        self.mapper_updates, self.mapper_adds, self.user_creates, self.profile_puts = [], [], [], []
        self.posts = []
        self.seq = 0
        self._register(mock)

    def _id(self, prefix):
        self.seq += 1
        return f"{prefix}-{self.seq}"

    def _created(self, path, oid):
        return httpx.Response(201, headers={"Location": f"{BASE}{path}/{oid}"})

    def _register(self, m):
        A = f"/admin/realms/{REALM}"
        m.post("/realms/master/protocol/openid-connect/token").mock(side_effect=self.token)
        m.get(f"/admin/realms/{REALM}").mock(side_effect=lambda r: httpx.Response(200, json={"id": "realm-id", "realm": REALM}) if self.realm_exists else httpx.Response(404))
        m.post("/admin/realms").mock(side_effect=self.create_realm)
        m.get(f"{A}/authentication/required-actions").mock(side_effect=lambda r: httpx.Response(200, json=self.required_actions))
        m.get(f"{A}/groups").mock(side_effect=lambda r: httpx.Response(200, json=[g for g in self.groups if g["name"] == r.url.params.get("search")]))
        m.post(f"{A}/groups").mock(side_effect=self.create_group)
        m.get(f"{A}/users").mock(side_effect=lambda r: httpx.Response(200, json=[u for u in self.users if u["username"] == r.url.params.get("username")]))
        m.post(f"{A}/users").mock(side_effect=self.create_user)
        m.get(url__regex=rf"{BASE}{A}/users/(?P<uid>[^/]+)/groups$").mock(
            side_effect=lambda r, uid: httpx.Response(200, json=[g for g in self.groups if g["id"] in self.memberships.get(uid, [])]))
        m.put(url__regex=rf"{BASE}{A}/users/(?P<uid>[^/]+)/groups/(?P<gid>[^/]+)$").mock(side_effect=self.join_group)
        m.get(f"{A}/client-scopes").mock(side_effect=lambda r: httpx.Response(200, json=self.scopes))
        m.post(f"{A}/client-scopes").mock(side_effect=self.create_scope)
        m.post(url__regex=rf"{BASE}{A}/client-scopes/(?P<sid>[^/]+)/protocol-mappers/models$").mock(side_effect=self.add_mapper)
        m.put(url__regex=rf"{BASE}{A}/client-scopes/(?P<sid>[^/]+)/protocol-mappers/models/(?P<mid>[^/]+)$").mock(side_effect=self.update_mapper)
        m.get(f"{A}/default-default-client-scopes").mock(
            side_effect=lambda r: httpx.Response(200, json=[{"id": s["id"], "name": s["name"]} for s in self.scopes if s["id"] in self.default_scopes]))
        m.put(url__regex=rf"{BASE}{A}/default-default-client-scopes/(?P<sid>[^/]+)$").mock(side_effect=self.add_default)
        m.get(f"{A}/clients").mock(side_effect=lambda r: httpx.Response(200, json=[c for c in self.clients if c["clientId"] == r.url.params.get("clientId")]))
        m.post(f"{A}/clients").mock(side_effect=self.create_client)
        m.get(f"{A}/components").mock(side_effect=lambda r: httpx.Response(200, json=[c for c in self.components if c["providerType"] == r.url.params.get("type")]))
        m.post(f"{A}/components").mock(side_effect=self.create_component)
        m.put(url__regex=rf"{BASE}{A}/components/(?P<cid>[^/]+)$").mock(side_effect=self.update_component)
        m.get(f"{A}/client-policies/profiles").mock(side_effect=lambda r: httpx.Response(200, json=self.profiles))
        m.put(f"{A}/client-policies/profiles").mock(side_effect=self.put_profiles)
        m.get(f"{A}/client-policies/policies").mock(side_effect=lambda r: httpx.Response(200, json=self.policies))
        m.put(f"{A}/client-policies/policies").mock(side_effect=self.put_policies)

    # ---- handlers
    def token(self, request):
        form = dict(p.split("=", 1) for p in request.content.decode().split("&"))
        if form.get("username") == "admin" and form.get("password") == "pw" and form.get("client_id") == "admin-cli":
            return httpx.Response(200, json={"access_token": "adm-token", "token_type": "Bearer"})
        return httpx.Response(401, json={"error": "invalid_grant"})

    def create_realm(self, request):
        self.posts.append(("realm", _json(request)))
        self.realm_exists = True
        return httpx.Response(201)

    def create_group(self, request):
        body = _json(request); gid = self._id("g")
        self.groups.append({"id": gid, "name": body["name"], "path": f"/{body['name']}"})
        self.posts.append(("group", body))
        return self._created(f"/admin/realms/{REALM}/groups", gid)

    def create_user(self, request):
        body = _json(request); uid = self._id("u")
        self.users.append({"id": uid, "username": body["username"]})
        self.memberships[uid] = [g["id"] for g in self.groups if g["path"] in body.get("groups", [])]
        self.user_creates.append(body); self.posts.append(("user", body))
        return self._created(f"/admin/realms/{REALM}/users", uid)

    def join_group(self, request, uid, gid):
        self.memberships.setdefault(uid, []).append(gid)
        return httpx.Response(204)

    def create_scope(self, request):
        body = _json(request); sid = self._id("s")
        self.scopes.append({"id": sid, **body})
        self.posts.append(("scope", body))
        return self._created(f"/admin/realms/{REALM}/client-scopes", sid)

    def add_mapper(self, request, sid):
        body = _json(request)
        next(s for s in self.scopes if s["id"] == sid).setdefault("protocolMappers", []).append({"id": self._id("pm"), **body})
        self.mapper_adds.append((sid, body))
        return self._created(f"/admin/realms/{REALM}/client-scopes/{sid}/protocol-mappers/models", "x")

    def update_mapper(self, request, sid, mid):
        body = _json(request)
        scope = next(s for s in self.scopes if s["id"] == sid)
        scope["protocolMappers"] = [body if m["id"] == mid else m for m in scope["protocolMappers"]]
        self.mapper_updates.append((sid, mid, body))
        return httpx.Response(204)

    def add_default(self, request, sid):
        self.default_scopes.append(sid)
        return httpx.Response(204)

    def create_client(self, request):
        body = _json(request)
        self.clients.append({"id": self._id("c"), **body}); self.posts.append(("client", body))
        return self._created(f"/admin/realms/{REALM}/clients", self.clients[-1]["id"])

    def create_component(self, request):
        body = _json(request)
        self.components.append({"id": self._id("comp"), **body}); self.posts.append(("component", body))
        return self._created(f"/admin/realms/{REALM}/components", self.components[-1]["id"])

    def update_component(self, request, cid):
        body = _json(request)
        self.components = [body if c["id"] == cid else c for c in self.components]
        return httpx.Response(204)

    def put_profiles(self, request):
        body = _json(request); self.profile_puts.append(body)
        self.profiles = {**self.profiles, "profiles": body["profiles"]}
        return httpx.Response(204)

    def put_policies(self, request):
        self.policies = _json(request)
        return httpx.Response(204)


# ---------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def builtin_ctx(ctx):
    ctx.config.identity.mode = "builtin"
    ctx.config.identity.server = ctx.config.identity.server or __import__("cerebro.core.config", fromlist=["AuthServerConfig"]).AuthServerConfig()
    ctx.secrets.values.update({"CEREBRO_AUTH_ADMIN_USER": "admin", "CEREBRO_AUTH_ADMIN_PASSWORD": "pw"})
    return ctx


@pytest.fixture
def adapter(builtin_ctx):
    return registry.build("auth", "keycloak", {}, builtin_ctx)


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        yield m


USERS = [UserSeed(name="alice", groups=["payments-team"]), UserSeed(name="bob", groups=["sre"], email="bob@example.com")]
GROUPS = ["payments-team", "sre", "platform-leads"]


# ---------------------------------------------------------------------------------------------------- shape
def test_is_authorization_server_and_issuer_format(adapter):
    assert isinstance(adapter, AuthorizationServer) and adapter.kind == "auth" and adapter.name == "keycloak"
    assert adapter.issuer() == "http://auth:8080/realms/cerebro"
    assert adapter.registration_endpoint() == "http://auth:8080/realms/cerebro/clients-registrations/openid-connect"


def test_issuer_follows_public_url(builtin_ctx):
    builtin_ctx.config.identity.server.public_url = "http://localhost:8180/"
    builtin_ctx.config.identity.server.realm = "eng"
    assert registry.build("auth", "keycloak", {}, builtin_ctx).issuer() == "http://localhost:8180/realms/eng"


def test_units_dev_shape(adapter):
    (u,) = adapter.units()
    assert u.name == "auth" and u.role == "auth" and u.image == keycloak.IMAGE == "quay.io/keycloak/keycloak:26.7.3"
    assert u.args == ["start-dev"] and u.http_port == 8080 and u.health_path == "/health/ready"
    assert u.depends_on == ["postgres"] and u.volumes == []
    assert u.env["KC_DB"] == "postgres" and u.env["KC_DB_URL"] == "jdbc:postgresql://postgres:5432/keycloak"
    assert u.env["KC_DB_USERNAME"] == "cerebro" and u.env["KC_DB_PASSWORD"] == "${POSTGRES_PASSWORD}"
    assert u.env["KC_BOOTSTRAP_ADMIN_USERNAME"] == "${CEREBRO_AUTH_ADMIN_USER}"
    assert u.env["KC_BOOTSTRAP_ADMIN_PASSWORD"] == "${CEREBRO_AUTH_ADMIN_PASSWORD}"
    assert u.env["KC_HEALTH_ENABLED"] == "true" and u.env["KC_HTTP_MANAGEMENT_HEALTH_ENABLED"] == "false"
    assert "KC_FEATURES" not in u.env
    assert u.secret_env == ["POSTGRES_PASSWORD", "CEREBRO_AUTH_ADMIN_USER", "CEREBRO_AUTH_ADMIN_PASSWORD"]


def test_units_with_public_url_features_and_custom_secret_names(builtin_ctx):
    s = builtin_ctx.config.identity.server
    s.public_url, s.admin_user_env, s.admin_password_env = "https://sso.example.org", "KC_ADMIN", "KC_ADMIN_PW"
    (u,) = registry.build("auth", "keycloak", {"features": ["cimd"], "proxy_headers": "xforwarded"}, builtin_ctx).units()
    assert u.args == ["start", "--hostname", "https://sso.example.org", "--http-enabled", "true", "--proxy-headers", "xforwarded"]
    assert u.env["KC_FEATURES"] == "cimd"
    assert u.env["KC_BOOTSTRAP_ADMIN_USERNAME"] == "${KC_ADMIN}" and "KC_ADMIN_PW" in u.secret_env


def test_example_yaml_builtin_block_loads_into_adapter():
    cfg = load_config(ROOT / "cerebro.example.yaml", env={})
    cfg.identity.mode = "builtin"
    assert cfg.identity.server is None       # the validator fills it only when mode is builtin at load time
    from cerebro.core.config import AuthServerConfig
    cfg.identity.server = AuthServerConfig(public_url="http://localhost:8180")
    from cerebro.core import AdapterContext
    a = registry.build("auth", "keycloak", {}, AdapterContext(cfg))
    assert a.issuer() == "http://localhost:8180/realms/cerebro"


# ---------------------------------------------------------------------------------------------------- seed
async def test_seed_requires_admin_secrets(ctx, api):
    with pytest.raises(SeedError, match="CEREBRO_AUTH_ADMIN_USER"):
        await registry.build("auth", "keycloak", {}, ctx).seed([], [], RESOURCE)


async def test_seed_login_failure_is_reported(builtin_ctx, api):
    builtin_ctx.secrets.values["CEREBRO_AUTH_ADMIN_PASSWORD"] = "wrong"
    FakeAdmin(api)
    with pytest.raises(SeedError, match="admin login failed"):
        await registry.build("auth", "keycloak", {}, builtin_ctx).seed([], [], RESOURCE)


async def test_seed_reuses_existing_and_creates_missing(adapter, api, capsys):
    fake = FakeAdmin(api)
    report = await adapter.seed(USERS, GROUPS, RESOURCE)

    # every admin call after login carried the token
    assert all(c.request.headers.get("authorization") == "Bearer adm-token" for c in api.calls[1:])
    # realm existed: never re-created
    assert report["created"]["realm"] is False and not [p for p in fake.posts if p[0] == "realm"]
    # groups: sre looked up, payments-team and platform-leads created
    assert report["existing"]["groups"] == ["sre"] and report["created"]["groups"] == ["payments-team", "platform-leads"]
    assert [g["name"] for g in fake.groups] == ["sre", "payments-team", "platform-leads"]
    # users: alice existed and was only added to her group; bob created with passkey action and a temp password
    assert report["existing"]["users"] == ["alice"] and report["created"]["users"] == ["bob"]
    payments_id = next(g["id"] for g in fake.groups if g["name"] == "payments-team")
    assert fake.memberships["u-alice"] == [payments_id]
    (bob,) = fake.user_creates
    assert bob["username"] == "bob" and bob["email"] == "bob@example.com" and bob["groups"] == ["/sre"]
    assert bob["requiredActions"] == [keycloak.PASSKEY_ACTION] and report["first_login_action"] == keycloak.PASSKEY_ACTION
    assert bob["credentials"][0]["temporary"] is True and len(bob["credentials"][0]["value"]) >= 12
    assert report["password_generated"] is True and bob["credentials"][0]["value"] in capsys.readouterr().err
    # client scopes: one per TokenScope plus groups; docs.read existed (audience corrected), code.read got a mapper
    names = {s["name"] for s in fake.scopes}
    assert TokenScope.all() <= names and "groups" in names
    assert set(report["created"]["client_scopes"]) == (TokenScope.all() - {TokenScope.DOCS_READ.value, TokenScope.CODE_READ.value}) | {"groups"}
    assert fake.mapper_updates and fake.mapper_updates[0][:2] == ("s-docs", "pm-1")
    assert fake.mapper_updates[0][2]["config"]["included.custom.audience"] == RESOURCE
    assert fake.mapper_adds == [("s-code", adapter._audience_mapper(RESOURCE))]
    for s in fake.scopes:
        if s["name"] in TokenScope.all():
            aud = [m for m in s["protocolMappers"] if m["protocolMapper"] == "oidc-audience-mapper"]
            assert len(aud) == 1 and aud[0]["config"]["included.custom.audience"] == RESOURCE
            assert s["id"] in fake.default_scopes
    groups_scope = next(s for s in fake.scopes if s["name"] == "groups")
    gm = groups_scope["protocolMappers"][0]
    assert gm["protocolMapper"] == "oidc-group-membership-mapper" and gm["config"]["full.path"] == "false" and gm["config"]["claim.name"] == "groups"
    assert groups_scope["id"] in fake.default_scopes
    # the public MCP client
    assert report["created"]["client"] is True
    (client,) = fake.clients
    assert client["clientId"] == "cerebro-mcp" and client["publicClient"] is True
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == keycloak.LOOPBACK_REDIRECTS and client["directAccessGrantsEnabled"] is False
    # anonymous DCR: trusted hosts updated in place (not duplicated), allowed scopes created
    anon = [c for c in fake.components if c["subType"] == "anonymous"]
    th = [c for c in anon if c["providerId"] == "trusted-hosts"]
    assert len(th) == 1 and th[0]["id"] == "c-th"
    assert th[0]["config"]["trusted-hosts"] == keycloak.LOOPBACK_HOSTS
    assert th[0]["config"]["host-sending-registration-request-must-match"] == ["false"]
    assert th[0]["config"]["client-uris-must-match"] == ["true"]
    acs = [c for c in anon if c["providerId"] == "allowed-client-templates"]
    assert len(acs) == 1 and acs[0]["parentId"] == "realm-id" and set(acs[0]["config"]["allowed-client-scopes"]) == TokenScope.all() | {"groups"}
    assert report["created"]["registration_policies"] == ["trusted-hosts (updated)", "allowed-client-templates"]
    # the authenticated policy is untouched
    assert next(c for c in fake.components if c["id"] == "c-th-auth")["config"] == {"trusted-hosts": []}
    # no CIMD unless the feature is on
    assert fake.profiles["profiles"] == [] and fake.policies == {"policies": []}
    assert report["issuer"] == "http://auth:8080/realms/cerebro" and report["client_id"] == "cerebro-mcp"


async def test_seed_twice_creates_nothing_the_second_time(adapter, api):
    fake = FakeAdmin(api)
    await adapter.seed(USERS, GROUPS, RESOURCE)
    posts, updates, adds = list(fake.posts), list(fake.mapper_updates), list(fake.mapper_adds)
    membership = {k: list(v) for k, v in fake.memberships.items()}
    report = await adapter.seed(USERS, GROUPS, RESOURCE)
    assert fake.posts == posts and fake.mapper_updates == updates and fake.mapper_adds == adds
    assert fake.memberships == membership
    assert report["created"] == {"realm": False, "groups": [], "users": [], "client_scopes": [], "client": False, "registration_policies": []}
    assert set(report["existing"]["groups"]) == set(GROUPS) and report["existing"]["users"] == ["alice", "bob"]
    assert "password_generated" not in report


async def test_seed_fresh_realm_uses_configured_password_and_update_password_fallback(builtin_ctx, api, capsys):
    builtin_ctx.secrets.values["CEREBRO_SEED_PASSWORD"] = "hunter2-tmp"
    builtin_ctx.config.identity.groups_claim = "teams"
    fake = FakeAdmin(api, realm_exists=False, passkeys=False)
    a = registry.build("auth", "keycloak", {"trusted_hosts": ["dev.internal"], "ssl_required": "none"}, builtin_ctx)
    report = await a.seed(USERS, [], RESOURCE)
    assert report["created"]["realm"] is True
    realm_post = next(b for k, b in fake.posts if k == "realm")
    assert realm_post["realm"] == REALM and realm_post["enabled"] is True and realm_post["sslRequired"] == "none"
    assert report["first_login_action"] == "UPDATE_PASSWORD"
    assert all(u["requiredActions"] == ["UPDATE_PASSWORD"] and u["credentials"][0]["value"] == "hunter2-tmp" for u in fake.user_creates)
    assert "password_generated" not in report and "hunter2-tmp" not in capsys.readouterr().err
    # groups come from the users when the explicit list is empty
    assert report["created"]["groups"] == ["payments-team", "sre"]
    gm = next(s for s in fake.scopes if s["name"] == "groups")["protocolMappers"][0]
    assert gm["config"]["claim.name"] == "teams"
    th = next(c for c in fake.components if c["providerId"] == "trusted-hosts" and c["subType"] == "anonymous")
    assert th["config"]["trusted-hosts"] == keycloak.LOOPBACK_HOSTS + ["dev.internal"]
    assert report["created"]["registration_policies"] == ["trusted-hosts", "allowed-client-templates"]


async def test_seed_cimd_profile_and_policy(builtin_ctx, api):
    fake = FakeAdmin(api)
    a = registry.build("auth", "keycloak", {"features": ["cimd"], "cimd_trusted_domains": ["*.example.org"]}, builtin_ctx)
    report = await a.seed([], [], RESOURCE)
    assert report["cimd"] == {"trusted_domains": ["*.example.org"], "allow_http": False}
    (prof,) = fake.profiles["profiles"]
    assert prof["name"] == "cerebro-cimd" and prof["executors"][0]["executor"] == "client-id-metadata-document"
    assert prof["executors"][0]["configuration"]["trusted-domains"] == ["*.example.org"]
    assert all("globalProfiles" not in body for body in fake.profile_puts)     # PUT profiles never echoes the globals back
    (pol,) = fake.policies["policies"]
    assert pol["conditions"][0]["condition"] == "client-id-uri" and pol["conditions"][0]["configuration"]["uri-scheme"] == ["https"]
    assert pol["profiles"] == ["cerebro-cimd"] and pol["enabled"] is True
    # second run replaces, does not duplicate
    await a.seed([], [], RESOURCE)
    assert len(fake.profiles["profiles"]) == 1 and len(fake.policies["policies"]) == 1
    # without trusted domains nothing is written, and the report says why
    fake2_report = await registry.build("auth", "keycloak", {"features": ["cimd"]}, builtin_ctx).seed([], [], RESOURCE)
    assert any("cimd_trusted_domains" in n for n in fake2_report["notes"])


async def test_ready(adapter, api):
    api.get("/health/ready").mock(return_value=httpx.Response(200, json={"status": "UP"}))
    assert await adapter.ready()
    api.get("/health/ready").mock(return_value=httpx.Response(503))
    assert not await adapter.ready()
    api.get("/health/ready").mock(side_effect=httpx.ConnectError("no"))
    assert not await adapter.ready()
