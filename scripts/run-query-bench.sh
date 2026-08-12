#!/usr/bin/env bash
# Run the engine's query_bench example against a throwaway MinIO container.
#
# This replaces two scripts that used to live in the engine repo,
# minio_query_bench.sh and query_bench.sh, so the benchmark harness lives with
# the benchmark results rather than with the engine.
#
# The benchmark program itself is bench/ in this repo, vendored from what used
# to be the engine's examples/query_bench.rs. The engine is a git dependency, so
# nothing is needed from an engine checkout unless you want to build against a
# local one.
#
# Usage:
#   ./scripts/run-query-bench.sh                       # clones the engine, runs
#   HYDRADB_SRC=~/hydradb ./scripts/run-query-bench.sh  # use a local checkout
#   BENCH_FANOUTS=100,1000 BENCH_HOPS=1,5 ./scripts/run-query-bench.sh
#   BENCH_RESULTS=out.csv BENCH_CONCURRENCY=32 ./scripts/run-query-bench.sh
#
# Requires Docker, git and a Rust toolchain. On macOS run scripts/setup-macos.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

# Engine source. The benchmark itself lives in bench/ as its own crate and
# consumes the engine as a git dependency, so nothing needs to be on disk.
#
# HYDRADB_SRC builds against a local engine checkout instead. That copies the
# crate to a scratch directory and swaps the dependency for a path, because
# Cargo's own override mechanisms ([patch] and paths) still resolve the original
# git source first, which fails while the engine repo is private.
ENGINE_BRANCH="${HYDRADB_REF:-main}"
SRC="${HYDRADB_SRC:-}"
CACHE="${HYDRADB_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/hydradb-bench}"

RESULTS="${BENCH_RESULTS:-bench-results/query_bench.csv}"
LOG="${BENCH_LOG:-${RESULTS%.csv}.log}"
# Used as given. The script cds to the repo root above, so a relative path lands
# in the repo and an absolute one is left alone. An earlier version prefixed the
# repo path unconditionally, which turned BENCH_RESULTS=/tmp/x.csv into
# <repo>/tmp/x.csv.

# Sweep
FANOUTS="${BENCH_FANOUTS:-50,100,1000,5000,10000}"
HOPS="${BENCH_HOPS:-1,5,10,15,20}"
DATA_HOPS="${BENCH_DATA_HOPS:-20}"
COLD_ITERS="${BENCH_COLD_ITERS:-5}"
HOT_ITERS="${BENCH_HOT_ITERS:-9}"
CONCURRENCY="${BENCH_CONCURRENCY:-8}"
CONCURRENT_ITERS="${BENCH_CONCURRENT_ITERS:-16}"
PAGE_SIZE="${BENCH_PAGE_SIZE:-64}"
WORKLOADS="${BENCH_WORKLOADS:-all}"
MODE="${BENCH_MODE:-full}"
INDEX_POLICY="${BENCH_INDEX_POLICY:-outbound-only}"
BULK_CHUNK="${BENCH_BULK_CHUNK_SIZE:-10000}"
# Defaults to 1 in the engine's own script. One shared slot was the obvious
# suspect for the concurrency plateau, so it is exposed here for the control test.
MATRICES="${BENCH_MAX_GRAPHBLAS_MATRICES:-1}"
ADJACENCIES="${BENCH_MAX_MATRIX_ADJACENCIES:-0}"
RUNTIME="${BENCH_RUNTIME:-multi-thread}"
RUNTIME_WORKERS="${BENCH_RUNTIME_WORKERS:-}"
FEATURES="${BENCH_FEATURES:-opencypher}"

# MinIO. A fresh container and bucket per invocation, so a run never reads state
# another run left behind.
NAME="${BENCH_MINIO_NAME:-hydradb-bench-minio}"
NETWORK="${BENCH_MINIO_NETWORK:-hydradb-bench-net}"
PORT="${BENCH_MINIO_PORT:-19012}"
ACCESS_KEY="${BENCH_MINIO_ACCESS_KEY:-bench$(date +%s)$$}"
SECRET_KEY="${BENCH_MINIO_SECRET_KEY:-bench-secret-$(date +%s)-$$}"
BUCKET="${BENCH_MINIO_BUCKET:-query-bench-$(date +%s)-$$}"
# MinIO's Homebrew build segfaults on macOS 26 (a cgo crash in go-m1cpu), which
# is why this uses the container image rather than a local binary.
MINIO_IMAGE="${BENCH_MINIO_IMAGE:-minio/minio:RELEASE.2025-07-23T15-54-02Z}"
MC_IMAGE="${BENCH_MC_IMAGE:-minio/mc:RELEASE.2025-04-16T18-13-26Z}"

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon is not running" >&2; exit 1; }
command -v cargo >/dev/null || { echo "cargo is required" >&2; exit 1; }

CRATE="$REPO/bench"
if [ -n "$SRC" ]; then
  [ -f "$SRC/Cargo.toml" ] || { echo "no Cargo.toml at HYDRADB_SRC=$SRC" >&2; exit 1; }
  CRATE="$CACHE/bench-local"
  mkdir -p "$CRATE/src"
  cp "$REPO/bench/src/main.rs" "$CRATE/src/main.rs"
  sed -e "s|git = \"https://github.com/hydra-db/hydradb.git\", branch = \"main\"|path = \"$SRC\"|" \
      "$REPO/bench/Cargo.toml" > "$CRATE/Cargo.toml"
  echo "==> building against local engine at $SRC"
  ENGINE_REV="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo local)"
