"""Confluence live source, confined to the caller's spaces. Two transports:

  live: { confluence: { via: rest } }                               REST v1 with a service account (default)
        env CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN
  live: { confluence: { via: mcp, url: http://mcp-atlassian:9000/mcp, auth_env: ATLASSIAN_MCP_TOKEN } }
        an upstream MCP server (defaults match sooperset/mcp-atlassian: confluence_search(query=<CQL>, limit),
        confluence_get_page(page_id)); tool/argument names are configurable:
        search_tool, search_arg, limit_arg, fetch_tool, fetch_arg. If CONFLUENCE_URL/USER/TOKEN are also set, page-level
        restrictions are cross-checked through REST for every search hit and fetch (MCP upstreams rarely expose them).

allowed = [{"scope": "public", "spaces": ["ENG", "DOCS"]}, ...] from each scope's docs: confluence entry.
search -> CQL: type=page AND space in (<allowed spaces>) AND text ~ "<query>"; results outside the spaces or with a
          page-level read restriction are dropped.
fetch  -> refused unless the page's space is allowed and (REST) it carries no page-level restriction.
MCP limitation: an upstream that does not return restriction metadata cannot be checked for page-level
restrictions; use REST, or an upstream authenticating as the *user* so the source enforces its own ACL.
"""
import json, re
import httpx
from markdownify import markdownify
from gateway.live import LiveSource

EXPAND = "space,version,restrictions.read.restrictions.user,restrictions.read.restrictions.group"


def _restricted(p: dict) -> bool | None:
    """True/False when restriction data is present, None when the payload cannot tell."""
    read = p.get("restrictions", {}).get("read", {}).get("restrictions")
    if read is None:
        return None
    return bool(read.get("user", {}).get("results") or read.get("group", {}).get("results"))


def _space_key(p: dict) -> str | None:
    sp = p.get("space")
    if isinstance(sp, dict):
        return sp.get("key")
    return sp if isinstance(sp, str) else p.get("space_key") or p.get("spaceKey")


