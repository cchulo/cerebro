"""The idle operator (kubernetes target): scale docs/code units to zero when nobody used them for idle_ttl.

    cerebro provision operator [-c cerebro.yaml]        (or: kopf run -m cerebro.provision.operator -n <namespace>)

Watches Deployments labelled `cerebro.io/role` in {docs, code} that carry the `cerebro.io/idle-ttl` annotation
(rendered from UnitSpec.idle_ttl). Every INTERVAL seconds it compares `cerebro.io/last-used` (set by the
provisioner's ensure() and touch(); the Deployment's creation time when absent) with the TTL and patches
`spec.replicas: 0` when the unit is idle. `Provisioner.ensure()` scales it back to 1 on the next use.

The decision itself is `should_scale_to_zero()`, a pure function, so it is unit-tested without a cluster.
"""
from __future__ import annotations
import logging, os
from datetime import datetime, timezone
from .common import IDLE_TTL_ANNOTATION, LAST_USED_ANNOTATION, ROLE_LABEL, parse_ttl

log = logging.getLogger("cerebro.provision.operator")
IDLE_ROLES = ("docs", "code")
INTERVAL = float(os.environ.get("CEREBRO_IDLE_INTERVAL", "60"))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def should_scale_to_zero(labels: dict | None, annotations: dict | None, replicas: int | None,
                         now: datetime, created: str | None = None) -> bool:
    labels, annotations = labels or {}, annotations or {}
    if labels.get(ROLE_LABEL) not in IDLE_ROLES or not replicas:
        return False
    try:
        ttl = parse_ttl(annotations.get(IDLE_TTL_ANNOTATION))
    except ValueError as e:
        log.warning("%s", e)
        return False
    if not ttl:
        return False
    last = _parse_time(annotations.get(LAST_USED_ANNOTATION)) or _parse_time(created)
    if last is None:
        return False
    return (now - last).total_seconds() > ttl


try:
    import kopf
except ImportError:                                   # the module stays importable without the kubernetes extra
    kopf = None

if kopf is not None:
    @kopf.on.startup()
    def _configure(settings: kopf.OperatorSettings, **_):
        settings.posting.enabled = False              # no Events spam; decisions go to the log

    @kopf.timer("apps", "v1", "deployments", interval=INTERVAL, initial_delay=INTERVAL,
                labels={ROLE_LABEL: kopf.PRESENT}, annotations={IDLE_TTL_ANNOTATION: kopf.PRESENT})
    def idle_check(spec, meta, patch, logger, **_):
        now = datetime.now(timezone.utc)
        if should_scale_to_zero(meta.get("labels"), meta.get("annotations"), spec.get("replicas"), now,
                                meta.get("creationTimestamp")):
            logger.info("%s idle for more than %s: scaling to 0", meta["name"], meta["annotations"][IDLE_TTL_ANNOTATION])
            patch.spec["replicas"] = 0


def run(namespace: str) -> int:
    """Blocking: run the operator for one namespace (in-cluster credentials or the local kubeconfig)."""
    if kopf is None:
        raise SystemExit("kopf is not installed: pip install 'cerebro[kubernetes]'")
    kopf.run(namespaces=[namespace], clusterwide=False, standalone=True)
    return 0
