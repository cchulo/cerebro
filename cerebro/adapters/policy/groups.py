"""policy.type groups: the v1 mapping, ported. IdP groups decide scopes, repos and memory banks.

    groups   = principal.groups | policy.always_groups
    scopes   = every scope whose `groups:` intersects them (config order)
    repos    = the repos of those scopes, with the branches declared in config
    banks    = personal `user-<bank_slug>` (users only; services have none)
             + `team-<group>` for each group outside always_groups, when policy.team_banks_from_groups

Token scopes pass through untouched: what the *token* may do is the identity provider's statement, not the
policy's. Tools check both (Grants.check_token_scope, then check_scope / check_bank).
"""
from __future__ import annotations
from cerebro.core import Principal, Grants
from cerebro.core.contracts import AccessPolicy
from cerebro.core.principal import RepoGrant


class Adapter(AccessPolicy):
    name = "groups"

    def grants(self, principal: Principal) -> Grants:
        config = self.ctx.config
        policy = config.policy
        always = set(policy.always_groups)
        groups = set(principal.groups) | always
        scopes = [name for name, sc in config.scopes.items() if groups & set(sc.groups)]
        repos = [RepoGrant(url=r.url, scope=s, branches=list(r.branches)) for s in scopes for r in config.scopes[s].code.repos]
        personal = f"user-{principal.bank_slug}" if principal.kind == "user" else None
        team = [f"team-{g}" for g in sorted(groups - always)] if policy.team_banks_from_groups else []
        banks = ([personal] if personal else []) + team
        return Grants(subject=principal.subject, scopes=scopes, repos=repos, banks=banks, personal_bank=personal,
                      team_banks=team, token_scopes=principal.token_scopes)
