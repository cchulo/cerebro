import pytest
from cerebro.core import Principal
from cerebro.core.contracts import IdentityProvider, RequestInfo
from cerebro.core.types import Unauthenticated


class IdentityProviderContract:
    @pytest.fixture
    def adapter(self, ctx) -> IdentityProvider:
        raise NotImplementedError

    @pytest.fixture
    def good_request(self) -> RequestInfo:
        """A request the adapter under test accepts. Override."""
        raise NotImplementedError

    @pytest.fixture
    def bad_request(self) -> RequestInfo | None:
        """A request the adapter rejects, or None when the adapter accepts everything (mode none)."""
        return RequestInfo(headers={"authorization": "Bearer definitely-not-valid"}, client_host="10.0.0.9")

    def test_is_identity_provider(self, adapter):
        assert isinstance(adapter, IdentityProvider) and adapter.kind == "identity"

    async def test_resolves_principal(self, adapter, good_request):
        p = await adapter.resolve(good_request)
        assert isinstance(p, Principal) and p.subject

    async def test_rejects_bad_request(self, adapter, bad_request):
        if bad_request is None:
            pytest.skip("adapter accepts every request")
        with pytest.raises(Unauthenticated):
            await adapter.resolve(bad_request)

    def test_challenge_and_metadata_shapes(self, adapter):
        ch = adapter.challenge()
        assert "WWW-Authenticate" in ch
        md = adapter.protected_resource_metadata()
        assert md is None or ("resource" in md and "authorization_servers" in md)
