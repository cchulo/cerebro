import pytest
from cerebro.core import registry, Principal, Grants, TokenScope
from cerebro.core.context import Adapter
from cerebro.core.types import Forbidden


class Dummy(Adapter):
    kind = "docs"


def test_registry_module_colon_class(ctx):
    inst = registry.build("docs", f"{__name__}:Dummy", {"a": 1}, ctx)
    assert isinstance(inst, Dummy) and inst.option("a") == 1 and inst.name == f"{__name__}:Dummy"


def test_registry_unknown_type(ctx):
    with pytest.raises(LookupError, match="no docs adapter"):
        registry.resolve("docs", "does-not-exist")
    with pytest.raises(KeyError):
        registry.resolve("nope", "x")


def test_principal_scopes_default_to_all():
    p = Principal(subject="alice", groups=frozenset({"sre"}))
    assert p.has_scope(TokenScope.MEMORY_WRITE) and p.bank_slug == "alice"
    q = Principal(subject="ci@svc", kind="service", token_scopes=frozenset({TokenScope.CODE_READ.value}))
    assert q.has_scope(TokenScope.CODE_READ) and not q.has_scope(TokenScope.DOCS_READ)
    assert Principal(subject="x", token_scopes=frozenset({TokenScope.ADMIN.value})).has_scope(TokenScope.DOCS_READ)


def test_grants_checks():
    g = Grants(subject="alice", scopes=["public"], banks=["user-alice", "team-sre"], personal_bank="user-alice",
               token_scopes=frozenset({TokenScope.DOCS_READ.value}))
    assert g.check_scopes(None) == ["public"] and g.check_bank(None) == "user-alice"
    with pytest.raises(Forbidden):
        g.check_scope("payments")
    with pytest.raises(Forbidden):
        g.check_bank("team-payments")
    with pytest.raises(Forbidden):
        g.check_token_scope(TokenScope.MEMORY_WRITE)
    svc = Grants(subject="ci", scopes=["public"], banks=["team-sre"])
    with pytest.raises(Forbidden, match="no personal bank"):
        svc.check_bank(None)
