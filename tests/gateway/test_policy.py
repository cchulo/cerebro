"""The groups policy against cerebro.example.yaml: the v1 smoke-test expectations."""
import pytest
from cerebro.core import Principal, TokenScope, registry
from cerebro.core.contracts import AccessPolicy
from cerebro.core.types import Forbidden


@pytest.fixture
def policy(ctx) -> AccessPolicy:
    return registry.build("policy", ctx.config.policy.type, ctx.config.policy.options, ctx)


def test_user_in_no_group_sees_only_public(policy):
    g = policy.grants(Principal(subject="alice"))
    assert g.scopes == ["public"]
    assert g.banks == ["user-alice"] and g.personal_bank == "user-alice" and g.team_banks == []
    assert {r.name for r in g.repos} == {"github.com/pallets/click", "github.com/pallets/flask"}
    assert next(r for r in g.repos if r.name.endswith("flask")).branches == ["main", "stable"]
    with pytest.raises(Forbidden, match="not allowed"):
        g.check_scope("payments")
    with pytest.raises(Forbidden, match="not allowed"):
        g.check_bank("team-payments-team")


def test_payments_team_sees_public_and_payments_with_a_team_bank(policy):
    g = policy.grants(Principal(subject="bob", groups=frozenset({"payments-team"})))
    assert g.scopes == ["public", "payments"]
    assert g.banks == ["user-bob", "team-payments-team"]
    assert "github.com/pallets/jinja" in {r.name for r in g.repos}
    assert [r.name for r in g.repos_in(["payments"])] == ["github.com/pallets/jinja"]
    assert all(r.scope in ("public", "payments") for r in g.repos)


def test_platform_leads_span_scopes_and_always_groups_make_no_team_bank(policy):
    g = policy.grants(Principal(subject="carol", groups=frozenset({"platform-leads", "everyone"})))
    assert g.scopes == ["public", "payments", "infra"]
    assert g.team_banks == ["team-platform-leads"]


def test_service_principal_has_no_personal_bank(policy):
    g = policy.grants(Principal(subject="ci-bot", kind="service", groups=frozenset({"sre"}),
                                token_scopes=frozenset({TokenScope.CODE_READ.value})))
    assert g.personal_bank is None and g.banks == ["team-sre"] and g.scopes == ["public", "infra"]
    assert g.token_scopes == {TokenScope.CODE_READ.value}
    with pytest.raises(Forbidden, match="no personal bank"):
        g.check_bank(None)
    with pytest.raises(Forbidden, match="token lacks"):
        g.check_token_scope(TokenScope.DOCS_READ)


def test_bank_slug_and_team_banks_switch(make_config, make_ctx):
    cfg = make_config(policy={"team_banks_from_groups": False, "always_groups": ["everyone", "staff"]})
    p = registry.build("policy", "groups", {}, make_ctx(cfg))
    g = p.grants(Principal(subject="Dan.O'Neil@example.com", groups=frozenset({"sre", "staff"})))
    assert g.personal_bank == "user-dan-o-neil-example-com" and g.team_banks == [] and g.banks == [g.personal_bank]
    assert g.scopes == ["public", "infra"]
