from datetime import datetime, timedelta, timezone
import pytest
from cerebro.provision import operator
from cerebro.provision.common import parse_ttl

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _ann(ttl="2h", last=None):
    a = {"cerebro.io/idle-ttl": ttl}
    if last is not None:
        a["cerebro.io/last-used"] = (NOW - last).strftime("%Y-%m-%dT%H:%M:%SZ")
    return a


def test_parse_ttl():
    assert parse_ttl("2h") == 7200 and parse_ttl("30m") == 1800 and parse_ttl("1d") == 86400 and parse_ttl("90s") == 90
    assert parse_ttl(15) == 15 and parse_ttl(None) is None and parse_ttl("") is None
    with pytest.raises(ValueError):
        parse_ttl("soon")


def test_idle_units_scale_to_zero_after_the_ttl():
    code = {"cerebro.io/role": "code"}
    assert operator.should_scale_to_zero(code, _ann("2h", timedelta(hours=3)), 1, NOW)
    assert not operator.should_scale_to_zero(code, _ann("2h", timedelta(hours=1)), 1, NOW)
    assert not operator.should_scale_to_zero(code, _ann("2h", timedelta(hours=3)), 0, NOW), "already at zero"
    assert operator.should_scale_to_zero({"cerebro.io/role": "docs"}, _ann("30m", timedelta(minutes=31)), 1, NOW)


def test_only_docs_and_code_roles_are_idled():
    for role in ("infra", "gateway", "memory", "auth", "mcp", "ingest", "inference"):
        assert not operator.should_scale_to_zero({"cerebro.io/role": role}, _ann("1m", timedelta(hours=9)), 1, NOW), role
    assert not operator.should_scale_to_zero({}, _ann("1m", timedelta(hours=9)), 1, NOW)


def test_missing_last_used_falls_back_to_creation_time_and_bad_ttl_is_ignored():
    code = {"cerebro.io/role": "code"}
    created = (NOW - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert operator.should_scale_to_zero(code, _ann("2h"), 1, NOW, created=created)
    assert not operator.should_scale_to_zero(code, _ann("2h"), 1, NOW, created=None)
    assert not operator.should_scale_to_zero(code, _ann("soon", timedelta(hours=9)), 1, NOW)
    assert not operator.should_scale_to_zero(code, {"cerebro.io/last-used": created}, 1, NOW), "no ttl annotation"


def test_kopf_handlers_are_registered_when_kopf_is_installed():
    if operator.kopf is None:
        pytest.skip("kopf not installed")
    assert callable(operator.idle_check)
