"""identity.mode none: one person on their own machine. Every request is the configured `principal:`.

On a host (`cerebro gateway serve`) the gateway binds to loopback in this mode, and this adapter refuses anything
that is not a loopback request. Inside a workload the peer is never loopback (compose: the Docker bridge; kubernetes:
the port-forward or Service), so the provisioner sets $CEREBRO_TRUSTED_NETWORK=1 on the gateway unit and this
adapter then accepts any peer: the network it sits on is the isolation (compose publishes the port on 127.0.0.1 only;
a kubernetes Service is ClusterIP, reached by port-forward). Never set that variable by hand on a reachable port.

`identity.allow_remote` is the explicit LAN option: every request (loopback included, so a same-host proxy cannot
bypass it) must carry `Authorization: Bearer <value of the secret named identity.static_token_env>` (default
CEREBRO_TOKEN), so a LAN exposure is never open.
"""
from __future__ import annotations
import hmac
import os
from cerebro.core import Principal
from cerebro.core.contracts import IdentityProvider, RequestInfo
from cerebro.core.types import Unauthenticated
from ._claims import principal_from_seed

TRUSTED_NETWORK_ENV = "CEREBRO_TRUSTED_NETWORK"      # set by the provisioner on the gateway unit in mode none


def trusted_network(env=None) -> bool:
    return (os.environ if env is None else env).get(TRUSTED_NETWORK_ENV, "").strip().lower() in ("1", "true", "yes")


class Adapter(IdentityProvider):
    name = "none"

    async def resolve(self, request: RequestInfo) -> Principal:
        ident = self.ctx.config.identity
        if ident.allow_remote:
            expected = self.ctx.secret(ident.static_token_env)
            if not expected:
                raise Unauthenticated(f"identity.allow_remote is set but secret {ident.static_token_env} is empty")
            if not hmac.compare_digest(request.bearer() or "", expected):
                raise Unauthenticated("missing or invalid bearer token")
        elif not request.is_loopback and not trusted_network():
            raise Unauthenticated("identity.mode none accepts loopback requests only (set identity.allow_remote to change that)")
        return principal_from_seed(ident.principal, issuer=None)
