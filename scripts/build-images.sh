#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -t demo-app:dev apps/demo-app
docker build -t dfc-controller:dev controller
kind load docker-image demo-app:dev dfc-controller:dev --name dfc-lab
