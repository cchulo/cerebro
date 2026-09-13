"""A stdio MCP engine as the bridge sees it: how to start it, which tools to expose, how to point a call at a repo.

Everything engine-specific lives in one small class so a different stdio engine can be dropped in with a subclass
(or `--engine module:Class`). The bridge itself only knows: spawn `command()`, list tools, keep those `exposes()`
accepts, rewrite their arguments with `prepare()`.

TokenSave facts this module relies on (verified 2026-09-13 against tokensave 7.12.1 with the real binary; sources:
https://github.com/aovestdipaperino/tokensave/blob/main/README.md,
https://github.com/aovestdipaperino/tokensave/blob/main/docs/BRANCHING-USER-GUIDE.md,
https://github.com/aovestdipaperino/tokensave/blob/main/docs/USER-GUIDE.md):

  * `tokensave serve [-p PATH] [--idle-timeout-secs N]` is MCP over stdio; the served project is fixed at start.
  * 84 tools, 74 annotated readOnlyHint=true. Non-read-only (never exposed): tokensave_str_replace,
    tokensave_multi_str_replace, tokensave_insert_at, tokensave_replace_symbol, tokensave_insert_at_symbol,
    tokensave_session_start, tokensave_session_end, tokensave_run_affected_tests, tokensave_record_decision,
    tokensave_record_code_area. (`tokensave_ast_grep_rewrite` only appears when ast-grep is on PATH; it is not.)
  * 53 read-only tools take the selectors `graph_root` (absolute root of another initialised project) and
    `graph_branch` (a tracked branch of it). The 21 that do not are the VCS tools (diff, log, blame, changelog,
    commit/pr/diff_context, affected, simplify_scan, port_*), the three tokensave_branch_* tools, diagnostics,
    runtime, dependencies, redundancy, config and tokensave_session_recall (memory). They only ever answer about
    the served project, which under cerebro's layout is empty (cerebro.bridge.workspace), so they are not exposed.
  * Errors for a bad selector come back as JSON-RPC errors (McpError), e.g. "graph_root '...' could not be
    canonicalized" and "branch 'x' is not tracked; run `tokensave branch add 'x'`".
  * Network: `sync`/`status` upload a token count unless `upload_enabled = false` in ~/.tokensave/config.toml; a
    GitHub version check runs unless TOKENSAVE_UPDATE_CHECK=off. Both are switched off here (org-control rule).
  * There is no daemon or shared server mode: the "daemon mode" spec (docs/superpowers/specs/2026-03-27-daemon-mode-
    design.md) describes a file watcher, and USER-GUIDE records that daemon mode was removed in 6.0.0 and the
    embedded watcher in 6.1.1. One `tokensave serve` per bridge process is the only option.
"""
from __future__ import annotations
import copy, importlib, os, pathlib, shutil, subprocess
from typing import Any
from mcp import types as mt
from .workspace import Workspace

REPO_ARG, BRANCH_ARG = "repo", "branch"
REPO_SCHEMA = {"type": "string", "description": "Repository of this unit (github.com/org/x or its workspace directory). "
                                                "Omit to ask every repository of the unit."}
BRANCH_SCHEMA = {"type": "string", "description": "Tracked branch to answer from; default: the repository's default branch."}


