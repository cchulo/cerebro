"""Confluence: ingest (REST v1) + live fallback (through mcp-atlassian, or REST) + the mcp-atlassian upstream image.

Scope config:   docs: { confluence: { spaces: [ENG, DOCS] } }        one entry drives ingest AND fallback
Env:            CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN     (Server/DC: CONFLUENCE_PERSONAL_TOKEN for mcp-atlassian)
live overrides: live: { confluence: { enabled: true, via: mcp | rest, url: ..., auth_env: ..., fallback: true } }  (all optional)
Webhook filter: {"space": "ENG"}

Pages with page-level read restrictions are never indexed nor served: the space's scope is not a valid ACL for them.
"""
import asyncio, json
import httpx
from stack_plugins import Plugin, McpUpstream, Source, LiveSource, Document, ScopeContext, html_to_text

EXPAND = "space,version,restrictions.read.restrictions.user,restrictions.read.restrictions.group"


def _restricted(p: dict) -> bool | None:
    """True/False when restriction data is present, None when the payload cannot tell."""
    read = (p.get("restrictions") or {}).get("read", {}).get("restrictions")
    if read is None:
        return None
    return bool(read.get("user", {}).get("results") or read.get("group", {}).get("results"))


def _space_key(p: dict) -> str | None:
    sp = p.get("space")
    if isinstance(sp, dict):
        return sp.get("key")
    return sp if isinstance(sp, str) else p.get("space_key") or p.get("spaceKey")


class _Confluence:
    """Shared credentials/clients for both parts."""
    def _init_creds(self):
        self.rest_url = (self.option("rest_url") or self.env("CONFLUENCE_URL")).rstrip("/")
        self.user, self.token = self.env("CONFLUENCE_USER"), self.env("CONFLUENCE_TOKEN")

    def _rest(self, timeout=60) -> httpx.Client:
        return httpx.Client(base_url=self.rest_url, auth=(self.user, self.token), timeout=timeout)

    def _arest(self, timeout=30) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.rest_url, auth=(self.user, self.token), timeout=timeout)


# ============================================================================================= ingest part
class ConfluenceSource(_Confluence, Source):
    def __init__(self, options=None):
        super().__init__(options); self._init_creds()

    def configured(self) -> bool:
        return bool(self.rest_url and self.token)

    def _pages(self, c: httpx.Client, space: str):
        start = 0
        while True:
            r = c.get("/rest/api/content", params={
                "spaceKey": space, "type": "page", "status": "current",
                "expand": "body.storage,version,ancestors,restrictions.read.restrictions.user,restrictions.read.restrictions.group",
                "limit": 50, "start": start})
            r.raise_for_status()
            data = r.json()
            yield from data.get("results", [])
            if "next" not in data.get("_links", {}):
                return
            start += data.get("limit", 50)

    def documents(self, ctx: ScopeContext, filter: dict | None = None):
        spaces = ctx.config.get("spaces", [])
        if filter and filter.get("space"):
            spaces = [s for s in spaces if s == filter["space"]]
        with self._rest() as c:
            for space in spaces:
                for p in self._pages(c, space):
                    if _restricted(p):
                        continue
                    crumbs = " / ".join(a["title"] for a in p.get("ancestors", []))
                    header = (f"Space: {space}\nPath: {crumbs + ' / ' if crumbs else ''}{p['title']}\n"
                              f"URL: {self.rest_url}{p.get('_links', {}).get('webui', '')}\n\n")
                    yield Document(key=f"{space}/{p['id']}", version=str(p["version"]["number"]),
                                   text=header + html_to_text(p["body"]["storage"]["value"]), title=p["title"])

    def covers(self, key: str, filter: dict | None) -> bool:
        return not filter or (bool(filter.get("space")) and key.startswith(filter["space"] + "/"))


