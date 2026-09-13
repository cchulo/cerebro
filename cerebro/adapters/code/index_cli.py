"""cerebro index: clone / refresh a unit's repositories into /workspace and (re)index them with TokenSave.

    cerebro index run --unit <name> [--force] [--workspace /workspace] [--repos JSON | -c cerebro.yaml]
    cerebro index all [--force] [--workspace DIR] [-c cerebro.yaml]      every unit, into DIR/<unit>/

This is the job the adapter schedules (`index-<unit>`), running in the unit image with CEREBRO_UNIT and
CEREBRO_REPOS set. Per repository:

    clone --filter=blob:none (history without old blobs: enough to check any branch out; `--depth 1` is not,
      because tracked branches need their own commits) or fetch --prune
    default branch  = origin/HEAD:  git checkout -B <b> origin/<b>; `tokensave init` when there is no .tokensave,
      else `tokensave sync` when the commit moved (or --force)
    each configured branch (globs resolved against origin's branches, default excluded):
      git checkout -B <b> origin/<b>; `tokensave branch add` (idempotent: "already tracked"); `tokensave sync`
    branches indexed before but no longer configured: `tokensave branch remove <b>`
    finally: git checkout <default>, so the checkout the bridge's ripgrep sees is the default branch

Skips are decided per repo+branch from `.cerebro-index.json` (branch -> commit indexed), so the job can run often.
No edit or memory tool of the engine is ever run; only init / sync / branch add|remove.

TokenSave facts (verified 2026-09-13 with tokensave 7.12.1; docs/BRANCHING-USER-GUIDE.md and README "Multi-Branch
Indexing"): `branch add` with no name tracks the checked-out branch and needs a local ref (a remote-only branch is
refused, hence `checkout -B`); the new branch DB is copied from the nearest tracked ancestor and only the diff is
re-parsed; `sync` writes into the checked-out branch's DB; `init`/`sync` upload a token count unless the user
config says `upload_enabled = false` (cerebro.bridge.engine.disable_uploads) and check GitHub for updates unless
TOKENSAVE_UPDATE_CHECK=off. Auth for github.com clones is a `url.<with token>.insteadOf` rule passed through
GIT_CONFIG_* environment variables, so the token is never on a command line or in .git/config.
"""
from __future__ import annotations
import argparse, base64, datetime, fnmatch, json, logging, os, pathlib, shutil, subprocess, sys
from cerebro.bridge.engine import TokenSaveEngine, disable_uploads
from cerebro.bridge.workspace import REPOS_ENV, UNIT_ENV, RepoEntry, RepoState, Workspace, entries_from_env, entries_from_specs

log = logging.getLogger("cerebro.index")


# ----------------------------------------------------------------------------- pure helpers (unit tested)
def resolve_branches(patterns: list[str], remote_branches: list[str], default: str) -> list[str]:
    """Configured names/globs -> concrete remote branches, default excluded, order kept, no duplicates."""
    out: list[str] = []
    for pat in patterns:
        matches = [b for b in remote_branches if fnmatch.fnmatchcase(b, pat)] if any(c in pat for c in "*?[") else \
                  ([pat] if pat in remote_branches else [])
        if not matches:
            log.warning("branch pattern %r matches nothing on origin (%d branches)", pat, len(remote_branches))
        for b in matches:
            if b != default and b not in out:
                out.append(b)
    return out


