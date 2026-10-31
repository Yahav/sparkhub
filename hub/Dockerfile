# SparkHub hub. Base pinned by digest (python:3.12-slim-bookworm, 2026-10).
FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY pyproject.toml .
COPY sparkhub ./sparkhub
RUN pip install --no-deps . && useradd -u 1000 -m sparkhub
USER 1000:1000
EXPOSE 8090
CMD ["uvicorn", "sparkhub.main:app_factory", "--factory", "--host", "127.0.0.1", "--port", "8090", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--timeout-keep-alive", "75", "--no-server-header"]
