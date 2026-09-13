"""Policy: Principal -> Grants. The only place group membership is turned into scopes, repos and banks."""
from __future__ import annotations
from abc import abstractmethod
from ..context import Adapter
from ..principal import Principal, Grants


class AccessPolicy(Adapter):
    kind = "policy"

    @abstractmethod
    def grants(self, principal: Principal) -> Grants: ...