class ConfluenceLive(LiveSource):
    name = "confluence"

    def __init__(self, options=None):
        super().__init__(options)
        self.via = self.option("via", "rest")
        # REST base (site URL) always comes from CONFLUENCE_URL / rest_url; in MCP mode `url` is the upstream MCP endpoint
        self.rest_url = (self.option("rest_url") or self.env("CONFLUENCE_URL")).rstrip("/")
        self.mcp_url = (self.option("url") or "").rstrip("/") if self.via == "mcp" else ""
        self.url = self.mcp_url if self.via == "mcp" else (self.option("url") or self.rest_url).rstrip("/")
        self.user, self.token = self.env("CONFLUENCE_USER"), self.env("CONFLUENCE_TOKEN")
        self.mcp_token = self.env(self.option("auth_env", "")) if self.option("auth_env") else ""
        self.search_tool = self.option("search_tool", "confluence_search")
        self.search_arg = self.option("search_arg", "query")
        self.limit_arg = self.option("limit_arg", "limit")
        self.fetch_tool = self.option("fetch_tool", "confluence_get_page")
        self.fetch_arg = self.option("fetch_arg", "page_id")
        self.web_url = (self.option("web_url") or self.rest_url or self.url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.url) and (self.via == "mcp" or bool(self.token and self.rest_url))

    @staticmethod
    def _space_map(allowed: list[dict]) -> dict[str, str]:
        return {sp: a["scope"] for a in allowed for sp in a.get("spaces", [])}

    @staticmethod
    def _cql(query: str, spaces) -> str:
        q = query.replace('"', '\\"')
        return 'type=page AND space in (' + ",".join('"' + s + '"' for s in spaces) + ') AND text ~ "' + q + '"'

    # ---------------------------------------------------------------- REST
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.rest_url or self.url, auth=(self.user, self.token), timeout=30)

    async def _rest_search(self, cql: str, limit: int) -> list[dict]:
        async with self._client() as c:
            r = await c.get("/rest/api/content/search", params={"cql": cql, "limit": limit, "expand": EXPAND})
            r.raise_for_status()
            return r.json().get("results", [])

    async def _rest_fetch(self, ref: str) -> dict:
        async with self._client() as c:
            r = await c.get(f"/rest/api/content/{ref}", params={"expand": "body.storage," + EXPAND})
            if r.status_code == 404:
                raise KeyError(f"confluence page {ref} not found")
            r.raise_for_status()
            p = r.json()
        p["_text"] = markdownify(p.get("body", {}).get("storage", {}).get("value", ""), heading_style="ATX")
        return p

    # ---------------------------------------------------------------- MCP upstream
    async def _mcp_call(self, tool: str, args: dict):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        headers = {"Authorization": f"Bearer {self.mcp_token}"} if self.mcp_token else None
        async with streamablehttp_client(self.url, headers=headers, timeout=60, sse_read_timeout=60) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, args)
                text = "".join(getattr(b, "text", "") for b in res.content)
                if res.isError:
                    raise RuntimeError(f"upstream {tool}: {text[:300]}")
                try:
                    return json.loads(text)
                except ValueError:
                    return {"text": text}

    @staticmethod
    def _as_list(data) -> list[dict]:
        if isinstance(data, list):
            return data
        for k in ("results", "pages", "items", "data"):
            if isinstance(data, dict) and isinstance(data.get(k), list):
                return data[k]
        return [data] if isinstance(data, dict) else []

    async def _mcp_search(self, cql: str, limit: int) -> list[dict]:
        return self._as_list(await self._mcp_call(self.search_tool, {self.search_arg: cql, self.limit_arg: limit}))

    async def _mcp_fetch(self, ref: str) -> dict:
        p = await self._mcp_call(self.fetch_tool, {self.fetch_arg: ref})
        if isinstance(p, dict) and isinstance(p.get("metadata"), dict):     # mcp-atlassian shape: {metadata, content}
            meta = p["metadata"]; body = p.get("content", {}).get("value") if isinstance(p.get("content"), dict) else p.get("content")
            p = {**meta, "_text": body if isinstance(body, str) else json.dumps(body)}
        elif isinstance(p, dict):
            body = p.get("body")
            if isinstance(body, dict):
                body = body.get("storage", {}).get("value") or body.get("view", {}).get("value") or ""
            p["_text"] = markdownify(body, heading_style="ATX") if body and re.search(r"<\w+", body) else (body or p.get("text", ""))
        return p

    # ---------------------------------------------------------------- contract
    async def search(self, query: str, allowed: list[dict], limit: int = 10) -> list[dict]:
        spaces = self._space_map(allowed)
        if not spaces:
            return []
        cql = self._cql(query, spaces)
        raw = await (self._mcp_search(cql, limit) if self.via == "mcp" else self._rest_search(cql, limit))
        if self.via == "mcp" and self.token:                    # upstream gave no restriction data: ask REST
            import asyncio
            checks = await asyncio.gather(*(self._rest_restriction(str(p.get("id"))) for p in raw if _restricted(p) is None))
            it = iter(checks)
            for p in raw:
                if _restricted(p) is None:
                    p["restrictions"] = {"read": {"restrictions": {"user": {"results": ["x"] if next(it) else []}, "group": {"results": []}}}}
        out = []
        for p in raw:
            sp = _space_key(p)
            if sp not in spaces or _restricted(p):          # None (unknown, no REST credentials) passes: search only
                continue
            out.append({"ref": str(p.get("id")), "title": p.get("title"), "space": sp, "scope": spaces[sp],
                        "url": p.get("url") or f"{self.web_url}{(p.get('_links') or {}).get('webui', '')}",
                        "excerpt": (p.get("excerpt") or "")[:300]})
        return out[:limit]

    async def _rest_restriction(self, ref: str) -> bool | None:
        """MCP upstreams rarely expose restrictions; when REST credentials exist, ask the REST API instead."""
        if not (self.token and self.rest_url):
            return None
        async with self._client() as c:
            r = await c.get(f"/rest/api/content/{ref}", params={"expand": EXPAND})
            if r.status_code != 200:
                return None
            return _restricted(r.json())

    async def fetch(self, ref: str, allowed: list[dict], max_chars: int = 20000) -> dict:
        spaces = self._space_map(allowed)
        p = await (self._mcp_fetch(ref) if self.via == "mcp" else self._rest_fetch(ref))
        sp = _space_key(p)
        if sp not in spaces:
            raise PermissionError(f"confluence page {ref} is in space '{sp}', outside your scopes")
        restricted = _restricted(p)
        if restricted is None and self.via == "mcp":
            restricted = await self._rest_restriction(ref)
            if restricted is not None:
                p["restrictions"] = {"read": {"restrictions": {"user": {"results": ["x"] if restricted else []}, "group": {"results": []}}}}
        if restricted:
            raise PermissionError(f"confluence page {ref} has a page-level read restriction and is not served")
        text = p.get("_text", "")
        return {"ref": ref, "title": p.get("title"), "space": sp, "scope": spaces[sp],
                "url": p.get("url") or f"{self.web_url}{(p.get('_links') or {}).get('webui', '')}",
                "version": (p.get("version") or {}).get("number") if isinstance(p.get("version"), dict) else p.get("version"),
                "text": text[:max_chars], "truncated": len(text) > max_chars,
                "restriction_checked": _restricted(p) is not None}
