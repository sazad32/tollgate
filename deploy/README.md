# deploy

Kubernetes manifests for k3s (plain YAML + Kustomize, which is built into kubectl), and the Docker
Compose setup for vLLM on the workstation.

```
deploy/
  kustomization.yaml     everything Tollgate runs in k3s
  namespace.yaml         the tollgate namespace
  storage/               tollgate-local StorageClass and the PVs: models, Prometheus, Postgres (ADR 0006)
  llama-server/          StatefulSet (one replica per GPU node), Services, settings (ADR 0004, 0005)
  postgres/              Postgres 18: databases for the gateway, Keycloak and billing (ADR 0011)
  keycloak/              Keycloak 26 and the tollgate realm export (ADR 0003, 0010)
  redis/                 Redis 8: token buckets, fair queue, usage stream (ADR 0012, 0014, 0015)
  gateway/               the API gateway: Deployment (2 replicas), Service, settings, migration Job (0009, 0010)
  billing/               the billing consumer: Deployment, settings, database and migration Jobs (ADR 0016)
  monitoring/            Prometheus + Grafana, DCGM GPU exporter, scrape targets, dashboards (ADR 0007)
  workstation/vllm/      vLLM in Docker on the workstation, outside k3s (ADR 0004)
```

## One-time prep
A local PV only mounts a directory that already exists (needs sudo on the node):
```
# on each GPU node: model weights
sudo mkdir -p /mnt/storage/tollgate/models
# on vps-node-2 only: Prometheus, Postgres and Redis data, owned by the users they run as
sudo mkdir -p /mnt/storage/tollgate/prometheus && sudo chown 1000:2000 /mnt/storage/tollgate/prometheus
sudo mkdir -p /mnt/storage/tollgate/postgres && sudo chown 999:999 /mnt/storage/tollgate/postgres
sudo mkdir -p /mnt/storage/tollgate/redis && sudo chown 999:999 /mnt/storage/tollgate/redis
```
The tollgate Secrets (PowerShell). Random 64-character hex values, never printed or written to disk. The
gateway's database password goes into two Secrets, so it's generated once into `$pg`:
```powershell
function New-Secret { $b = [byte[]]::new(32); [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); ($b | ForEach-Object { $_.ToString('x2') }) -join '' }
$pg = New-Secret

$a = @("--from-literal=POSTGRES_PASSWORD=$(New-Secret)", "--from-literal=TOLLGATE_DB_PASSWORD=$pg",
       "--from-literal=KEYCLOAK_DB_PASSWORD=$(New-Secret)")
kubectl -n tollgate create secret generic postgres-credentials @a

$a = @("--from-literal=KC_BOOTSTRAP_ADMIN_USERNAME=admin", "--from-literal=KC_BOOTSTRAP_ADMIN_PASSWORD=$(New-Secret)")
kubectl -n tollgate create secret generic keycloak-admin @a

$a = @("--from-literal=SUPPORT_CLIENT_SECRET=$(New-Secret)", "--from-literal=BILLING_CLIENT_SECRET=$(New-Secret)")
kubectl -n tollgate create secret generic keycloak-clients @a

$a = @("--from-literal=TOLLGATE_API_KEY_PEPPER=$(New-Secret)",
       "--from-literal=TOLLGATE_DATABASE_URL=postgresql+asyncpg://tollgate:$pg@postgres.tollgate.svc:5432/tollgate")
kubectl -n tollgate create secret generic gateway-auth @a

$rp = New-Secret
$a = @("--from-literal=REDIS_PASSWORD=$rp", "--from-literal=TOLLGATE_REDIS_URL=redis://:$rp@redis.tollgate.svc:6379/0")
kubectl -n tollgate create secret generic redis-auth @a

$bp = New-Secret
$a = @("--from-literal=BILLING_DB_PASSWORD=$bp",
       "--from-literal=TOLLGATE_BILLING_DATABASE_URL=postgresql+asyncpg://billing:$bp@postgres.tollgate.svc:5432/billing")
kubectl -n tollgate create secret generic billing-db @a
Remove-Variable pg, rp, bp, a
```
Changing the pepper later invalidates every API key (their hashes no longer match).
Grafana's admin login, as a Secret that never goes in Git (PowerShell):
```powershell
$pw = -join ((48..57 + 65..90 + 97..122) | ForEach-Object { [char]$_ } | Get-Random -Count 24)
kubectl -n monitoring create secret generic grafana-admin --from-literal=admin-user=admin --from-literal=admin-password=$pw
```
Where vLLM runs: copy `workstation/vllm/endpointslice.example.yaml` to `endpointslice.local.yaml`
(gitignored), put in the workstation's LAN address, and `kubectl apply -f` it.

