# Identity

The gateway is always the OAuth 2.1 **resource server**: it receives a bearer token, validates it, and turns it into
a `Principal` (subject, groups, token scopes, user or service). Who mints tokens is `identity.mode`. `builtin` and
`external` run the same adapters with a different issuer URL; the builtin server is a convenience, not a second
security model. The adapters are in `cerebro/adapters/identity/`, the mode-to-adapter mapping in
`cerebro/gateway/identity.py`.

| `identity.mode` | Adapter | Token minted by |
|---|---|---|
| `none` | `none` | nobody |
| `static` | `static` | you, in the config |
| `builtin` | `bearer_jwt` or `bearer_introspect` | the Keycloak unit the stack runs |
| `external` | `bearer_jwt` or `bearer_introspect` | your IdP |
| any, plus `identity.legacy` | a chain: the mode's adapter first, `trusted_headers` second | an SSO proxy |

## Mode `none`: one person, one machine

Every request is `identity.principal` (default `{subject: local, groups: [everyone, admin]}`) with every token scope.
The gateway binds `127.0.0.1` whatever `gateway.host` says, and the adapter refuses a request whose peer address is not
loopback. Verified in the built image: inside a container this means the container's own loopback, so a published
port answers nothing.

`identity.allow_remote: true` lifts both rules and adds one: every request, loopback included (a same-host proxy
cannot bypass it), must carry `Authorization: Bearer <value of the secret named identity.static_token_env>` (default
`CEREBRO_TOKEN`); an empty secret is a 401, not an open door. `gateway.host` then decides the bind address and, on
compose, the host interface the port is published on (`0.0.0.0:8090:8090` with `gateway.host: 0.0.0.0`). Verified:
401 without the token, MCP `initialize` with it, `/.well-known/oauth-protected-resource` is 404 in this mode.

## Mode `static`: tests and demos

`identity.tokens` maps bearer strings to principals (`subject`, `groups`, `kind: user | service`, `token_scopes`,
default all). Tokens never expire and cannot be revoked except by editing the file. Never production.

## Mode `builtin`: Keycloak provisioned by the stack

`identity.server: { type: keycloak, realm: cerebro, public_url: https://auth.example.org }` makes the plan include
unit `auth`: `quay.io/keycloak/keycloak:26.7.3`, Postgres database `keycloak`, health on `/health/ready` moved to the
main port 8080 (`KC_HTTP_MANAGEMENT_HEALTH_ENABLED=false`, so block `/health` at your proxy). Without `public_url` it
runs `start-dev` and derives `iss` from the request's Host header; with it, `start --hostname <public_url>` and `iss`
is fixed. Secrets: `POSTGRES_PASSWORD`, `CEREBRO_AUTH_ADMIN_USER`, `CEREBRO_AUTH_ADMIN_PASSWORD` (bootstrap admin;
add them to `secrets.keys`), optional `CEREBRO_SEED_PASSWORD`.

The issuer is `<public_url or http://auth:8080>/realms/<realm>`. Today the gateway does **not** derive it from the
adapter: set `identity.issuer` to exactly that value (no trailing slash) or `bearer_jwt` raises at the first request
and the metadata endpoint returns an error. `identity.audience` is optional; the accepted `aud` is
`gateway.public_url` (or `http://host:port/path`) plus `identity.audience` when set.

**Seeding.** `Adapter.seed(users, groups, resource_id)` creates, idempotently: the realm (`sslRequired` from
`options.ssl_required`, default `external`; brute-force protection on), one group per name in `scopes.*.groups`
and `identity.users[].groups`, the users (`requiredActions`: `webauthn-register-passwordless` when that action is
enabled in the realm, else `UPDATE_PASSWORD`; a temporary password from `CEREBRO_SEED_PASSWORD` or generated and
printed once), a client scope per token scope with an **Audience mapper** whose custom audience is the resource id,
a `groups` client scope (group membership mapper, names without path), all of them realm defaults, the public client
`cerebro-mcp` (authorization code + PKCE S256, loopback redirect URIs `http://127.0.0.1:*`, `http://localhost:*`,
`urn:ietf:wg:oauth:2.0:oob`), and the anonymous registration policies below. There is no `cerebro` command for it
yet; run it from Python once Keycloak is ready, with `CEREBRO_AUTH_ADMIN_USER` / `CEREBRO_AUTH_ADMIN_PASSWORD` in the
environment and `admin_url` pointing at where this machine reaches Keycloak:

```python
import asyncio, json, sys
from cerebro.core import AdapterContext, load_config, registry
cfg = load_config("cerebro.yaml")
server = cfg.identity.server
auth = registry.build("auth", server.type, {**server.options, "admin_url": sys.argv[1]}, AdapterContext(cfg))
groups = sorted({g for s in cfg.scopes.values() for g in s.groups})
print(json.dumps(asyncio.run(auth.seed(cfg.identity.users, groups, cfg.resource_id())), indent=2))
```

