"""provisioning.target: kubernetes -- plain Deployments / StatefulSets / PVCs / Services / CronJobs, created through
the API and idled by the kopf operator (cerebro.provision.operator).

    render()   -> deploy/generated/k8s/: namespace.yaml, one <unit>.yaml / <job>.yaml per spec, cerebro.yaml and
                  plugins/ (copied so kustomize can build ConfigMaps from them) and kustomization.yaml
    ensure()   -> create the unit's objects if missing, scale its workload to 1, wait for readiness
    release()  -> scale to 0          status() -> Deployment/StatefulSet status + cerebro.io/last-used
    run_job()  -> create a Job from the job's CronJob template (like `kubectl create job --from=cronjob/<job>`)
    touch()    -> patch the cerebro.io/last-used annotation (throttled); the operator compares it with idle-ttl
    endpoint() -> http://<unit>.<namespace>.svc:<port>

Secrets: never inline. kustomization.yaml declares `secretGenerator: cerebro-secrets` from `secrets.env` (the CLI
copies your env file next to the kustomization; it is gitignored). A unit's `secret_env` names become env entries
with `valueFrom.secretKeyRef` and `${NAME}` inside other env values becomes `$(NAME)`, which the kubelet expands from
those entries. Every name a unit references must be a key in secrets.env or the pod does not start.

Storage: every VolumeSpec is a PersistentVolumeClaim named `<owner>-<volume>` (see cerebro.provision.plan); a job that
shares a unit's volume mounts the same claim. Claims are ReadWriteOnce unless options.shared_access_mode says
otherwise, so a job and its unit must land on the same node (single-node clusters, or use ReadWriteMany storage).

Jobs without a schedule are rendered as *suspended* CronJobs so `run_job()` has a template to instantiate.

Options: output_dir (deploy/generated), env_file (secrets.env), config_path, context (kubeconfig context),
ready_timeout (600 s), shared_access_mode (ReadWriteOnce), touch_interval (60 s).
"""
from __future__ import annotations
import asyncio, logging, os, pathlib, re, time
from datetime import datetime, timezone
from typing import Any
import yaml
from ...core.contracts.provision import Endpoint, JobSpec, Provisioner, UnitRef, UnitSpec, UnitStatus
from ...provision.common import (IDLE_TTL_ANNOTATION, LAST_USED_ANNOTATION, PORT_LABEL, labels_for, parse_ttl,
                                 resources_for, volume_key)
from ...provision.plan import CONFIG_MOUNT, PLUGINS_MOUNT

log = logging.getLogger("cerebro.provision.kubernetes")
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECRET_NAME, CONFIG_CM, PLUGINS_CM = "cerebro-secrets", "cerebro-config", "cerebro-plugins"
DEFAULT_PORT = 8080
_KIND_ORDER = {"Namespace": 0, "PersistentVolumeClaim": 1, "ConfigMap": 2, "Service": 3, "Deployment": 4,
               "StatefulSet": 4, "CronJob": 5}