## Deploy and check
```
kubectl apply -k deploy                       # create or update everything
kubectl -n tollgate get pods,pvc -o wide      # one llama-server pod per node, each claim Bound
kubectl -n tollgate logs llama-server-0 -c fetch-model   # model download / checksum
kubectl -n tollgate logs llama-server-0       # model load, VRAM use
```
First start per node: the image is about 2.5 GB and the model 1.9 GB. Later starts reuse both.

On a fresh cluster, run `kubectl apply -k deploy` **twice**. The first run creates the monitoring
`HelmChart`, and k3s installs kube-prometheus-stack in a background Job. That Job brings the CRDs the
PodMonitors and ServiceMonitors need, so they only apply on the second run.

Then apply the gateway's database migrations (once per release; see the comments in the file):
```
kubectl apply -f deploy/gateway/migrate-job.yaml
kubectl -n tollgate wait --for=condition=complete job/gateway-migrate --timeout=2m
kubectl -n tollgate delete job gateway-migrate
```
Billing's database and login are created by a Job, once (and again after changing the
`billing-db` Secret, to set the new password). Then come billing's migrations, once per release:
```
kubectl apply -f deploy/billing/db-init-job.yaml
kubectl -n tollgate wait --for=condition=complete job/billing-db-init --timeout=2m
kubectl -n tollgate delete job billing-db-init
kubectl apply -f deploy/billing/migrate-job.yaml
kubectl -n tollgate wait --for=condition=complete job/billing-migrate --timeout=2m
kubectl -n tollgate delete job billing-migrate
```
Until then billing logs `billing paused, will retry every 5 s` and loses nothing: events wait in the
stream.

## The gateway
The OpenAI-compatible entry point (ADR 0009), inside the cluster at
`http://tollgate-gateway.tollgate.svc:8000/v1`. From the workstation:
```powershell
kubectl -n tollgate port-forward svc/tollgate-gateway 8000:8000
curl.exe -s http://localhost:8000/v1/models
```
- **Where it routes (ADR 0013):** to the backends in `TOLLGATE_BACKENDS` (`gateway/gateway.env`): vLLM on
  the workstation first while it has a free slot, else the llama-server pod with the most room. The
  gateway reads each backend's `/metrics` every second; `x-tollgate-backend` on every response says who
  answered. Watch the decisions with `kubectl -n tollgate logs deploy/tollgate-gateway -f`.
- **Shipping a code change:** push the branch, wait for the *Gateway image* workflow, then put the new
  `sha-<commit>@sha256:<digest>` in `gateway/deployment.yaml` and `gateway/migrate-job.yaml`, run the
  migration Job if the schema changed, and re-apply.

### The hosted model
Claude, for `hosted_allowed` tenants only (ADR 0013): when a request asks for `claude-opus-5-5` by name, or
when every local backend is full. It needs an Anthropic API key (console.anthropic.com, prepaid credits;
keep auto-reload off as a spend cap), stored as a Secret. Run this in your own PowerShell window, so the
key never lands in a log or transcript, then restart the gateway:
```powershell
$s = Read-Host "Anthropic API key" -AsSecureString
$k = [Net.NetworkCredential]::new("", $s).Password
kubectl -n tollgate create secret generic anthropic-api-key --from-literal=ANTHROPIC_API_KEY=$k
Remove-Variable s, k
kubectl -n tollgate rollout restart deploy/tollgate-gateway
```
Without the Secret there is no hosted model: every request stays local. A tenant's data policy is set
when it's created (`tenant create <id> --data-policy hosted_allowed`); the default is `local_only`.

