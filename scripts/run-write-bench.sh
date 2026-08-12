#!/usr/bin/env bash
# Measure Cypher writes against a throwaway MinIO container.
#
# The companion to run-query-bench.sh. Reads and writes go through the same
# Cypher entry point on the same shard, so the two sets of numbers are measured
# the same way. Writes emit their own CSV because the columns differ: there is
# no cold or hot split for a write, and there is a mutation count instead of a
# row count.
#
# Usage:
#   ./scripts/run-write-bench.sh                        # clones the engine, runs
#   HYDRADB_SRC=~/hydradb ./scripts/run-write-bench.sh   # use a local checkout
#   BENCH_WRITE_OPS=500 BENCH_REPEATS=1 ./scripts/run-write-bench.sh
#
# Requires Docker, git and a Rust toolchain. On macOS run scripts/setup-macos.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

ENGINE_BRANCH="${HYDRADB_REF:-main}"
SRC="${HYDRADB_SRC:-}"
CACHE="${HYDRADB_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/hydradb-bench}"

RESULTS_DIR="${BENCH_RESULTS_DIR:-bench-results/writes}"
LOG="${BENCH_LOG:-$RESULTS_DIR/writes.log}"

# Sweep. Two graph sizes rather than the full read sweep: the question for a
# write is whether the size of the graph it lands in changes its cost, and two
# points an order of magnitude apart answer that.
FANOUTS="${BENCH_FANOUTS:-100,10000}"
DATA_HOPS="${BENCH_DATA_HOPS:-20}"
WRITE_OPS="${BENCH_WRITE_OPS:-200}"
WRITE_CONCURRENCY="${BENCH_WRITE_CONCURRENCY:-1,4,8,32}"
WRITE_WORKLOADS="${BENCH_WRITE_WORKLOADS:-create,merge,delete}"
# Repeats, because a single run of this on a laptop varies by tens of percent.
# The dataset builder takes the median across them.
REPEATS="${BENCH_REPEATS:-4}"
INDEX_POLICY="${BENCH_INDEX_POLICY:-outbound-only}"
BULK_CHUNK="${BENCH_BULK_CHUNK_SIZE:-10000}"
MATRICES="${BENCH_MAX_GRAPHBLAS_MATRICES:-1}"
RUNTIME="${BENCH_RUNTIME:-multi-thread}"

NAME="${BENCH_MINIO_NAME:-hydradb-write-minio}"
NETWORK="${BENCH_MINIO_NETWORK:-hydradb-write-net}"
PORT="${BENCH_MINIO_PORT:-19013}"
ACCESS_KEY="${BENCH_MINIO_ACCESS_KEY:-bench$(date +%s)$$}"
SECRET_KEY="${BENCH_MINIO_SECRET_KEY:-bench-secret-$(date +%s)-$$}"
BUCKET="${BENCH_MINIO_BUCKET:-write-bench-$(date +%s)-$$}"
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

mkdir -p "$RESULTS_DIR"
[ "${BENCH_APPEND:-0}" = "1" ] || rm -f "$RESULTS_DIR"/r*.csv "$LOG"

# One file per repeat. Every run rebuilds the graph from scratch under a fresh
# run id, so a repeat never writes into a shard another repeat already grew.
IFS=',' read -ra fanouts <<< "$FANOUTS"
for repeat in $(seq 1 "$REPEATS"); do
  out="$RESULTS_DIR/r$repeat.csv"
  rm -f "$out"
  for fanout in "${fanouts[@]}"; do
    echo "==> repeat=$repeat fanout=$fanout ops=$WRITE_OPS workers=$WRITE_CONCURRENCY"
    tmp="$(mktemp)"
    GRAPH_QUERY_BENCH_OBJECT_ENV="$ENV_FILE" \
    GRAPH_QUERY_BENCH_MODE=writes \
    GRAPH_QUERY_BENCH_RUN_ID="write-bench-r$repeat-$$" \
    GRAPH_QUERY_BENCH_FANOUTS="$fanout" \
    GRAPH_QUERY_BENCH_DATA_HOPS="$DATA_HOPS" \
    GRAPH_QUERY_BENCH_WRITE_OPS="$WRITE_OPS" \
    GRAPH_QUERY_BENCH_WRITE_CONCURRENCY="$WRITE_CONCURRENCY" \
    GRAPH_QUERY_BENCH_WRITE_WORKLOADS="$WRITE_WORKLOADS" \
    GRAPH_QUERY_BENCH_INDEX_POLICY="$INDEX_POLICY" \
    GRAPH_QUERY_BENCH_BULK_CHUNK_SIZE="$BULK_CHUNK" \
    GRAPH_QUERY_BENCH_MAX_GRAPHBLAS_MATRICES="$MATRICES" \
    GRAPH_QUERY_BENCH_RUNTIME="$RUNTIME" \
      "$BIN" > "$tmp" 2>> "$LOG"
    if [ -s "$out" ]; then tail -n +2 "$tmp" >> "$out"
    else cat "$tmp" >> "$out"; fi
    rm -f "$tmp"
  done
done

printf '%s\n' "$ENGINE_REV" > "$RESULTS_DIR/engine-rev.txt"
echo "==> $RESULTS_DIR ($REPEATS repeats, engine $ENGINE_REV)"
