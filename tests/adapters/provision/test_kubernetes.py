import pathlib, shutil, subprocess
import pytest, yaml
from cerebro.adapters.provision import kubernetes as k8s
from cerebro.core.contracts.provision import UnitRef, UnitSpec
from cerebro.provision.plan import plan
from tests.conftest import ROOT
from tests.contracts.provision import ProvisionerContract
from tests.provision.fakes import CANARY, canary_ctx, fake_adapters, write_plugins


class TestKubernetesContract(ProvisionerContract):
    @pytest.fixture
    def adapter(self, ctx):
        return k8s.Adapter({"output_dir": "deploy/generated"}, ctx)


@pytest.fixture
def rendered(example_config, tmp_path, monkeypatch):
    """(adapter, files, {(kind, name): object}) for the fake plan, rendered from a fake checkout at tmp_path."""
    monkeypatch.chdir(tmp_path)
    shutil.copy(ROOT / "cerebro.example.yaml", tmp_path / "cerebro.yaml")
    write_plugins(tmp_path / "plugins")
    ctx = canary_ctx(example_config)
    units, jobs = plan(example_config, ctx, adapters=fake_adapters(ctx))
    adapter = k8s.Adapter({"output_dir": "deploy/generated", "config_path": "cerebro.yaml", "storage_class": "fast"}, ctx)
    files = adapter.render(units, jobs)
    objects = {}
    for path, text in files.items():
        if path.endswith(".yaml") and pathlib.Path(path).name not in ("kustomization.yaml", "cerebro.yaml"):
            for doc in yaml.safe_load_all(text):
                if doc:
                    objects[(doc["kind"], doc["metadata"]["name"])] = doc
    return adapter, files, objects


def test_one_file_per_unit_and_job_plus_kustomization(rendered):
    adapter, files, objects = rendered
    names = sorted(str(pathlib.Path(p).relative_to("deploy/generated/k8s")) for p in files)
    for n in ("namespace.yaml", "postgres.yaml", "docs-public.yaml", "code-public.yaml", "index-code-public.yaml",
              "reindex-code-public.yaml", "mcp-fakemcp.yaml", "kustomization.yaml", "cerebro.yaml", "plugins/fakemcp.py"):
        assert n in names, n
    kust = yaml.safe_load(files[str(adapter.output_dir / "kustomization.yaml")])
    assert kust["namespace"] == "cerebro" and "docs-public.yaml" in kust["resources"] and "namespace.yaml" in kust["resources"]
    assert kust["secretGenerator"] == [{"name": "cerebro-secrets", "envs": ["secrets.env"]}]
    assert {"name": "cerebro-config", "files": ["cerebro.yaml"]} in kust["configMapGenerator"]
    assert {"name": "cerebro-plugins", "files": ["plugins/fakemcp.py"]} in kust["configMapGenerator"]
    assert kust["generatorOptions"]["disableNameSuffixHash"] is True
    assert not any(k == "Secret" for k, _ in objects), "secrets are generated from secrets.env, never rendered"
    assert files[str(adapter.output_dir / "cerebro.yaml")] == (ROOT / "cerebro.example.yaml").read_text()


def test_no_secret_value_in_any_rendered_file(rendered):
    _, files, _ = rendered
    for path, text in files.items():
        assert CANARY not in text, path


def test_deployment_or_statefulset_with_pvcs(rendered):
    _, _, objects = rendered
    pg = objects[("StatefulSet", "postgres")]
    assert pg["spec"]["serviceName"] == "postgres" and pg["spec"]["replicas"] == 1
    pvc = objects[("PersistentVolumeClaim", "postgres-data")]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "20Gi" and pvc["spec"]["storageClassName"] == "fast"
    assert pvc["metadata"]["labels"]["cerebro.io/project"] == "cerebro"
    docs = objects[("Deployment", "docs-public")]
    assert docs["spec"]["strategy"] == {"type": "Recreate"} and docs["spec"]["selector"]["matchLabels"] == {"app": "docs-public"}
    assert ("PersistentVolumeClaim", "docs-public-data") in objects
    vols = docs["spec"]["template"]["spec"]["volumes"]
    assert {"name": "data", "persistentVolumeClaim": {"claimName": "docs-public-data"}} in vols
    assert docs["metadata"]["labels"]["cerebro.io/scope"] == "public" and docs["metadata"]["labels"]["cerebro.io/role"] == "docs"
    assert ("Deployment", "postgres") not in objects and ("StatefulSet", "docs-public") not in objects


