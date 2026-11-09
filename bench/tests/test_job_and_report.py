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


def test_a_load_job_runs_one_pod_per_share_of_the_load_all_starting_together() -> None:
    from tollgate_bench.job import load_manifest

    job = load_manifest(name="load-1", url="http://tollgate-gateway-load.tollgate.svc:8000",
                        label="1 replica", steps="100,500", step_s=60, pods=3,
                        start_at=1_800_000_000.0, key_secret="loadtest-key")  # fmt: skip

    spec = job["spec"]
    assert (job["metadata"]["name"], spec["completionMode"]) == ("load-1", "Indexed")
    assert spec["completions"] == spec["parallelism"] == 3
    pod = spec["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "vps-node-1"}
    [container] = pod["containers"]
    args = container["args"]
    assert args[:2] == ["-m", "tollgate_bench"]
    assert args[args.index("--pods") + 1] == "3"
    assert args[args.index("--start-at") + 1] == "1800000000.0"
    assert args[args.index("--steps") + 1] == "100,500"
    assert args[args.index("--key-env") + 1] == "TG_LOAD_KEY"
    key = next(e for e in container["env"] if e["name"] == "TG_LOAD_KEY")
    assert key["valueFrom"]["secretKeyRef"] == {"name": "loadtest-key", "key": "TG_LOAD_KEY"}


def test_a_load_job_straight_at_the_mock_needs_no_key() -> None:
    from tollgate_bench.job import load_manifest

    job = load_manifest(name="load-0", url="http://tollgate-mock.tollgate.svc:8080", label="mock",
                        steps="100", step_s=30, pods=1, start_at=1.0, key_secret=None)  # fmt: skip

    [container] = job["spec"]["template"]["spec"]["containers"]
    assert "--key-env" not in container["args"]
    assert not any(e["name"] == "TG_LOAD_KEY" for e in container["env"])