else
  echo "==> building against the engine at $ENGINE_BRANCH"
  ENGINE_REV="$ENGINE_BRANCH"
fi

export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/hydradb-bench-target}"
( cd "$CRATE" && cargo build --release --quiet ) || {
  echo "build failed. If the engine repo is not public yet, set HYDRADB_SRC to a" >&2
  echo "local checkout." >&2; exit 1; }
BIN="$CARGO_TARGET_DIR/release/query-bench"
[ -x "$BIN" ] || { echo "no binary at $BIN" >&2; exit 1; }
echo "==> engine $ENGINE_REV"

ENV_FILE="$(mktemp)"
cleanup() {
  rm -f "$ENV_FILE"
  if [ "${BENCH_KEEP_MINIO:-0}" != "1" ]; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "==> starting MinIO on 127.0.0.1:$PORT"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker network rm "$NETWORK" >/dev/null 2>&1 || true
docker network create "$NETWORK" >/dev/null
docker run --detach --name "$NAME" --network "$NETWORK" \
  --publish "127.0.0.1:$PORT:9000" \
  --env "MINIO_ROOT_USER=$ACCESS_KEY" \
  --env "MINIO_ROOT_PASSWORD=$SECRET_KEY" \
  "$MINIO_IMAGE" server /data >/dev/null

mc() { docker run --rm --network "$NETWORK" --entrypoint /bin/sh "$MC_IMAGE" -c "$1"; }
ready=0
for _ in $(seq 1 60); do
  if mc "mc alias set local 'http://$NAME:9000' '$ACCESS_KEY' '$SECRET_KEY' >/dev/null" >/dev/null 2>&1
  then ready=1; break; fi
  sleep 1
done
[ "$ready" = 1 ] || { echo "MinIO did not become ready" >&2; docker logs "$NAME" >&2; exit 1; }
mc "mc alias set local 'http://$NAME:9000' '$ACCESS_KEY' '$SECRET_KEY' >/dev/null && \
    mc mb --ignore-existing 'local/$BUCKET'" >/dev/null

# CLOUD_PROVIDER=aws selects the S3 protocol, not Amazon. AWS_ENDPOINT points it
# at the container.
cat >"$ENV_FILE" <<ENV
CLOUD_PROVIDER=aws
AWS_ACCESS_KEY_ID=$ACCESS_KEY
AWS_SECRET_ACCESS_KEY=$SECRET_KEY
AWS_DEFAULT_REGION=us-east-1
AWS_ENDPOINT=http://127.0.0.1:$PORT
AWS_BUCKET=$BUCKET
AWS_ALLOW_HTTP=true
AWS_VIRTUAL_HOSTED_STYLE_REQUEST=false
ENV

mkdir -p "$(dirname "$RESULTS")"
[ "${BENCH_APPEND:-0}" = "1" ] || rm -f "$RESULTS" "$LOG"

# One cargo invocation per fanout, results concatenated with a single header.
# The example writes a complete CSV each time, so every run after the first has
# its header line dropped.
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/hydradb-bench-target}"
IFS=',' read -ra fanouts <<< "$FANOUTS"
for fanout in "${fanouts[@]}"; do
  echo "==> fanout=$fanout hops=$HOPS concurrency=$CONCURRENCY matrices=$MATRICES"
  tmp="$(mktemp)"
  GRAPH_QUERY_BENCH_OBJECT_ENV="$ENV_FILE" \
  GRAPH_QUERY_BENCH_FANOUTS="$fanout" \
  GRAPH_QUERY_BENCH_HOPS="$HOPS" \
  GRAPH_QUERY_BENCH_DATA_HOPS="$DATA_HOPS" \
  GRAPH_QUERY_BENCH_COLD_ITERS="$COLD_ITERS" \
  GRAPH_QUERY_BENCH_HOT_ITERS="$HOT_ITERS" \
  GRAPH_QUERY_BENCH_INDEX_POLICY="$INDEX_POLICY" \
  GRAPH_QUERY_BENCH_BULK_CHUNK_SIZE="$BULK_CHUNK" \
  GRAPH_QUERY_BENCH_WORKLOADS="$WORKLOADS" \
  GRAPH_QUERY_BENCH_MODE="$MODE" \
  GRAPH_QUERY_BENCH_MAX_GRAPHBLAS_MATRICES="$MATRICES" \
  GRAPH_QUERY_BENCH_MAX_MATRIX_ADJACENCIES="$ADJACENCIES" \
  GRAPH_QUERY_BENCH_RUNTIME="$RUNTIME" \
  GRAPH_QUERY_BENCH_RUNTIME_WORKERS="$RUNTIME_WORKERS" \
  GRAPH_QUERY_BENCH_CONCURRENCY="$CONCURRENCY" \
  GRAPH_QUERY_BENCH_CONCURRENT_ITERS="$CONCURRENT_ITERS" \
  GRAPH_QUERY_BENCH_PAGE_SIZE="$PAGE_SIZE" \
    "$BIN" > "$tmp" 2>> "$LOG"
  if [ -s "$RESULTS" ]; then tail -n +2 "$tmp" >> "$RESULTS"
  else cat "$tmp" >> "$RESULTS"; fi
  rm -f "$tmp"
done

# Stamp the engine revision beside the results so a CSV can always be traced
# back to the build that produced it.
printf '%s\n' "$ENGINE_REV" > "$(dirname "$RESULTS")/engine-rev.txt"
echo "==> $RESULTS ($(( $(wc -l < "$RESULTS") - 1 )) rows, engine $ENGINE_REV)"