# ============================================================================================= live part
class ConfluenceLive(_Confluence, LiveSource):
    """search -> CQL: type=page AND space in (<allowed spaces>) AND text ~ "<query>"; fetch -> page by id.
    via: mcp   the mcp-atlassian upstream declared below (default; tools confluence_search / confluence_get_page)
    via: rest  the REST API directly.  In MCP mode page-level restrictions are cross-checked through REST
    (mcp-atlassian returns no restriction metadata), so keep the REST credentials configured too."""
    def __init__(self, options=None):
        super().__init__(options); self._init_creds()
        self.via = self.option("via", "mcp")
        self.mcp_url = (self.option("url") or f"http://mcp-{self.name}:{MCP.port}{MCP.path}").rstrip("/") if self.via == "mcp" else ""
        self.mcp_token = self.env(self.option("auth_env")) if self.option("auth_env") else ""
        self.search_tool = self.option("search_tool", "confluence_search")
        self.search_arg, self.limit_arg = self.option("search_arg", "query"), self.option("limit_arg", "limit")
        self.spaces_arg = self.option("spaces_arg", "spaces_filter")
        self.fetch_tool, self.fetch_arg = self.option("fetch_tool", "confluence_get_page"), self.option("fetch_arg", "page_id")
        self.web_url = (self.option("web_url") or self.rest_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.mcp_url) if self.via == "mcp" else bool(self.rest_url and self.token)

    @staticmethod
    def _space_map(allowed):
        return {sp: a["scope"] for a in allowed for sp in a.get("spaces", [])}

    @staticmethod
    def _cql(query, spaces):
        q = query.replace('"', '\\"')
        return 'type=page AND space in (' + ",".join('"' + s + '"' for s in spaces) + ') AND text ~ "' + q + '"'

    # ---- REST
    async def _rest_search(self, cql, limit):
        async with self._arest() as c:
            r = await c.get("/rest/api/content/search", params={"cql": cql, "limit": limit, "expand": EXPAND})
            r.raise_for_status(); return r.json().get("results", [])

    async def _rest_fetch(self, ref):
        async with self._arest() as c:
            r = await c.get(f"/rest/api/content/{ref}", params={"expand": "body.storage," + EXPAND})
            if r.status_code == 404:
                raise KeyError(f"confluence page {ref} not found")
            r.raise_for_status(); p = r.json()
        p["_text"] = html_to_text(p.get("body", {}).get("storage", {}).get("value", "")); return p

    async def _rest_restriction(self, ref) -> bool | None:
        if not (self.token and self.rest_url):
            return None
        async with self._arest() as c:
            r = await c.get(f"/rest/api/content/{ref}", params={"expand": EXPAND})
            return _restricted(r.json()) if r.status_code == 200 else None

    # ---- MCP upstream
    async def _mcp_call(self, tool, args):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        headers = {"Authorization": f"Bearer {self.mcp_token}"} if self.mcp_token else None
        # collect inside the transport contexts, raise outside them: an exception raised inside is wrapped into an
        # ExceptionGroup by the client's task group and its message is lost
        async with streamablehttp_client(self.mcp_url, headers=headers, timeout=60, sse_read_timeout=60) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize(); res = await s.call_tool(tool, args)
                text = "".join(getattr(b, "text", "") for b in res.content); is_error = bool(res.isError)
        if is_error:
            raise RuntimeError(f"upstream {tool}: {text[:400]}")
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}

    @staticmethod
    def _as_list(data):
        if isinstance(data, list):
            return data
        for k in ("results", "pages", "items", "data"):
            if isinstance(data, dict) and isinstance(data.get(k), list):
                return data[k]
        return [data] if isinstance(data, dict) else []

    async def _mcp_search(self, cql, limit, spaces):
        args = {self.search_arg: cql, self.limit_arg: limit}
        if self.spaces_arg and spaces:
            args[self.spaces_arg] = ",".join(spaces)
        return self._as_list(await self._mcp_call(self.search_tool, args))

    async def _mcp_fetch(self, ref):
        p = await self._mcp_call(self.fetch_tool, {self.fetch_arg: ref, "include_metadata": True, "convert_to_markdown": True})
        if isinstance(p, dict) and isinstance(p.get("metadata"), dict):        # mcp-atlassian: {metadata, content}
            meta = p["metadata"]; content = p.get("content", meta.get("content"))
            body = content.get("value") if isinstance(content, dict) else content
            return {**meta, "_text": body if isinstance(body, str) else json.dumps(body)}
        if isinstance(p, dict):
            body = p.get("body")
            if isinstance(body, dict):
                body = body.get("storage", {}).get("value") or body.get("view", {}).get("value") or ""
            p["_text"] = html_to_text(body) if body and "<" in body else (body or p.get("text", ""))
        return p

    # ---- contract
    async def search(self, query, allowed, limit=10):
        spaces = self._space_map(allowed)
        if not spaces:
            return []
        cql = self._cql(query, spaces)
        raw = await (self._mcp_search(cql, limit, list(spaces)) if self.via == "mcp" else self._rest_search(cql, limit))
        if self.via == "mcp" and self.token:                       # upstream gives no restriction data: ask REST
            unknown = [p for p in raw if _restricted(p) is None]
            checks = await asyncio.gather(*(self._rest_restriction(str(p.get("id"))) for p in unknown))
            for p, restricted in zip(unknown, checks):
                p["restrictions"] = {"read": {"restrictions": {"user": {"results": ["x"] if restricted else []}, "group": {"results": []}}}}
        out = []
        for p in raw:
            sp = _space_key(p)
            if sp not in spaces or _restricted(p):
                continue
            out.append({"ref": str(p.get("id")), "title": p.get("title"), "space": sp, "scope": spaces[sp],
                        "url": p.get("url") or f"{self.web_url}{(p.get('_links') or {}).get('webui', '')}",
                        "excerpt": (p.get("excerpt") or "")[:300]})
        return out[:limit]

    async def fetch(self, ref, allowed, max_chars=20000):
        spaces = self._space_map(allowed)
        p = await (self._mcp_fetch(ref) if self.via == "mcp" else self._rest_fetch(ref))
        sp = _space_key(p)
        if sp not in spaces:
            raise PermissionError(f"confluence page {ref} is in space '{sp}', outside your scopes")
        restricted = _restricted(p)
        if restricted is None and self.via == "mcp":
            restricted = await self._rest_restriction(ref)
        if restricted:
            raise PermissionError(f"confluence page {ref} has a page-level read restriction and is not served")
        text = p.get("_text", "")
        version = p.get("version"); version = version.get("number") if isinstance(version, dict) else version
        return {"ref": ref, "title": p.get("title"), "space": sp, "scope": spaces[sp],
                "url": p.get("url") or f"{self.web_url}{(p.get('_links') or {}).get('webui', '')}",
                "version": version, "text": text[:max_chars], "truncated": len(text) > max_chars,
                "restriction_checked": restricted is not None}


