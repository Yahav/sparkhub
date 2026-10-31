"""Prometheus metrics exported on the hub's /metrics. Labels stay small: mode, outcome."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

REQUESTS = Counter("sparkhub_requests_total", "Requests through the gateway", ["mode", "outcome"], registry=REGISTRY)
TOKENS = Counter("sparkhub_tokens_total", "Tokens through the gateway", ["mode", "kind"], registry=REGISTRY)
TTFT = Histogram("sparkhub_time_to_first_token_seconds", "Time to first token (streamed requests)", ["mode"],
                 buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60, 120), registry=REGISTRY)
GEN_TPS = Histogram("sparkhub_generation_tokens_per_second", "Generation speed after the first token, per request",
                    ["mode"], buckets=(2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 200),
                    registry=REGISTRY)
QUEUE_WAIT = Histogram("sparkhub_queue_wait_seconds", "Wait for the key's concurrency slot", ["mode"],
                       buckets=(0.01, 0.1, 0.5, 1, 5, 15, 30, 60, 300, 600), registry=REGISTRY)
DURATION = Histogram("sparkhub_request_duration_seconds", "Total request time", ["mode"],
                     buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1200, 1800), registry=REGISTRY)
INFLIGHT = Gauge("sparkhub_inflight_requests", "Requests in flight through the hub", ["mode"], registry=REGISTRY)
MODE_INFO = Gauge("sparkhub_mode", "1 for the loaded mode and its state", ["mode", "state"], registry=REGISTRY)
SWITCHES = Counter("sparkhub_switches_total", "Mode switches", ["to", "result"], registry=REGISTRY)
LOAD_SECONDS = Histogram("sparkhub_model_load_seconds", "Model load time (start to ready)", ["mode"],
                         buckets=(60, 120, 180, 240, 300, 400, 600, 900, 1200), registry=REGISTRY)
LOGIN_FAILURES = Counter("sparkhub_login_failures_total", "Failed dashboard logins", ["reason"], registry=REGISTRY)
LOCKOUTS = Counter("sparkhub_lockouts_total", "Accounts locked after failed logins", registry=REGISTRY)
GPU_UTIL = Gauge("sparkhub_gpu_utilization_percent", "GPU utilisation (nvidia-smi)", registry=REGISTRY)
GPU_TEMP = Gauge("sparkhub_gpu_temperature_celsius", "GPU temperature (nvidia-smi)", registry=REGISTRY)
GPU_POWER = Gauge("sparkhub_gpu_power_watts", "GPU power draw (nvidia-smi)", registry=REGISTRY)