### Tenants and API keys
Every `/v1` call needs `Authorization: Bearer <API key or Keycloak token>` (ADR 0010). Manage tenants and
keys with the admin CLI inside a gateway pod:
```powershell
$admin = "kubectl -n tollgate exec deploy/tollgate-gateway -- python -m tollgate_gateway.admin"
Invoke-Expression "$admin tenant list"
Invoke-Expression "$admin key create support --name 'my laptop'"   # prints the key ONCE; save it
Invoke-Expression "$admin key list support"
Invoke-Expression "$admin key revoke <key id>"                     # takes up to 60 s to bite
Invoke-Expression "$admin tenant disable billing"                  # 403 for its keys and tokens
```
Then: `curl.exe -s http://localhost:8000/v1/models -H "Authorization: Bearer <key>"`.

### Keycloak tokens
Each tenant has a service client in the `tollgate` realm (`support-service`, `billing-service`) that gets
tokens with the client-credentials grant; their secrets are in the `keycloak-clients` Secret. Tokens
carry `tenant`, audience `tollgate-gateway` and issuer `http://keycloak.tollgate.svc:8080/realms/tollgate`.
From inside the cluster:
```
curl -s http://keycloak.tollgate.svc:8080/realms/tollgate/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=support-service -d client_secret=<secret>
```
**Admin console from the workstation:** Keycloak's hostname is pinned to `keycloak.tollgate.svc` (so token
issuers always match), and it sends the browser there. Map that name to your own machine once, by adding
`127.0.0.1 keycloak.tollgate.svc` to `C:\Windows\System32\drivers\etc\hosts` (as admin). Then run
`kubectl -n tollgate port-forward svc/keycloak 8080:8080` and open http://keycloak.tollgate.svc:8080/admin.
Log in as `admin`; the password is in the `keycloak-admin` Secret. Ingress replaces this in week 17.

The realm is imported from `keycloak/realm-tollgate.json` on the **first** start only; later edits to that
file are ignored (change the realm in Keycloak, or export it back into the file).

### Fair queuing
When every local slot is busy, requests wait in the gateway, in Redis, and each free slot goes to the
tenant with the fewest requests running, on a specific backend (ADR 0014). A tenant may have 16
waiting and a request waits at most 120 s; both limits answer 429 with `Retry-After`
(`TOLLGATE_QUEUE_MAX_WAITING`, `TOLLGATE_QUEUE_MAX_WAIT_S` in `gateway/gateway.env`). If Redis is
down, requests go ahead unqueued. To see the queue right now:
```powershell
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ZRANGE tollgate:queue:running 0 -1'
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ZRANGE tollgate:queue:waiting 0 -1'
# compare with first-come queuing inside llama-server, then remove the override
kubectl -n tollgate set env deploy/tollgate-gateway TOLLGATE_FAIR_QUEUE=false
kubectl -n tollgate set env deploy/tollgate-gateway TOLLGATE_FAIR_QUEUE-
```

### Rate limits
Each tenant has a token bucket in Redis (ADR 0012): `TOLLGATE_RATE_LIMITS` in `gateway/gateway.env` sets
tokens per minute (prompt + output) by tier; the standard tier is 10,000. Every response carries
`x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens`; a refused
request gets 429 with `Retry-After`. If Redis is down, requests go through unlimited (logged).
```powershell
# a tenant's bucket right now (tokens may be negative: debt from an underestimated request)
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" HGETALL tollgate:bucket:support'
# try a different limit without editing Git, then remove the override
kubectl -n tollgate set env deploy/tollgate-gateway 'TOLLGATE_RATE_LIMITS={"standard": 400}'
kubectl -n tollgate set env deploy/tollgate-gateway TOLLGATE_RATE_LIMITS-
```

