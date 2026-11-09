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
    fairness.py   two tenants through the gateway: a burst and a trickle (week 9)
    metering.py   a request mix through the gateway, recorded per request ID, for billing's audit (week 13)
    mock.py       a fake model server: paced tokens, no GPU, for load tests (week 14)
    load.py       open-loop load generator in steps, with mergeable latency histograms (week 14)
  tests/          no network needed: a fake streaming server (httpx.MockTransport)
  raw/            Job logs, one JSON object per line (gitignored)
```

## Run it
Targets: `node-2`, `node-1`, `both-nodes` (the `llama-server` Service), `vllm`, and `gateway`
(whatever the gateway forwards to; name the run with `--label`, e.g. `--label "gateway -> vLLM"`).
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

## Two-tenant fairness demo
`fairness` runs from the workstation against the gateway: one tenant sends a burst, the other a
request every few seconds, and the table shows how long each waited (ADR 0014; results at the end of
RESULTS.md). It needs an API key for each tenant (`admin key create`; revoke them afterwards) and a
port-forward to each gateway pod, so the burst and the trickle use different replicas:
```powershell
kubectl -n tollgate get pods -l app.kubernetes.io/name=tollgate-gateway
kubectl -n tollgate port-forward pod/<first pod> 8001:8000    # and pod/<second pod> 8002:8000
$env:TG_FLOOD = "<support key>"; $env:TG_TRICKLE = "<billing key>"
uv run python -m tollgate_bench fairness --url http://localhost:8001 --trickle-url http://localhost:8002 `
  --label "fair queue on" --flood-key-env TG_FLOOD --trickle-key-env TG_TRICKLE > bench/raw/fairness-on.jsonl
uv run python -m tollgate_bench report bench/raw/fairness-*.jsonl
```
For the "off" run: `kubectl -n tollgate set env deploy/tollgate-gateway TOLLGATE_FAIR_QUEUE=false`, then
remove it again with `TOLLGATE_FAIR_QUEUE-`. Wait a minute between runs, so token buckets refill.

## Metering check
`metering` sends each tenant's share of a request mix through the gateway (half plain, four in ten
streamed, one in ten hung up on after 5 chunks) and writes one JSON line per request: the gateway's
request ID and the usage the client was told. Billing then checks every line against its ledger
row, and the run's span against the model servers' counters (ADR 0018; results at the end of
RESULTS.md). It needs an API key for each tenant (revoke them afterwards) and a port-forward to the
gateway:
```powershell
kubectl -n tollgate port-forward svc/tollgate-gateway 8000:8000
$env:TG_SUPPORT = "<support key>"; $env:TG_BILLING = "<billing key>"
uv run python -m tollgate_bench metering --url http://localhost:8000 --key support=TG_SUPPORT --key billing=TG_BILLING > bench/raw/metering.jsonl
Get-Content bench/raw/metering.jsonl | kubectl -n tollgate exec -i deploy/tollgate-billing -- python -m tollgate_billing check-requests
kubectl -n tollgate exec deploy/tollgate-billing -- python -m tollgate_billing reconcile --start <start> --end <end>
```
Give `reconcile` the run's start, and an end at least 30 s after the run, so Prometheus has scraped
the servers' counters once more.

## Load test
Where the gateway itself breaks, with the GPUs out of the way (ADR 0019; results at the end of
RESULTS.md). `deploy/loadtest` adds a fake model server and a second gateway in front of it. The
load runs as an Indexed Job of client pods, about 150 streams each, all starting together. It needs
the `loadtest` tenant and its key in the `loadtest-key` Secret (made once, by hand):
```powershell
# the rig: the bench code as a ConfigMap, the fake server, the load-test gateway
kubectl -n tollgate create configmap bench-client --from-file=bench/src/tollgate_bench --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -k deploy/loadtest

# first the clients and the fake server alone: they must hold every step cleanly
uv run python -m tollgate_bench load-job --name load-mock --url http://tollgate-mock.tollgate.svc:8080 `
  --label "client -> mock" --steps 250,500,1000 --pods 7 | kubectl apply -f -
# then through the gateway (scale it with: kubectl -n tollgate scale deploy/tollgate-gateway-load --replicas=2)
uv run python -m tollgate_bench load-job --name load-gw --url http://tollgate-gateway-load.tollgate.svc:8000 `
  --label "gateway, 1 replica" --steps 100,250,500,1000 --pods 7 --key-secret loadtest-key | kubectl apply -f -

# each pod's log is one JSON line per step; merge them into one table
kubectl -n tollgate wait --for=condition=complete job/load-gw --timeout=10m
foreach ($p in kubectl -n tollgate get pods -l job-name=load-gw -o name) { kubectl -n tollgate logs $p >> bench/raw/load-gw.jsonl }
uv run python -m tollgate_bench load-report bench/raw/load-gw.jsonl

kubectl delete -k deploy/loadtest   # afterwards
```
While it runs, the gateway's own metrics are in Prometheus (`tollgate_gateway_*`): event-loop lag,
time in each step before the model, and requests in flight.

## Later
- **Phase 4:** run the same client through the gateway and record the overhead, before and after tuning.
