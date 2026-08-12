#!/usr/bin/env python3
"""Turn the engine harness CSVs into the single JSON the site reads.

The published page loads exactly one file, `docs/data/minio.json`. This script
produces it, so the path from raw run output to what is on the site is a
committed step rather than something done by hand.

Input is whatever `scripts/collect-minio.sh` left in the results directory:

    <results>/depth.csv              full fanout x hops sweep, one concurrency
    <results>/conc/c<N>.csv          one file per worker count
    <results>/matrix/m8-c<N>.csv     matrix-slot control test, one per worker count
    <results>/writes/r<N>.csv        Cypher write sweep, one file per repeat

Each is a `query_bench` CSV as written by the engine's examples/query_bench.rs.
Only the columns the site uses are carried across; the rest stay in the CSVs.

Usage:
    python3 scripts/build-dataset.py --results bench-results --out docs/data/minio.json
"""

import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys
from datetime import date


# Paged workloads are dropped at build time. execute_cypher_rows_page returns
# only the first page (64 rows) and sets has_next, so its latency is flat with
# depth and roughly 3,000x lower than the same traversal returning all rows. It
# answers "how fast is the first screenful", not "how fast is the query", and
# publishing it beside the others invites exactly the wrong comparison.
SKIP_WORKLOADS = {"multi_hop_page", "one_hop_page"}


def rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def num(row, key, cast=float, default=None):
    v = (row.get(key) or "").strip()
    if not v:
        return default
    try:
        return cast(v)
    except ValueError:
        return default


