import time
import random
import math
from prometheus_client import start_http_server, Gauge, Counter, Histogram

# --- Service definitions ---
SERVICES = {
    "claims-api": {
        "slo_availability": 0.999,   # 99.9% uptime target
        "slo_latency_p99": 800,      # 800ms p99 target
        "base_error_rate": 0.004,    # normally healthy
        "degraded_error_rate": 0.045, # during incident window
        "base_latency": 320,
        "degraded_latency": 950,
    },
    "policy-service": {
        "slo_availability": 0.995,
        "slo_latency_p99": 500,
        "base_error_rate": 0.001,
        "degraded_error_rate": 0.012,
        "base_latency": 180,
        "degraded_latency": 620,
    },
}

# --- Prometheus metrics ---
availability_gauge = Gauge(
    "slo_availability_ratio",
    "Current availability ratio vs SLO target",
    ["service", "env"]
)

error_rate_gauge = Gauge(
    "slo_error_rate",
    "Current HTTP error rate (5xx / total)",
    ["service", "env"]
)

latency_p99_gauge = Gauge(
    "slo_latency_p99_ms",
    "Simulated p99 latency in milliseconds",
    ["service", "env"]
)

error_budget_remaining = Gauge(
    "slo_error_budget_remaining_ratio",
    "Fraction of monthly error budget remaining (0.0 - 1.0)",
    ["service", "env"]
)

burn_rate_gauge = Gauge(
    "slo_burn_rate",
    "Error budget burn rate (1.0 = burning exactly at SLO boundary)",
    ["service", "env"]
)

slo_target_gauge = Gauge(
    "slo_availability_target",
    "Configured SLO availability target",
    ["service"]
)

request_counter = Counter(
    "simulated_requests_total",
    "Total simulated requests",
    ["service", "env", "status_class"]
)

# --- State tracking for budget calculation ---
budget_state = {
    svc: {"consumed": 0.0, "ticks": 0}
    for svc in SERVICES
}

MONTHLY_TICKS = 43200   # 30 days × 1440 min, approximated at 1 tick/sec = 43200 ticks


def is_degraded(service_name: str, tick: int) -> bool:
    """Inject a degraded window for claims-api every ~200 ticks, lasting 30 ticks."""
    if service_name == "claims-api":
        return (tick % 200) < 30
    # policy-service gets a milder blip every 400 ticks
    if service_name == "policy-service":
        return (tick % 400) < 10
    return False


def compute_metrics(service_name: str, cfg: dict, tick: int) -> dict:
    degraded = is_degraded(service_name, tick)

    # Error rate with small jitter
    base = cfg["degraded_error_rate"] if degraded else cfg["base_error_rate"]
    error_rate = max(0, base + random.gauss(0, base * 0.15))

    # Availability derived from error rate
    availability = 1.0 - error_rate

    # Latency with jitter
    base_lat = cfg["degraded_latency"] if degraded else cfg["base_latency"]
    latency = max(10, base_lat + random.gauss(0, base_lat * 0.10))

    # Simulate request counts (for counter labels)
    total_reqs = random.randint(80, 140)
    error_reqs = int(total_reqs * error_rate)
    success_reqs = total_reqs - error_reqs

    return {
        "error_rate": error_rate,
        "availability": availability,
        "latency_p99": latency,
        "total_reqs": total_reqs,
        "error_reqs": error_reqs,
        "success_reqs": success_reqs,
    }


def update_error_budget(service_name: str, cfg: dict, availability: float):
    """
    Tracks cumulative error budget consumption.
    Budget = (1 - SLO target) per period. Each tick we consume proportionally.
    """
    state = budget_state[service_name]
    state["ticks"] += 1

    allowed_error_rate = 1.0 - cfg["slo_availability"]
    actual_error_rate = 1.0 - availability

    # Budget consumed this tick as fraction of total monthly budget
    tick_consumption = actual_error_rate / (allowed_error_rate * MONTHLY_TICKS) if allowed_error_rate > 0 else 0
    state["consumed"] = min(1.0, state["consumed"] + tick_consumption)

    remaining = max(0.0, 1.0 - state["consumed"])
    burn = actual_error_rate / allowed_error_rate if allowed_error_rate > 0 else 0

    return remaining, burn


def main():
    start_http_server(8000)
    print("Metrics exporter running on :8000")

    tick = 0
    while True:
        for svc_name, cfg in SERVICES.items():
            m = compute_metrics(svc_name, cfg, tick)
            remaining, burn = update_error_budget(svc_name, cfg, m["availability"])
            env = "production"

            availability_gauge.labels(service=svc_name, env=env).set(m["availability"])
            error_rate_gauge.labels(service=svc_name, env=env).set(m["error_rate"])
            latency_p99_gauge.labels(service=svc_name, env=env).set(m["latency_p99"])
            error_budget_remaining.labels(service=svc_name, env=env).set(remaining)
            burn_rate_gauge.labels(service=svc_name, env=env).set(burn)
            slo_target_gauge.labels(service=svc_name).set(cfg["slo_availability"])

            request_counter.labels(service=svc_name, env=env, status_class="2xx").inc(m["success_reqs"])
            request_counter.labels(service=svc_name, env=env, status_class="5xx").inc(m["error_reqs"])

        tick += 1
        time.sleep(1)


if __name__ == "__main__":
    main()