# Methodology

What was run, how, and what the published numbers do not support. Written so a
reader can reproduce the results and know their limits. Benchmarks without their
caveats are marketing.

## 1. What is published

Everything on [the site](https://hydra-db.github.io/benchmark/) is produced by
`scripts/run-query-bench.sh` in this repo, which stands up a throwaway MinIO
container and drives the engine's `query_bench` example against it. Reproduce
with:

```
./scripts/collect-minio.sh
```

That runs the engine harness three times over and writes `docs/data/minio.json`,
which is the only file the page reads.

| | |
|---|---|
| Harness | `scripts/run-query-bench.sh`, calling the engine **in-process** |
| Object store | MinIO in Docker, **same host** |
| Host | single 15-core Apple Silicon machine |
| Graph | synthetic, **layered**, fanout 50 to 10,000 |
| Depth | 1 to 20 hops |
| Workers | 1, 4, 8, 32 |
| Writes | `scripts/run-write-bench.sh`, same shard, same Cypher entry point |

## 2. Two limits that shape every number

**In-process.** The harness calls the engine as a library. Nothing published
includes a network client, the Bolt protocol, authentication, or the server
runtime. Real application latency is higher by whatever those cost.

**Local object storage.** MinIO runs on the same machine, so a cache miss costs
a fraction of a millisecond. This is the single biggest caveat on the site,
because HydraDB exists to hide object-store latency and there is almost none
here to hide.

Measured during this work, same engine, same query, only the endpoint changed:

| Storage | First query (cold) |
|---|---|
| Local MinIO / SeaweedFS | ~0.8 ms |
| Real S3, us-east-1, client on a laptop | **27 s** |

Round trip to S3 from that laptop was ~280 ms; from EC2 in the same region it is
1 to 15 ms. So the published cold figures are far better than a deployment would
see, and the laptop-to-S3 figures are far worse. **Production sits between them,
nearer the local end.** These are engine numbers, not deployment numbers.

## 3. Why the graph is synthetic

The harness builds a layered graph: every vertex in layer 1 links into layer 2,
and so on. Each hop is a distinct layer, so a 10-hop query traverses 10 levels.

A real social graph cannot measure depth. On SNAP Pokec (1.6M vertices), tested
during this work:

- A **5-hop** neighbourhood from a degree-2 seed returned **80,982 of 100,000**
  vertices, about 81% of the graph.
- At **10 hops** every seed returned the identical **96,448** vertices, the whole
  connected component, whether it started from a degree-2 vertex or a degree-6,661
  one.

Past roughly 3 hops the query stops measuring depth and starts measuring
whole-graph traversal, so the numbers converge and depth becomes meaningless.
That is a property of social graphs, not of the engine.

## 4. Cold and hot

**Cold** is the first query after the local cache is wiped and the process
restarted, so it must fetch from object storage. **Hot** is the steady state
after the same query has run repeatedly.

One trap worth naming, because it was hit during this work: a warm-up pass added
to make hot measurements stable also destroyed the cold measurement, since by
the time timing began every seed had already been touched. Cold read 776 µs
against a hot 816 µs, which is no cold penalty at all. Cold is only cold on
**first touch**, and only once per process.

The object cache is also **per block, not per query**, so the first cell of a
cold pass warms the blocks every later cell reads. Measured, running a cell
first rather than later cost 31% to 52% more. A cold sweep therefore has to
restart with a wiped cache per cell, not per pass.

## 5. Concurrency, and what the CPU sample proves

Throughput at fanout 10,000 stops improving past 4 workers and holds near 152
q/s, while latency rises in step with worker count: 11 ms, 24, 49, 208.

That pattern alone has two possible causes, and they need different fixes:

- **A lock.** Threads blocked on a serialised resource, fixable in code.
- **Saturation.** Cores or memory bandwidth already full, not fixable by tuning.

Sampling process CPU during the 32-worker run separates them. Result: **231% at
p50, 330% at p95, out of 1,500% available** on 15 cores. Twelve cores idle while
latency grows fourfold means threads are waiting, not computing. It is a lock.

The GraphBLAS matrix cache defaults to a single slot in this harness and was the
obvious suspect. Re-running with eight slots moved throughput by **&minus;4% to
+4%**, which is run-to-run noise, so it is ruled out. The cause remains open and
needs a profiler rather than more benchmarking.

## 6. Writes

Writes go through the same call the reads do, `execute_cypher` on the same
shard, so a write number and a read number are measured the same way and belong
in the same table. Three statements are run, verbatim from `bench/src/main.rs`:

```
CREATE (a {id: src})-[:USER_FOLLOWS_USER]->(b {id: dst})
MERGE  (a {id: src})-[:USER_FOLLOWS_USER]->(b {id: dst})
MATCH  (a {id: src})-[e:USER_FOLLOWS_USER]->(b {id: dst}) DELETE e
```

`merge` runs against an edge that is already present, which is the path a
retrying client takes. `delete` removes an edge that was created beforehand,
untimed, so what is measured is the delete and not its setup. Every workload
gets a disjoint id range, so no workload can collide with another or with the
graph the build loaded.

Two things about the shape of the measurement:

**One writer handle, many callers.** A second handle would take the writer lease
from the first, so "32 writers" means 32 tasks calling one writer, which is what
an application does. It does not mean 32 independent writer processes.

**The first write is not counted.** Opening a handle costs a lease acquisition
and WAL setup, which lands entirely on the first statement. It is run once
before timing starts so it does not sit in one workload's percentiles.

A write commits to object storage, so it costs milliseconds where a hot read
costs microseconds. That gap is the design, not a defect: durability on every
statement is what the object store buys.

The number worth reading twice is what concurrency does. Throughput does not
move when writers are added, while p50 rises in exact proportion to the writer
count. That is the signature of a fully serialised commit path: the extra time
is queueing, not work. Adding writers to this workload buys nothing.

## 7. What is not measured

- **Multiple nodes.** Single host, single process. Nothing about placement,
  writer handoff, or failover.
- **The Bolt server.** Nothing through the network path a real client uses.
- **Repetition.** The worker sweep and the write sweep are medians of four
  runs. The depth and cache figures are still single runs, so do not read small
  differences in those as real.

## 8. Where the benchmark lives

The measuring program is `bench/` in this repo: a Rust crate that opens graphs,
runs the queries and writes the CSV. It consumes the engine as an ordinary git
dependency, using only public API, so it needs nothing from the engine's
`examples/` or `scripts/`.

It began life as `examples/query_bench.rs` inside the engine repo and was
vendored here unchanged when that repo dropped its examples and scripts. The
program is the same and calls the same API, so results either side of the move
are comparable.

`HYDRADB_SRC` builds against a local engine checkout rather than the published
one, which is how a change is measured before it is merged. The revision used is
recorded as `meta.engine_rev` in the dataset, so any published figure can be
traced to the build behind it.