### Usage events
Every request a model ran gives one event in the Redis stream `tollgate:usage`, for billing (ADR
0015; the format is in `docs/usage-events.md`). Redis keeps it in an append-only file on node-2, so
it survives restarts. If Redis is down, the gateway holds events in memory and writes them later.
```powershell
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" XLEN tollgate:usage'
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" XRANGE tollgate:usage - + COUNT 5'
# events lost to a full buffer are logged in full
kubectl -n tollgate logs deploy/tollgate-gateway | Select-String "usage event"
```
**Upgrading from before week 10:** Redis was a Deployment. Delete it before applying, so two Redis
instances never serve at once: `kubectl -n tollgate delete deploy redis`, then `kubectl apply -k deploy`.

## Billing
A worker with no port (ADR 0016). It reads `tollgate:usage` as the consumer group `billing` and
writes each event once to the usage ledger, in its own database `billing`. It acknowledges a batch
only after the commit, and the ledger is keyed by the event's idempotency key, so copies and replays
are skipped. If billing, Postgres or Redis is down, events wait in the stream and the API carries on.
```powershell
kubectl -n tollgate exec deploy/tollgate-billing -- python -m tollgate_billing status   # stream, group, ledger, per tenant
kubectl -n tollgate logs deploy/tollgate-billing -f                                     # "billed N usage events (...)"
# the ledger itself
kubectl -n tollgate exec postgres-0 -- psql -U postgres -d billing -c "SELECT tenant_id, status, billable_input_tokens, billable_output_tokens FROM usage_ledger ORDER BY started_at DESC LIMIT 10"
# entries billing refused, with the reason
kubectl -n tollgate exec postgres-0 -- psql -U postgres -d billing -c "SELECT stream_id, reason FROM usage_rejects"
# re-read the whole stream (safe: every copy is skipped)
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" XGROUP SETID tollgate:usage billing 0'
```
- **Shipping a code change:** as for the gateway, with the *Billing image* workflow, and the new
  image in `billing/deployment.yaml` and `billing/migrate-job.yaml`.
- **The stream is trimmed** by billing, once a minute: only entries every group has acknowledged and
  that are older than 24 hours (`TOLLGATE_BILLING_RETENTION_S` in `billing/billing.env`).

## Look at the dashboards
```powershell
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
# open http://localhost:3000, log in as admin with:
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | ForEach-Object { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
```
Our dashboards are in the **Tollgate** folder: *Model servers* and *GPUs*. Prometheus itself:
`kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090`, then
http://localhost:9090/targets.

## Try it
```
kubectl -n tollgate port-forward svc/llama-server 8080:8080
curl http://localhost:8080/v1/models
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "smollm3-3b", "messages": [{"role": "user", "content": "Say hi in five words."}]}'
```
A port-forward to a Service sticks to one pod. To reach a specific replica from inside the cluster, use
its headless DNS name: `llama-server-0.llama-server-headless.tollgate.svc:8080`.

## Change the model or its settings
Edit `llama-server/llama-server.env` (URL, SHA-256, file name, alias, context) and run
`kubectl apply -k deploy`. The ConfigMap name carries a content hash, so the pods roll one at a time.
Keep the alias and context in step with `workstation/vllm/compose.yaml`.

## Gotchas
- **The PVs use `Retain`.** Deleting a claim keeps the weights but leaves the PV `Released`, and a
  Released PV won't bind again until you clear its claim:
  `kubectl patch pv tollgate-models-vps-node-1 -p '{"spec":{"claimRef":null}}'`.
- **One GPU per node.** A third replica, or a second GPU workload on a node, stays `Pending`.
- **First request after a fresh start is slower** on the Maxwell nodes: the driver compiles GPU kernels
  from PTX on first use. The compiled cache is kept on the model volume (`CUDA_CACHE_PATH`).

- **Dashboards are generated JSON.** Edit `monitoring/dashboards/*.json` (or export from Grafana's UI)
  and re-apply. Changes made only in the UI are lost when Grafana restarts: it has no storage.
- **vLLM shows Down when the workstation is off.** That's expected, not an outage: the gateway routes
  around it within a second, and logs `backend vllm is down` / `is back`.

## Later phases
- **Phase 3:** pricing and invoices (weeks 12–13).
- **Phase 6:** one Helm chart for a one-command deploy.

Secrets never go in this folder. Create Kubernetes Secrets outside git (`.gitignore` blocks `**/secrets/`).
