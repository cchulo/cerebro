"""Resolve the caller's identity and allowed scopes.

Identity comes from headers set by the reverse proxy after SSO (oauth2-proxy, Authelia, Caddy OIDC, ...).
The gateway trusts these headers ONLY because the proxy is the only thing allowed to reach it
(keep port 8090 off the public network). Nothing here talks to the client's own credentials.
"""
import os, yaml
from dataclasses import dataclass, field

CFG = yaml.safe_load(open(os.environ.get("SCOPES_FILE", "/config/scopes.yaml")))
IDENT = CFG.get("identity", {})
USER_HDR = IDENT.get("user_header", "X-Forwarded-User").lower()
GROUPS_HDR = IDENT.get("groups_header", "X-Forwarded-Groups").lower()
ALWAYS = set(IDENT.get("always_groups", ["everyone"]))
TEAM_BANKS = IDENT.get("team_banks_from_groups", True)
SCOPES: dict[str, dict] = CFG["scopes"]
LIVE: dict[str, dict] = {k: (v or {}) for k, v in (CFG.get("live") or {}).items()}   # per-plugin overrides for live fallbacks

@dataclass
class Caller:
    user: str
    groups: set[str]
    scopes: list[str] = field(default_factory=list)

    @property
    def personal_bank(self) -> str:
        return "user-" + "".join(c if c.isalnum() else "-" for c in self.user.lower())

    @property
    def team_banks(self) -> list[str]:
        return [f"team-{g}" for g in sorted(self.groups - ALWAYS)] if TEAM_BANKS else []

    @property
    def repos(self) -> list[str]:
        return [r for s in self.scopes for r in scope_repos(s)]

def scope_repos(scope: str) -> list[str]:
    """A scope's code repositories: `code: { repos: [...] }` (Sourcebot, CodeGraphContext and the git docs plugin)."""
    return list((SCOPES[scope].get("code") or {}).get("repos") or [])


def caller_from_headers(headers) -> Caller:
    user = headers.get(USER_HDR)
    if not user:
        raise PermissionError(f"missing {USER_HDR} header — is the gateway behind the SSO proxy?")
    groups = {g.strip() for g in headers.get(GROUPS_HDR, "").split(",") if g.strip()} | ALWAYS
    scopes = [name for name, sc in SCOPES.items() if groups & set(sc.get("groups", []))]
    return Caller(user=user, groups=groups, scopes=scopes)

def live_allowed(caller: Caller, source: str, scopes: list[str] | None = None) -> list[dict]:
    """Per allowed scope, the caller's `docs:` config for `source` -> what a live source may touch."""
    out = []
    for s in (scopes or caller.scopes):
        check_scope(caller, s)
        cfg = (SCOPES[s].get("docs") or {}).get(source)
        if cfg is not None:
            out.append({"scope": s, **(cfg or {})})
    return out


def check_scope(caller: Caller, scope: str) -> None:
    if scope not in caller.scopes:
        raise PermissionError(f"{caller.user} is not allowed to access scope '{scope}'")