The compose renderer publishes only the gateway's port; browsers reach Keycloak through the reverse proxy you put in
front of `auth` (or an Ingress on Kubernetes). First login: the seeded user signs in with the temporary password and
is asked to register a passkey (or set a password when the realm has no passkey action).

**Two Keycloak facts that shape this** (read from Keycloak's documentation for 26.7.3, quoted in
`cerebro/adapters/auth/keycloak.py`):

- RFC 8707 Resource Indicators are **not supported**; Keycloak "cannot recognize resource parameter". The documented
  substitute is the Audience mapper per client scope, which is what `seed()` does, so `aud` still equals the URL the
  MCP client passes as `resource` and the gateway keeps validating `aud`. Clients that send `resource` lose nothing.
- Client ID Metadata Documents (CIMD) are **experimental** since 26.6.0: `identity.server.options.features: [cimd]`
  passes `KC_FEATURES=cimd`, and with `options.cimd_trusted_domains: ["*.example.org"]` `seed()` adds the client
  profile (`client-id-metadata-document` executor) and policy (`client-id-uri` condition); `cimd_allow_http` is for
  dev only. Without CIMD, clients register through Dynamic Client Registration (RFC 7591): `seed()` sets the anonymous
  **Trusted Hosts** policy to loopback (plus `options.trusted_hosts`) with "client URIs must match", so any machine may
  register a client whose redirect URIs are `127.0.0.1` / `localhost`, and lets such clients use the realm's default
  scopes. The CIMD-to-DCR shim mentioned in the design is not implemented.

Other options: `proxy_headers` (`xforwarded` | `forwarded`, when behind a proxy with `public_url`),
`first_login_action`, `timeout`, `admin_url`.

## Mode `external`: your IdP

```yaml
identity:
  mode: external
  issuer: https://sso.internal/realms/eng       # RFC 8414 / OIDC discovery must work from the gateway
  audience: https://context.internal/mcp        # what your IdP puts in aud for this gateway
  groups_claim: groups                          # or a dotted path such as realm_access.roles
  scope_claim: scope
  token_validation: jwks                        # or introspection
  # introspection: { client_id: cerebro-gateway, client_secret_env: CEREBRO_INTROSPECTION_SECRET }
```

`bearer_jwt` checks signature (key from `identity.jwks_url` or the discovery document's `jwks_uri`, cached one hour,
refetched once on an unknown `kid`), `iss`, `aud`, `exp`, and requires `sub`; asymmetric algorithms only.
`bearer_introspect` posts the token to the introspection endpoint with HTTP basic auth, requires `active`, and checks
`iss` / `aud` when present. Claims map the same way in both: groups from `groups_claim` (list or delimited string),
token scopes are the `cerebro:*` values of `scope_claim` (none present means every scope, for IdPs that know nothing
about cerebro), `kind: service` when there is no `email` / `preferred_username` and `client_id` or `azp` equals `sub`.

Service accounts: a client-credentials token from your IdP with `cerebro:code.read` (and whatever else) becomes a
service principal with no personal bank; its groups decide its scopes like any user's. Enterprise-managed
authorization (identity-assertion grants) is your IdP's job; the gateway only validates what it issues. Verified:
unit tests against a mocked issuer (discovery, JWKS with rotation, introspection).

## Legacy: `trusted_headers`

`identity.legacy: { type: trusted_headers, user_header: X-Forwarded-User, groups_header: X-Forwarded-Groups }` adds
the v1 model as a second provider behind the mode's own: an SSO reverse proxy that did the OIDC flow sets the two
headers (groups comma-separated). The chain tries the bearer adapter first, headers second, so clients can move to
tokens while the proxy keeps working. Only safe when the proxy is the single route to the gateway; anyone else who
can reach the port can claim any identity.

## Discovery: how an MCP client finds all this

1. The client posts to `/mcp` without a token and gets `401` with
   `WWW-Authenticate: Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource"`.
2. It fetches that document (RFC 9728; the path-suffixed form is served too): `resource` (the gateway's identifier),
   `authorization_servers: [issuer]`, `scopes_supported` (the five `cerebro:*` scopes), `bearer_methods_supported: [header]`.
3. It fetches the issuer's metadata (`/.well-known/openid-configuration`, then RFC 8414 forms), registers itself
   (CIMD, DCR, or a pre-registered client id such as `cerebro-mcp`), runs the authorization code flow with PKCE in the
   browser, and retries with the access token.
4. `whoami` shows what the gateway made of the token: subject, kind, groups, token scopes, scopes, repositories,
   banks. `tools/list` only shows tools the token's scopes allow.

In mode `none` the metadata endpoint is 404 and the challenge is a bare `Bearer`; there is nothing to discover.