class StdioEngine:
    """Base: a read-only-tool proxy for any stdio MCP server that addresses projects by an absolute root."""
    name: str = "engine"
    binary: str = ""
    root_param: str = "graph_root"                  # engine argument naming the project root
    branch_param: str | None = "graph_branch"       # engine argument naming a tracked branch (None: no branches)
    denied: frozenset[str] = frozenset()            # exposed=False even if read-only and selector-capable
    denied_prefixes: tuple[str, ...] = ()
    index_marker: str | None = None                # file/dir inside a repo that says "indexed" (None: any checkout)

    def __init__(self, workspace: Workspace, binary: str | None = None):
        self.workspace = workspace
        self.binary_path = binary or os.environ.get(f"{self.name.upper()}_BIN") or shutil.which(self.binary) or self.binary

    # ---- process
    def command(self) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return dict(os.environ)

    def cwd(self) -> pathlib.Path:
        return self.workspace.served_root

    def version(self) -> str | None:
        try:
            out = subprocess.run([self.binary_path, "--version"], capture_output=True, text=True, timeout=20)
            return (out.stdout or out.stderr).strip().split()[-1] if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, IndexError):
            return None

    def ensure_ready(self) -> None:
        """Called once before the engine is spawned (create the served root, initialise it, ...)."""
        self.workspace.served_root.mkdir(parents=True, exist_ok=True)

    # ---- tools
    def exposes(self, tool: mt.Tool) -> bool:
        ro = bool(tool.annotations and tool.annotations.readOnlyHint)
        props = (tool.inputSchema or {}).get("properties", {}) or {}
        if not ro or tool.name in self.denied or tool.name.startswith(self.denied_prefixes):
            return False
        return self.root_param in props

    def public_schema(self, tool: mt.Tool) -> dict[str, Any]:
        """The engine's schema with our repo/branch arguments in place of its selectors."""
        schema = copy.deepcopy(tool.inputSchema or {"type": "object", "properties": {}})
        props = schema.setdefault("properties", {})
        for p in (self.root_param, self.branch_param):
            props.pop(p, None)
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in (self.root_param, self.branch_param)]
        props[REPO_ARG] = REPO_SCHEMA
        if self.branch_param:
            props[BRANCH_ARG] = BRANCH_SCHEMA
        return schema

    def prepare(self, args: dict[str, Any], root: pathlib.Path, branch: str | None) -> dict[str, Any]:
        """Strip caller-supplied selectors (a caller must never pick an arbitrary path) and inject ours."""
        out = {k: v for k, v in args.items() if k not in (REPO_ARG, BRANCH_ARG, self.root_param, self.branch_param)}
        out[self.root_param] = str(root)
        if branch and self.branch_param:
            out[self.branch_param] = branch
        return out

    def indexed(self, repo_path: pathlib.Path) -> bool:
        return (repo_path / self.index_marker).exists() if self.index_marker else (repo_path / ".git").exists()

    def health(self) -> dict[str, Any]:
        return {"binary": self.binary_path}


class TokenSaveEngine(StdioEngine):
    name = "tokensave"
    binary = "tokensave"
    denied = frozenset({"tokensave_session_recall", "tokensave_redundancy", "tokensave_config"})
    denied_prefixes = ("tokensave_session_", "tokensave_record_")
    index_marker = ".tokensave"

    def command(self) -> list[str]:
        return [self.binary_path, "serve", "-p", str(self.workspace.served_root)]

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("TOKENSAVE_UPDATE_CHECK", "off")
        env.setdefault("TOKENSAVE_REPORT_SAVINGS", "off")
        return env

    def ensure_ready(self) -> None:
        """Make the served root an initialised, empty project; also silence TokenSave's two network calls."""
        root = self.workspace.served_root
        root.mkdir(parents=True, exist_ok=True)
        (root / ".gitignore").write_text("*\n!.gitignore\n")     # nothing in here is ever code
        disable_uploads()
        if not (root / ".tokensave").exists():
            subprocess.run([self.binary_path, "init", "--no-git-hook", str(root)], check=True, env=self.env(),
                           capture_output=True, text=True, timeout=120)


def disable_uploads() -> None:
    """upload_enabled=false in ~/.tokensave/config.toml (TokenSave's own opt-out) before anything runs."""
    home = pathlib.Path(os.environ.get("HOME", "~")).expanduser()
    try:
        d = home / ".tokensave"
        d.mkdir(parents=True, exist_ok=True)
        cfg = d / "config.toml"
        text = cfg.read_text() if cfg.exists() else ""
        if "upload_enabled" not in text:
            cfg.write_text(text + ("\n" if text and not text.endswith("\n") else "") + "upload_enabled = false\n")
    except OSError:
        pass


ENGINES: dict[str, str] = {"tokensave": "cerebro.bridge.engine:TokenSaveEngine"}


def load_engine(spec: str, workspace: Workspace, binary: str | None = None) -> StdioEngine:
    """'tokensave' or 'module:Class'."""
    target = ENGINES.get(spec, spec)
    if ":" not in target:
        raise LookupError(f"unknown engine '{spec}'; known: {sorted(ENGINES)} or module:Class")
    mod, cls = target.split(":", 1)
    engine_cls = getattr(importlib.import_module(mod), cls)
    return engine_cls(workspace, binary=binary)