def test_secrets_via_secretkeyref_and_kubelet_expansion(rendered):
    _, _, objects = rendered
    env = objects[("Deployment", "memory")]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env[0] == {"name": "HINDSIGHT_API_KEY", "valueFrom": {"secretKeyRef": {"name": "cerebro-secrets", "key": "HINDSIGHT_API_KEY"}}}
    assert env[1]["name"] == "POSTGRES_PASSWORD" and "secretKeyRef" in env[1]["valueFrom"]
    url = next(e for e in env if e["name"] == "HINDSIGHT_API_DATABASE_URL")["value"]
    assert url == "postgresql://cerebro:$(POSTGRES_PASSWORD)@postgres:5432/hindsight"
    docs_env = {e["name"]: e for e in objects[("Deployment", "docs-public")]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert docs_env["MAX_ASYNC"]["value"] == "2" and "secretKeyRef" in docs_env["LIGHTRAG_API_KEY"]["valueFrom"]
    assert k8s.k8s_value("a ${X} $(lit) b") == "a $(X) $$(lit) b"


def test_services_are_clusterip_and_endpoint_is_cluster_dns(rendered):
    adapter, _, objects = rendered
    for (kind, name), obj in objects.items():
        if kind == "Service":
            assert obj["spec"]["type"] == "ClusterIP", name
    svc = objects[("Service", "docs-public")]
    assert svc["spec"]["ports"] == [{"name": "http", "port": 9621, "targetPort": 9621, "protocol": "TCP"}]
    assert svc["metadata"]["labels"]["cerebro.io/port"] == "9621"
    assert adapter.endpoint("docs-public") == "http://docs-public.cerebro.svc:9621"
    assert adapter.endpoint("gateway") == "http://gateway.cerebro.svc:8090"


def test_idle_ttl_annotation_and_resources(rendered):
    _, _, objects = rendered
    code = objects[("Deployment", "code-public")]
    assert code["metadata"]["annotations"] == {"cerebro.io/idle-ttl": "2h"}
    assert "cerebro.io/last-used" not in code["metadata"].get("annotations", {}), "stamped at run time, not render time"
    c = code["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"] == {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"memory": "2Gi"}}
    assert c["imagePullPolicy"] == "IfNotPresent" and c["image"] == "cerebro/code-unit"
    assert "annotations" not in objects[("Deployment", "docs-public")]["metadata"]


def test_readiness_probes(rendered):
    _, _, objects = rendered
    pg = objects[("StatefulSet", "postgres")]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert pg["exec"]["command"][:2] == ["pg_isready", "-U"]
    docs = objects[("Deployment", "docs-public")]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert docs["httpGet"] == {"path": "/health", "port": 9621}
    assert "readinessProbe" not in objects[("Deployment", "mcp-fakemcp")]["spec"]["template"]["spec"]["containers"][0]


