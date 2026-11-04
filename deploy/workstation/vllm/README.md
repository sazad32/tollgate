# vLLM on the workstation

vLLM in Docker Desktop on the workstation's RTX 3060 Laptop GPU (6 GB), outside k3s (ADR 0004). It serves
the same model name (`smollm3-3b`) and OpenAI-compatible API as `llama-server` in the cluster (ADR 0005).
It holds most of the GPU, so it runs only when started on purpose.

## Requirements
- An NVIDIA driver that supports CUDA 13 (the image uses CUDA 13.0).
- A current WSL (`wsl --update`) and Docker Desktop. With Docker Desktop 4.30 (2024) the container saw
  the GPU in `nvidia-smi` but CUDA failed to start: `cudaGetDeviceCount ... Error 500: named symbol not
  found`. Updating Docker Desktop fixed it.

## Run
From this folder:
```powershell
# once: create .env with a random API key (see example.env for the one-liner)
docker compose up -d
docker compose logs -f vllm          # wait for "Application startup complete"
docker compose down                  # stop and free the GPU; downloads and caches are kept
```
First start downloads the model (1.8 GB) and compiles the model graph: about 3.5 minutes. Later starts take
about 90 seconds.

## Try it
```powershell
$key = (Get-Content .env | Select-String '^VLLM_API_KEY=').Line.Split('=', 2)[1]
curl.exe -s http://localhost:8000/v1/models -H "Authorization: Bearer $key"
curl.exe -s http://localhost:8000/v1/chat/completions -H "Authorization: Bearer $key" `
  -H "Content-Type: application/json" `
  -d '{\"model\": \"smollm3-3b\", \"messages\": [{\"role\": \"user\", \"content\": \"Say hi in five words.\"}]}'
```
`/v1/*` needs the key. `/health` and `/metrics` don't, so health checks and Prometheus can reach them.

## Tuning GPU memory
`--gpu-memory-utilization` is a fraction of the GPU's **total** memory. Windows uses 1–2 GB of this GPU for
the display, and vLLM refuses to start if the fraction asks for more than is free. The default is 0.65
(3.9 GB). Override it in `.env`, for example `VLLM_GPU_MEMORY_UTILIZATION=0.6`, and check the startup log
for the KV cache size it got.

## Reaching it from the LAN
Docker Desktop publishes port 8000 on all of the workstation's network interfaces. Whether the k3s nodes can
reach it depends on the Windows firewall. Check from a node with `curl http://<workstation-ip>:8000/health`.

Watch out for the **"Docker Desktop Backend"** firewall rule. Windows creates it the first time you allow
Docker through, and it can allow every published Docker port from any address on Public networks. Scope it
to your home network: mark the home Wi-Fi as Private and restrict the rule to the Private profile and the
local subnet (admin PowerShell):
```powershell
Set-NetConnectionProfile -InterfaceAlias Wi-Fi -NetworkCategory Private
Set-NetFirewallRule -DisplayName "Docker Desktop Backend" -Profile Private
Set-NetFirewallRule -DisplayName "Docker Desktop Backend" -EdgeTraversalPolicy Block -RemoteAddress LocalSubnet
```
The rule Windows creates has edge traversal set to "Defer to user", which doesn't allow an address
condition. That's why the second line also sets it to `Block`. Edge traversal lets internet traffic reach the
app through NAT tunnelling (Teredo), which Docker doesn't need.

Check from a node: `/health` should return 200 and `/v1/models` without a key should return 401.
Re-check after Docker Desktop updates: a new install can prompt again and create a new rule.
