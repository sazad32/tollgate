import pytest
from tollgate_bench.job import manifest
from tollgate_bench.report import markdown_table, read_records
from tollgate_bench.targets import TARGETS


def container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["containers"][0]


def test_client_never_runs_on_the_node_it_measures() -> None:
    for target in TARGETS.values():
        job = manifest(target, "r1")
        node = job["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"]
        assert node not in target.gpu_nodes or len(target.gpu_nodes) > 1, target.name


def test_node_job_points_at_one_pod_and_records_its_gpu() -> None:
    args = container(manifest(TARGETS["node-2"], "r1"))["args"]

    assert args[args.index("--url") + 1].startswith("http://llama-server-0.")
    assert args[args.index("--gpu-nodes") + 1] == "vps-node-2"
    assert "--api-key-env" not in args


def test_vllm_job_reads_its_api_key_from_a_secret() -> None:
    c = container(manifest(TARGETS["vllm"], "r1"))
    env = {e["name"]: e for e in c["env"]}

    assert env["API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "vllm-api-key"
    assert "--gpu-nodes" not in c["args"]


def test_report_skips_non_json_lines_and_builds_a_table() -> None:
    lines = [
        "Downloading httpx",
        '{"type": "level", "backend": "b", "concurrency": 8, "requests": 32, "errors": 0, '
        '"output_tokens_per_s": 25.0, "requests_per_s": 0.2, "ttft_p50_s": 0.4, '
        '"ttft_p95_s": 2.5, "tpot_p50_s": 0.08, "tpot_p95_s": 0.1, '
        '"mean_prompt_tokens": 256, "mean_output_tokens": 128, "gpu_temp_max_c": 71.0}',
    ]

    table = markdown_table(read_records(lines))

    assert "### b" in table
    assert "| 8 | 32 / 32 | 25.0 | 0.20 | 400 ms | 2.5 s | 80 ms | 100 ms | 71 °C | – |" in table
    assert "256 prompt / 128 output" in table


def test_a_load_run_has_one_job_per_node_sharing_one_load_all_starting_together() -> None:
    # Node-local (ADR 0019, amended in week 15): clients on each node reach the gateway and the mock
    # on that node, so the load is split across nodes by the number of client pods on each.
    from tollgate_bench.job import load_manifest

    run = load_manifest(name="load-1", url="http://tollgate-gateway-load.tollgate.svc:8000",
                        label="gateway", steps="100,500", step_s=60,
                        nodes={"vps-node-1": 5, "vps-node-2": 2}, start_at=1_800_000_000.0,
                        key_secret="loadtest-key")  # fmt: skip

    assert run["kind"] == "List"
    one, two = run["items"]
    assert [j["metadata"]["name"] for j in run["items"]] == [
        "load-1-vps-node-1",
        "load-1-vps-node-2",
    ]
    for job, node, pods, offset in ((one, "vps-node-1", 5, 0), (two, "vps-node-2", 2, 5)):
        spec = job["spec"]
        assert spec["completionMode"] == "Indexed"
        assert spec["completions"] == spec["parallelism"] == pods
        assert spec["template"]["spec"]["nodeSelector"] == {"kubernetes.io/hostname": node}
        # one label for the whole run, to wait for it and collect its logs
        assert job["metadata"]["labels"]["app.kubernetes.io/instance"] == "load-1"
        assert spec["template"]["metadata"]["labels"]["app.kubernetes.io/instance"] == "load-1"
        [container] = spec["template"]["spec"]["containers"]
        assert "uvloop==0.23.0" in container["command"]  # a cheaper client (fast_runner)
        args = container["args"]
        assert args[:2] == ["-m", "tollgate_bench"]
        assert args[args.index("--pods") + 1] == "7"  # each pod sends 1/7 of every step
        assert args[args.index("--pod-offset") + 1] == str(offset)  # pods 0-4, then 5-6
        assert args[args.index("--start-at") + 1] == "1800000000.0"
        assert args[args.index("--steps") + 1] == "100,500"
        assert args[args.index("--key-env") + 1] == "TG_LOAD_KEY"
        key = next(e for e in container["env"] if e["name"] == "TG_LOAD_KEY")
        assert key["valueFrom"]["secretKeyRef"] == {"name": "loadtest-key", "key": "TG_LOAD_KEY"}


def test_client_pods_per_node_are_written_node_equals_pods() -> None:
    from tollgate_bench.job import parse_nodes

    assert parse_nodes("vps-node-1=5,vps-node-2=2") == {"vps-node-1": 5, "vps-node-2": 2}
    with pytest.raises(ValueError):
        parse_nodes("vps-node-1")


def test_a_load_job_straight_at_the_mock_needs_no_key() -> None:
    from tollgate_bench.job import load_manifest

    run = load_manifest(name="load-0", url="http://tollgate-mock.tollgate.svc:8080", label="mock",
                        steps="100", step_s=30, nodes={"vps-node-1": 1}, start_at=1.0,
                        key_secret=None)  # fmt: skip

    [job] = run["items"]
    [container] = job["spec"]["template"]["spec"]["containers"]
    assert "--key-env" not in container["args"]
    assert not any(e["name"] == "TG_LOAD_KEY" for e in container["env"])
