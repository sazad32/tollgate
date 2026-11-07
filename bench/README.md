# bench

The model-server benchmark (ADR 0008) and its results ([`RESULTS.md`](RESULTS.md)).

`tollgate_bench` is a small asyncio + httpx client. It runs N users in a closed loop against an
OpenAI-compatible server, streams every response, and records time to first token (TTFT), time per
output token, throughput and errors per concurrency level. It runs inside the cluster as a Kubernetes
Job, on the node that isn't being measured.

```
bench/
  src/tollgate_bench/
    workload.py   the fixed request: synthetic ticket, 128 output tokens, ignore_eos
    client.py     the closed-loop load generator
    stats.py      percentiles and per-level summary
    targets.py    the backends, as seen from inside the cluster
    job.py        the Kubernetes Job for a target
    gpu.py        GPU temperature and clock per level, from Prometheus
    report.py     Job logs -> the Markdown table
  tests/          no network needed: a fake streaming server (httpx.MockTransport)
  raw/            Job logs, one JSON object per line (gitignored)
```

## Run it
Targets: `node-2`, `node-1`, `both-nodes` (the `llama-server` Service), `vllm`.
```powershell
# upload the client code (again after any change to it)
kubectl -n tollgate create configmap bench-client --from-file=bench/src/tollgate_bench --dry-run=client -o yaml | kubectl apply -f -

# start a benchmark; a quick smoke test first is cheap
uv run python -m tollgate_bench job node-2 --levels 1,2 --requests 2,4 | kubectl apply -f -
uv run python -m tollgate_bench job node-2 | kubectl apply -f -

# follow it, then keep the log and build the table
kubectl -n tollgate logs -f job/bench-node-2
kubectl -n tollgate logs job/bench-node-2 > bench/raw/node-2.jsonl
uv run python -m tollgate_bench report bench/raw/node-2.jsonl

# a Job name can only be used once: delete it before running the same target again
kubectl -n tollgate delete job bench-node-2
```
The `vllm` target needs vLLM running on the workstation and its API key in the cluster:
`kubectl -n tollgate create secret generic vllm-api-key --from-env-file=deploy/workstation/vllm/.env`.

The Grafana dashboards (*Tollgate / Model servers*, *Tollgate / GPUs*) show each run live.

## Later
- **Phase 4:** run the same client through the gateway and record the overhead, before and after tuning.