def git_env(token: str | None, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for git: no prompts; with a token, an insteadOf rule for github.com that never touches argv."""
    env = dict(base if base is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        env[f"GIT_CONFIG_KEY_{n}"] = f"url.https://x-access-token:{token}@github.com/.insteadOf"
        env[f"GIT_CONFIG_VALUE_{n}"] = "https://github.com/"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


# ----------------------------------------------------------------------------- the indexer
class Indexer:
    def __init__(self, unit: str, ws: Workspace, *, force: bool = False, tokensave: str | None = None,
                 token: str | None = None, env: dict[str, str] | None = None):
        self.unit, self.ws, self.force = unit, ws, force
        self.engine = TokenSaveEngine(ws, binary=tokensave)
        self.env = self.engine.env() if env is None else dict(env)
        self.env.setdefault("TOKENSAVE_UPDATE_CHECK", "off")
        self.git_env = git_env(token, self.env)

    # ---- process helpers
    def git(self, repo: pathlib.Path, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=self.git_env)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed in {repo.name}: {r.stderr.strip()[:500]}")
        return r.stdout.strip()

    def tokensave(self, repo: pathlib.Path, *args: str) -> str:
        cmd = [self.engine.binary_path, *args]
        r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, env=self.env)
        tail = [l for l in (r.stdout + r.stderr).splitlines() if _plain(l)][-3:]
        for line in tail:
            log.info("[%s] tokensave %s: %s", repo.name, " ".join(args[:2]), _plain(line))
        if r.returncode != 0:
            raise RuntimeError(f"tokensave {' '.join(args)} failed in {repo.name}: {_plain((r.stderr or r.stdout)[-500:])}")
        return r.stdout

    # ---- steps
    def run(self) -> int:
        self.ws.root.mkdir(parents=True, exist_ok=True)
        disable_uploads()
        self.engine.ensure_ready()                                 # the served root the bridge binds to
        failures = 0
        for entry in self.ws.repos:
            try:
                self.index_repo(entry)
            except Exception as e:
                failures += 1
                log.error("[%s] FAILED: %s", entry.dir, e)
        log.info("unit %s: %d repos, %d failed", self.unit, len(self.ws.repos), failures)
        return 1 if failures else 0

    def clone_or_fetch(self, entry: RepoEntry) -> pathlib.Path:
        d = self.ws.path(entry)
        if (d / ".git").exists():
            log.info("[%s] fetch", entry.dir)
            self.git(d, "remote", "set-url", "origin", entry.url)
            self.git(d, "fetch", "--prune", "--no-tags", "origin")
        else:
            log.info("[%s] clone %s (blobless)", entry.dir, entry.url)
            r = subprocess.run(["git", "-c", "uploadpack.allowfilter=true", "clone", "--filter=blob:none", "--no-tags",
                                entry.url, str(d)], capture_output=True, text=True, env=self.git_env)
            if r.returncode != 0:
                raise RuntimeError(f"git clone {entry.url} failed: {r.stderr.strip()[:500]}")
        return d

    def default_branch(self, d: pathlib.Path) -> str:
        head = self.git(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
        if not head:
            self.git(d, "remote", "set-head", "origin", "--auto", check=False)
            head = self.git(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
        if not head:
            raise RuntimeError("cannot determine the default branch (origin/HEAD)")
        return head.removeprefix("origin/")

    def remote_branches(self, d: pathlib.Path) -> list[str]:
        out = self.git(d, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/")
        return [b.removeprefix("origin/") for b in out.splitlines() if b and b != "origin/HEAD"]

    def index_repo(self, entry: RepoEntry) -> None:
        d = self.clone_or_fetch(entry)
        default = self.default_branch(d)
        remote = self.remote_branches(d)
        branches = resolve_branches(entry.branches, remote, default)
        prev = self.ws.state(entry)
        state = RepoState(url=entry.url, name=entry.name, default_branch=default, engine="tokensave",
                          branches=dict(prev.branches) if prev else {})
        log.info("[%s] default %s; branches %s", entry.dir, default, branches or "-")
        try:
            # default branch: the main DB
            sha = self.git(d, "rev-parse", f"origin/{default}")
            self.git(d, "checkout", "-q", "-B", default, f"origin/{default}")
            if not (d / ".tokensave").exists():
                log.info("[%s] %s: init (full index) at %s", entry.dir, default, sha[:12])
                self.tokensave(d, "init", "--no-git-hook")
            elif self.force or state.branches.get(default) != sha:
                log.info("[%s] %s: sync at %s%s", entry.dir, default, sha[:12], " (forced)" if self.force else "")
                self.tokensave(d, "sync", *(["--force"] if self.force else []))
            else:
                log.info("[%s] %s: unchanged at %s, skipped", entry.dir, default, sha[:12])
            state.branches[default] = sha
            self._save(entry, state)
            # tracked branches: one DB each
            for b in branches:
                sha = self.git(d, "rev-parse", f"origin/{b}")
                if not self.force and state.branches.get(b) == sha and b in self._tracked(d):
                    log.info("[%s] %s: unchanged at %s, skipped", entry.dir, b, sha[:12])
                    continue
                log.info("[%s] %s: track + sync at %s", entry.dir, b, sha[:12])
                self.git(d, "checkout", "-q", "-B", b, f"origin/{b}")
                self.tokensave(d, "branch", "add")
                self.tokensave(d, "sync", *(["--force"] if self.force else []))
                state.branches[b] = sha
                self._save(entry, state)
            # branches that fell out of the configuration
            for b in [b for b in list(state.branches) if b != default and b not in branches]:
                log.info("[%s] %s: no longer configured, removing its graph", entry.dir, b)
                if b in self._tracked(d):
                    self.tokensave(d, "branch", "remove", b)
                state.branches.pop(b, None)
                self._save(entry, state)
        finally:
            self.git(d, "checkout", "-q", default, check=False)   # what the bridge's ripgrep sees

    def _tracked(self, d: pathlib.Path) -> set[str]:
        meta = d / ".tokensave" / "branch-meta.json"
        try:
            return set(json.loads(meta.read_text()).get("branches", {})) if meta.exists() else set()
        except (OSError, ValueError):
            return set()

    def _save(self, entry: RepoEntry, state: RepoState) -> None:
        state.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        self.ws.write_state(entry, state)


def _plain(s: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s).strip()


# ----------------------------------------------------------------------------- cli
def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cerebro index", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "all"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", default=os.environ.get("CEREBRO_WORKSPACE", "/workspace"))
        s.add_argument("--force", action="store_true", help="full re-index (tokensave sync --force)")
        s.add_argument("-c", "--config", default=os.environ.get("CEREBRO_CONFIG", "cerebro.yaml"))
        s.add_argument("--tokensave", default=os.environ.get("TOKENSAVE_BIN"), help="path to the tokensave binary")
        s.add_argument("--log-level", default=os.environ.get("CEREBRO_LOG_LEVEL", "info"))
        if name == "run":
            s.add_argument("--unit", default=os.environ.get(UNIT_ENV))
            s.add_argument("--repos", default=None, help=f"JSON list of repos (default: ${REPOS_ENV}, else the unit from -c)")
    return p


def _entries_for_unit(args) -> list[RepoEntry]:
    if args.repos is not None or os.environ.get(REPOS_ENV):
        return entries_from_env(args.repos)
    from cerebro.core import load_config, code_units
    for u in code_units(load_config(args.config)):
        if u.name == args.unit:
            return entries_from_specs(u.repos)
    raise SystemExit(f"unit {args.unit!r} not found in {args.config} and ${REPOS_ENV} is not set")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stdout)
    token = os.environ.get("GITHUB_TOKEN")
    if args.cmd == "run":
        if not args.unit:
            raise SystemExit(f"--unit or ${UNIT_ENV} is required")
        ws = Workspace(args.workspace, _entries_for_unit(args))
        return Indexer(args.unit, ws, force=args.force, tokensave=args.tokensave, token=token).run()
    from cerebro.core import load_config, code_units
    rc = 0
    for u in code_units(load_config(args.config)):
        ws = Workspace(pathlib.Path(args.workspace) / u.name, entries_from_specs(u.repos))
        rc |= Indexer(u.name, ws, force=args.force, tokensave=args.tokensave, token=token).run()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
