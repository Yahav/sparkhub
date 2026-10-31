# Releasing

For maintainers. A release is a git tag `vX.Y.Z` plus a GitHub release with the same name: installs find it through
the GitHub releases API (`/releases/latest`, which skips drafts and pre-releases), and `./install.sh upgrade`
checks out the tag.

## What goes into a release

Everything an install runs is pinned in this repository, so an upgrade replays exactly what changed:

| To change | Edit | What an upgrade then does |
|---|---|---|
| Fast or Quality (image, weights, arguments) | `presets/<mode>.json`, and for new weights `ops/models/models.json` plus a manifest (`ops/models/make_manifest.py`) | downloads the weights and pulls the image before restarting, then reloads that mode if it is loaded |
| Default model settings | `defaults` in `presets/<mode>.json` | applies on the next load (settings an admin changed stay) |
| Prometheus, node_exporter, Grafana, nginx | the image digest in `templates/compose.yaml` (keep the version comment) | pulls the image, recreates that container |
| Arcane, chat-ui | `ARCANE_PIN` / `CHATUI_PIN` in `ops/install/installer.py` | builds the new commit, recreates the container |
| The hub, the benchmark runner | `hub/`, `bench/` | rebuilds the image, restarts the hub (adopting the running model) |
| Terminal-Bench tasks | `TB2_PIN` and `BENCH_PREPARED` in `ops/install/installer.py` | fetches the new tasks |
| A benchmark suite's settings | `BENCH_VERSION` in `hub/sparkhub/bench.py` | new results are not compared with older ones |
| The database | append a migration to `SCHEMA` in `hub/sparkhub/db.py`: SQL, or a Python function for a backfill SQL can't express (never edit one that shipped) | backs up the database, migrates it at the hub's start; `hub/tests/test_migrations.py` migrates every older version with data |
| A new installer question | `QUESTIONS`, plus a default in `detect_defaults_only()` | upgraded installs get the default without being asked |

Pin images by digest, never by tag, and source code by commit.

## Steps

1. Bump `VERSION` ([semantic versioning](https://semver.org): patch for fixes, minor for features and pin bumps,
   major when an install needs a step by hand).
2. Add a section on top of `CHANGELOG.md`, `## X.Y.Z (YYYY-MM-DD)`. Say what an upgrade does to a running install:
   which model reloads, how much it downloads, anything to do by hand. `./install.sh upgrade` prints this before
   asking.
3. Run the tests (`cd hub && ../.venv/bin/pytest -q`, `.venv/bin/pytest -q ops/install`); they also check that
   `VERSION` and the changelog's top section agree.
4. Upgrade a test install from the previous release: `./install.sh upgrade vX.Y.Z` on a checkout of the previous
   tag.
5. Commit, tag and publish:

   ```bash
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin main vX.Y.Z
   gh release create vX.Y.Z --title vX.Y.Z --notes "<this release's CHANGELOG.md section>"
   ```

Installs see the release within a day (Settings → Updates → Check now to see it at once).
