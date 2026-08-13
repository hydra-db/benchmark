# HydraDB Benchmark

Reproducible read and write benchmarks for
[HydraDB](https://github.com/hydra-db/hydradb), a graph database that stores its
data in object storage.

### [See the results](https://hydra-db.github.io/benchmark/)

Every number here comes from a script in this repo. Clone it, run one command,
and you get the same dataset the site is built from.

## Results

Traversal on a 200,000 edge graph, one query at a time. A `MATCH` of length
`1..h` visits every path up to `h` hops.

| Depth | Hot | Cold |
|---|---|---|
| 1 hop | 912 us | 51 ms |
| 5 hops | 3.2 ms | 55 ms |
| 10 hops | 5.8 ms | 56 ms |
| 20 hops | 11.1 ms | 63 ms |

Writes, one writer, same graph. Each statement is durable when it returns,
because it commits to object storage.

| Statement | p50 | Throughput |
|---|---|---|
| `CREATE` | 4.9 ms | 194 writes/sec |
| `MERGE` | 4.7 ms | 220 writes/sec |
| `DELETE` | 6.7 ms | 152 writes/sec |

Fastest hot read measured: **408 us**. Peak read throughput: **16,805
queries/sec** at 32 workers.

Reads scale with workers up to a point and writes do not scale at all, because
commits serialise. The [site](https://hydra-db.github.io/benchmark/) has the
worker sweeps, and [METHODOLOGY.md](METHODOLOGY.md) explains what was measured
and where the limits are.

## Scope

Read this before quoting anything above.

The benchmark calls the engine **in process**, so there is no Bolt server,
driver, or network between the client and the engine. Storage is **MinIO on the
same machine**, so a cache miss costs a fraction of a millisecond. The same
query against real S3 from a laptop took 27 seconds cold.

These are engine numbers on one 15-core machine, not deployment numbers.

## Reproducing

```bash
./scripts/setup-macos.sh      # toolchain, libcypher-parser, Docker (macOS only)
./scripts/collect-minio.sh    # clones the engine, runs every sweep, builds the dataset
```

About 18 minutes. It needs Docker and a Rust toolchain, and starts its own
throwaway MinIO container, so there is nothing to configure and no cloud account
to connect. Output lands in `docs/data/minio.json`, which is the only file the
site reads.

Worker and write sweeps are published as the median of four runs, because a
single run of the same configuration varied by up to 53% on this hardware.

To measure a change before it is merged, point the harness at a local engine
checkout:

```bash
HYDRADB_SRC=~/hydradb ./scripts/collect-minio.sh
```

The engine revision used is recorded in the dataset as `meta.engine_rev`, so any
published figure can be traced back to the build that produced it.

## Docker/Bolt comparison: Turbolay versus FalkorDB

`compose.yaml` runs both databases at once: Turbolay persists to a local MinIO
container and FalkorDB is pinned to `falkordb/falkordb:v4.20.2`. The runner
seeds an equivalent fixture into both engines, executes each paired correctness
operation concurrently, compares rows against each other and against the
synthetic graph's expected result, then writes reports. The seed adapter keeps
the graph identical despite the two engines' different `MERGE` node-identity
semantics; all tested reads and writes use the same portable Cypher. Turbolay
uses Bolt; FalkorDB 4.20.2 uses its native RESP `GRAPH.QUERY` protocol (it does
not expose a Bolt listener).

```bash
# Fresh stack, seed both backends, execute paired read/write checks, compare.
./scripts/run-compose-bench.sh verify

# The same correctness gate, followed by Bolt latency measurement.
./scripts/run-compose-bench.sh bench

# Optional co-located contention measurement; do not compare it with isolated
# latency because both engines share the host at the same instant.
BENCH_EXECUTION_MODE=parallel ./scripts/run-compose-bench.sh bench
```

Every invocation uses a generated Compose project and `RUN_ID`, then removes
its containers and MinIO volume. Results remain at
`artifacts/<run-id>/`:

| File | Contents |
|---|---|
| `correctness.jsonl` | Every paired statement, parameters, expected result, both result sets, and timing |
| `comparison.csv` | Per-backend p50/p95/p99/QPS for a successful latency run |
| `summary.json` | Run configuration and final correctness status |

The runner uses `bolt://turbolay:7687` and `falkor:6379` on the internal
Compose network. For manual debugging only, Docker exposes Turbolay Bolt on
`127.0.0.1:17687` and FalkorDB RESP on `127.0.0.1:16379`; the two services run
concurrently without a port conflict.

## Layout

| Path | |
|---|---|
| `bench/` | the benchmark, a Rust crate that uses the engine as a library |
| `scripts/collect-minio.sh` | runs every sweep, then builds the dataset |
| `scripts/run-query-bench.sh` | the read sweep against a throwaway MinIO |
| `scripts/run-write-bench.sh` | the write sweep, medians of repeated runs |
| `scripts/build-dataset.py` | harness CSVs to the site's JSON |
| `docs/` | the published site and its data |

`bench/` depends on the engine as an ordinary git dependency and uses only its
public API, so nothing is needed from the engine's own `examples/` or `scripts/`.

## Contributing

Useful contributions, roughly in order of value:

- **Run it on other hardware.** Every number here is from one machine. Results
  from a different core count or a remote object store would say more than
  another sweep on this one.
- **Add a workload.** The read and write sweeps live in `bench/src/main.rs` and
  emit CSV that `scripts/build-dataset.py` turns into the site's dataset.
- **Challenge the method.** If a number looks wrong, [METHODOLOGY.md](METHODOLOGY.md)
  describes exactly how it was produced. Issues that point at a measurement
  error are welcome.

Please include the engine revision and the machine you ran on with any results.

## Licence

Copyright 2026 HydraDB. Licensed under the GNU Affero General Public License,
Version 3 (SPDX: `AGPL-3.0`). Full text in [LICENSE](LICENSE).

This matches [the engine](https://github.com/hydra-db/hydradb), which matters
because `bench/` began as the engine's `examples/query_bench.rs` and is a derived
work of it.
