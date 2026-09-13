"""Keycloak as the builtin AuthorizationServer (identity.mode builtin).

Two jobs: describe the unit the provisioner runs, and seed the realm through the admin REST API so that
`users:` in cerebro.yaml can log in and MCP clients can obtain tokens for the gateway without hand work.

Issuer. `issuer()` is `<public_url or http://auth:8080>/realms/<realm>`. Without `public_url` the unit runs
`start-dev`, where Keycloak derives `iss` from the request's Host header; the gateway reaches it at the unit's
internal address (`http://auth:8080`), so tokens fetched through that address carry that issuer. With `public_url`
the unit runs `start --hostname <public_url> --http-enabled true`, and `iss` is fixed to that URL regardless of how
the request arrived. The gateway's bearer_jwt adapter must therefore expect exactly
`{public_url or 'http://auth:8080'}/realms/{realm}` (no trailing slash) and fetch JWKS from
`<issuer>/protocol/openid-connect/certs` (RFC 8414 metadata at `<issuer>/.well-known/openid-configuration`).

Image and version. quay.io/keycloak/keycloak:26.7.3 (listed by https://quay.io/api/v1/repository/keycloak/keycloak/tag/
on 2026-09-13; 26.3 asked for originally is superseded). Health: Keycloak serves /health/ready on the management port
9000 by default (https://www.keycloak.org/observability/health: "The Keycloak health checks are exposed on the
management port 9000 by default"). Since 26.4.0 `http-management-health-enabled=false` moves them to the main
HTTP port (release notes, "Expose health endpoints on the main HTTP(S) port"); the unit sets
KC_HTTP_MANAGEMENT_HEALTH_ENABLED=false so `health_path=/health/ready` on port 8080 is what the provisioner probes,
which keeps a single port in the UnitSpec. /health must then be blocked at any public proxy.

RFC 8707 (Resource Indicators). Not supported in 26.7.x. Keycloak's own guide
https://www.keycloak.org/securing-apps/mcp-authz-server (docs for 26.7.3) lists "Resource Indicators for OAuth 2.0
(RFC 8707): Not supported" and says "Keycloak cannot recognize resource parameter. The Keycloak community is
planning to support Resource Indicators for OAuth 2.0 (RFC 8707)"; MCP 2025-06-18, 2025-11-25 and 2026-07-28 are
"Partially Supported without Resource Indicators for OAuth 2.0". The 26.5.0 release notes say the same
("resource indicators which are currently not implemented in Keycloak",
https://www.keycloak.org/docs/latest/release_notes/index.html). A `resource-indicators[:v1]` feature id appears in
the 26.7.3 `--features` value list (https://www.keycloak.org/server/features) but is not described in any feature
table and the MCP guide still says unsupported. The documented workaround, which `seed()` implements, is the one
the guide gives: a client scope per token scope, each carrying an Audience mapper whose "Included Custom Audience"
is the gateway's resource id, so the access token's `aud` equals the URL the MCP client passes as `resource`. The
gateway keeps validating `aud` against its resource id; clients that send `resource` lose nothing, the server just
ignores the parameter.

Client ID Metadata Documents (CIMD). Experimental since 26.6.0 ("OAuth Client ID Metadata Document (experimental)",
release notes above), listed as "Supported" (experimental) in the MCP guide. It needs `--features=cimd`, a client
profile with the `client-id-metadata-document` executor (trusted-domains required; "If empty, all domains are
denied") and a client policy with the `client-id-uri` condition. `seed()` creates both when
`options.features` contains "cimd" and `options.cimd_trusted_domains` is non-empty; the gateway's CIMD-to-DCR
shim from DESIGN-V2 section 6 remains the fallback for a realm without it.

Dynamic Client Registration (RFC 7591). Supported; the endpoint is
`<issuer>/clients-registrations/openid-connect`. Anonymous registration is off until a Trusted Hosts policy lists
hosts (https://www.keycloak.org/securing-apps/client-registration: "by default, there is not any whitelisted host,
so anonymous client registration is de-facto disabled"). `seed()` sets the anonymous Trusted Hosts policy to the
loopback hosts with "client URIs must match" on and "host sending the request must match" off, so any machine may
register a client but only with 127.0.0.1 / localhost redirect URIs, and lets anonymous clients use the realm's
default scopes (Allowed Client Scopes policy). Both are components of provider type
org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy with subType `anonymous`.

Options (identity.server.options):
    features                list passed as KC_FEATURES (e.g. [cimd]); default none
    cimd_trusted_domains    wildcard domain list for the CIMD executor/condition; required for cimd to do anything
    cimd_allow_http         allow http client_id URLs (dev only), default false
    proxy_headers           xforwarded | forwarded: adds --proxy-headers when public_url is set
    ssl_required            realm sslRequired on creation: external (default) | none | all
    trusted_hosts           extra hosts for the anonymous DCR Trusted Hosts policy (default loopback only)
    admin_url               where the admin API is reachable from this process (default: Locator endpoint of unit auth)
    first_login_action      override: webauthn-register-passwordless | UPDATE_PASSWORD
    timeout                 seconds per admin call, default 60

Secrets: POSTGRES_PASSWORD, the names in identity.server.admin_user_env / admin_password_env (defaults
CEREBRO_AUTH_ADMIN_USER / CEREBRO_AUTH_ADMIN_PASSWORD), CEREBRO_SEED_PASSWORD (temporary password for seeded users;
generated and printed once when unset).
"""
from __future__ import annotations
import logging, secrets as _secrets, sys
from typing import Any
import httpx
from cerebro.core.config import AuthServerConfig, UserSeed
from cerebro.core.contracts.identity import AuthorizationServer
from cerebro.core.contracts.provision import UnitSpec, PortSpec
from cerebro.core.principal import TokenScope

