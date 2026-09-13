import pytest
from cerebro.bridge.workspace import Workspace
from tests.bridge.fake_engine import FakeEngine
from tests.workspace_fixture import entries, make_repo, running_bridge

ALPHA = {"src/app.py": "def main():\n    return helper()\n\n\ndef helper():\n    return 1\n", "README.md": "# alpha\n"}
ALPHA_FEATURE = {"src/app.py": "def main():\n    return helper()\n\n\ndef helper():\n    return 2\n\n\ndef only_on_feature():\n    pass\n"}
BETA = {"b.py": "def beta_main():\n    pass\n\n\ndef main():\n    pass\n"}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    make_repo(root, "alpha", ALPHA, {"feature": ALPHA_FEATURE, "release/1.0": {"rel.py": "REL = 1\n"}})
    make_repo(root, "beta", BETA)
    make_repo(root, "gamma", {"g.py": "x = 1\n"}, indexed=False)          # cloned but never indexed
    return Workspace(root, entries(("https://github.com/acme/alpha.git", ["main", "feature", "release/*"]),
                                   ("https://github.com/acme/beta.git", []),
                                   ("https://github.com/acme/gamma.git", [])))


@pytest.fixture
async def bridge(workspace):
    async with running_bridge("code-test", workspace, FakeEngine(workspace)) as (b, url):
        yield b, url
