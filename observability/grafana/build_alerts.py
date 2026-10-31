#!/usr/bin/env python3
"""Builds the provisioned Grafana alerting files: rules, the ntfy contact point, the policy.

Run after editing: python3 observability/grafana/build_alerts.py (./install.sh runs it with SPARKHUB_HUB_PORT set)
The hub itself pushes events at once (model crashed / restarted, failed switch, backup failed); these rules cover
conditions over time. The contact point posts to the hub (/internal/grafana-alert, localhost only), which relays to
ntfy with the topic and on/off set on the hub's Settings page, so Grafana never holds the topic.
"""
import json
import os
from pathlib import Path

OUT = Path(__file__).parent / "provisioning" / "alerting"
DS = "prometheus"
HUB_PORT = int(os.environ.get("SPARKHUB_HUB_PORT", "8090"))


def rule(uid, title, expr, op, value, for_="5m", summary="", severity="warning", nodata="OK"):
    return {
        "uid": uid, "title": title, "condition": "C", "for": for_,
        "noDataState": nodata, "execErrState": "Error",
        "labels": {"severity": severity, "source": "sparkhub"},
        "annotations": {"summary": summary},
        "data": [
            {"refId": "A", "datasourceUid": DS, "relativeTimeRange": {"from": 600, "to": 0},
             "model": {"refId": "A", "expr": expr, "instant": True, "range": False, "intervalMs": 60000}},
            {"refId": "C", "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0},
             "model": {"refId": "C", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": op, "params": [value]}}]}},
        ],
    }


RULES = [
    rule("sh-cert-expiry", "TLS certificate expires soon",
         "(sparkhub_tls_cert_expiry_timestamp_seconds - time()) / 86400", "lt", 21, for_="1h",
         summary="The certificate for {{ $labels.name }} expires in {{ humanize $values.A.Value }} days. "
                 "Let's Encrypt: ./install.sh tls renews it (needs port 80, or DNS-01); local CA: re-run ./install.sh.",
         severity="critical"),
    rule("sh-cert-invalid", "TLS certificate does not verify",
         "sparkhub_tls_check_ok", "lt", 1, for_="2h",
         summary="The certificate nginx serves for {{ $labels.name }} does not verify (expired, wrong name or "
                 "missing). Check nginx and the certificate files.", severity="critical"),
    rule("sh-no-model", "No model loaded",
         'sum(sparkhub_mode{state=~"failed|idle"})', "gt", 0, for_="10m",
         summary="SparkHub has had no model loaded for 10 minutes. Switch from the dashboard or check the logs.",
         severity="critical"),
    rule("sh-hub-down", "SparkHub hub not reachable",
         'up{job="sparkhub"}', "lt", 1, for_="5m",
         summary=f"Prometheus cannot reach the hub on 127.0.0.1:{HUB_PORT} (container down or hung).",
         severity="critical", nodata="Alerting"),
    rule("sh-slow-fast", "Fast mode generating slowly",
         'histogram_quantile(0.5, sum by (le) (rate(sparkhub_generation_tokens_per_second_bucket{mode="fast"}[30m])))'
         ' and on() sum(increase(sparkhub_generation_tokens_per_second_count{mode="fast"}[30m])) >= 10',
         "lt", 30, for_="15m",
         summary="Median generation speed on Fast is {{ humanize $values.A.Value }} tok/s over 30 min; the "
                 "benchmark gives ~60 per stream with 4 agents (105 alone)."),
    rule("sh-slow-quality", "Quality mode generating slowly",
         'histogram_quantile(0.5, sum by (le) (rate(sparkhub_generation_tokens_per_second_bucket{mode="quality"}[30m])))'
         ' and on() sum(increase(sparkhub_generation_tokens_per_second_count{mode="quality"}[30m])) >= 10',
         "lt", 11, for_="15m",
         summary="Median generation speed on Quality is {{ humanize $values.A.Value }} tok/s over 30 min; the "
                 "benchmark gives ~22 per stream with 4 agents (43 alone)."),
    rule("sh-memory", "Memory nearly full",
         "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", "lt", 0.05, for_="10m",
         summary="Only {{ humanizePercentage $values.A.Value }} of memory is available (unified CPU/GPU memory)."),
    rule("sh-swap", "Heavy swapping",
         "(node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes) / 2^30", "gt", 8, for_="30m",
         summary="{{ humanize $values.A.Value }} GiB of swap in use for 30 minutes: the model may be slowed down."),
    rule("sh-disk", "Disk space low",
         'node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}', "lt", 0.10,
         for_="30m", summary="Only {{ humanizePercentage $values.A.Value }} of the root disk is free."),
    rule("sh-logins", "Many failed logins",
         "sum(increase(sparkhub_login_failures_total[15m]))", "gt", 20, for_="0s",
         summary="{{ humanize $values.A.Value }} failed dashboard logins in 15 minutes."),
    rule("sh-lockout", "Account locked after failed logins",
         "sum(increase(sparkhub_lockouts_total[15m]))", "gt", 0, for_="0s",
         summary="A dashboard account (or username) was locked after 5 failed attempts."),
    rule("sh-backup", "Backup overdue",
         "(time() - sparkhub_backup_last_success_timestamp_seconds) / 3600", "gt", 36, for_="1h",
         summary="The last SparkHub backup is {{ humanize $values.A.Value }} hours old (nightly expected).",
         nodata="Alerting"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rules = {"apiVersion": 1, "groups": [{"orgId": 1, "name": "sparkhub", "folder": "SparkHub", "interval": "1m",
                                          "rules": RULES}]}
    contact = {"apiVersion": 1, "contactPoints": [{"orgId": 1, "name": "ntfy", "receivers": [{
        "uid": "ntfy-sparkhub", "type": "webhook", "disableResolveMessage": False,
        "settings": {"url": f"http://127.0.0.1:{HUB_PORT}/internal/grafana-alert", "httpMethod": "POST"}}]}]}
    policy = {"apiVersion": 1, "policies": [{"orgId": 1, "receiver": "ntfy", "group_by": ["alertname"],
                                             "group_wait": "30s", "group_interval": "5m", "repeat_interval": "6h"}]}
    for name, obj in (("rules", rules), ("contact-points", contact), ("policies", policy)):
        # JSON is valid YAML; Grafana reads .yaml provisioning files
        (OUT / f"{name}.yaml").write_text(json.dumps(obj, indent=1) + "\n")
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.yaml"))), f"({len(RULES)} rules)")


if __name__ == "__main__":
    main()