def k8s_value(value: str) -> str:
    """`${NAME}` -> `$(NAME)` (kubelet expansion from the secretKeyRef env entries); other `$(` stay literal."""
    out = _REF.sub(lambda m: "\0" + m.group(1) + "\1", value)
    return out.replace("$(", "$$(").replace("\0", "$(").replace("\1", ")")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Adapter(Provisioner):
    name = "kubernetes"

    def __init__(self, options=None, ctx=None):
        super().__init__(options, ctx)
        cfg = ctx.config if ctx else None
        prov = cfg.provisioning if cfg else None
        self.project: str = self.option("project") or (prov.project if prov else "cerebro")
        self.namespace: str = self.option("namespace") or (prov.namespace if prov else "cerebro")
        self.storage_class: str | None = self.option("storage_class") or (prov.storage_class if prov else None)
        self.image_registry: str | None = prov.image_registry if prov else None
        self.output_dir = pathlib.Path(self.option("output_dir", "deploy/generated")) / "k8s"
        self.env_file: str = self.option("env_file", "secrets.env")
        self.config_path: str = self.option("config_path") or os.environ.get("CEREBRO_CONFIG", "cerebro.yaml")
        self.plugins_dir: str = cfg.gateway.plugins_dir if cfg else "plugins"
        self.ports: dict[str, int] = {}
        self.unit_names: set[str] = set()
        self._touched: dict[str, float] = {}
        self._api: Any = None
        self._ports_loaded = False

    # ------------------------------------------------------------------------------------------ Locator
    def endpoint(self, unit: str) -> str:
        if unit not in self.ports and not self._ports_loaded:
            self._load_ports()
        return f"http://{unit}.{self.namespace}.svc:{self.ports.get(unit, DEFAULT_PORT)}"

    def _load_ports(self) -> None:
        self._ports_loaded = True
        for f in self.output_dir.glob("*.yaml") if self.output_dir.is_dir() else []:
            try:
                for doc in yaml.safe_load_all(f.read_text()):
                    if doc and doc.get("kind") == "Service":
                        port = (doc["metadata"].get("labels") or {}).get(PORT_LABEL)
                        if port:
                            self.ports.setdefault(doc["metadata"]["name"], int(port))
            except (OSError, yaml.YAMLError):
                continue

    # ------------------------------------------------------------------------------------------ render
    def render(self, units: list[UnitSpec], jobs: list[JobSpec]) -> dict[str, str]:
        self.unit_names = {u.name for u in units}
        files: dict[str, str] = {}
        header = "# GENERATED by `cerebro provision render` (target kubernetes) from cerebro.yaml -- do not edit.\n"
        files[str(self.output_dir / "namespace.yaml")] = header + _dump([self.namespace_object()])
        resources = ["namespace.yaml"]
        for u in units:
            self.ports[u.name] = u.http_port
            files[str(self.output_dir / f"{u.name}.yaml")] = header + _dump(self.unit_objects(u))
            resources.append(f"{u.name}.yaml")
        for j in jobs:
            files[str(self.output_dir / f"{j.name}.yaml")] = header + _dump(self.job_objects(j))
            resources.append(f"{j.name}.yaml")
        files[str(self.output_dir / "cerebro.yaml")] = self._config_text()
        plugin_files = []
        pdir = pathlib.Path(self.plugins_dir)
        for f in sorted(pdir.glob("*.py")) if pdir.is_dir() else []:
            if not f.name.startswith("_"):
                files[str(self.output_dir / "plugins" / f.name)] = f.read_text()
                plugin_files.append(f"plugins/{f.name}")
        kust = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "namespace": self.namespace,
            "resources": resources,
            "secretGenerator": [{"name": SECRET_NAME, "envs": [pathlib.Path(self.env_file).name]}],
            "configMapGenerator": [{"name": CONFIG_CM, "files": ["cerebro.yaml"]},
                                   {"name": PLUGINS_CM, "files": plugin_files} if plugin_files
                                   else {"name": PLUGINS_CM, "literals": ["README.txt=no plugins were found at render time"]}],
            "generatorOptions": {"disableNameSuffixHash": True, "labels": {"cerebro.io/project": self.project}},
        }
        files[str(self.output_dir / "kustomization.yaml")] = (
            header + f"# Secret {SECRET_NAME} is built from {pathlib.Path(self.env_file).name} in this directory at apply "
            "time (copied here by `cerebro provision up`; never committed). Values are never rendered.\n"
            + yaml.safe_dump(kust, sort_keys=False))
        return files

    def _config_text(self) -> str:
        p = pathlib.Path(self.config_path)
        if p.is_file():
            return p.read_text()
        cfg = self.ctx.config if self.ctx else None
        return yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False) if cfg else "version: 2\n"

    def _image(self, spec: UnitSpec | JobSpec) -> str:
        return f"{self.image_registry.rstrip('/')}/{spec.image}" if (spec.build and self.image_registry) else spec.image

    def namespace_object(self) -> dict:
        return {"apiVersion": "v1", "kind": "Namespace",
                "metadata": {"name": self.namespace, "labels": {"cerebro.io/project": self.project}}}

    def _meta(self, name: str, labels: dict[str, str], annotations: dict[str, str] | None = None) -> dict:
        meta = {"name": name, "namespace": self.namespace, "labels": labels}
        if annotations:
            meta["annotations"] = annotations
        return meta

    def _env(self, spec: UnitSpec | JobSpec) -> list[dict]:
        env = [{"name": n, "valueFrom": {"secretKeyRef": {"name": SECRET_NAME, "key": n}}} for n in spec.secret_env]
        # `KEY: ${KEY}` is already covered by the secretKeyRef entry above; anything else is a literal or an expansion
        env += [{"name": k, "value": k8s_value(str(v))} for k, v in spec.env.items()
                if not (k in spec.secret_env and v == "${" + k + "}")]
        return env

    def _pvcs(self, spec: UnitSpec | JobSpec, labels: dict[str, str]) -> list[dict]:
        out = []
        for v in spec.volumes:
            key = volume_key(spec, v, self.unit_names)
            if key != f"{spec.name}-{v.name}":
                continue        # owned by another unit; that unit's file declares the claim
            mode = self.option("shared_access_mode", "ReadWriteOnce") if v.shared_with else "ReadWriteOnce"
            pvc_spec: dict = {"accessModes": [mode], "resources": {"requests": {"storage": v.size}}}
            if self.storage_class:
                pvc_spec["storageClassName"] = self.storage_class
            out.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                        "metadata": self._meta(key, {k: val for k, val in labels.items() if k != PORT_LABEL}), "spec": pvc_spec})
        return out

    def _files_configmap(self, spec: UnitSpec | JobSpec, labels: dict[str, str]) -> tuple[dict | None, list[dict], list[dict]]:
        """(ConfigMap, volumeMounts, volumes) for spec.files."""
        if not spec.files:
            return None, [], []
        data, mounts = {}, []
        for path, content in spec.files.items():
            key = re.sub(r"[^A-Za-z0-9._-]", "-", pathlib.Path(path).name)
            data[key] = content
            mounts.append({"name": "files", "mountPath": path, "subPath": key, "readOnly": True})
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": self._meta(f"{spec.name}-files", labels), "data": data}
        return cm, mounts, [{"name": "files", "configMap": {"name": f"{spec.name}-files"}}]

    def _pod_spec(self, spec: UnitSpec | JobSpec, labels: dict[str, str]) -> tuple[dict, list[dict]]:
        """(podSpec, extra objects such as the files ConfigMap)."""
        container: dict = {"name": spec.name, "image": self._image(spec)}
        if spec.build:
            container["imagePullPolicy"] = "IfNotPresent"
        if spec.command:
            container["command"] = list(spec.command)
        if spec.args:
            container["args"] = list(spec.args)
        env = self._env(spec)
        if env:
            container["env"] = env
        mounts, volumes, extra = [], [], []
        for v in spec.volumes:
            key = volume_key(spec, v, self.unit_names)
            mounts.append({"name": v.name, "mountPath": v.mount_path, **({"readOnly": True} if v.read_only else {})})
            volumes.append({"name": v.name, "persistentVolumeClaim": {"claimName": key}})
        cm, fm, fv = self._files_configmap(spec, labels)
        if cm:
            extra.append(cm); mounts += fm; volumes += fv
        if isinstance(spec, UnitSpec):
            container["ports"] = [{"name": p.name, "containerPort": p.port, "protocol": p.protocol} for p in spec.ports]
            if spec.role in ("gateway", "ingest"):
                mounts.append({"name": "config", "mountPath": str(pathlib.Path(CONFIG_MOUNT).parent), "readOnly": True})
                mounts.append({"name": "plugins", "mountPath": PLUGINS_MOUNT, "readOnly": True})
                volumes.append({"name": "config", "configMap": {"name": CONFIG_CM}})
                volumes.append({"name": "plugins", "configMap": {"name": PLUGINS_CM}})
            probe = self._probe(spec)
            if probe:
                container["readinessProbe"] = probe
            res = resources_for(spec)
            if res:
                container["resources"] = res
        if mounts:
            container["volumeMounts"] = mounts
        pod: dict = {"containers": [container], "automountServiceAccountToken": False}
        if volumes:
            pod["volumes"] = volumes
        return pod, extra

    @staticmethod
    def _probe(spec: UnitSpec) -> dict | None:
        if spec.health_cmd:
            probe: dict = {"exec": {"command": list(spec.health_cmd)}}
        elif spec.health_path:
            probe = {"httpGet": {"path": spec.health_path, "port": spec.http_port}}
        else:
            return None
        return {**probe, "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 30}

    def unit_objects(self, spec: UnitSpec) -> list[dict]:
        labels = labels_for(spec, self.project)
        pod_labels = {"app": spec.name, **{k: v for k, v in labels.items() if k != PORT_LABEL}}
        pod, extra = self._pod_spec(spec, pod_labels)
        objs = [*self._pvcs(spec, pod_labels), *extra]
        objs.append({"apiVersion": "v1", "kind": "Service", "metadata": self._meta(spec.name, labels),
                     "spec": {"type": "ClusterIP", "selector": {"app": spec.name},
                              "ports": [{"name": p.name, "port": p.port, "targetPort": p.port, "protocol": p.protocol} for p in spec.ports]}})
        annotations = {}
        if spec.idle_ttl:
            parse_ttl(spec.idle_ttl)                       # fail at render time on a bad value
            annotations[IDLE_TTL_ANNOTATION] = spec.idle_ttl     # last-used is stamped by ensure()/touch(), not here
        template = {"metadata": {"labels": dict(pod_labels)}, "spec": pod}
        if spec.stateful:
            objs.append({"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": self._meta(spec.name, dict(pod_labels), annotations),
                         "spec": {"serviceName": spec.name, "replicas": 1, "selector": {"matchLabels": {"app": spec.name}},
                                  "template": template}})
        else:
            objs.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": self._meta(spec.name, dict(pod_labels), annotations),
                         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": spec.name}},
                                  "template": template}})
        return objs

    def job_objects(self, spec: JobSpec) -> list[dict]:
        labels = labels_for(spec, self.project)
        pod_labels = {"app": spec.name, **labels}
        pod, extra = self._pod_spec(spec, pod_labels)
        pod["restartPolicy"] = "Never"
        objs = [*self._pvcs(spec, pod_labels), *extra]
        objs.append({"apiVersion": "batch/v1", "kind": "CronJob", "metadata": self._meta(spec.name, labels),
                     "spec": {"schedule": spec.schedule or "0 0 1 1 *", "suspend": spec.schedule is None,
                              "concurrencyPolicy": "Forbid", "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 2,
                              "jobTemplate": {"spec": {"backoffLimit": 1,
                                                       "template": {"metadata": {"labels": dict(pod_labels)}, "spec": pod}}}}})
        return objs

    # ------------------------------------------------------------------------------------------ runtime
    def api(self):
        """The `kubernetes` client, in-cluster or from kubeconfig (options.context)."""
        if self._api is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config(context=self.option("context"))
            self._api = client
        return self._api

    def _workload_kind(self, spec_or_name: UnitSpec | str) -> str:
        if isinstance(spec_or_name, UnitSpec):
            return "StatefulSet" if spec_or_name.stateful else "Deployment"
        return "Deployment"

    def _read_workload(self, name: str, kind: str):
        from kubernetes.client.exceptions import ApiException
        apps = self.api().AppsV1Api()
        try:
            return apps.read_namespaced_stateful_set(name, self.namespace) if kind == "StatefulSet" \
                else apps.read_namespaced_deployment(name, self.namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _scale(self, name: str, kind: str, replicas: int) -> None:
        apps = self.api().AppsV1Api()
        body = {"spec": {"replicas": replicas}, "metadata": {"annotations": {LAST_USED_ANNOTATION: _now()}}}
        if kind == "StatefulSet":
            apps.patch_namespaced_stateful_set(name, self.namespace, body)
        else:
            apps.patch_namespaced_deployment(name, self.namespace, body)

    def _create_missing(self, objects: list[dict]) -> None:
        from kubernetes import utils
        from kubernetes.client.exceptions import ApiException
        core = self.api().CoreV1Api()
        try:
            core.read_namespace(self.namespace)
        except ApiException as e:
            if e.status != 404:
                raise
            core.create_namespace(self.namespace_object())
        client = self.api().ApiClient()
        for obj in sorted(objects, key=lambda o: _KIND_ORDER.get(o["kind"], 9)):
            try:
                utils.create_from_dict(client, obj, namespace=self.namespace)
            except utils.FailToCreateError as e:
                if not all(getattr(x, "status", None) == 409 for x in e.api_exceptions):
                    raise

    def _ensure_sync(self, spec: UnitSpec) -> bool:
        kind = self._workload_kind(spec)
        if self._read_workload(spec.name, kind) is None:
            self.unit_names.add(spec.name)
            self._create_missing(self.unit_objects(spec))
        self._scale(spec.name, kind, 1)
        deadline = time.monotonic() + float(self.option("ready_timeout", 600))
        while True:
            w = self._read_workload(spec.name, kind)
            if w is not None and (w.status.ready_replicas or 0) >= 1:
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(3)

    async def ensure(self, spec: UnitSpec) -> Endpoint:
        self.ports[spec.name] = spec.http_port
        ready = await asyncio.to_thread(self._ensure_sync, spec)
        self._touched[spec.name] = time.monotonic()
        return Endpoint(url=self.endpoint(spec.name), ready=ready)

    async def release(self, ref: UnitRef) -> None:
        def go():
            for kind in ("Deployment", "StatefulSet"):
                if self._read_workload(ref.name, kind) is not None:
                    self._scale(ref.name, kind, 0)
                    return
        await asyncio.to_thread(go)

    async def status(self, ref: UnitRef) -> UnitStatus:
        def go() -> UnitStatus:
            for kind in ("Deployment", "StatefulSet"):
                w = self._read_workload(ref.name, kind)
                if w is None:
                    continue
                ann = w.metadata.annotations or {}
                last = ann.get(LAST_USED_ANNOTATION)
                return UnitStatus(name=ref.name, exists=True, ready=(w.status.ready_replicas or 0) >= 1,
                                  replicas=w.spec.replicas or 0,
                                  last_used=datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None,
                                  message=f"{kind} {w.status.ready_replicas or 0}/{w.spec.replicas or 0} ready")
            return UnitStatus(name=ref.name, message="not created")
        return await asyncio.to_thread(go)

    async def run_job(self, job: JobSpec, *, wait: bool = False) -> str:
        def go() -> str:
            from kubernetes.client.exceptions import ApiException
            batch = self.api().BatchV1Api()
            try:
                cj = batch.read_namespaced_cron_job(job.name, self.namespace)
            except ApiException as e:
                if e.status != 404:
                    raise
                self._create_missing(self.job_objects(job))
                cj = batch.read_namespaced_cron_job(job.name, self.namespace)
            name = f"{job.name}-{int(time.time())}"
            body = self.api().V1Job(metadata=self.api().V1ObjectMeta(name=name, labels=cj.metadata.labels),
                                    spec=cj.spec.job_template.spec)
            batch.create_namespaced_job(self.namespace, body)
            if wait:
                deadline = time.monotonic() + float(self.option("job_timeout", 3600))
                while time.monotonic() < deadline:
                    j = batch.read_namespaced_job(name, self.namespace)
                    if j.status.succeeded:
                        return name
                    if j.status.failed:
                        raise RuntimeError(f"job {name} failed")
                    time.sleep(5)
                raise TimeoutError(f"job {name} still running")
            return name
        return await asyncio.to_thread(go)

    async def down(self, volumes: bool = False) -> None:
        """Delete every object with the project label; with volumes also the claims and the namespace."""
        def go():
            from kubernetes.client.exceptions import ApiException
            sel = f"cerebro.io/project={self.project}"
            apps, core, batch = self.api().AppsV1Api(), self.api().CoreV1Api(), self.api().BatchV1Api()
            ns = self.namespace
            for fn in (apps.delete_collection_namespaced_deployment, apps.delete_collection_namespaced_stateful_set,
                       batch.delete_collection_namespaced_cron_job, batch.delete_collection_namespaced_job,
                       core.delete_collection_namespaced_config_map, core.delete_collection_namespaced_secret):
                fn(ns, label_selector=sel)
            for svc in core.list_namespaced_service(ns, label_selector=sel).items:
                core.delete_namespaced_service(svc.metadata.name, ns)
            if volumes:
                core.delete_collection_namespaced_persistent_volume_claim(ns, label_selector=sel)
                try:
                    core.delete_namespace(ns)
                except ApiException as e:
                    if e.status != 404:
                        raise
        await asyncio.to_thread(go)

    def touch(self, ref: UnitRef) -> None:
        now = time.monotonic()
        if now - self._touched.get(ref.name, 0) < float(self.option("touch_interval", 60)):
            return
        self._touched[ref.name] = now
        try:
            body = {"metadata": {"annotations": {LAST_USED_ANNOTATION: _now()}}}
            self.api().AppsV1Api().patch_namespaced_deployment(ref.name, self.namespace, body)
        except Exception as e:     # noqa: BLE001 - touch must never break a request
            log.debug("touch %s: %s", ref.name, e)


def _dump(objects: list[dict]) -> str:
    return "---\n".join(yaml.safe_dump(o, sort_keys=False, width=4096) for o in objects)
