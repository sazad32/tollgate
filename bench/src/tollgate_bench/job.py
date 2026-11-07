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
