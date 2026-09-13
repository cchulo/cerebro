"""`cerebro identity seed` against a fake AuthorizationServer named by module:Class in identity.server.type."""
import json
import pytest, yaml
from cerebro import identity_cli
from cerebro.cli import main as cerebro_main
from cerebro.core.contracts.identity import AuthorizationServer
from tests.conftest import ROOT

TYPE = "tests.adapters.auth.test_identity_cli:FakeAuth"


class FakeAuth(AuthorizationServer):
    name = "fakecloak"
    ready_after = 0            # ready() answers False this many times first
    calls: list = []

    def issuer(self):
        return f"{self.ctx.config.identity.server.public_url or 'http://localhost:8180'}/realms/{self.ctx.config.identity.server.realm}"

    async def ready(self):
        if FakeAuth.ready_after > 0:
            FakeAuth.ready_after -= 1
            return False
        return True

    async def seed(self, users, groups, resource_id):
        FakeAuth.calls.append({"users": [u.name for u in users], "groups": groups, "resource": resource_id,
                               "admin_url": self.options.get("admin_url"),
                               "admin": (self.ctx.secret("CEREBRO_AUTH_ADMIN_USER"), self.ctx.secret("CEREBRO_AUTH_ADMIN_PASSWORD"))})
        return {"realm": "cerebro", "issuer": self.issuer(), "resource": resource_id, "first_login_action": "UPDATE_PASSWORD",
                "created": {"users": [u.name for u in users]}, "password_generated": True, "client_id": "cerebro-mcp"}


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    cfg = yaml.safe_load((ROOT / "cerebro.example.yaml").read_text())
    cfg["engines"] = {"docs": None, "code": None, "memory": None}
    cfg["identity"] = {"mode": "builtin", "server": {"type": TYPE, "realm": "cerebro"},
                       "users": [{"name": "alice", "groups": ["payments-team"]}]}
    cfg["gateway"]["public_url"] = "https://context.test/mcp"
    (tmp_path / "cerebro.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "secrets.env").write_text("CEREBRO_AUTH_ADMIN_USER=admin\nCEREBRO_AUTH_ADMIN_PASSWORD=pw\n")
    (tmp_path / "plugins").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEREBRO_AUTH_ADMIN_USER", raising=False)
    monkeypatch.delenv("CEREBRO_AUTH_ADMIN_PASSWORD", raising=False)
    FakeAuth.calls.clear()
    FakeAuth.ready_after = 0
    return tmp_path


def test_seed_groups_are_every_scope_group_minus_always_groups(example_config):
    assert identity_cli.seed_groups(example_config) == ["payments-team", "platform-leads", "sre"]
    example_config.policy.always_groups = ["everyone", "sre"]
    assert identity_cli.seed_groups(example_config) == ["payments-team", "platform-leads"]


def test_seed_waits_for_ready_then_seeds_with_secrets_from_the_env_file(checkout, capsys, monkeypatch):
    FakeAuth.ready_after = 2
    monkeypatch.setattr(identity_cli, "READY_INTERVAL", 0)
    assert cerebro_main(["identity", "seed", "-c", "cerebro.yaml", "--env-file", "secrets.env"]) == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["issuer"] == "http://localhost:8180/realms/cerebro" and report["created"]["users"] == ["alice"]
    (call,) = FakeAuth.calls
    assert call["users"] == ["alice"] and call["groups"] == ["payments-team", "platform-leads", "sre"]
    assert call["resource"] == "https://context.test/mcp", "the gateway's resource id is what tokens must carry"
    assert call["admin_url"] == "http://localhost:8180", "default: the issuer's origin, where this machine reaches it"
    assert call["admin"] == ("admin", "pw"), "admin secrets come from --env-file when the environment lacks them"
    assert "first login for alice" in err and "temporary password printed above" in err and "set a new password" in err
    assert "seeding fakecloak at http://localhost:8180" in err


def test_admin_url_override_and_not_ready(checkout, monkeypatch):
    assert identity_cli.main(["seed", "--admin-url", "http://127.0.0.1:9999"]) == 0
    assert FakeAuth.calls[-1]["admin_url"] == "http://127.0.0.1:9999"
    FakeAuth.ready_after = 10 ** 6
    with pytest.raises(SystemExit, match="not ready"):
        identity_cli.main(["seed", "--timeout", "0"])


def test_seed_refuses_other_modes(checkout):
    cfg = yaml.safe_load((checkout / "cerebro.yaml").read_text())
    cfg["identity"] = {"mode": "none"}
    (checkout / "cerebro.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(SystemExit, match="mode builtin"):
        identity_cli.main(["seed"])


def test_validate_reads_the_env_file(checkout, capsys):
    cfg = yaml.safe_load((checkout / "cerebro.yaml").read_text())
    cfg["identity"] = {"mode": "static", "tokens": {"${CEREBRO_TOKEN_ALICE}": {"subject": "alice"}}}
    (checkout / "cerebro.yaml").write_text(yaml.safe_dump(cfg))
    with pytest.raises(KeyError, match="CEREBRO_TOKEN_ALICE"):
        cerebro_main(["validate", "-c", "cerebro.yaml", "--env-file", "nope.env"])
    (checkout / "secrets.env").write_text("CEREBRO_TOKEN_ALICE=tok\n")
    assert cerebro_main(["validate", "-c", "cerebro.yaml"]) == 0                 # default --env-file secrets.env
    assert "identity.mode=static" in capsys.readouterr().out
