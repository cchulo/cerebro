"""`cerebro index`: branch resolution, auth env, and the git + tokensave command sequence against a local
"remote" (a bare repo with several branches) and a fake `tokensave` on PATH that records every invocation."""
import json, os, pathlib, shutil, stat, subprocess
import pytest
from cerebro.adapters.code import index_cli
from cerebro.adapters.code.index_cli import Indexer, git_env, resolve_branches
from cerebro.bridge.workspace import RepoState, Workspace, entries_from_env
from tests.workspace_fixture import git, make_repo

SHIM = pathlib.Path(__file__).with_name("tokensave_shim.py")


# ----------------------------------------------------------------------------- pure helpers
def test_resolve_branches_globs_literals_default_and_order():
    remote = ["main", "feature/a", "feature/b", "release/1.0", "release/2.0", "stable"]
    assert resolve_branches(["main", "release/*", "stable"], remote, "main") == ["release/1.0", "release/2.0", "stable"]
    assert resolve_branches(["feature/*", "feature/a", "nope", "no*pe"], remote, "main") == ["feature/a", "feature/b"]
    assert resolve_branches([], remote, "main") == []
    assert resolve_branches(["*"], remote, "main") == [b for b in remote if b != "main"]


def test_git_env_keeps_the_token_off_argv_and_out_of_git_config():
    env = git_env("ghp_secret", {"PATH": "/bin"})
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "url.https://x-access-token:ghp_secret@github.com/.insteadOf"
    assert env["GIT_CONFIG_VALUE_0"] == "https://github.com/"
    env2 = git_env("t", {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a", "GIT_CONFIG_VALUE_0": "b"})
    assert env2["GIT_CONFIG_COUNT"] == "2" and env2["GIT_CONFIG_KEY_1"].endswith(".insteadOf")
    assert "GIT_CONFIG_COUNT" not in git_env(None, {})


# ----------------------------------------------------------------------------- the command sequence
@pytest.fixture
def remote(tmp_path):
    """A bare 'origin' with main, feature/x, release/1.0 and release/2.0."""
    src = make_repo(tmp_path / "src", "alpha", {"a.py": "def main():\n    pass\n"},
                    {"feature/x": {"f.py": "F = 1\n"}, "release/1.0": {"r.py": "R = 1\n"}, "release/2.0": {"r.py": "R = 2\n"}},
                    indexed=False)
    bare = tmp_path / "origin" / "alpha.git"
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return src, bare


@pytest.fixture
def shim(tmp_path, monkeypatch):
    d = tmp_path / "bin"; d.mkdir()
    exe = d / "tokensave"
    shutil.copy(SHIM, exe); exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "tokensave.log"
    monkeypatch.setenv("TOKENSAVE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CEREBRO_REPOS", raising=False)

    def calls():
        return [line.split("|", 1) for line in log.read_text().splitlines()] if log.exists() else []
    return exe, calls, log


def run(ws_root, url, branches, extra=()):
    repos = json.dumps([{"url": url, "branches": branches}])
    return index_cli.main(["run", "--unit", "code-t", "--workspace", str(ws_root), "--repos", repos, "--log-level", "warning", *extra])


def test_first_run_clones_inits_and_tracks_branches(tmp_path, remote, shim):
    src, bare = remote
    exe, calls, log = shim
    ws = tmp_path / "ws"
    assert run(ws, str(bare), ["main", "release/*", "feature/x"]) == 0
    d = ws / "alpha"
    assert (d / ".git").exists() and (d / ".tokensave").exists() and (ws / ".cerebro-root" / ".tokensave").exists()
    seq = [(pathlib.Path(c).name, a) for c, a in calls() if a != "--version"]
    assert seq[0] == (".cerebro-root", "init --no-git-hook " + str(ws / ".cerebro-root")) or seq[0][1].startswith("init")
    repo_seq = [a for c, a in seq if c == "alpha"]
    assert repo_seq == ["init --no-git-hook",
                        "branch add", "sync", "branch add", "sync", "branch add", "sync"]
    meta = json.loads((d / ".tokensave" / "branch-meta.json").read_text())
    assert set(meta["branches"]) == {"main", "release/1.0", "release/2.0", "feature/x"}
    state = RepoState.model_validate_json((d / ".cerebro-index.json").read_text())
    assert state.default_branch == "main" and set(state.branches) == {"main", "release/1.0", "release/2.0", "feature/x"}
    assert state.branches["main"] == git(src, "rev-parse", "main") and state.branches["feature/x"] == git(src, "rev-parse", "feature/x")
    assert git(d, "symbolic-ref", "--short", "HEAD") == "main", "the checkout returns to the default branch"
    assert git(d, "rev-parse", "release/2.0") == git(src, "rev-parse", "release/2.0"), "tracked branches have local refs"
    assert "branch add" not in " ".join(a for c, a in seq if c != "alpha")


def test_second_run_skips_unchanged_and_syncs_only_what_moved(tmp_path, remote, shim):
    src, bare = remote
    exe, calls, log = shim
    ws = tmp_path / "ws"
    run(ws, str(bare), ["release/*"])
    log.unlink(missing_ok=True)
    assert run(ws, str(bare), ["release/*"]) == 0
    assert [a for c, a in calls() if pathlib.Path(c).name == "alpha"] == [], "nothing moved: no tokensave call"
    # a commit on release/1.0 only
    git(src, "checkout", "-q", "release/1.0"); (src / "r.py").write_text("R = 11\n"); git(src, "commit", "-qam", "bump")
    git(src, "push", "-q", str(bare), "release/1.0"); git(src, "checkout", "-q", "main")
    log.unlink(missing_ok=True)
    assert run(ws, str(bare), ["release/*"]) == 0
    assert [a for c, a in calls() if pathlib.Path(c).name == "alpha"] == ["branch add", "sync"]
    state = RepoState.model_validate_json((ws / "alpha" / ".cerebro-index.json").read_text())
    assert state.branches["release/1.0"] == git(src, "rev-parse", "release/1.0")
    # --force re-syncs everything
    log.unlink(missing_ok=True)
    assert run(ws, str(bare), ["release/*"], ["--force"]) == 0
    assert [a for c, a in calls() if pathlib.Path(c).name == "alpha"] == ["sync --force", "branch add", "sync --force", "branch add", "sync --force"]


def test_dropped_branch_is_removed_and_default_moves_trigger_sync(tmp_path, remote, shim):
    src, bare = remote
    exe, calls, log = shim
    ws = tmp_path / "ws"
    run(ws, str(bare), ["feature/x", "release/1.0"])
    git(src, "checkout", "-q", "main"); (src / "a.py").write_text("def main():\n    return 2\n"); git(src, "commit", "-qam", "m2")
    git(src, "push", "-q", str(bare), "main")
    log.unlink(missing_ok=True)
    assert run(ws, str(bare), ["feature/x"]) == 0
    assert [a for c, a in calls() if pathlib.Path(c).name == "alpha"] == ["sync", "branch remove release/1.0"]
    meta = json.loads((ws / "alpha" / ".tokensave" / "branch-meta.json").read_text())
    assert set(meta["branches"]) == {"main", "feature/x"}
    state = RepoState.model_validate_json((ws / "alpha" / ".cerebro-index.json").read_text())
    assert set(state.branches) == {"main", "feature/x"}


def test_failure_in_one_repo_does_not_stop_the_others(tmp_path, remote, shim, monkeypatch):
    src, bare = remote
    exe, calls, log = shim
    ws = tmp_path / "ws"
    repos = json.dumps([{"url": str(tmp_path / "missing.git"), "branches": []}, {"url": str(bare), "branches": []}])
    rc = index_cli.main(["run", "--unit", "code-t", "--workspace", str(ws), "--repos", repos, "--log-level", "warning"])
    assert rc == 1 and (ws / "alpha" / ".tokensave").exists()
    monkeypatch.setenv("TOKENSAVE_SHIM_FAIL", "sync")
    git(src, "checkout", "-q", "main"); (src / "a.py").write_text("x = 1\n"); git(src, "commit", "-qam", "m3"); git(src, "push", "-q", str(bare), "main")
    assert run(ws, str(bare), []) == 1
    assert git(ws / "alpha", "symbolic-ref", "--short", "HEAD") == "main", "checkout restored even on failure"


def test_repos_from_env_and_unit_from_config(tmp_path, remote, shim, monkeypatch):
    _, bare = remote
    exe, calls, log = shim
    monkeypatch.setenv("CEREBRO_REPOS", json.dumps([{"url": str(bare), "branches": []}]))
    monkeypatch.setenv("CEREBRO_UNIT", "code-env")
    assert index_cli.main(["run", "--workspace", str(tmp_path / "ws"), "--log-level", "warning"]) == 0
    assert (tmp_path / "ws" / "alpha" / ".tokensave").exists()
    monkeypatch.delenv("CEREBRO_REPOS")
    with pytest.raises(SystemExit):
        index_cli.main(["run", "--unit", "code-nope", "--workspace", str(tmp_path / "ws2"), "-c", "cerebro.example.yaml"])
