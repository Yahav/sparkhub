# SparkHub benchmark runner: Harbor (Terminal-Bench) and lm-eval (knowledge, reasoning), started
# by the hub for one run and removed after. Versions are the ones the original benchmark used on the Spark.
# Docker CLI, compose and buildx: Harbor drives the host's Docker through the mounted socket.
FROM docker@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d AS docker-cli

# python:3.12-slim-bookworm, the hub's base
FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
RUN apt-get update && apt-get install -y --no-install-recommends git patch sqlite3 && rm -rf /var/lib/apt/lists/*
# two venvs: Harbor and lm-eval pin different versions of shared packages
COPY harbor-requirements.txt lmeval-requirements.txt /bench/
RUN python -m venv /opt/harbor && /opt/harbor/bin/pip install -r /bench/harbor-requirements.txt \
 && python -m venv /opt/lmeval && /opt/lmeval/bin/pip install -r /bench/lmeval-requirements.txt
# Harbor's egress sidecar (agents without internet) drops DNS on hosts with systemd-resolved; this patch fixes it
COPY harbor-network-policy.patch /bench/
RUN cd /opt/harbor/lib/python3.12/site-packages/harbor/environments/docker/harbor-docker-egress-control-sidecar \
 && patch -p1 < /bench/harbor-network-policy.patch
COPY runner.py lmeval_run.py lmeval_rescore.py tb2-quick.txt tb2-full.txt /bench/
ENV PATH=/opt/harbor/bin:$PATH
ENTRYPOINT ["python", "/bench/runner.py"]
