# code-unit image

One image for a code unit: [TokenSave](https://github.com/aovestdipaperino/tokensave) 7.12.1 (pinned, SHA256-checked
release tarball), ripgrep, git, and the cerebro bridge (`cerebro.bridge`). It runs as the unit (bridge on port 8045)
and as the unit's index job.

```
docker build -f images/code-unit/Dockerfile -t cerebro/code-unit:2.0.0a0 .        # from the repository root
docker run --rm -e CEREBRO_UNIT=code-public \
  -e CEREBRO_REPOS='[{"url":"https://github.com/pallets/click.git","branches":[]}]' \
  -v code-public:/workspace cerebro/code-unit:2.0.0a0 cerebro index run --unit code-public
docker run --rm -p 8045:8045 -e CEREBRO_UNIT=code-public -e CEREBRO_REPOS='[...]' -v code-public:/workspace cerebro/code-unit:2.0.0a0
curl -s localhost:8045/.well-known/cerebro-capabilities
```

## Contract with the provisioner

| what | value |
|---|---|
| unit name | `<unit.name>` from `cerebro.core.code_units` (`code-<scope>` or `code-<scope>-<repo slug>`) |
| job name | `index-<unit.name>`, args `cerebro index run --unit <name>`, cron from `engines.code.options.schedule` (default `0 3 * * *`) |
| env | `CEREBRO_UNIT`, `CEREBRO_REPOS` (JSON `[{url, branches}]`), `CEREBRO_WORKSPACE=/workspace` |
| secret env | `GITHUB_TOKEN` (clone auth for github.com; the job needs it, the unit only gets it for parity) |
| volume | `workspace-<unit.name>` at `/workspace`, shared by the unit and its job, size `engines.code.resources.storage` |
| port | 8045: `/health`, `/.well-known/cerebro-capabilities`, `/mcp` |

## Workspace layout (see `cerebro/bridge/workspace.py`)

```
/workspace/.cerebro-root/          the project TokenSave SERVES: initialised, empty (see below)
/workspace/<dir>/                  one checkout per repository, default branch checked out
/workspace/<dir>/.tokensave/       its index; tracked branches under branches/<b>.db
/workspace/<dir>/.cerebro-index.json   what the indexer last did (default branch, branch -> commit)
```

`<dir>` is the last path segment of the repository name (`click`); if two repos of a unit share it, all of them use
`cerebro.core.units.repo_slug`.

## Why TokenSave serves an empty project

Verified with the real binary (7.12.1): `graph_root` may only name a project *other* than the served one, and
`graph_branch` is refused for the served project ("selecting a different branch of the served project is not
supported"). Serving `/workspace/.cerebro-root` therefore makes every repository addressable the same way,
`graph_root=/workspace/<dir>` + `graph_branch=<tracked branch>`, on all 53 read-only TokenSave tools that take
selectors, and those opens are read-only, so the index job can init/sync/branch-add while the bridge serves.
What this costs: the 21 read-only tools without selectors (`tokensave_branch_search/diff/list`, the VCS tools,
diagnostics, runtime, dependencies, redundancy, config, session_recall) would only see the empty root and are not
exposed; the unit's branches are listed by `unit_info`, per-branch graphs are reached through `branch` on every
other tool, and text search on any indexed branch is the bridge's `grep` (ripgrep on the checkout, `git grep` on
other branches).

Network: TokenSave's token-counter upload (`upload_enabled = false` in `~/.tokensave/config.toml`) and GitHub
version check (`TOKENSAVE_UPDATE_CHECK=off`) are both disabled in the image. Nothing leaves the unit.