log = logging.getLogger(__name__)

IMAGE = "quay.io/keycloak/keycloak:26.7.3"
UNIT = "auth"
PORT = 8080
CLIENT_ID = "cerebro-mcp"
GROUPS_SCOPE = "groups"
LOOPBACK_REDIRECTS = ["http://127.0.0.1:*", "http://localhost:*", "urn:ietf:wg:oauth:2.0:oob"]
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1"]
POSTGRES_SECRET = "POSTGRES_PASSWORD"
SEED_PASSWORD_SECRET = "CEREBRO_SEED_PASSWORD"
PASSKEY_ACTION = "webauthn-register-passwordless"
PASSWORD_ACTION = "UPDATE_PASSWORD"
CRP_TYPE = "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
AUDIENCE_MAPPER = "cerebro-audience"
CIMD_NAME = "cerebro-cimd"


class SeedError(RuntimeError):
    pass


class _Admin:
    """Thin admin REST client: one bearer for the run, JSON in and out, 404 -> None on GET."""

    def __init__(self, base_url: str, realm: str, user: str, password: str, timeout: float):
        self.base, self.realm, self.user, self.password = base_url.rstrip("/"), realm, user, password
        self.http = httpx.AsyncClient(base_url=self.base, timeout=timeout)

    async def login(self) -> None:
        r = await self.http.post("/realms/master/protocol/openid-connect/token",
                                 data={"grant_type": "password", "client_id": "admin-cli",
                                       "username": self.user, "password": self.password})
        if r.status_code >= 400:
            raise SeedError(f"keycloak admin login failed ({r.status_code}): {r.text[:200]}")
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    async def aclose(self) -> None:
        await self.http.aclose()

    def _p(self, path: str) -> str:
        return path if path.startswith("/admin/") else f"/admin/realms/{self.realm}{path}"

    async def get(self, path: str, **params) -> Any | None:
        r = await self.http.get(self._p(path), params=params or None)
        if r.status_code == 404:
            return None
        self._check(r, "GET", path)
        return r.json() if r.content else None

    async def post(self, path: str, body: dict) -> str | None:
        """Returns the created object's id (from Location) when Keycloak sends one."""
        r = await self.http.post(self._p(path), json=body)
        if r.status_code == 409:
            return None
        self._check(r, "POST", path)
        loc = r.headers.get("location", "")
        return loc.rstrip("/").rsplit("/", 1)[-1] if loc else None

    async def put(self, path: str, body: dict | None = None) -> None:
        r = await self.http.put(self._p(path), json=body if body is not None else {})
        self._check(r, "PUT", path)

    @staticmethod
    def _check(r: httpx.Response, method: str, path: str) -> None:
        if r.status_code >= 400:
            raise SeedError(f"keycloak {method} {path} -> {r.status_code}: {r.text[:300]}")


