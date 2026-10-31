#!/usr/bin/env python3
"""Builds the three provisioned Grafana dashboards (SparkHub, vLLM, System) as JSON.

Run after editing: python3 observability/grafana/build_dashboards.py
Reference lines come from the benchmark (docs/benchmark-summary.md): qwen36 105 tok/s single stream, ~60 per agent with 4;
Flash 43 single, ~22 per agent; time to first token on a 16k prompt 2.3 s / 6.8 s.
"""
import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


class Board:
    def __init__(self, uid, title, tags):
        self.uid, self.title, self.tags = uid, title, tags
        self.panels, self.y, self.x, self.row_h, self.pid = [], 0, 0, 0, 1

    def _place(self, w, h):
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_h
            self.row_h = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title):
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        self.panels.append({"type": "row", "title": title, "id": self.pid, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.pid += 1
        self.y += 1

    def panel(self, kind, title, targets, w=12, h=8, unit=None, desc=None, stack=False, refs=None, decimals=None,
              opts=None, field=None):
        tg = []
        for i, t in enumerate(targets):
            expr, legend = t if isinstance(t, tuple) else (t, None)
            tg.append({"refId": chr(65 + i), "datasource": DS, "expr": expr, "legendFormat": legend or "__auto",
                       "range": True})
        defaults = {"custom": {}}
        if unit:
            defaults["unit"] = unit
        if decimals is not None:
            defaults["decimals"] = decimals
        if kind == "timeseries":
            defaults["custom"] = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 8, "showPoints": "never",
                                  "spanNulls": False, "stacking": {"mode": "normal" if stack else "none"}}
        if field:
            defaults.update(field)
        overrides = []
        for name in refs or []:
            overrides.append({"matcher": {"id": "byRegexp", "options": f".*{name}.*"},
                              "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
                                             {"id": "custom.lineWidth", "value": 1},
                                             {"id": "custom.fillOpacity", "value": 0}]})
        p = {"type": kind, "title": title, "id": self.pid, "datasource": DS, "targets": tg,
             "gridPos": self._place(w, h), "fieldConfig": {"defaults": defaults, "overrides": overrides},
             "options": opts or ({"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "mean"]},
                                  "tooltip": {"mode": "multi"}} if kind == "timeseries" else {})}
        if desc:
            p["description"] = desc
        self.panels.append(p)
        self.pid += 1

    def save(self, refresh="30s", time_from="now-24h"):
        d = {"uid": self.uid, "title": self.title, "tags": self.tags, "timezone": "browser", "editable": False,
             "schemaVersion": 39, "version": 1, "refresh": refresh, "time": {"from": time_from, "to": "now"},
             "panels": self.panels, "templating": {"list": []}, "annotations": {"list": [{
                 "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True,
                 "hide": True, "name": "Annotations & Alerts", "type": "dashboard"}]}}
        (OUT / f"{self.uid}.json").write_text(json.dumps(d, indent=1) + "\n")


def ref(value, label):
    return (f'vector({value})', label)


def q(phi, metric, by="mode", window="30m", flt=""):
    return f'histogram_quantile({phi}, sum by (le, {by}) (rate({metric}_bucket{flt}[{window}])))'


# ---------------------------------------------------------------------------- SparkHub
b = Board("sparkhub", "SparkHub", ["sparkhub"])
b.row("Mode")
b.panel("state-timeline", "Loaded mode", [('max by (mode) (sparkhub_mode{state="ready"})', "{{mode}}"),
                                          ('max by (state) (sparkhub_mode{state!="ready"})', "{{state}}")],
        w=24, h=5, opts={"showValue": "never", "mergeValues": True, "rowHeight": 0.8},
        field={"color": {"mode": "palette-classic"}, "mappings": [{"type": "value", "options": {"1": {"text": "on"}}}]},
        desc="Which mode was loaded, and when a switch or failure was in progress.")
b.panel("timeseries", "Switches", [('sum by (to, result) (increase(sparkhub_switches_total[1h]))', "{{to}} {{result}}")],
        w=12, h=7, decimals=0)
b.panel("timeseries", "Model load time", [('sparkhub_model_load_seconds_sum / sparkhub_model_load_seconds_count', "{{mode}} avg"),
                                         ref(280, "fast bench 280 s"), ref(210, "quality bench 210 s")],
        w=12, h=7, unit="s", refs=["bench"])
b.row("Usage")
b.panel("timeseries", "Requests per hour", [('sum by (mode) (increase(sparkhub_requests_total{outcome!~"refused|unauthorized"}[1h]))', "{{mode}}")],
        w=8, decimals=0, stack=True, opts={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}})