# ============================================================================================= upstream image
# Declared here, at the plugin level: the generator runs it as `mcp-confluence` (compose service / k8s pod), the live
# part reaches it at http://mcp-confluence:9000/mcp. sooperset/mcp-atlassian (MIT), read-only, only the two read tools.
MCP = McpUpstream(
    image="ghcr.io/sooperset/mcp-atlassian:0.23.1",
    port=9000, path="/mcp",
    args=["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000", "--path", "/mcp", "--stateless",
          "--read-only", "--enabled-tools", "confluence_search,confluence_get_page"],
    env={"CONFLUENCE_URL": "${CONFLUENCE_URL}", "CONFLUENCE_USERNAME": "${CONFLUENCE_USER}",
         "CONFLUENCE_API_TOKEN": "${CONFLUENCE_TOKEN}", "CONFLUENCE_PERSONAL_TOKEN": "${CONFLUENCE_PERSONAL_TOKEN:-}",
         "ALLOW_GLOBAL_CRED_FALLBACK": "true",     # the gateway is the only client and sends no per-user token
         "READ_ONLY_MODE": "true"},
)

PLUGIN = Plugin(name="confluence", source=ConfluenceSource, live=ConfluenceLive, mcp=MCP,
                description="Confluence Cloud/Server pages: ingest per space, live fallback via mcp-atlassian")
