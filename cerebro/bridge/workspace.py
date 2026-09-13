"""The /workspace layout shared by the bridge (serving) and the indexer job (writing).

    /workspace/
      .cerebro-root/            the project the engine SERVES: empty, initialised, never holds code (see below)
      <dir>/                    one checkout per repository of the unit, default branch checked out
        .cerebro-index.json     written by `cerebro index`: default branch, branch -> commit indexed, url
        .tokensave/             TokenSave's index (tokensave.db + branches/<b>.db + branch-meta.json)

Directory names. A repository's directory is the last path segment of its name (`github.com/pallets/click` ->
`click`) because that is what shows up in engine output and is what a human expects next to the volume. If two
repositories of the same unit share that basename, ALL of them use `cerebro.core.units.repo_slug` instead, so the
mapping is deterministic whatever the order in cerebro.yaml. A repository never moves between units (a repo
belongs to exactly one scope, and `unit: repo` units hold one repo), so a name is stable for the life of a volume.

Why the served root is an empty project (verified against TokenSave 7.12.1, 2026-09-13, with the real binary;
docs: https://github.com/aovestdipaperino/tokensave/blob/main/README.md "Query another initialized project"):

  * `graph_root` must be the absolute root of an initialised project OTHER than the served one; naming the served
    project is rejected ("graph_root selects the same project already served by this MCP server").
  * `graph_branch` on the served project is rejected ("selecting a different branch of the served project is not
    supported"); on a `graph_root` project any tracked branch works, and those opens are read-only: they never
    initialise, sync or migrate the repo's index, so the indexer job can run while the bridge serves.
  * `tokensave serve` in a directory without `.tokensave` does not fail: it walks up, then falls back to a
    registered project, which is not something a unit may depend on.

Serving `/workspace/.cerebro-root` (an initialised project with no files) makes every repository a sibling reached
the same way, `graph_root=/workspace/<dir>` plus `graph_branch=<tracked branch>`, and keeps the engine's own
writable database out of every repository. The price: TokenSave's three `tokensave_branch_*` tools and its VCS
tools take no selector and would only ever see the empty root, so the bridge does not expose them; branches are
listed by `unit_info` and per-branch graphs are reached through `branch` on every other tool.
"""
from __future__ import annotations
import json, os, pathlib, subprocess
from typing import Any
from pydantic import BaseModel, Field
from cerebro.core.principal import repo_name
from cerebro.core.units import repo_slug
from cerebro.core.config import RepoSpec

SERVED_ROOT = ".cerebro-root"
STATE_FILE = ".cerebro-index.json"
REPOS_ENV = "CEREBRO_REPOS"
UNIT_ENV = "CEREBRO_UNIT"


class RepoEntry(BaseModel):
    """One repository of the unit as the bridge and the indexer see it (from CEREBRO_REPOS)."""
    url: str
    name: str                                       # github.com/org/x
    branches: list[str] = Field(default_factory=list, description="configured branch names / globs")
    dir: str                                        # directory under the workspace

    def matches(self, ref: str) -> bool:
        ref = ref.strip()
        if ref in (self.name, self.dir, self.url, self.url.removesuffix(".git")):
            return True
        rn = repo_name(ref)
        return rn is not None and rn == self.name


class RepoState(BaseModel):
    """`.cerebro-index.json`: what the indexer last did for a repository."""
    url: str
    name: str
    default_branch: str
    branches: dict[str, str] = Field(default_factory=dict, description="branch -> commit indexed")
    engine: str | None = None
    updated_at: str | None = None


def repo_dir_names(names: list[str]) -> dict[str, str]:
    """repo name -> directory name (basename, or repo_slug for every repo whose basename is shared)."""
    bases: dict[str, list[str]] = {}
    for n in names:
        bases.setdefault(n.rsplit("/", 1)[-1].removesuffix(".git"), []).append(n)
    out: dict[str, str] = {}
    for base, group in bases.items():
        for n in group:
            out[n] = base if len(group) == 1 else repo_slug(RepoSpec(url=n))
    return out


