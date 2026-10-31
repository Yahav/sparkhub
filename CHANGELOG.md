# Changelog

One section per release, newest first. `./install.sh upgrade` prints the sections between the installed version and
the one it moves to, so say here what an upgrade does to a running install: a model pin that changes (the model
reloads during the upgrade), new weights to download, a component bumped, a new question, anything to do by hand.

Versions follow [semantic versioning](https://semver.org): a major release may need a step by hand (it says which).

## 1.0.0 (2026-10-31)

First public release.

- Two modes on one DGX Spark, switched from the dashboard or the API: Fast (Qwen3.6-35B-A3B NVFP4, vLLM) and
  Quality (Qwen3.8-Flash-Next NVFP4, b12x kernels); a third, Custom, from a reviewed recipe.
- OpenAI-compatible gateway with per-user API keys, roles, per-key concurrency limits, usage history.
- Dashboard with password + authenticator-app sign-in, Grafana with alerts to ntfy, optional Arcane, chat-ui and
  embeddings.
- Benchmarks page: speed, Terminal-Bench 2.0, knowledge and reasoning tests, compared across models.
- Installer (`./install.sh`): local, Tailscale or public access, local CA or Let's Encrypt, host hardening.
- Versioned releases: the hub checks once a day for a newer release; `./install.sh upgrade` applies it, backs up
  the database first and offers to go back if the new release fails to start.
