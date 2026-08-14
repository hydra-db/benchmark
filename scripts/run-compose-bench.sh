#!/usr/bin/env bash
# Run a completely fresh Docker/Bolt comparison. The in-process MinIO harness
# remains the kernel benchmark; this script measures deployed client paths.
set -euo pipefail

cd "$(dirname "$0")/.."

target="${1:-bench}"
case "$target" in
  verify|bench) ;;
  *) echo "usage: $0 [verify|bench]" >&2; exit 2 ;;
esac

run_id="${RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}"
project="turbolay-falkor-${run_id//[^a-zA-Z0-9]/}"
export RUN_ID="$run_id"

cleanup() {
  docker compose --project-name "$project" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose --project-name "$project" up --build --abort-on-container-exit \
  --exit-code-from "$target" "$target"
