"""The backends we benchmark, as seen from inside the cluster (ADR 0008).

The client runs on the node that is *not* being measured, so it doesn't share CPU with the server
it measures, and its traffic crosses the network the way the gateway's will.
"""

from dataclasses import dataclass

MODEL = "smollm3-3b"
PROMETHEUS_URL = "http://kube-prometheus-stack-prometheus.monitoring.svc:9090"


@dataclass(frozen=True)
class Target:
    name: str  # what you type: `job node-2`
    backend: str  # label in the results
    url: str
    client_node: str  # where the benchmark Job runs
    gpu_nodes: tuple[str, ...]  # nodes whose GPU temperature and clock we record
    api_key_secret: str | None = None  # Kubernetes Secret holding the API key, if one is needed
    levels: tuple[int, ...] = (1, 8, 32, 64)
    requests: tuple[int, ...] = (10, 32, 64, 128)  # per level; at least 2 rounds of every user


TARGETS = {
    t.name: t
    for t in (
        Target(
            name="node-2",
            backend="llama-server-0 (node-2, GTX 960M)",
            url="http://llama-server-0.llama-server-headless.tollgate.svc:8080",
            client_node="vps-node-1",
            gpu_nodes=("vps-node-2",),
        ),
        Target(
            name="node-1",
            backend="llama-server-1 (node-1, GTX 860M)",
            url="http://llama-server-1.llama-server-headless.tollgate.svc:8080",
            client_node="vps-node-2",
            gpu_nodes=("vps-node-1",),
        ),
        Target(
            name="both-nodes",
            backend="llama-server Service (both nodes)",
            url="http://llama-server.tollgate.svc:8080",
            client_node="vps-node-1",
            gpu_nodes=("vps-node-1", "vps-node-2"),
        ),
        Target(
            name="vllm",
            backend="vLLM (workstation, RTX 3060 Laptop)",
            url="http://vllm-workstation.tollgate.svc:8000",
            client_node="vps-node-1",
            gpu_nodes=(),  # Windows GPU, not scraped
            api_key_secret="vllm-api-key",
        ),
    )
}
