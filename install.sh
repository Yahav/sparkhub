#!/usr/bin/env bash
# SparkHub installer: see ./install.sh --help and docs/INSTALL.md.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
command -v python3 >/dev/null || { echo "python3 is needed (DGX OS ships it)" >&2; exit 1; }
exec python3 -I ops/install/installer.py "$@"