class Adapter(AuthorizationServer):
    name = "keycloak"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        self.server: AuthServerConfig = (ctx.config.identity.server if ctx and ctx.config.identity.server else AuthServerConfig())
        self.realm: str = self.server.realm
        self.timeout = float(self.option("timeout", 60))
        self.features: list[str] = [str(f) for f in (self.option("features") or [])]

    # ------------------------------------------------------------------ addresses
    @property
    def internal_url(self) -> str:
        return f"http://{UNIT}:{PORT}"

    def public_base(self) -> str:
        return (self.server.public_url or self.internal_url).rstrip("/")

    def issuer(self) -> str:
        return f"{self.public_base()}/realms/{self.realm}"

    def admin_url(self) -> str:
        url = self.option("admin_url") or (self.ctx.locator.endpoint(UNIT) if self.ctx else self.internal_url)
        return str(url).rstrip("/")

    def registration_endpoint(self) -> str:
        return f"{self.issuer()}/clients-registrations/openid-connect"

    def configured(self) -> bool:
        return bool(self.ctx and self.ctx.secret(self.server.admin_user_env) and self.ctx.secret(self.server.admin_password_env))

    # ------------------------------------------------------------------ the unit
    def units(self) -> list[UnitSpec]:
        s = self.server
        env = {
            "KC_DB": "postgres",
            "KC_DB_URL": "jdbc:postgresql://postgres:5432/keycloak",
            "KC_DB_USERNAME": "cerebro",
            "KC_DB_PASSWORD": f"${{{POSTGRES_SECRET}}}",
            "KC_BOOTSTRAP_ADMIN_USERNAME": f"${{{s.admin_user_env}}}",
            "KC_BOOTSTRAP_ADMIN_PASSWORD": f"${{{s.admin_password_env}}}",
            "KC_HEALTH_ENABLED": "true",
            "KC_HTTP_MANAGEMENT_HEALTH_ENABLED": "false",     # /health/* on 8080 (26.4+), one port in the spec
            "KC_HTTP_ENABLED": "true",
        }
        if self.features:
            env["KC_FEATURES"] = ",".join(self.features)
        if s.public_url:
            args = ["start", "--hostname", s.public_url, "--http-enabled", "true"]
            if self.option("proxy_headers"):
                args += ["--proxy-headers", str(self.option("proxy_headers"))]
        else:
            args = ["start-dev"]
        return [UnitSpec(
            name=UNIT, role="auth", image=IMAGE, args=args, env=env,
            secret_env=[POSTGRES_SECRET, s.admin_user_env, s.admin_password_env],
            ports=[PortSpec(name="http", port=PORT)],
            health_path="/health/ready", depends_on=["postgres"],
            resources=dict(self.option("resources") or {}),
            labels={"cerebro.engine": "keycloak"},
        )]

    async def ready(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self.admin_url(), timeout=self.timeout) as h:
                r = await h.get("/health/ready")
                if r.status_code == 404:
                    r = await h.get("/realms/master")
                return r.status_code < 400
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------ seeding
    def _admin(self) -> _Admin:
        user = self.ctx.secret(self.server.admin_user_env) if self.ctx else None
        pw = self.ctx.secret(self.server.admin_password_env) if self.ctx else None
        if not user or not pw:
            raise SeedError(f"secrets {self.server.admin_user_env} and {self.server.admin_password_env} are required to seed Keycloak")
        return _Admin(self.admin_url(), self.realm, user, pw, self.timeout)

    async def seed(self, users: list[UserSeed], groups: list[str], resource_id: str) -> dict:
        admin = self._admin()
        report: dict[str, Any] = {"realm": self.realm, "issuer": self.issuer(), "resource": resource_id,
                                  "created": {"realm": False, "groups": [], "users": [], "client_scopes": [],
                                              "client": False, "registration_policies": []},
                                  "existing": {"groups": [], "users": [], "client_scopes": []},
                                  "notes": []}
        try:
            await admin.login()
            realm = await admin.get(f"/admin/realms/{self.realm}")
            if realm is None:
                await admin.post("/admin/realms", self._realm_rep())
                realm = await admin.get(f"/admin/realms/{self.realm}") or {}
                report["created"]["realm"] = True
            realm_id = realm.get("id", self.realm)

            action = await self._first_login_action(admin)
            report["first_login_action"] = action

            wanted_groups: list[str] = []
            for g in list(groups) + [g for u in users for g in u.groups]:
                if g and g not in wanted_groups:
                    wanted_groups.append(g)
            group_ids = {g: await self._ensure_group(admin, g, report) for g in wanted_groups}

            configured_pw = self.ctx.secret(SEED_PASSWORD_SECRET) if self.ctx else None
            generated: list[str] = []

            def password_for() -> str:              # only called when a user is actually created
                if configured_pw:
                    return configured_pw
                if not generated:
                    generated.append(_secrets.token_urlsafe(12))
                return generated[0]

            for u in users:
                await self._ensure_user(admin, u, group_ids, action, password_for, report)
            if generated:
                msg = (f"keycloak: temporary password for {', '.join(report['created']['users'])} is {generated[0]} "
                       f"(set {SEED_PASSWORD_SECRET} to choose it; shown once, must be changed at first login)")
                print(msg, file=sys.stderr)
                log.warning(msg)
                report["password_generated"] = True

            scope_ids = {s.value: await self._ensure_token_scope(admin, s.value, resource_id, report) for s in TokenScope}
            scope_ids[GROUPS_SCOPE] = await self._ensure_groups_scope(admin, report)
            await self._ensure_realm_default_scopes(admin, list(scope_ids.values()))
            report["created"]["client"] = await self._ensure_client(admin)
            await self._ensure_registration_policies(admin, realm_id, list(scope_ids), report)
            if "cimd" in self.features:
                await self._ensure_cimd(admin, report)
            report["client_id"] = CLIENT_ID
            report["registration_endpoint"] = self.registration_endpoint()
            return report
        finally:
            await admin.aclose()

    # ---- realm
    def _realm_rep(self) -> dict:
        return {"realm": self.realm, "enabled": True, "displayName": "cerebro",
                "registrationAllowed": False, "loginWithEmailAllowed": True, "duplicateEmailsAllowed": False,
                "sslRequired": str(self.option("ssl_required", "external")), "bruteForceProtected": True}

    async def _first_login_action(self, admin: _Admin) -> str:
        override = self.option("first_login_action")
        if override:
            return str(override)
        actions = await admin.get("/authentication/required-actions") or []
        for a in actions:
            if a.get("alias") == PASSKEY_ACTION and a.get("enabled", False):
                return PASSKEY_ACTION
        return PASSWORD_ACTION

    # ---- groups / users
    async def _ensure_group(self, admin: _Admin, name: str, report: dict) -> str:
        found = [g for g in (await admin.get("/groups", search=name, exact="true") or []) if g.get("name") == name]
        if found:
            report["existing"]["groups"].append(name)
            return found[0]["id"]
        gid = await admin.post("/groups", {"name": name})
        if gid is None:
            found = [g for g in (await admin.get("/groups", search=name, exact="true") or []) if g.get("name") == name]
            gid = found[0]["id"]
        report["created"]["groups"].append(name)
        return gid

    async def _ensure_user(self, admin: _Admin, u: UserSeed, group_ids: dict[str, str], action: str,
                           password_for, report: dict) -> bool:
        found = [x for x in (await admin.get("/users", username=u.name, exact="true") or []) if x.get("username") == u.name]
        if found:
            uid = found[0]["id"]
            member = {g.get("name") for g in (await admin.get(f"/users/{uid}/groups") or [])}
            for g in u.groups:
                if g not in member:
                    await admin.put(f"/users/{uid}/groups/{group_ids[g]}")
            report["existing"]["users"].append(u.name)
            return False
        rep: dict[str, Any] = {"username": u.name, "enabled": True, "groups": [f"/{g}" for g in u.groups],
                               "requiredActions": [action],
                               "credentials": [{"type": "password", "value": password_for(), "temporary": True}]}
        if u.email:
            rep["email"], rep["emailVerified"] = u.email, True
        await admin.post("/users", rep)
        report["created"]["users"].append(u.name)
        return True

    # ---- client scopes
    async def _scopes_by_name(self, admin: _Admin) -> dict[str, dict]:
        return {s["name"]: s for s in (await admin.get("/client-scopes") or []) if "name" in s}

    @staticmethod
    def _audience_mapper(resource_id: str) -> dict:
        return {"name": AUDIENCE_MAPPER, "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {"included.custom.audience": resource_id, "id.token.claim": "false",
                           "access.token.claim": "true", "introspection.token.claim": "true"}}

    async def _ensure_token_scope(self, admin: _Admin, name: str, resource_id: str, report: dict) -> str:
        existing = (await self._scopes_by_name(admin)).get(name)
        if existing:
            report["existing"]["client_scopes"].append(name)
            mappers = existing.get("protocolMappers") or []
            aud = next((m for m in mappers if m.get("name") == AUDIENCE_MAPPER), None)
            if aud is None:
                await admin.post(f"/client-scopes/{existing['id']}/protocol-mappers/models", self._audience_mapper(resource_id))
            elif (aud.get("config") or {}).get("included.custom.audience") != resource_id:
                await admin.put(f"/client-scopes/{existing['id']}/protocol-mappers/models/{aud['id']}",
                                {**aud, "config": {**(aud.get("config") or {}), "included.custom.audience": resource_id}})
            return existing["id"]
        rep = {"name": name, "protocol": "openid-connect", "description": f"cerebro token scope {name}",
               "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "true",
                              "consent.screen.text": name},
               "protocolMappers": [self._audience_mapper(resource_id)]}
        sid = await admin.post("/client-scopes", rep) or (await self._scopes_by_name(admin))[name]["id"]
        report["created"]["client_scopes"].append(name)
        return sid

    async def _ensure_groups_scope(self, admin: _Admin, report: dict) -> str:
        claim = self.ctx.config.identity.groups_claim if self.ctx else "groups"
        existing = (await self._scopes_by_name(admin)).get(GROUPS_SCOPE)
        if existing:
            report["existing"]["client_scopes"].append(GROUPS_SCOPE)
            return existing["id"]
        rep = {"name": GROUPS_SCOPE, "protocol": "openid-connect", "description": "IdP group membership (names, no path)",
               "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
               "protocolMappers": [{"name": "groups", "protocol": "openid-connect",
                                    "protocolMapper": "oidc-group-membership-mapper", "consentRequired": False,
                                    "config": {"claim.name": claim, "full.path": "false", "id.token.claim": "true",
                                               "access.token.claim": "true", "userinfo.token.claim": "true",
                                               "introspection.token.claim": "true"}}]}
        sid = await admin.post("/client-scopes", rep) or (await self._scopes_by_name(admin))[GROUPS_SCOPE]["id"]
        report["created"]["client_scopes"].append(GROUPS_SCOPE)
        return sid

    async def _ensure_realm_default_scopes(self, admin: _Admin, scope_ids: list[str]) -> None:
        current = {s["id"] for s in (await admin.get("/default-default-client-scopes") or [])}
        for sid in scope_ids:
            if sid not in current:
                await admin.put(f"/default-default-client-scopes/{sid}")

    # ---- the public MCP client
    async def _ensure_client(self, admin: _Admin) -> bool:
        if [c for c in (await admin.get("/clients", clientId=CLIENT_ID) or []) if c.get("clientId") == CLIENT_ID]:
            return False
        rep = {"clientId": CLIENT_ID, "name": "cerebro MCP clients", "protocol": "openid-connect", "enabled": True,
               "publicClient": True, "standardFlowEnabled": True, "implicitFlowEnabled": False,
               "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False, "fullScopeAllowed": False,
               "redirectUris": list(LOOPBACK_REDIRECTS), "webOrigins": ["+"],
               "attributes": {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "+"}}
        await admin.post("/clients", rep)
        return True

    # ---- anonymous dynamic client registration, loopback redirects only
    async def _ensure_registration_policies(self, admin: _Admin, realm_id: str, scope_names: list[str], report: dict) -> None:
        comps = [c for c in (await admin.get("/components", type=CRP_TYPE) or []) if c.get("subType") == "anonymous"]
        hosts = list(LOOPBACK_HOSTS) + [str(h) for h in (self.option("trusted_hosts") or [])]
        wanted = {
            "trusted-hosts": ("Trusted Hosts", {"trusted-hosts": hosts,
                                                 "host-sending-registration-request-must-match": ["false"],
                                                 "client-uris-must-match": ["true"]}),
            "allowed-client-templates": ("Allowed Client Scopes", {"allow-default-scopes": ["true"],
                                                                    "allowed-client-scopes": scope_names}),
        }
        for provider_id, (name, config) in wanted.items():
            existing = next((c for c in comps if c.get("providerId") == provider_id), None)
            if existing:
                if {k: v for k, v in (existing.get("config") or {}).items() if k in config} != config:
                    await admin.put(f"/components/{existing['id']}", {**existing, "config": {**(existing.get("config") or {}), **config}})
                    report["created"]["registration_policies"].append(f"{provider_id} (updated)")
            else:
                await admin.post("/components", {"name": name, "providerId": provider_id, "providerType": CRP_TYPE,
                                                 "parentId": realm_id, "subType": "anonymous", "config": config})
                report["created"]["registration_policies"].append(provider_id)

    # ---- CIMD (experimental in Keycloak 26.6+; needs --features=cimd)
    async def _ensure_cimd(self, admin: _Admin, report: dict) -> None:
        domains = [str(d) for d in (self.option("cimd_trusted_domains") or [])]
        if not domains:
            report["notes"].append("cimd feature on but cimd_trusted_domains is empty: Keycloak denies every client_id URL")
            return
        allow_http = bool(self.option("cimd_allow_http", False))
        profiles = await admin.get("/client-policies/profiles", **{"include-global-profiles": "false"}) or {}
        plist = [p for p in profiles.get("profiles", []) if p.get("name") != CIMD_NAME]
        plist.append({"name": CIMD_NAME, "description": "cerebro: accept OAuth Client ID Metadata Documents",
                      "executors": [{"executor": "client-id-metadata-document",
                                     "configuration": {"allow-http-scheme": allow_http, "trusted-domains": domains,
                                                       "restrict-same-domain": False,
                                                       "required-properties": ["redirect_uris"]}}]})
        await admin.put("/client-policies/profiles", {"profiles": plist})
        policies = await admin.get("/client-policies/policies") or {}
        pol = [p for p in policies.get("policies", []) if p.get("name") != CIMD_NAME]
        pol.append({"name": CIMD_NAME, "description": "cerebro: client_id is a URL", "enabled": True,
                    "conditions": [{"condition": "client-id-uri",
                                    "configuration": {"uri-scheme": ["https"] + (["http"] if allow_http else []),
                                                      "trusted-domains": domains}}],
                    "profiles": [CIMD_NAME]})
        await admin.put("/client-policies/policies", {"policies": pol})
        report["cimd"] = {"trusted_domains": domains, "allow_http": allow_http}
