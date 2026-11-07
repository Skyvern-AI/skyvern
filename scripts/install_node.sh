#!/bin/bash
set -euo pipefail

NVMRC_PATH="${1:-/app/.nvmrc}"
KEY_PATH="${2:-/tmp/nodesource-repo.gpg.key}"

NODE_VERSION="$(tr -d '[:space:]' < "$NVMRC_PATH")"
NODE_MAJOR="${NODE_VERSION%%.*}"

mkdir -p /etc/apt/keyrings
gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg < "$KEY_PATH"
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list
apt-get update

# NodeSource can publish multiple Debian package revisions for one Node release.
NODE_PACKAGE_VERSION="$(
    apt-cache madison nodejs | grep -F " ${NODE_VERSION}-" | head -n 1 | cut -d '|' -f 2 | tr -d '[:space:]' || true
)"
if [ -z "$NODE_PACKAGE_VERSION" ]; then
    echo "NodeSource node_${NODE_MAJOR}.x has no package for Node ${NODE_VERSION}; available:" >&2
    apt-cache madison nodejs >&2 || true
    exit 1
fi

apt-get install -y "nodejs=${NODE_PACKAGE_VERSION}"
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -f "$KEY_PATH"

if [ "$(node --version)" != "v${NODE_VERSION}" ]; then
    echo "installed $(node --version) but .nvmrc asks for v${NODE_VERSION}" >&2
    exit 1
fi
npm --version
