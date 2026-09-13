"""Helpers both renderers share: physical volume names, labels, TTL parsing."""
from __future__ import annotations
import re
from ..core.contracts.provision import JobSpec, UnitSpec, VolumeSpec

PROJECT_LABEL, SCOPE_LABEL, ROLE_LABEL = "cerebro.io/project", "cerebro.io/scope", "cerebro.io/role"
PORT_LABEL, IDLE_TTL_ANNOTATION, LAST_USED_ANNOTATION = "cerebro.io/port", "cerebro.io/idle-ttl", "cerebro.io/last-used"
_TTL = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")


def volume_key(owner: UnitSpec | JobSpec, vol: VolumeSpec, unit_names: set[str] | None = None) -> str:
    """Physical volume name. A volume V declared on unit U is `U-V`; anything listing U in shared_with mounts the same
    one. When several candidates exist the owner is a unit rather than a job, then the alphabetically first."""
    candidates = sorted({owner.name, *vol.shared_with})
    if unit_names:
        units = [c for c in candidates if c in unit_names]
        candidates = units or candidates
    return f"{candidates[0]}-{vol.name}"


def labels_for(spec: UnitSpec | JobSpec, project: str) -> dict[str, str]:
    out = {PROJECT_LABEL: project, **spec.labels}
    if spec.scope:
        out[SCOPE_LABEL] = spec.scope
    if isinstance(spec, UnitSpec):
        out[ROLE_LABEL] = spec.role
        out[PORT_LABEL] = str(spec.http_port)
    else:
        out[ROLE_LABEL] = "job"
    return out


_LABEL_VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")


def label_value(value: str) -> str:
    """A Kubernetes-valid label value: alphanumerics, '-', '_', '.', alphanumeric at both ends, at most 63 chars."""
    v = re.sub(r"[^A-Za-z0-9_.-]", "-", str(value))[:63].strip("-_.")
    assert _LABEL_VALUE.match(v), v
    return v


def k8s_labels(labels: dict[str, str]) -> dict[str, str]:
    return {k: label_value(v) for k, v in labels.items()}


def parse_ttl(value: str | int | None) -> int | None:
    """'2h' | '30m' | '1d' | '90s' | 90 -> seconds; None/'' -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    m = _TTL.match(value)
    if not m:
        raise ValueError(f"bad idle_ttl {value!r}; use e.g. 30m, 2h, 1d")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def resources_for(spec: UnitSpec) -> dict[str, dict[str, str]]:
    """{cpu, memory} -> Kubernetes requests (and a memory limit); storage is a volume matter."""
    req = {k: v for k, v in spec.resources.items() if k in ("cpu", "memory")}
    out: dict[str, dict[str, str]] = {}
    if req:
        out["requests"] = req
    if "memory" in req:
        out["limits"] = {"memory": spec.resources.get("memory_limit", req["memory"])}
    return out
