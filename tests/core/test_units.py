from cerebro.core import code_units, docs_unit_name, slug
from cerebro.core.units import units_for_repos
from cerebro.core.principal import RepoGrant


def test_units_follow_scope_and_repo_granularity(example_config):
    units = {u.name: u for u in code_units(example_config)}
    assert "code-public" in units and units["code-public"].kind == "scope" and len(units["code-public"].repos) == 2
    assert "code-infra" in units
    repo_units = [u for u in units.values() if u.scope == "payments"]
    assert len(repo_units) == 1 and repo_units[0].kind == "repo" and repo_units[0].name.startswith("code-payments-")
    assert units["code-public"].branches_for("https://github.com/pallets/flask") == ["main", "stable"]


def test_units_for_repos_groups_by_unit(example_config):
    grants = [RepoGrant(url="https://github.com/pallets/flask.git", scope="public"),
              RepoGrant(url="https://github.com/pallets/jinja.git", scope="payments")]
    got = units_for_repos(example_config, grants)
    assert [(u.name, [r.name for r in rs]) for u, rs in got] == [
        ("code-public", ["github.com/pallets/flask"]),
        (next(u.name for u in code_units(example_config) if u.scope == "payments"), ["github.com/pallets/jinja"])]


def test_slugs_are_dns_safe_and_distinct():
    assert slug("payments") == "payments"
    a, b = slug("github.com/org/x"), slug("github.com/org/x.git")
    assert a != b and all(c.isalnum() or c == "-" for c in a + b)
    assert docs_unit_name("public") == "docs-public"
