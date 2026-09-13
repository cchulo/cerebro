import pytest, yaml
from pydantic import ValidationError
from cerebro.core import Config, load_config
from cerebro.core.config import interpolate


def test_example_loads(example_config):
    cfg = example_config
    assert cfg.version == 2 and cfg.identity.mode == "none"
    assert set(cfg.scopes) == {"public", "payments", "infra"}
    assert cfg.scopes["public"].code.repos[1].branches == ["main", "stable"]
    assert cfg.scopes["public"].code.repos[0].branches == []          # string form = default branch
    assert cfg.scope_unit("payments") == "repo" and cfg.scope_unit("public") == "scope"
    assert cfg.source_names() == ["confluence", "backstage", "git", "files", "jama"]


def test_interpolation():
    assert interpolate("${A}-${B:-dflt}", {"A": "x"}) == "x-dflt"
    with pytest.raises(KeyError):
        interpolate("${MISSING}", {})
    assert interpolate({"k": ["${A}"]}, {"A": "1"}) == {"k": ["1"]}
    assert interpolate({"${T}": {"subject": "alice"}}, {"T": "tok-1"}) == {"tok-1": {"subject": "alice"}}, "mapping keys too"


def test_read_env_file_and_keys_from_env(tmp_path):
    from cerebro.core.config import read_env_file
    f = tmp_path / "secrets.env"
    f.write_text("# comment\n\nPOSTGRES_PASSWORD=pw\nexport QUOTED='a=b'\nDQ=\"x\"\nEMPTY=\nnoequals\n")
    assert read_env_file(f) == {"POSTGRES_PASSWORD": "pw", "QUOTED": "a=b", "DQ": "x", "EMPTY": ""}
    assert read_env_file(tmp_path / "missing.env") == {} and read_env_file(None) == {}
    (tmp_path / "c.yaml").write_text("version: 2\nidentity:\n  mode: static\n  tokens:\n    '${T_A}': {subject: alice}\n")
    assert list(load_config(tmp_path / "c.yaml", env={"T_A": "secret-a"}).identity.tokens) == ["secret-a"]


def test_duplicate_repo_rejected():
    with pytest.raises(ValidationError, match="listed in scopes"):
        Config.model_validate({"scopes": {"a": {"groups": ["x"], "code": {"repos": ["https://g/o/r.git"]}},
                                          "b": {"groups": ["y"], "code": {"repos": ["https://g/o/r"]}}}})


def test_duplicate_space_rejected():
    with pytest.raises(ValidationError, match="Confluence space"):
        Config.model_validate({"scopes": {"a": {"groups": ["x"], "docs": {"confluence": {"spaces": ["ENG"]}}},
                                          "b": {"groups": ["y"], "docs": {"confluence": {"spaces": ["ENG"]}}}}})


def test_scope_name_must_be_dns_label():
    with pytest.raises(ValidationError, match="DNS label"):
        Config.model_validate({"scopes": {"Pay_Ments": {"groups": ["x"]}}})


def test_external_requires_issuer_and_audience():
    with pytest.raises(ValidationError, match="issuer and audience"):
        Config.model_validate({"identity": {"mode": "external"}})
    cfg = Config.model_validate({"identity": {"mode": "external", "issuer": "https://i", "audience": "https://a/mcp"}})
    assert cfg.identity.token_validation == "jwks"


def test_builtin_defaults_server():
    cfg = Config.model_validate({"identity": {"mode": "builtin"}})
    assert cfg.identity.server.type == "keycloak" and cfg.identity.server.realm == "cerebro"


def test_static_requires_tokens():
    with pytest.raises(ValidationError, match="tokens"):
        Config.model_validate({"identity": {"mode": "static"}})


def test_docs_none_becomes_empty():
    cfg = Config.model_validate({"scopes": {"a": {"groups": ["x"], "docs": {"backstage": None}}}})
    assert cfg.scopes["a"].docs == {"backstage": {}}


def test_json_schema_has_top_level_keys():
    from cerebro.core import json_schema
    props = json_schema()["properties"]
    for k in ("identity", "policy", "inference", "engines", "provisioning", "secrets", "gateway", "sources", "scopes"):
        assert k in props


def test_load_from_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"version": 2, "gateway": {"port": "${PORT:-9000}"}}))
    assert load_config(p, env={}).gateway.port == 9000
