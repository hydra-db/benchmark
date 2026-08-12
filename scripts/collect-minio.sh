#!/usr/bin/env bash
# Reproduce the published dataset end to end.
#
# Runs scripts/run-query-bench.sh three times over: a depth sweep, a worker-count
# sweep, and a control test on the GraphBLAS matrix cache. Then the write sweep
# via scripts/run-write-bench.sh. Then builds docs/data/minio.json from the CSVs.
#
# The write sweep is not optional here. build-dataset.py reads it from
# <results>/writes, and the page drops its write section when that series is
# empty, so skipping it silently unpublishes those numbers.
#
#   ./scripts/collect-minio.sh            # full run, roughly 18 minutes
#   HYDRADB_SRC=~/src/hydradb ./scripts/collect-minio.sh
#
# Requires Docker (the engine script starts its own MinIO container per
# invocation) and a Rust toolchain. On macOS run scripts/setup-macos.sh first.
#
# The benchmark program is bench/ in this repo. The object store, the
# environment and the sweeps live here in scripts/.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="${HYDRADB_SRC:-$HOME/hydradb}"
OUT="${BENCH_RESULTS:-bench-results}"
FANOUTS="${BENCH_FANOUTS:-50,100,1000,5000,10000}"
HOPS="${BENCH_HOPS:-1,5,10,15,20}"
CONC="${BENCH_CONC:-1 4 8 32}"
CONC_FANOUTS="${BENCH_CONC_FANOUTS:-100,1000,10000}"
CONC_HOPS="${BENCH_CONC_HOPS:-1,5,10,20}"
WRITE_REPEATS="${BENCH_WRITE_REPEATS:-4}"

[ -d "$SRC" ] || { echo "engine checkout not found at $SRC (set HYDRADB_SRC)" >&2; exit 1; }
# run-query-bench.sh checks Docker and the engine checkout itself.

mkdir -p "$OUT/conc" "$OUT/matrix"
run() {  # run <results-csv> <extra env assignments...>
  local csv="$1"; shift
  env "$@" BENCH_RESULTS="$csv" ./scripts/run-query-bench.sh >/dev/null
  echo "  wrote $csv ($(( $(wc -l < "$csv") - 1 )) rows)"
}

echo "==> depth sweep: fanouts $FANOUTS, hops $HOPS"
run "$OUT/depth.csv" \
  BENCH_FANOUTS="$FANOUTS" \
  BENCH_HOPS="$HOPS"

echo "==> worker sweep: $CONC"
for c in $CONC; do
  # CONCURRENT_ITERS scales with worker count. Left at its default of 16, a
  # 32-worker run would issue fewer iterations than it has workers, so some
  # workers would do nothing and the figure would be noise.
  run "$OUT/conc/c$c.csv" \
    BENCH_CONCURRENCY="$c" \
    BENCH_CONCURRENT_ITERS="$(( c * 4 < 16 ? 16 : c * 4 ))" \
    BENCH_FANOUTS="$CONC_FANOUTS" \
    BENCH_HOPS="$CONC_HOPS"
done

echo "==> matrix-slot control: 8 slots instead of the default 1, fanout 10000"
for c in $CONC; do
  run "$OUT/matrix/m8-c$c.csv" \
    BENCH_CONCURRENCY="$c" \
    BENCH_CONCURRENT_ITERS="$(( c * 4 < 16 ? 16 : c * 4 ))" \
    BENCH_FANOUTS=10000 \
    BENCH_HOPS="$CONC_HOPS" \
    BENCH_MAX_GRAPHBLAS_MATRICES=8
done

echo "==> write sweep: $WRITE_REPEATS repeats"
BENCH_REPEATS="$WRITE_REPEATS" BENCH_RESULTS_DIR="$OUT/writes" \
  ./scripts/run-write-bench.sh >/dev/null
echo "  wrote $OUT/writes ($(ls "$OUT"/writes/r*.csv | wc -l | tr -d ' ') repeats)"

echo "==> building docs/data/minio.json"
python3 scripts/build-dataset.py --results "$OUT" --out docs/data/minio.json

cat <<'NOTE'

Done. Commit docs/data/minio.json to publish it; the Pages workflow refuses to
deploy if that file is missing or its series are empty.

CPU utilisation is sampled separately and passed to build-dataset.py. It is the
measurement that separates a lock from saturation: at 32 workers on fanout
10,000 the process used 231% CPU out of 1,500% available, so the threads were
blocked rather than busy. To repeat it, sample `ps -o pcpu` against the
query_bench process while a concurrent run is in flight.
NOTE
