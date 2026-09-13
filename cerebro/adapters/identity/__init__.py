"""Identity adapters: request -> Principal. `identity.mode` in cerebro.yaml picks one through cerebro.gateway.identity:

    none              the fixed `principal:` from config (loopback only unless allow_remote + static token)
    static            a token -> principal map (tests, demos)
    trusted_headers   X-Forwarded-User / X-Forwarded-Groups set by an SSO proxy (the v1 model)
    bearer_jwt        OAuth 2.1 / OIDC access tokens validated against the issuer's JWKS (builtin and external)
    bearer_introspect RFC 7662 introspection for opaque tokens

Every adapter raises cerebro.core.types.Unauthenticated when it cannot vouch for the request; the gateway turns
that into a 401 carrying the adapter's challenge() headers.
"""