def test_jobs_are_cronjobs_sharing_the_units_claim(rendered):
    adapter, files, objects = rendered
    cj = objects[("CronJob", "index-code-public")]
    assert cj["spec"]["schedule"] == "0 3 * * *" and cj["spec"]["suspend"] is False and cj["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never" and pod["containers"][0]["args"] == ["index", "all"]
    assert pod["volumes"] == [{"name": "repos", "persistentVolumeClaim": {"claimName": "code-public-repos"}}]
    assert pod["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["key"] == "GITHUB_TOKEN"
    od = objects[("CronJob", "reindex-code-public")]
    assert od["spec"]["suspend"] is True, "on-demand jobs are suspended CronJobs that run_job() instantiates"
    assert ("PersistentVolumeClaim", "code-public-repos") in objects
    assert "kind: PersistentVolumeClaim" in files[str(adapter.output_dir / "code-public.yaml")]
    assert "kind: PersistentVolumeClaim" not in files[str(adapter.output_dir / "index-code-public.yaml")]
    assert objects[("PersistentVolumeClaim", "code-public-repos")]["spec"]["resources"]["requests"]["storage"] == "10Gi"


def test_files_and_config_mounts(rendered):
    _, _, objects = rendered
    cm = objects[("ConfigMap", "postgres-files")]
    assert "CREATE DATABASE keycloak;" in cm["data"]["init.sql"]
    c = objects[("StatefulSet", "postgres")]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "files", "mountPath": "/docker-entrypoint-initdb.d/init.sql", "subPath": "init.sql", "readOnly": True} in c["volumeMounts"]
    gw = objects[("Deployment", "gateway")]["spec"]["template"]["spec"]
    assert {"name": "config", "configMap": {"name": "cerebro-config"}} in gw["volumes"]
    assert {"name": "plugins", "configMap": {"name": "cerebro-plugins"}} in gw["volumes"]
    mounts = gw["containers"][0]["volumeMounts"]
    assert {"name": "config", "mountPath": "/config", "readOnly": True} in mounts
    assert gw["automountServiceAccountToken"] is False
    assert objects[("Namespace", "cerebro")]["metadata"]["labels"] == {"cerebro.io/project": "cerebro"}


def test_every_label_value_is_kubernetes_valid(rendered):
    import re
    valid = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
    seen = 0
    for (kind, name), obj in objects_with_templates(rendered[2]):
        for k, v in (obj.get("metadata", {}).get("labels") or {}).items():
            assert valid.match(str(v)), f"{kind}/{name}: {k}={v!r}"
            seen += 1
    assert seen > 40
    assert rendered[2][("Deployment", "docs-public")]["metadata"]["labels"]["cerebro.io/adapter"] == "docs.fakerag"
    assert k8s.k8s_labels({"a": "docs:x/y", "b": "ok"}) == {"a": "docs-x-y", "b": "ok"}


def objects_with_templates(objects):
    for key, obj in objects.items():
        yield key, obj
        tpl = obj.get("spec", {}).get("template") or obj.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template")
        if tpl:
            yield key, tpl


def test_bad_idle_ttl_fails_at_render(rendered):
    adapter, _, _ = rendered
    with pytest.raises(ValueError, match="idle_ttl"):
        adapter.render([UnitSpec(name="x", role="code", image="i", idle_ttl="soon")], [])


async def test_ensure_creates_then_scales_and_waits(rendered):
    adapter, _, _ = rendered
    log, state = [], {"exists": False, "ready": 0}

    class W:
        class status: ready_replicas = None
        class spec: replicas = 0
        class metadata: annotations = {"cerebro.io/last-used": "2026-01-01T00:00:00Z"}

    def read(name, kind):
        if not state["exists"]:
            return None
        w = W(); w.status.ready_replicas = state["ready"]; w.spec.replicas = 1
        return w
    adapter._read_workload = read
    adapter._create_missing = lambda objs: (log.append(("create", sorted(o["kind"] for o in objs))), state.update(exists=True))
    adapter._scale = lambda name, kind, r: (log.append(("scale", name, kind, r)), state.update(ready=r))
    unit = next(u for u in plan(adapter.ctx.config, adapter.ctx, adapters=fake_adapters(adapter.ctx))[0] if u.name == "docs-public")
    ep = await adapter.ensure(unit)
    assert ep.ready and ep.url == "http://docs-public.cerebro.svc:9621"
    assert log[0] == ("create", ["Deployment", "PersistentVolumeClaim", "Service"]) and log[1] == ("scale", "docs-public", "Deployment", 1)
    st = await adapter.status(UnitRef(name="docs-public"))
    assert st.exists and st.ready and st.replicas == 1 and st.last_used.year == 2026
    await adapter.release(UnitRef(name="docs-public"))
    assert log[-1] == ("scale", "docs-public", "Deployment", 0)


def _kubectl_ready() -> str | None:
    if not shutil.which("kubectl"):
        return "kubectl not installed"
    try:
        r = subprocess.run(["kubectl", "version", "--client"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    return None if r.returncode == 0 else r.stderr


@pytest.mark.skipif(_kubectl_ready() is not None, reason=f"kubectl: {_kubectl_ready()}")
def test_kubectl_dry_run_accepts_the_kustomization(rendered):
    adapter, files, _ = rendered
    for p, c in files.items():
        pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True); pathlib.Path(p).write_text(c)
    (adapter.output_dir / "secrets.env").write_text("".join(f"{k}={CANARY}-{k}\n" for k in adapter.ctx.config.secrets.keys))
    res = subprocess.run(["kubectl", "apply", "--dry-run=client", "-k", str(adapter.output_dir)], capture_output=True, text=True, timeout=120)
    if res.returncode != 0 and any(s in res.stderr for s in ("connection refused", "Unable to connect", "no such host", "context was not found")):
        pytest.skip(f"no reachable cluster for kubectl: {res.stderr.strip()[:200]}")
    assert res.returncode == 0, res.stderr
    for line in ("deployment.apps/docs-public created", "statefulset.apps/postgres created", "cronjob.batch/index-code-public created",
                 "secret/cerebro-secrets created", "configmap/cerebro-config created", "persistentvolumeclaim/code-public-repos created"):
        assert line in res.stdout, line


def test_endpoint_inside_the_cluster_comes_from_the_environment(ctx, tmp_path, monkeypatch):
    from cerebro.adapters.provision import kubernetes
    monkeypatch.setenv("CEREBRO_UNIT_PORTS", "docs-public=9621,memory=8888")
    inside = kubernetes.Adapter({"output_dir": str(tmp_path / "nowhere")}, ctx)
    assert inside.endpoint("docs-public").endswith(".svc:9621") and inside.endpoint("memory").endswith(".svc:8888")
