# HydraDB Benchmark

Performance results for [HydraDB](https://github.com/hydra-db/hydradb), an
object-store-native graph database.

**[hydra-db.github.io/benchmark](https://hydra-db.github.io/benchmark/)**

## What is published

Read latency and throughput from `scripts/run-query-bench.sh`, run against MinIO
in Docker on a single 15-core host. Synthetic layered graphs,
fanout 50 to 10,000, traversal depth 1 to 20 hops, worker counts 1 to 32.

Write latency and throughput from `scripts/run-write-bench.sh`: `CREATE`, `MERGE`
and `DELETE` through the same Cypher entry point on the same shard, so a write
number and a read number are measured the same way.

Measurement is **in-process** against the library, so no Bolt or network cost is
included, and the object store is **local**, so a cache miss costs a fraction of
a millisecond. These are engine numbers, not deployment numbers.

## Headline

Throughput at fanout 10,000 stops improving past 4 workers and holds near 152
queries per second, while latency grows in step with worker count (11 ms, 24,
49, 208) and CPU sits at 231% of 1,500% available. Twelve of fifteen cores idle
means threads are blocked, not busy. Raising the GraphBLAS matrix cache from 1
to 8 slots moved throughput by 4% either way, ruling that out as the cause.

Writes show the same shape more starkly. Throughput does not move at all when
writers are added, holding near 230 writes per second from 1 to 32 writers,
while p50 rises in proportion to the writer count (4.9 ms, 17, 35, 138). That is
a fully serialised commit path: the extra time is queueing, not work. A hundred
times more data in the store costs about 12% more per write, so the price is the
commit rather than the size of the graph.

## Reproducing

```bash
./scripts/setup-macos.sh        # toolchain, libcypher-parser, Docker (macOS)
./scripts/collect-minio.sh      # clones the engine, runs every sweep, builds the dataset
```

That runs the read sweeps and the write sweep. Running only part of it produces
a dataset with an empty series, and the page drops a section whose series is
empty, so the deploy check refuses it.

The benchmark program is `bench/`, a Rust crate in this repo that consumes the
engine as a git dependency. Nothing needs to be on disk beyond Docker and a Rust
toolchain. `HYDRADB_SRC` builds against a local engine checkout instead.

The engine is consumed as a library, so nothing is taken from its `examples/` or
`scripts/`. The revision used is recorded in the dataset as `meta.engine_rev`. It writes `docs/data/minio.json`, the only file the site reads.
Commit that to publish.

## Layout

| Path | |
|---|---|
| `docs/` | the published site, and `docs/data/minio.json` |
| `bench/` | the benchmark itself, a Rust crate depending on the engine |
| `scripts/run-query-bench.sh` | MinIO container, builds and runs `bench/` reads |
| `scripts/run-write-bench.sh` | the same for Cypher writes, medians of repeats |
| `scripts/collect-minio.sh` | every sweep, then builds the dataset |
| `scripts/build-dataset.py` | harness CSVs to the site's JSON |
| `scripts/setup-macos.sh` | toolchain setup |

`METHODOLOGY.md` records what these numbers do **not** support. Read it before
quoting any of them.

## Licence

Copyright 2026 HydraDB. Licensed under the GNU Affero General Public License,
Version 3 (SPDX: `AGPL-3.0`). The full text is in [LICENSE](LICENSE).

The same licence as [the engine](https://github.com/hydra-db/hydradb), which
matters here because `bench/` began as the engine's `examples/query_bench.rs`
and is therefore a derived work.