b.panel("timeseries", "Tokens per hour", [('sum by (mode, kind) (increase(sparkhub_tokens_total[1h]))', "{{mode}} {{kind}}")],
        w=8, unit="short", opts={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}})
b.panel("timeseries", "In flight through the hub", [('sum by (mode) (sparkhub_inflight_requests)', "{{mode}}")],
        w=8, decimals=0, opts={"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}})
b.panel("stat", "Requests (range)", [('sum by (mode) (increase(sparkhub_requests_total{outcome!~"refused|unauthorized"}[$__range]))', "{{mode}}")],
        w=8, h=5, decimals=0, opts={"reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value_and_name"})
b.panel("stat", "Output tokens (range)", [('sum by (mode) (increase(sparkhub_tokens_total{kind="completion"}[$__range]))', "{{mode}}")],
        w=8, h=5, unit="short", opts={"reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value_and_name"})
b.panel("stat", "Prompt tokens served from cache (range)", [('sum(increase(sparkhub_tokens_total{kind="cached"}[$__range])) / sum(increase(sparkhub_tokens_total{kind="prompt"}[$__range]))', "cached share")],
        w=8, h=5, unit="percentunit", opts={"reduceOptions": {"calcs": ["lastNotNull"]}})
b.row("Speed (per request, measured by the hub)")
b.panel("timeseries", "Generation speed per request, median and p95",
        [(q(0.5, "sparkhub_generation_tokens_per_second"), "{{mode}} median"),
         (q(0.05, "sparkhub_generation_tokens_per_second"), "{{mode}} slowest 5%"),
         ref(105, "fast bench single 105"), ref(60, "fast bench x4 60"),
         ref(43, "quality bench single 43"), ref(22, "quality bench x4 22")],
        w=12, h=9, unit="short", refs=["bench"],
        desc="Tokens per second after the first token, streamed requests only (30-min window). Dashed: benchmark.")
b.panel("timeseries", "Time to first token, median and p95",
        [(q(0.5, "sparkhub_time_to_first_token_seconds"), "{{mode}} median"),
         (q(0.95, "sparkhub_time_to_first_token_seconds"), "{{mode}} p95"),
         ref(2.3, "fast bench 16k prompt 2.3 s"), ref(6.8, "quality bench 16k prompt 6.8 s")],
        w=12, h=9, unit="s", refs=["bench"], desc="Streamed requests only (30-min window). Dashed: benchmark, 16k-token prompt.")
b.panel("timeseries", "Wait for the key's slot (p95)", [(q(0.95, "sparkhub_queue_wait_seconds"), "{{mode}} p95")], w=12, h=7, unit="s")
b.panel("timeseries", "Request duration, median and p95", [(q(0.5, "sparkhub_request_duration_seconds"), "{{mode}} median"),
                                                           (q(0.95, "sparkhub_request_duration_seconds"), "{{mode}} p95")], w=12, h=7, unit="s")
b.row("Problems")
b.panel("timeseries", "Errors, disconnects, cut by length, refused (per hour)",
        [('sum by (outcome) (increase(sparkhub_requests_total{outcome=~"error|disconnected|length|refused|unauthorized"}[1h]))', "{{outcome}}")],
        w=12, h=8, decimals=0)
b.panel("timeseries", "Failed logins and lockouts (per hour)",
        [('sum by (reason) (increase(sparkhub_login_failures_total[1h]))', "failed: {{reason}}"),
         ('increase(sparkhub_lockouts_total[1h])', "lockouts")], w=12, h=8, decimals=0)
b.save()

# ---------------------------------------------------------------------------- vLLM
v = Board("vllm", "vLLM", ["sparkhub", "vllm"])
v.row("Load")
v.panel("timeseries", "Requests running and waiting", [('sum by (mode) (vllm:num_requests_running)', "{{mode}} running"),
                                                       ('sum by (mode) (vllm:num_requests_waiting)', "{{mode}} waiting")],
        w=12, decimals=0, desc="Waiting = queued inside vLLM because all sequences (max-num-seqs) are busy.")
v.panel("timeseries", "Throughput (tokens/s)", [('sum by (mode) (rate(vllm:generation_tokens_total[1m]))', "{{mode}} generated"),
                                                ('sum by (mode) (rate(vllm:prompt_tokens_total[1m]))', "{{mode}} prompt")], w=12, unit="short")
v.panel("timeseries", "KV cache use", [('max by (mode) (vllm:kv_cache_usage_perc)', "{{mode}}")], w=8, unit="percentunit",
        field={"max": 1, "min": 0})
v.panel("timeseries", "Prefix cache hit rate (5 min)", [('sum by (mode) (rate(vllm:prefix_cache_hits_total[5m])) / sum by (mode) (rate(vllm:prefix_cache_queries_total[5m]))', "{{mode}}")],
        w=8, unit="percentunit", field={"max": 1, "min": 0})
v.panel("timeseries", "Preemptions per minute", [('sum by (mode) (rate(vllm:num_preemptions_total[5m])) * 60', "{{mode}}")], w=8)
v.row("Latency")
v.panel("timeseries", "Time to first token", [(q(0.5, "vllm:time_to_first_token_seconds", window="5m"), "{{mode}} median"),
                                              (q(0.95, "vllm:time_to_first_token_seconds", window="5m"), "{{mode}} p95")], w=8, unit="s")
v.panel("timeseries", "Time per output token", [(q(0.5, "vllm:inter_token_latency_seconds", window="5m"), "{{mode}} median"),
                                                (q(0.95, "vllm:inter_token_latency_seconds", window="5m"), "{{mode}} p95")], w=8, unit="s")
v.panel("timeseries", "Queue time and end-to-end", [(q(0.95, "vllm:request_queue_time_seconds", window="5m"), "{{mode}} queue p95"),
                                                     (q(0.5, "vllm:e2e_request_latency_seconds", window="5m"), "{{mode}} e2e median")], w=8, unit="s")
v.row("Speculative decoding (MTP)")
v.panel("timeseries", "Draft tokens accepted", [('sum by (mode) (rate(vllm:spec_decode_num_accepted_tokens_total[5m])) / sum by (mode) (rate(vllm:spec_decode_num_draft_tokens_total[5m]))', "{{mode}} acceptance")],
        w=12, unit="percentunit", field={"max": 1, "min": 0})
v.panel("timeseries", "Request outcomes (per hour)", [('sum by (mode, finished_reason) (increase(vllm:request_success_total[1h]))', "{{mode}} {{finished_reason}}")],
        w=12, decimals=0)
v.save(refresh="15s", time_from="now-3h")

# ---------------------------------------------------------------------------- System
s = Board("system", "System", ["sparkhub", "system"])
s.row("Memory (unified: CPU and GPU share it)")
s.panel("timeseries", "Memory", [('node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes', "used"),
                                 ('node_memory_Cached_bytes', "page cache"),
                                 ('node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes', "swap used"),
                                 ref(73.1 * 2**30, "fast bench 73.1 GiB"), ref(101.6 * 2**30, "quality bench 101.6 GiB")],
        w=16, h=9, unit="bytes", refs=["bench"])
s.panel("gauge", "Memory used now", [('1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes', "used")], w=8, h=9,
        unit="percentunit", field={"min": 0, "max": 1, "thresholds": {"mode": "absolute", "steps": [
            {"color": "green", "value": None}, {"color": "orange", "value": 0.9}, {"color": "red", "value": 0.95}]}})
s.row("GPU (nvidia-smi via the hub)")
s.panel("timeseries", "GPU temperature", [('sparkhub_gpu_temperature_celsius', "°C")], w=8, unit="celsius")
s.panel("timeseries", "GPU power", [('sparkhub_gpu_power_watts', "W")], w=8, unit="watt")
s.panel("timeseries", "GPU busy", [('sparkhub_gpu_utilization_percent', "%")], w=8, unit="percent")
s.row("CPU, disk, network")
s.panel("timeseries", "CPU busy", [('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m]))', "all cores")], w=8, unit="percentunit")
s.panel("timeseries", "Disk I/O", [('sum(rate(node_disk_read_bytes_total[2m]))', "read"), ('sum(rate(node_disk_written_bytes_total[2m]))', "write")],
        w=8, unit="Bps")
s.panel("timeseries", "Network", [('sum(rate(node_network_receive_bytes_total{device!~"lo|docker.*|veth.*|br-.*"}[2m]))', "in"),
                                  ('sum(rate(node_network_transmit_bytes_total{device!~"lo|docker.*|veth.*|br-.*"}[2m]))', "out")],
        w=8, unit="Bps")
s.panel("bargauge", "Disk space used", [('1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}', "{{mountpoint}}")],
        w=24, h=6, unit="percentunit", field={"min": 0, "max": 1})
s.save(time_from="now-24h")
print("wrote", ", ".join(p.name for p in OUT.glob("*.json")))
