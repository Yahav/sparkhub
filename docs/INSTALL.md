# Installing SparkHub on a DGX Spark

SparkHub turns a DGX Spark into a private model server: an OpenAI-compatible API, a dashboard, Grafana, and tools
for remote harnesses such as OpenCode. It has two model modes. You switch between them from the dashboard, and
only one fits in memory at a time.

| Mode | Model | Speed (benchmark) | Disk |
|---|---|---|---|
| Fast | qwen36: Qwen3.6-35B-A3B NVFP4, Apache-2.0 | ~105 tok/s single stream, ~60 per agent with 4 | 22 GiB |
| Quality | Flash: Qwen3.8-Flash-Next NVFP4 on b12x kernels ([ursuciprian's recipe](https://github.com/ursuciprian/qwen3.8-flash-next-1x-dgx-spark)), Qwen Community License 1.0 | ~58 tok/s single stream, ~21 per agent with 4 | ~101 GiB |

Optionally, an **embedding service** runs next to either mode (`/v1/embeddings`, `/v1/rerank`):
Qwen3-Embedding-0.6B and Qwen3-Reranker-0.6B on llama.cpp, Apache-2.0, 1.8 GiB on disk, ~3 GiB of memory each
while running. It is stateless (the hub stores no texts or vectors); clients keep their own index.

## What you need

- **A DGX Spark (GB10) on DGX OS**, with Docker, the NVIDIA runtime, Compose v2 and buildx. They are preinstalled.
  On a plain Ubuntu image with its `docker.io` package, add them with `sudo apt update && sudo apt install
  docker-compose-v2 docker-buildx` (the preflight check names what is missing).
- **Your normal user, in the `docker` group.** The installer asks for `sudo` only for the host pieces below, and
  shows each command before it runs.
- **~250 GB of free disk** for both modes, and network access to Hugging Face, Docker Hub and GitHub.
- **A way for clients to reach the Spark.** The installer asks which access mode you want and sets up the rest:

  | Mode | Who reaches the hub | Address | Certificate | What each device needs |
  |---|---|---|---|---|
  | `local` | this machine and the LAN | `https://<host>.local` (mDNS, announced by avahi) | the SparkHub local CA | trust `ca.crt` once (served at `http://<host>.local/ca.crt`) |
  | `tailscale` | devices on your tailnet; optionally LAN devices for the dashboard and API | `https://<host>.<tailnet>.ts.net` | a Tailscale certificate (Let's Encrypt), or the local CA | Tailscale; nothing else with the Tailscale certificate |
  | `public` | the internet, under your own domain | `https://your.domain` | Let's Encrypt | nothing; your router forwards 443 (and 80) to the Spark |

  - **Chat, Arcane and Grafana work under the mode's one address**, because their sign-in returns to a fixed URL.
    The dashboard and the API also answer under the other names (tailnet name, `<host>`, `<host>.local`, the LAN
    address) with the local CA certificate.
  - **`tailscale`:** the installer installs and connects Tailscale if needed. LAN devices without Tailscale get
    everything too when your router has a DNS entry for the tailnet name pointing at the Spark's LAN address
    (with the Tailscale certificate they then install nothing); without it, the dashboard and the API only. The Tailscale certificate needs
    "HTTPS Certificates" turned on once in the Tailscale admin console (DNS page); the installer waits for it. Its
    name appears in the public Certificate Transparency logs, like every Let's Encrypt name.
  - **`public`:** Let's Encrypt checks ownership either over port 80 (HTTP-01) or with a TXT record you add by hand
    (DNS-01). On the LAN the domain works when the router supports hairpin NAT (most do).
  - Right after the access questions the installer lists **what you need to do yourself** (router, DNS, Tailscale
    admin console, devices), before it installs anything. The self-test checks what it can from the Spark (router
    DNS entry, hairpin NAT) and repeats what is missing; at the end, **How users connect** gives the address and what
    each kind of device needs.

## Install

```bash
git clone https://github.com/Yahav/sparkhub.git ~/sparkhub && cd ~/sparkhub
./install.sh
```

The first run asks a few questions: which modes, where the weights go, the access mode (local, tailscale or
public, and only that mode's follow-up questions), and whether you want the optional pieces. It saves the answers to `sparkhub.conf`. Then it runs every step that is not done yet:

1. **Preflight:** GB10, Docker with the NVIDIA runtime, Compose and buildx, disk space, network. Then the two
   things that need you before the long downloads, so the rest runs unattended:
   - **The Quality license** (if Quality is chosen): the link, and whether you accept it. It is an agreement with
     the licensor, recorded in `sparkhub.conf`; the weights are not gated, so no Hugging Face account is needed.
   - **A Hugging Face token, optional:** downloads work anonymously at lower rate limits. A read-only token
     (https://huggingface.co/settings/tokens) is checked, kept in `data/hf-token` (mode 600) and used only for
     the installer's downloads. Press Enter to skip; you are not asked again (delete the file to be asked).
2. **Config:** generates `compose.yaml`, the nginx and Prometheus files and the Grafana alert files from
   `sparkhub.conf` and `templates/`. Don't edit the generated files; edit `sparkhub.conf` or the templates, then
   re-run.
3. **Data and certificate:**
   - `data/` (mode 700) and its secrets;
   - the local CA certificate, which covers every name and listen address;
   - `tailscale`: Tailscale installed and connected; `local`/`tailscale` with a `.local` name: avahi announces it.
4. **Images:** the pinned images (by digest), the hub image, and the Arcane and chat-ui images if enabled
   (chat-ui is built from a pinned release, ~10 min). With the Benchmarks page: the runner image (`bench/`, ~5 min),
   Terminal-Bench 2.0 at a pinned commit and the knowledge-test datasets (`data/bench`).
5. **Models.** Resumable, so if it stops just run it again. Expect an hour or more on a first install.
   - **Fast:** the weights at a pinned Hugging Face revision.
   - **Quality** (Qwen Community License 1.0, accepted in step 1):
     - pulls the recipe author's prebuilt image (vLLM fork + b12x kernels) by digest. The image's source was
       matched to git, but its compiled parts and seeded compile cache come from the author's build;
     - downloads the checkpoint (`ursuciprian/Qwen3.8-Flash-Next-NVFP4-GDN-MSE`) at a pinned revision, plus the two
       files of `local-inference-lab/Qwen3.8-Flash-Next-NVFP4` that b12x reads;
     - creates the runtime cache (`$MODELS_DIR/.runtime/quality`, compile and autotune results). The first load
       fills it and takes ~4 min; later loads ~2.5 min.

   - **Embeddings** (`EMBEDDINGS=1`): pulls llama.cpp's server image by digest and downloads the two GGUF files into
     `$MODELS_DIR/embed`.

   Every file is checked against `ops/models/manifests/`.
6. **Start:** `docker compose up -d`.
7. **Trusted certificate:**
   - `public`: certbot (installed with your OK), the Let's Encrypt certificate, and a hook that reloads nginx after
     each renewal;
   - `tailscale` with the Tailscale certificate: `tailscale cert` into `data/tls`, renewed by a daily systemd timer
     that reloads nginx when it changed.
8. **Arcane and chat-ui sign-in:** registers them with the hub's OIDC provider, so they use the SparkHub login.
   chat-ui also gets its own API key.
9. **First dashboard admin:** asks for a password; you enrol an authenticator app at first login.
10. **First model load:** about 4–5 minutes.
11. **Host pieces you chose:** SSH country allowlist, port guard, headless boot.
12. **Self-test:**
    - the TLS certificate verifies and the login page answers;
    - Grafana, Arcane, the chat and the NVIDIA dashboard need the login;
    - chat-ui answers (unless it is turned off on the Settings page);
    - internal endpoints are closed;
    - the API needs a key;
    - a chat completion and a stream work.

A second run on a finished install changes nothing. It is also the repair command: it redoes whatever is missing.

### Unattended

Copy `sparkhub.conf.example` to `sparkhub.conf` and edit it. Then run:

```bash
SPARKHUB_ADMIN_USER=you SPARKHUB_ADMIN_PASSWORD='at least 12 chars' ./install.sh
```

Without a terminal the token question is skipped; to use one, also set `HF_TOKEN=hf_...` (not stored).

Accepting the Quality license still needs an answer. Alternatively, put `qwen-community-1.0` and today's date in
`FLASH_LICENSE` (e.g. `FLASH_LICENSE="qwen-community-1.0 2026-10-28"`) once you have read the license. A date alone
was the acceptance of the earlier Quality model's NVIDIA license and is asked again.

## Questions and settings

### The questions

Asked in this order on the first run and by `./install.sh configure`. Enter keeps the value in brackets; on
`configure` that is your current answer.

| Question | Default | What it changes |
|---|---|---|
| Which modes? | both | `fast`, `quality` or both. Decides what is downloaded and what the dashboard can switch to. |
| Hugging Face cache directory | `~/.cache/huggingface` | Where the Quality checkpoint goes (~101 GiB). |
| Directory for the Fast weights | `~/models` | Fast in `<dir>/qwen36` (22 GiB), the embedding models in `<dir>/embed`. |
| Who should reach the hub? | `tailscale` when Tailscale is up, else `local` | The access mode (table under "What you need"). Only that mode's follow-ups are asked: |
| ↳ `local`: name LAN devices use | `<hostname>.local` | The address users open. A `.local` name needs no DNS. |
| ↳ `tailscale`: also serve LAN devices without Tailscale? | yes | Adds the LAN address, for the dashboard and API at `<hostname>.local`. |
| ↳ `tailscale`: use a Tailscale certificate? | yes | Publicly trusted, so devices install nothing. Its name appears in public certificate logs. No = the SparkHub certificate. |
| ↳ `public`: your domain name | (none) | Its DNS record must point at your router's public address. |
| ↳ `public`: Let's Encrypt check | `http` | `http`: port 80 must reach this machine and renewal is automatic. `dns`: you add a TXT record by hand, and again at each renewal. |
| HTTPS listen addresses | from the mode | The only addresses nginx accepts connections on (127.0.0.1 is always added). Nothing else is reachable from outside. |
| Docker UI (Arcane) at /arcane? | yes | Builds Arcane with base-path support from a pinned commit of a public fork (`github.com/Yahav/arcane`, branch `base-path`), ~10 min. To build something else, set `ARCANE_SOURCE` (a checkout or git URL) and `ARCANE_REF`. |
| Web chat (chat-ui) at /chat? | yes | Builds chat-ui (~10 min). Users sign in with their hub account. |
| Embedding service? | yes | `/v1/embeddings` and `/v1/rerank`, 1.8 GiB of weights. |
| Benchmarks page? | yes | Compares models (speed, Terminal-Bench, knowledge and reasoning tests). Builds the runner image (~5 min) and fetches Terminal-Bench and the test datasets into `data/bench`. |
| Proxy the NVIDIA DGX Dashboard at /nvidia? | yes if it runs on this machine | Behind the hub's login. |
| Allow SSH only from one country, the LAN and Tailscale? | no | Host firewall for SSH. Asks for the country code and your LAN. |
| Keep some forwarded ports closed to the internet (port guard)? | no | Host firewall for the listed ports (default `8000`), LAN and Tailscale still allowed. |
| Boot without the desktop (headless)? | no | Frees ~2 GB of memory. |

During the run, the installer also asks:

- **The Quality license** (default yes) and **a Hugging Face token** (optional), before any download (step 1 above).
- **The first dashboard admin:** user name (default: your user) and password.
- **`public` with `dns`:** to add the TXT record certbot shows, then press Enter.
- **Before every root command:** "Run it?" (default no). Declining skips that piece; a later run asks again.

### Changing a setting later

- **Answers to the questions above:** `./install.sh configure`, then `./install.sh`. Changing the access mode this
  way regenerates the nginx config and the certificate. It does not remove certbot or a Let's Encrypt certificate
  from an earlier `public` install; `sudo certbot certificates` lists them.
- **Anything else:** edit `sparkhub.conf`, then run `./install.sh`. It regenerates the config files and restarts
  only what they feed (it asks first when that would cut running requests). Settings you might change by hand:

| Setting | Default | Meaning |
|---|---|---|
| `HTTPS_PORT`, `HTTP_PORT` | `443`, `80` | nginx's ports. `HTTP_PORT="0"` turns plain HTTP off (no `ca.crt` download, no Let's Encrypt `http` check). |
| `LISTEN_ADDRS` | from the mode | As the question above. |
| `EXTRA_NAMES` | from the mode | More names nginx answers to with the SparkHub certificate. |
| `AUTOSTART` | `1` | `0`: the hub does not load a model when it starts. |
| `INSTANCE` | `sparkhub` | Names the containers and data. A second install on the same machine needs another name and other ports. |
| `HUB_PORT`, `PROMETHEUS_PORT`, `NODE_EXPORTER_PORT`, `GRAFANA_PORT`, `FAST_PORT`, `QUALITY_PORT`, `EMBED_PORT`, `RERANK_PORT`, `ARCANE_PORT`, `CHATUI_PORT` | `8090`, `9090`, `9100`, `3000`, `8001`, `18300`, `18310`, `18311`, `3552`, `3090` | Ports on 127.0.0.1 only; change them when something else uses one. |

`sparkhub.conf.example` lists every setting with a one-line explanation. Some are filled in by the installer and
are not meant to be edited: the user and group ids (`RUN_UID`, `RUN_GID`, `DOCKER_GID`), `TZ`,
`LETSENCRYPT_ISSUED`, `TAILSCALE_CERT_ISSUED` and `FLASH_LICENSE`.

## Day to day

| Command | What it does |
|---|---|
| `./install.sh --check` | Reports every step without changing anything; exits 1 if something is missing |
| `./install.sh selftest [--switch]` | The end-to-end check. `--switch` also switches modes and back (~10 min) |
| `./install.sh models --check --verify` | Hashes every weight file against the pinned manifests |
| `./install.sh upgrade [vX.Y.Z]` | Moves to the newest release (or the one given) and applies it; see [Upgrading](#upgrading) |
| `./install.sh update` | Applies the checkout as it is: re-renders, pulls images, builds what changed, downloads new weights, restarts what changed, then sets up whatever else is missing (e.g. a piece you just turned on in `sparkhub.conf`). It asks first if requests are running, and refuses while a benchmark runs |
| `./install.sh configure` | Asks the questions again |
| `./install.sh tls` | Gets or renews the certificate |
| `./install.sh uninstall [--purge]` | Removes the containers. Keeps `data/` and the weights unless `--purge` |

The dashboard's **Switch** page also shows whether each mode can be loaded. Before any switch, the hub checks that
the mode is installed, its image exists and its weights are complete. If not, the switch is refused with the reason,
and the running model is never stopped.

## Upgrading

Releases are git tags (`v1.2.0`) with notes in [`CHANGELOG.md`](../CHANGELOG.md). Once a day the hub asks GitHub for
the newest release (Settings → Updates; nothing about the install is sent, and it can be turned off). A newer one
shows a notice to admins on every page and sends one notification. To upgrade, on the Spark:

```bash
cd ~/sparkhub && ./install.sh upgrade
```

1. **Checks:** this is a git checkout without local changes to tracked files, and no benchmark is running.
2. **Shows** the release notes between your version and the new one, and asks.
3. **Backs up** the database to `data/backups/pre-upgrade-<from>-to-<to>-<time>.db`.
4. **Moves** the checkout to the release tag (fast-forwards your branch), then runs the *new* release's
   `./install.sh update`, so its own steps, pins and defaults for new questions apply.
5. **Prepares before restarting:** new images (pulled by digest, or built from a pinned commit, e.g. Arcane and
   chat-ui) and new model weights download while the current model keeps serving. If a step fails, nothing restarts.
6. **Restarts what changed.** Everything SparkHub runs is pinned in the repository (image digests, model revisions,
   source commits), so a release that bumps Grafana, Arcane or a model changes exactly those pins. When the release
   changed the loaded model (its image, weights or arguments), the hub reloads it right away (~4–5 min; it asked
   first if requests were running). Otherwise the running model is adopted as it is. A release with database
   changes migrates the database when the new hub starts (each migration all or nothing).
7. **Offers to go back if it fails.** If a step fails before the restart, it offers to move the checkout back
   (nothing was restarted). If the new hub does not come up, it offers to go back completely: the old release's code,
   the database from step 3, and the old stack restarted.

`./install.sh upgrade v1.1.0` moves to a given release, also an older one. Going back by hand does not undo database
changes: a hub refuses a database a newer release migrated (it says so in `docker compose logs hub`), and so does
`./install.sh update`; stop the stack and restore the pre-upgrade backup. Tracking the main branch instead of
releases also works: `git pull && ./install.sh update` backs the database up before a migration too
(`data/backups/pre-update-v<from>-to-v<to>-<time>.db`).

## What the installer changes on the host

Only these, each shown and confirmed before it runs:

| Piece | Commands | Undo |
|---|---|---|
| certbot (Let's Encrypt only) | `apt-get install certbot`, `certbot certonly …`, a deploy hook in `/etc/letsencrypt/renewal-hooks/deploy/` | `apt remove certbot` |
| Tailscale (`tailscale` mode, if missing) | Tailscale's install script, `tailscale up` | `apt remove tailscale` |
| Tailscale certificate (`TLS=tailscale`) | `ops/tailscale-cert` → `/usr/local/sbin`, `/etc/default/sparkhub-tailscale-cert`, a systemd service and daily timer | `./install.sh uninstall` offers it |
| avahi (`.local` name, if missing) | `apt-get install avahi-daemon libnss-mdns`, `systemctl enable --now avahi-daemon` | `apt remove avahi-daemon` |
| SSH allowlist (optional) | `ops/ssh-geo` → `/usr/local/sbin`, `/usr/local/lib/sparkhub-ssh-geo`, `/etc/default/sparkhub-ssh-geo`, a systemd service and weekly timer; nftables table `inet ssh_geo` | `./install.sh uninstall` offers it, or see `ops/ssh-geo/README.md` |
| Port guard (optional) | `/usr/local/lib/sparkhub-port-guard/port-guard.nft`, a systemd service; nftables table `inet port_guard` | the same |
| Headless boot (optional) | `systemctl set-default multi-user.target` | `sudo systemctl set-default graphical.target` |

Everything else lives under the repository directory (`data/`, `vendor/`) and in Docker: images, containers, and
one bridge network each for Arcane and chat-ui. The weights go in the two directories you chose.

## Troubleshooting

- **Let's Encrypt HTTP-01 fails although 443 works.** Something upstream (router, ISP, building firewall) does not
  forward port 80. Check that `http://<name>/.well-known/acme-challenge/` reaches the Spark. If it can't, set
  `TLS_CHALLENGE=dns` and run `./install.sh tls`: certbot shows a TXT record to add at your DNS provider. DNS-01
  done by hand does not renew itself. Grafana alerts 21 days before expiry; then repeat the command.
- **LAN clients can't reach the public name.** The router needs NAT loopback (hairpin), or a local DNS entry for the
  name that points at the Spark's LAN address.
- **`<host>.local` does not resolve on a device.** Older Android and some Linux setups don't resolve mDNS names. Add
  a DNS entry on the router for the name, or use the LAN address (with the local CA certificate).
- **The Tailscale certificate step waits.** Turn on HTTPS Certificates at https://login.tailscale.com/admin/dns, or
  answer `local` to use the SparkHub certificate. Renewal log: `journalctl -u sparkhub-tailscale-cert`.
- **A model fails to load, or the Spark runs out of memory.**
  - Only one mode fits: never start other GPU work while a model is loaded.
  - The dashboard's Logs page shows the model's log.
  - The hub retries once, then loads the previous mode back.
- **Hugging Face rate limits.** Export `HF_TOKEN` before `./install.sh models`.
- **Arcane image.** Serving Arcane under `/arcane` needs base-path support, which upstream Arcane doesn't have yet.
  The installer builds it from a pinned commit of a public fork (`ARCANE_SOURCE` empty), or from `ARCANE_SOURCE`
  (a checkout or git URL) when set. Once upstream ships sub-path support, the official image works with the same
  settings.
- **chat-ui.** `docker compose logs chatui`. If the chat shows no model, its API key may have been revoked:
  `./install.sh` creates a new one. The build needs Docker Hub (`node`, `mongo`) and npm.
- **SSH allowlist.** The country list comes from the RIPE NCC registry: Europe, the Middle East and Central Asia.
  Keep a second session open while it is applied. Tailscale and the LAN are always allowed.
- **A second install on the same machine,** for testing an update: clone into another directory, and write a
  `sparkhub.conf` with another `INSTANCE`, other ports, `LISTEN_ADDRS="127.0.0.1"`, `AUTOSTART=0`, `ARCANE=0` and `CHATUI=0`.
  The two installs never touch each other's model containers (`sparkhub.instance` label). Don't switch modes in the
  test install while the main one has a model loaded: there is memory for one model only.