def entries_from_specs(repos: list[RepoSpec]) -> list[RepoEntry]:
    dirs = repo_dir_names([r.name for r in repos])
    return [RepoEntry(url=r.url, name=r.name, branches=list(r.branches), dir=dirs[r.name]) for r in repos]


def repos_env_value(repos: list[RepoSpec]) -> str:
    """What the adapter puts in CEREBRO_REPOS for the unit and its index job."""
    return json.dumps([{"url": r.url, "branches": list(r.branches)} for r in repos], separators=(",", ":"))


def entries_from_env(value: str | None = None) -> list[RepoEntry]:
    raw = json.loads(value if value is not None else os.environ.get(REPOS_ENV, "[]")) or []
    specs = [RepoSpec(url=r) if isinstance(r, str) else RepoSpec(**r) for r in raw]
    return entries_from_specs(specs)


class Workspace:
    def __init__(self, root: str | os.PathLike, repos: list[RepoEntry]):
        self.root = pathlib.Path(root).resolve()
        self.repos = list(repos)

    @property
    def served_root(self) -> pathlib.Path:
        return self.root / SERVED_ROOT

    def path(self, repo: RepoEntry) -> pathlib.Path:
        return self.root / repo.dir

    def entry(self, ref: str) -> RepoEntry:
        for r in self.repos:
            if r.matches(ref):
                return r
        raise KeyError(f"repository '{ref}' is not part of this unit; repos: {[r.name for r in self.repos]}")

    def select(self, refs: list[str] | None) -> list[RepoEntry]:
        """Narrow to the named repos; unknown names are dropped (never widened), None = all."""
        if refs is None:
            return list(self.repos)
        out: list[RepoEntry] = []
        for ref in refs:
            try:
                e = self.entry(ref)
            except KeyError:
                continue
            if e not in out:
                out.append(e)
        return out

    # ---- per-repo state
    def state(self, repo: RepoEntry) -> RepoState | None:
        p = self.path(repo) / STATE_FILE
        if not p.exists():
            return None
        try:
            return RepoState.model_validate_json(p.read_text())
        except Exception:
            return None

    def write_state(self, repo: RepoEntry, state: RepoState) -> None:
        (self.path(repo) / STATE_FILE).write_text(state.model_dump_json(indent=2) + "\n")

    def default_branch(self, repo: RepoEntry) -> str | None:
        st = self.state(repo)
        if st:
            return st.default_branch
        try:
            out = subprocess.run(["git", "-C", str(self.path(repo)), "symbolic-ref", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=10)
            return (out.stdout.strip() or None) if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def tracked_branches(self, repo: RepoEntry) -> list[str]:
        """Branches with their own graph: the indexer's record, else TokenSave's branch-meta.json."""
        st = self.state(repo)
        if st:
            return [b for b in st.branches if b != st.default_branch]
        meta = self.path(repo) / ".tokensave" / "branch-meta.json"
        if meta.exists():
            try:
                data = json.loads(meta.read_text())
                return [b for b in data.get("branches", {}) if b != data.get("default_branch")]
            except (OSError, ValueError):
                pass
        return []

    def resolve_branch(self, repo: RepoEntry, branch: str | None) -> str | None:
        """None when the request means the default branch (no selector needed), the branch when it is tracked,
        ValueError otherwise."""
        if branch is None or branch == "":
            return None
        default = self.default_branch(repo)
        if branch == default:
            return None
        tracked = self.tracked_branches(repo)
        if branch in tracked:
            return branch
        raise ValueError(f"branch '{branch}' is not indexed for {repo.name}; available: "
                         f"{[b for b in ([default] if default else []) + tracked]}")

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for r in self.repos:
            st = self.state(r)
            out.append({"name": r.name, "url": r.url, "dir": r.dir, "configured_branches": r.branches,
                        "default_branch": st.default_branch if st else self.default_branch(r),
                        "indexed": st.branches if st else {}, "present": (self.path(r) / ".git").exists()})
        return out
