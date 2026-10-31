# SparkHub backups

The hub writes a backup every night (00:00–01:00 UTC), and at startup if the last one is
more than 30 hours old, to `data/backups/YYYY-MM-DD/` (mode 700). The 14 newest are kept. Grafana alerts if the
newest is more than 36 hours old; a failed backup also sends an ntfy push.

| File | Contents |
|---|---|
| `sparkhub.db` | Users (password hashes, TOTP secrets, recovery-code hashes), API keys (hashes), mode settings, activity, events |
| `grafana.db` | Grafana's own state (users created by the auth proxy, alert state, any UI changes) |
| `arcane.db`, `arcane.francis.db` | Arcane's state, if Arcane is installed |
| `chatui.archive.gz` | chat-ui's MongoDB (everyone's chat history), a `mongodump` taken while it runs |
| `chatui-db.tar.gz` | instead of the archive while chat-ui is turned off: a copy of `data/chatui` |
| `secrets.tar.gz` | `data/tls/` (local CA key and certificate, tailnet server certificate), `data/oidc/` (the OIDC signing key), `data/arcane.env`, `data/chatui.env`, and `data/ntfy-topic` if present |

Not backed up, on purpose: everything in git (compose, presets, nginx, dashboards, alert rules), Prometheus
history (large, re-creatable), model weights and images (re-downloadable and checked against pinned manifests: `./install.sh models`),
and `/etc/letsencrypt` (root-owned; re-issued with certbot).

**These copies live on the same disk as the data.** They protect against mistakes and corruption, not against
losing the disk. For that, copy `data/backups/` elsewhere, e.g. `rsync -a ~/sparkhub/data/backups/ otherhost:sparkhub-backups/`.

Backup now: `docker exec sparkhub-hub sparkhub-admin backup`

## Restore

```bash
cd ~/sparkhub
B=data/backups/2026-10-24                      # the backup to restore
docker compose stop hub grafana
cp data/sparkhub.db data/sparkhub.db.before-restore
cp "$B/sparkhub.db" data/sparkhub.db && rm -f data/sparkhub.db-wal data/sparkhub.db-shm
cp "$B/grafana.db" data/grafana/grafana.db
tar -xzf "$B/secrets.tar.gz" -C data            # only if data/tls or ntfy-topic were lost
docker compose up -d hub grafana                 # the hub adopts the running model
```

chat-ui's history, from an archive (chat-ui running):

```bash
docker exec -i sparkhub-chatui mongorestore --archive --gzip --drop < "$B/chatui.archive.gz"
```

or from a directory copy: `docker compose stop chatui`, replace `data/chatui` with the copy
(`tar -xzf "$B/chatui-db.tar.gz" -C data`), `docker compose start chatui`.
