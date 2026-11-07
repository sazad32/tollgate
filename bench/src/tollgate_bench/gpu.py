"""GPU temperature and clock during a level, from Prometheus (DCGM exporter, ADR 0007).

Recorded so a slow result can be told apart from a hot, throttling GPU.
"""

import math

import httpx


async def gpu_window(
    client: httpx.AsyncClient, prometheus_url: str, nodes: list[str], start: float, end: float
) -> dict[str, float | None]:
    """Hottest temperature and lowest core clock across `nodes` between `start` and `end`."""
    window = f"{max(math.ceil(end - start), 15)}s"
    selector = '{node=~"' + "|".join(nodes) + '"}'
    queries = {
        "gpu_temp_max_c": f"max(max_over_time(DCGM_FI_DEV_GPU_TEMP_CELSIUS{selector}[{window}]))",
        "gpu_clock_min_mhz": (
            f"min(min_over_time(DCGM_FI_DEV_SM_CLOCK_HERTZ{selector}[{window}])) / 1e6"
        ),
    }
    result: dict[str, float | None] = {}
    for key, query in queries.items():
        try:
            response = await client.get(
                f"{prometheus_url}/api/v1/query", params={"query": query, "time": end}
            )
            data = response.json()["data"]["result"]
            result[key] = float(data[0]["value"][1]) if data else None
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            result[key] = None  # missing GPU data must not fail the benchmark
    return result
