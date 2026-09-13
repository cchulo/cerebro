"""Shared fixtures for the gateway tests: a config factory over the example file, an RSA key with its JWKS, and
a respx-served issuer (discovery + JWKS + introspection) so the bearer adapters run without a network."""
import json, time
import httpx, jwt, pytest, respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cerebro.core import Config, AdapterContext, StaticLocator

ISSUER = "https://sso.test/realms/eng"
RESOURCE = "http://gateway.test/mcp"


class DictSecrets:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, name, default=None):
        return self.values.get(name, default)


@pytest.fixture
def make_config(example_config):
    """Copy of the example config with overrides: make_config(identity={...}, gateway={...})."""
    def _make(**overrides) -> Config:
        raw = example_config.model_dump(mode="json")
        for key, value in overrides.items():
            raw[key] = {**raw[key], **value} if isinstance(raw.get(key), dict) and isinstance(value, dict) else value
        return Config.model_validate(raw)
    return _make


@pytest.fixture
def make_ctx():
    def _make(config: Config, **secrets) -> AdapterContext:
        return AdapterContext(config, secrets=DictSecrets(**secrets), locator=StaticLocator(template="http://{unit}:8080"))
    return _make


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(rsa_key):
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    pub.update(kid="k1", alg="RS256", use="sig")
    return {"keys": [pub]}


@pytest.fixture
def sign(rsa_key):
    """sign(**claims) -> a JWT for the test issuer/resource; pass kid= to name another key, exp= to override."""
    def _sign(kid="k1", **claims) -> str:
        payload = {"iss": ISSUER, "aud": RESOURCE, "sub": "alice", "exp": int(time.time()) + 300,
                   "iat": int(time.time()), **claims}
        return jwt.encode(payload, rsa_key, algorithm="RS256", headers={"kid": kid})
    return _sign


@pytest.fixture
def issuer(jwks):
    """respx routes for the issuer: OIDC discovery, JWKS and an introspection endpoint whose answers the test
    sets through `issuer.introspection[token] = {...}`."""
    with respx.mock(assert_all_called=False) as mock:
        discovery = {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                     "introspection_endpoint": f"{ISSUER}/protocol/openid-connect/token/introspect",
                     "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                     "token_endpoint": f"{ISSUER}/protocol/openid-connect/token"}
        mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(return_value=httpx.Response(200, json=discovery))
        mock.jwks_route = mock.get(discovery["jwks_uri"]).mock(return_value=httpx.Response(200, json=jwks))
        mock.introspection = {}

        def introspect(request: httpx.Request):
            form = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            mock.last_introspection_auth = request.headers.get("authorization")
            return httpx.Response(200, json=mock.introspection.get(form.get("token"), {"active": False}))

        mock.introspect_route = mock.post(discovery["introspection_endpoint"]).mock(side_effect=introspect)
        yield mock
