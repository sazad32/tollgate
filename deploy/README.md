# deploy

Kubernetes manifests for k3s (plain YAML + Kustomize, which is built into kubectl), and the Docker
Compose setup for vLLM on the workstation.

```
deploy/
  kustomization.yaml     everything Tollgate runs in k3s
  namespace.yaml         the tollgate namespace
  storage/               tollgate-local StorageClass and the PVs: models, Prometheus, Postgres (ADR 0006)
  llama-server/          StatefulSet (one replica per GPU node), Services, settings (ADR 0004, 0005)
  postgres/              Postgres 18: databases for the gateway and Keycloak (ADR 0011)
  keycloak/              Keycloak 26 and the tollgate realm export (ADR 0003, 0010)
  gateway/               the API gateway: Deployment (2 replicas), Service, settings, migration Job (0009, 0010)
  monitoring/            Prometheus + Grafana, DCGM GPU exporter, scrape targets, dashboards (ADR 0007)
  workstation/vllm/      vLLM in Docker on the workstation, outside k3s (ADR 0004)
```

## One-time prep
A local PV only mounts a directory that already exists (needs sudo on the node):
```
# on each GPU node: model weights
sudo mkdir -p /mnt/storage/tollgate/models
# on vps-node-2 only: Prometheus and Postgres data, owned by the users they run as
sudo mkdir -p /mnt/storage/tollgate/prometheus && sudo chown 1000:2000 /mnt/storage/tollgate/prometheus
sudo mkdir -p /mnt/storage/tollgate/postgres && sudo chown 999:999 /mnt/storage/tollgate/postgres
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

## The gateway
The OpenAI-compatible entry point (ADR 0009), inside the cluster at
`http://tollgate-gateway.tollgate.svc:8000/v1`. From the workstation:
```powershell
kubectl -n tollgate port-forward svc/tollgate-gateway 8000:8000
curl.exe -s http://localhost:8000/v1/models
```
- **Where it forwards:** `TOLLGATE_UPSTREAM_URL` in `gateway/gateway.env` (the llama-server Service by
  default). Change it and re-apply; the pods roll.
- **Trying another upstream without editing Git:**
  `kubectl -n tollgate set env deploy/tollgate-gateway TOLLGATE_UPSTREAM_URL=http://vllm-workstation.tollgate.svc:8000`,
  then remove the override with `kubectl -n tollgate set env deploy/tollgate-gateway TOLLGATE_UPSTREAM_URL-`.
  An explicit env var wins over the ConfigMap.
- **Shipping a code change:** push the branch, wait for the *Gateway image* workflow, then put the new
  `sha-<commit>@sha256:<digest>` in `gateway/deployment.yaml` and `gateway/migrate-job.yaml`, run the
  migration Job if the schema changed, and re-apply.

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
- **vLLM shows Down when the workstation is off.** That's expected, not an outage.

## Later phases
- **Phases 2–3:** Redis, Postgres (reusing `tollgate-local` with its own PV on node-2), Keycloak (with a
  realm export), the gateway and the billing consumer.
- **Phase 6:** one Helm chart for a one-command deploy.

Secrets never go in this folder. Create Kubernetes Secrets outside git (`.gitignore` blocks `**/secrets/`).
