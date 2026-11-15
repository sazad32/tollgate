"""Build the Kubernetes Job that runs the benchmark inside the cluster.

Printed as JSON (kubectl accepts JSON as well as YAML): `python -m tollgate_bench job node-2`.
The Job mounts this package from the `bench-client` ConfigMap and runs it with uv's Python image,
so there's no image to build.
"""

from tollgate_bench.targets import MODEL, PROMETHEUS_URL, Target

# uv's official image with Python 3.12, pinned by digest (tag: python3.12-bookworm-slim).
IMAGE = (
    "ghcr.io/astral-sh/uv:python3.12-bookworm-slim"
    "@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
)
HTTPX = "httpx==0.28.1"  # keep in step with uv.lock
UVLOOP = "uvloop==0.23.0"  # keep in step with uv.lock; makes load clients cheaper
LOAD_COMMAND = ["uv", "run", "--no-project", "--with", HTTPX, "--with", UVLOOP, "python"]


def job_name(target: Target) -> str:
    return f"bench-{target.name}"


def manifest(
    target: Target,
    run_id: str,
    levels: tuple[int, ...] | None = None,
    requests: tuple[int, ...] | None = None,
    label: str | None = None,
) -> dict:
    """The Job for `target`. `levels` and `requests` override the target's (smoke tests)."""
    args = [
        "run",
        "--backend", label or target.backend,
        "--url", target.url,
        "--model", MODEL,
        "--levels", ",".join(map(str, levels or target.levels)),
        "--requests", ",".join(map(str, requests or target.requests)),
        "--run-id", run_id,
    ]  # fmt: skip
    env = [
        {"name": "PYTHONPATH", "value": "/app"},
        {"name": "PYTHONUNBUFFERED", "value": "1"},  # stream results to the log as they happen
        {"name": "UV_CACHE_DIR", "value": "/tmp/uv-cache"},
        {"name": "HOME", "value": "/tmp"},
    ]
    if target.gpu_nodes:
        args += ["--gpu-nodes", ",".join(target.gpu_nodes), "--prometheus-url", PROMETHEUS_URL]
    if target.api_key_secret:
        args += ["--api-key-env", "API_KEY"]
        env.append(
            {
                "name": "API_KEY",
                "valueFrom": {
                    "secretKeyRef": {"name": target.api_key_secret, "key": "VLLM_API_KEY"}
                },
            }
        )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(target),
            "namespace": "tollgate",
            "labels": {"app.kubernetes.io/name": "tollgate-bench", "tollgate/run-id": run_id},
        },
        "spec": {
            "backoffLimit": 0,  # a failed benchmark should be looked at, not silently re-run
            "ttlSecondsAfterFinished": 86400,  # keep the logs for a day
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "tollgate-bench"}},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.io/hostname": target.client_node},
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65534},
                    "containers": [
                        {
                            "name": "bench",
                            "image": IMAGE,
                            "command": ["uv", "run", "--no-project", "--with", HTTPX, "python"],
                            "args": ["-m", "tollgate_bench", *args],
                            "env": env,
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "128Mi"},
                                "limits": {"memory": "512Mi"},
                            },
                            "volumeMounts": [
                                {
                                    "name": "code",
                                    "mountPath": "/app/tollgate_bench",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [{"name": "code", "configMap": {"name": "bench-client"}}],
                },
            },
        },
    }


# Client pods per node (ADR 0019, amended in week 15). Requests stay on their node, from client to
# gateway to mock, so the slow link between the nodes isn't counted as the gateway's. node-2 has 4
# cores and also runs Redis and Postgres, so it takes the smaller share: 2 pods of 7.
LOAD_CLIENT_NODES = {"vps-node-1": 5, "vps-node-2": 2}


def parse_nodes(text: str) -> dict[str, int]:
    """ "vps-node-1=5,vps-node-2=2" -> client pods per node."""
    nodes = {}
    for part in text.split(","):
        node, sep, pods = part.partition("=")
        if not sep:
            raise ValueError(f"expected node=pods, got {part!r}")
        nodes[node.strip()] = int(pods)
    return nodes


def load_manifest(*, name: str, url: str, label: str, steps: str, step_s: float,
                  nodes: dict[str, int], start_at: float,
                  key_secret: str | None) -> dict:  # fmt: skip
    """The Jobs for one `load` run (ADR 0019), as a List: one Indexed Job per node, with that
    node's client pods. Every pod sends 1/total of every step, so a node's share of the load is its
    share of the pods. All start at `start_at`; all carry the run's name as their instance label."""
    total = sum(nodes.values())
    jobs, offset = [], 0
    for node, pods in nodes.items():
        jobs.append(_load_job(name=f"{name}-{node}", run=name, node=node, pods=pods,
                              args=["-m", "tollgate_bench", "load", "--url", url,
                                    "--label", label, "--steps", steps, "--step-s", str(step_s),
                                    "--pods", str(total), "--pod-offset", str(offset),
                                    "--start-at", str(start_at)],
                              key_secret=key_secret))  # fmt: skip
        offset += pods
    return {"apiVersion": "v1", "kind": "List", "items": jobs}


def _load_job(*, name: str, run: str, node: str, pods: int, args: list[str],
              key_secret: str | None) -> dict:  # fmt: skip
    env = [
        {"name": "PYTHONPATH", "value": "/app"},
        {"name": "PYTHONUNBUFFERED", "value": "1"},
        {"name": "UV_CACHE_DIR", "value": "/tmp/uv-cache"},
        {"name": "HOME", "value": "/tmp"},
    ]
    if key_secret:
        args = [*args, "--key-env", "TG_LOAD_KEY"]
        env.append(
            {
                "name": "TG_LOAD_KEY",
                "valueFrom": {"secretKeyRef": {"name": key_secret, "key": "TG_LOAD_KEY"}},
            }
        )
    labels = {"app.kubernetes.io/name": "tollgate-load", "app.kubernetes.io/instance": run}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": "tollgate", "labels": labels},
        "spec": {
            "completionMode": "Indexed",
            "completions": pods,
            "parallelism": pods,
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.io/hostname": node},
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65534},
                    "containers": [
                        {
                            "name": "load",
                            "image": IMAGE,
                            "command": LOAD_COMMAND,
                            "args": args,
                            "env": env,
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "256Mi"},
                                "limits": {"memory": "1Gi"},
                            },
                            "volumeMounts": [
                                {
                                    "name": "code",
                                    "mountPath": "/app/tollgate_bench",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],  # fmt: skip
                    "volumes": [{"name": "code", "configMap": {"name": "bench-client"}}],
                },
            },
        },
    }
