# SparkHub architecture

How SparkHub is put together: the repository layout, the pieces at run time, and the API. Installing is in
[`INSTALL.md`](INSTALL.md); upgrading in [`INSTALL.md#upgrading`](INSTALL.md#upgrading).

## Layout

| Path | What |
|---|---|
| `install.sh`, `ops/install/` | The installer (stdlib Python): questions → `sparkhub.conf`, render, models, TLS, host pieces, self-test, upgrade |
| `VERSION`, `CHANGELOG.md` | The release this checkout is, and what each release changes (`./install.sh upgrade` prints it) |
| `sparkhub.conf` | This machine's answers (not in git); `sparkhub.conf.example` documents every key |
| `templates/` | Sources of the generated files: `compose.yaml`, `nginx/*.conf`, `nginx/sparkhub-locations.inc`, Prometheus, Grafana datasource. **Edit these (or `sparkhub.conf`) and run `./install.sh render`**; the generated copies are not in git |
| `compose.yaml` | Generated. The stack: nginx, hub, Prometheus, node_exporter, Grafana, Arcane, chat-ui (host network except Arcane and chat-ui; only nginx listens beyond 127.0.0.1) |
| `nginx/` | Generated config (listen addresses from `LISTEN_ADDRS`) plus `proxy-headers.inc` |
| `bench/` | The benchmark runner image (Harbor and lm-evaluation-harness at pinned versions, `runner.py`, the Terminal-Bench task lists), built by the installer when `BENCH=1` |
| `hub/` | The hub (FastAPI): gateway `/v1`, control API `/api/v1`, dashboard, `sparkhub-admin` CLI, tests |
| `presets/` | Mode specs captured from the benchmark (`fast.json`, `quality.json`, each with its `weights` directory) and the Flash chat template. The third mode, Custom, has no file: its spec is an imported recipe in the database |
| `ops/models/` | Pinned model specs (`models.json`: revisions, recipe commit, licenses) and manifests (file sizes and hashes) |
| `observability/` | Prometheus config, Grafana provisioning and dashboards (`build_dashboards.py` writes the JSON) |
| `clients/` | `sparkhub` CLI (single file, stdlib only) and the OpenCode skill; the hub serves both under `/clients/`, plus a generated `opencode.json` and the CA certificate |
| `docs/benchmark-summary.md` | The benchmark report the model choice and presets are based on |
| `ops/` | Host firewall pieces: SSH country allowlist (`ssh-geo`), port guard (`port-guard`); `tls/make-local-cert.sh` (local CA); `tailscale-cert` (Tailscale certificate renewal); backups README |
| `data/` | State, not in git (the database migrates itself at the hub's start: `SCHEMA` in `hub/sparkhub/db.py`): SQLite, saved model logs, Prometheus and Grafana data (the ntfy topic now lives in SQLite, set on the Settings page) |

## Run

```bash
cd sparkhub
./install.sh --check        # everything in place?
./install.sh upgrade        # move to the newest release and apply it
./install.sh update         # apply the checkout as it is: render, build, restart what changed (asks if requests run)
docker compose logs -f hub
```

The hub listens on `127.0.0.1:8090`. Model containers (`sparkhub-fast` :8001, `sparkhub-quality` :18300) are
created by the hub with restart policy `no` and ports on 127.0.0.1 only. Restarting the hub adopts a running model
instead of reloading it, unless a release changed what that mode runs: each model container carries a hash of its
preset (`sparkhub.pin` label: image, weights, arguments), and a hub that finds a different one reloads the model.

### Custom mode (third model)

An admin can add a third mode, **Custom**, from a [sparkrun](https://github.com/spark-arena/sparkrun) recipe in the Custom
section of the dashboard's Models page. Code: `hub/sparkhub/recipes.py` (translation and lookups), `custom.py`
(lifecycle), `fetch_weights.py` (download helper).

1. **Import:** a GitHub recipe URL, a recipe picked from a registry (Spark Arena's are listed but off by default), or
   pasted YAML. Only vLLM recipes for one Spark with an explicit command, and no `pre_exec`/`post_exec` hooks.
   Import reads metadata only. It pins the image to its digest and the model to a commit, and gathers the review
   facts. sparkrun **mods** (patch scripts run before the server) are found the way sparkrun finds them (next to
   the recipe, the recipe's registry, else `spark-arena/eugr-recipes/mods`), pinned to a commit, and their files
   stored with the recipe. A mod that would use the network at launch (curl/wget, pip/apt, git, HF downloads,
   network code in Python) is refused.
2. **Review and approve:** the page shows the exact container (image, env, mounts, command with risky lines
   highlighted), flags for code from downloads, the license, a memory estimate and a checklist. Approval needs a
   fresh authenticator code and goes into the event log.
3. **Download and verify:** the image is pulled by digest; every model file (including other repos the command
   points at) is fetched into the HF cache by a helper container and checked against Hugging Face's sha256.
   Mods then run once in a throwaway container (`sparkhub-modcheck`, same image and isolated network); a failure
   stops the recipe here.
4. **Assign and load:** Custom loads through the normal switch, on the internal Docker network
   `sparkhub-isolated` (no route out, no published port; the hub talks to the container IP), with
   `HF_HUB_OFFLINE=1`, the HF cache read-only, and a Docker volume `sparkhub-custom-<id>-runtime` for compile caches.
   Mod files are copied into each new container (`/tmp/sparkhub-mods`, then `/workspace/mods/<name>` as sparkrun
   does) and run before the server; they are checked against the approval first.
   The first load records real memory, load time and KV size; smoke checks run from the recipe page.

Clients use the model `custom` (or the recipe's served name) once a recipe is assigned. Recipes live in the
database only (`recipes` table; nothing in `presets/`), and `./install.sh` doesn't recreate them on a new machine.
GitHub sources and mods are checked daily for changes; an update is shown as a diff and imported only on request.

### Benchmarks

The **Benchmarks** page (admins; installer question `BENCH`) measures Fast, Quality or Custom the same way, so a
Custom recipe can be compared with the other two. Code:
`hub/sparkhub/bench.py` (scheduling, blocking, speed suite, results) and `bench/runner.py` (the rest).

- **Suites.** Always: speed (measured by the hub: tokens per second and time to first token at 1, 2 and 4 requests,
  2k and 16k prompts) and Terminal-Bench 2.0, 15 tasks (`bench/tb2-quick.txt`). Optional: all 53 Terminal-Bench
  tasks that run on arm64, knowledge (MMLU-Pro CS and engineering, MMLU security and college CS, IFEval) and
  reasoning (BBH, with and without thinking), rescored with a format-tolerant answer extractor.
- **A run** starts now or at a time; runs never overlap. At its start the hub loads the run's mode if another one is
  loaded (the only automatic switch), then refuses switching and answers every other key with 503 until the end.
- **The runner** (`<instance>-bench`) is started by the hub for the run's Terminal-Bench and lm-eval suites and
  removed after. It drives Docker through the socket (Harbor builds one container per task) and reaches the model
  through nginx with a key made for the run and revoked after it. Without "internet", the agents reach only the hub
  and package registries (apt, PyPI, npm, Go, crates.io, Maven), through Harbor's egress allowlist.
- **Stored:** numbers in the `bench_runs` table; logs and per-task files in `data/bench/runs/<id>/` for 30 days.
  Test data: `data/bench/tb2` (Terminal-Bench at a pinned commit), `data/bench/hf` and `nltk` (lm-eval datasets).

### Access

Everything is under `https://<PUBLIC_HOST>` (from `sparkhub.conf`): dashboard at `/`, API at `/v1`, Grafana at
`/grafana/`, Arcane (Docker UI) at `/arcane/`, the NVIDIA DGX Dashboard at `/nvidia/`. All need the SparkHub login;
Grafana and Arcane then sign you in from it (Arcane through the hub's OIDC provider at `/oidc`,
`hub/sparkhub/oidc.py`: hub admins become Arcane admins), the NVIDIA dashboard asks for its own.
OIDC clients: `sparkhub-admin oidc add-client|list|remove`; Arcane's secret is `OIDC_CLIENT_SECRET` in `data/arcane.env`.
Arcane needs a build with base-path support: by default the installer builds a pinned commit of the public fork `github.com/Yahav/arcane` (`ARCANE_SOURCE` overrides it, see `docs/INSTALL.md`).

**Chat** (`CHATUI=1`): [Hugging Face chat-ui](https://github.com/huggingface/chat-ui) at `/chat/`, a ChatGPT-style
web chat for the hub's users.
- **Sign-in:** behind the hub login like the admin tools, then through the hub's OIDC provider. Each hub user has
  their own history; a new hub login (another user, or after logging out) ends chat-ui's session too.
- **Model:** it talks to `/v1` with its own `infer` key (named `chatui`, so its requests show under that key in
  Activity) and offers only `auto` and `auto-thinking`: whatever mode is loaded.
- **On/off:** Settings → Chat, or `sparkhub-admin chat on|off|status`. Off stops the container and closes `/chat/`;
  the history stays in `data/chatui` (MongoDB inside the container) and is in the nightly backup.
- **Privacy:** nginx sends a Content-Security-Policy with `/chat/`, so the browser loads nothing from other sites
  (chat-ui would otherwise fetch avatars from huggingface.co). The server only talks to the hub.
- **Build:** the installer builds the image from a pinned chat-ui release with `APP_BASE=/chat` (the prefix is
  compiled in). Secrets: `data/chatui.env`.

**Access mode** (`ACCESS`, asked by the installer; `docs/INSTALL.md` has the table): `local` (LAN, `https://<host>.local`),
`tailscale` (the tailnet name; LAN devices optionally get the dashboard and API) or `public` (your domain). Chat,
Arcane and Grafana work under `PUBLIC_HOST` only, since their sign-in returns to one fixed URL; the dashboard and
the API answer under every name in `EXTRA_NAMES` too.

With a Let's Encrypt certificate (`public`) or a Tailscale one (`tailscale`, `TLS=tailscale`, renewed by
`sparkhub-tailscale-cert.timer`) nothing needs installing on clients for `PUBLIC_HOST`.
The other names, and `PUBLIC_HOST` with `TLS=local-ca`, use a local SparkHub CA. Trust `data/tls/ca.crt`
once per client machine (outside `public` mode, nginx also serves it at `http://<name>/ca.crt`):
- **Windows (Chrome/Edge):** copy `ca.crt` over, double-click → Install Certificate → Current User →
  "Trusted Root Certification Authorities". Or: `certutil -addstore -user Root ca.crt`.
- **Firefox:** Settings → Certificates → View Certificates → Authorities → Import.
- **OpenCode / Node tools:** `NODE_EXTRA_CA_CERTS=/path/to/ca.crt`. **curl:** `--cacert ca.crt`. **Python:** `SSL_CERT_FILE`.

The local server certificate (180 days) is renewed by `./install.sh` once fewer than 30 days are left
(`./install.sh --check` shows the days).

### Harness tooling

The dashboard's **Connect** page has the steps. In short, on the client machine:

```bash
curl -fsS --cacert ca.crt -o ~/.local/bin/sparkhub https://<PUBLIC_HOST>/clients/sparkhub && chmod +x ~/.local/bin/sparkhub
export SPARKHUB_URL=https://<PUBLIC_HOST> SPARKHUB_API_KEY=shk_… SPARKHUB_CA=~/.config/sparkhub/ca.crt
sparkhub status | modes | switch quality [--drain] | cancel | watch
sparkhub install-opencode      # skill + provider (limits per mode) + MCP server into ~/.config/opencode
export NODE_EXTRA_CA_CERTS=$SPARKHUB_CA   # for OpenCode while the local CA is in use
```

### Admin CLI

On the Spark. It works on the database directly, so it also works when nobody can log in:

```bash
docker exec -it sparkhub-hub sparkhub-admin user create|list|reset-password|reset-totp|unlock|delete …
docker exec -it sparkhub-hub sparkhub-admin key create laptop --scope switch   # infer | switch | admin
docker exec -it sparkhub-hub sparkhub-admin key list|revoke|revoke-all
docker exec -it sparkhub-hub sparkhub-admin oidc add-client|list|remove
docker exec -it sparkhub-hub sparkhub-admin chat on|off|status
docker exec -it sparkhub-hub sparkhub-admin backup
```

## API in brief

- `POST /v1/chat/completions`, `/v1/completions`, `GET /v1/models` with `Authorization: Bearer shk_…`.
  Models: `auto` (whatever is loaded), `fast`, `quality`, `custom` (once a recipe is assigned), and `-thinking`
  variants. A request for the mode that isn't loaded gets `409` and never triggers a switch.
- `GET /api/v1/status` · `POST /api/v1/mode {"mode": "quality", "drain": false, "force": false}` ·
  `POST /api/v1/mode/cancel` · `GET|PUT /api/v1/modes/{mode}/settings` · `GET /api/v1/modes/{mode}/logs` ·
  `GET|POST /api/v1/keys` · `DELETE /api/v1/keys/{id}` · `GET /api/v1/activity` · `GET /api/v1/events`.
- A switch is refused (`409`, with the list) while other keys used the model in the last 5 minutes;
  `drain` waits until they have been quiet for 2 minutes; `force` (admin) switches at once.
- Embeddings (when installed, `EMBEDDINGS=1`): `POST /v1/embeddings` (OpenAI format; model
  `qwen3-embedding-0.6b`, 1024 dimensions, `dimensions` 32–1024 shortens, `encoding_format` float or base64) and
  `POST /v1/rerank` (`{"query", "documents", "top_n"}`; model `qwen3-reranker-0.6b`). Any key may use them. They run
  next to either mode and never depend on it. Run mode on Settings → Models: always on, on-demand (starts in ~2 s,
  stops when idle) or off (`503`). Large embedding requests wait while chat requests are running (at most 60 s).
  For search queries, prefix the query (not the documents) with
  `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:`.

## Tests

```bash
cd hub && ../.venv/bin/pytest -q      # fake vLLM + fake runtime: no GPU, no Docker, ~4 min
cd .. && .venv/bin/pytest -q ops/install  # installer: templates for every TLS/feature combination
./install.sh selftest                             # the running install, end to end
```