def conc_of(path):
    """Worker count from a filename such as c8.csv or m8-c32.csv."""
    m = re.search(r"c(\d+)\.csv$", os.path.basename(path))
    if not m:
        raise SystemExit(f"cannot read worker count from {path}")
    return int(m.group(1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="bench-results")
    ap.add_argument("--out", default="docs/data/minio.json")
    ap.add_argument("--host", default="15-core Apple Silicon")
    ap.add_argument("--backend", default="MinIO (Docker, local)")
    # CPU sampling is a separate observation, not part of the CSVs. It is what
    # distinguishes a lock from saturation, so it travels with the dataset.
    ap.add_argument("--cpu-p50", type=float, default=231)
    ap.add_argument("--cpu-p95", type=float, default=330)
    ap.add_argument("--cpu-max", type=float, default=515)
    ap.add_argument("--cores", type=int, default=15)
    ap.add_argument("--writes", default=None,
                    help="write-bench results dir (default <results>/writes)")
    args = ap.parse_args()

    # run-query-bench.sh stamps the engine revision beside the CSVs, so a
    # published dataset can always be traced to the build that produced it.
    rev_file = os.path.join(args.results, "engine-rev.txt")
    engine_rev = ""
    if os.path.exists(rev_file):
        engine_rev = open(rev_file).read().strip()

    depth_csv = os.path.join(args.results, "depth.csv")
    if not os.path.exists(depth_csv):
        sys.exit(f"missing {depth_csv}; run scripts/collect-minio.sh first")

    depth = []
    for r in rows(depth_csv):
        if r.get("kind") in SKIP_WORKLOADS:
            continue
        depth.append(dict(
            fanout=num(r, "fanout", int), hops=num(r, "hops", int),
            workload=r.get("kind", ""),
            hot_p50=num(r, "hot_p50_us"), hot_p95=num(r, "hot_p95_us"),
            cold_p50=num(r, "cold_query_p50_us"),
            hot_qps=num(r, "hot_qps"), conc_qps=num(r, "concurrent_qps", float, 0.0),
            edges=num(r, "edges", int),
        ))

    # Repeats: conc/c<N>.csv, or conc/r*/c<N>.csv for repeated runs. The median
    # across repeats is published, because a single run of the same
    # configuration was measured to vary by up to 53% - quoting one run to two
    # decimal places claims precision the measurement does not have.
    buckets = {}
    reps = 0
    paths = sorted(glob.glob(os.path.join(args.results, "conc", "c*.csv"))) \
          + sorted(glob.glob(os.path.join(args.results, "conc", "*", "c*.csv")))
    for f in paths:
        c = conc_of(f)
        reps = max(reps, 1)
        for r in rows(f):
            if r.get("kind") in SKIP_WORKLOADS:
                continue
            key = (c, num(r, "fanout", int), num(r, "hops", int), r.get("kind", ""))
            b = buckets.setdefault(key, {"p50": [], "qps": [], "edges": None})
            for k, col in (("p50", "concurrent_p50_us"), ("qps", "concurrent_qps")):
                v = num(r, col)
                if v is not None:
                    b[k].append(v)
            b["edges"] = b["edges"] or num(r, "edges", int)

    concurrency = []
    samples = 0
    for (c, fanout, hops, workload), b in buckets.items():
        if not b["p50"] or not b["qps"]:
            continue
        samples = max(samples, len(b["qps"]))
        concurrency.append(dict(
            conc=c, fanout=fanout, hops=hops, workload=workload,
            p50=round(statistics.median(b["p50"]), 3),
            qps=round(statistics.median(b["qps"]), 2),
            runs=len(b["qps"]), edges=b["edges"],
        ))

    matrix = []
    for f in sorted(glob.glob(os.path.join(args.results, "matrix", "m8-c*.csv")), key=conc_of):
        c = conc_of(f)
        for r in rows(f):
            if r.get("kind") in SKIP_WORKLOADS:
                continue
            matrix.append(dict(
                conc=c, hops=num(r, "hops", int), workload=r.get("kind", ""),
                qps=num(r, "concurrent_qps"),
            ))

    # Writes. run-write-bench.sh writes one CSV per repeat; the median across
    # them is published for the same reason the read concurrency numbers are.
    write_dir = args.writes or os.path.join(args.results, "writes")
    wbuckets = {}
    for f in sorted(glob.glob(os.path.join(write_dir, "r*.csv"))):
        for r in rows(f):
            key = (num(r, "fanout", int), r.get("workload", ""), num(r, "concurrency", int))
            b = wbuckets.setdefault(key, {"p50": [], "p95": [], "p99": [],
                                          "ops": [], "rss": [], "edges": None})
            for k, col in (("p50", "p50_us"), ("p95", "p95_us"), ("p99", "p99_us"),
                           ("ops", "ops_per_sec"), ("rss", "rss_mib")):
                v = num(r, col)
                if v is not None:
                    b[k].append(v)
            b["edges"] = b["edges"] or num(r, "edges", int)
            if num(r, "errors", int, 0):
                sys.exit(f"{f} reports write errors; not publishing a failed run")

    writes = []
    write_runs = 0
    for (fanout, workload, conc), b in sorted(wbuckets.items()):
        if not b["p50"] or not b["ops"]:
            continue
        write_runs = max(write_runs, len(b["ops"]))
        writes.append(dict(
            fanout=fanout, workload=workload, conc=conc,
            p50=round(statistics.median(b["p50"]), 3),
            p95=round(statistics.median(b["p95"]), 3),
            p99=round(statistics.median(b["p99"]), 3),
            ops=round(statistics.median(b["ops"]), 2),
            rss=round(statistics.median(b["rss"]), 1) if b["rss"] else None,
            runs=len(b["ops"]), edges=b["edges"],
        ))

    if not depth or not concurrency:
        sys.exit(f"empty dataset: depth={len(depth)} concurrency={len(concurrency)}")

    out = dict(
        meta=dict(
            engine="HydraDB", backend=args.backend, host=args.host,
            harness="scripts/minio_query_bench.sh (in-process)",
            graph="synthetic layered fanout", engine_rev=engine_rev,
            conc_runs=samples, write_runs=write_runs,
            captured=date.today().isoformat(),
            cpu_sample=dict(concurrency=32, fanout=10000, hops=20,
                            p50_pct=args.cpu_p50, p95_pct=args.cpu_p95,
                            max_pct=args.cpu_max, cores=args.cores),
        ),
        depth=depth, concurrency=concurrency, matrix=matrix, writes=writes,
    )

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {args.out}: depth={len(depth)} concurrency={len(concurrency)} "
          f"matrix={len(matrix)} writes={len(writes)}  "
          f"median of {samples} read / {write_runs} write run(s)  "
          f"({os.path.getsize(args.out)//1024} KB)")


if __name__ == "__main__":
    main()
