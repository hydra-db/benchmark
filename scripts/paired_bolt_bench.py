#!/usr/bin/env python3
"""Paired Turbolay/FalkorDB correctness and Bolt-latency harness.

The two backends are always seeded from the same portable-Cypher fixture. A
correctness run executes the corresponding statements concurrently, then
compares normalised rows and explicit synthetic-graph expectations. Benchmark
runs repeat that gate before reporting latency, so a CSV never represents a
known-wrong query result.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from falkordb.asyncio import FalkorDB
from neo4j import AsyncGraphDatabase
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


EDGE_TYPE = "BENCH_LINK"
WRITE_EDGE_TYPE = "WRITE_LINK"
MERGE_EDGE_TYPE = "MERGE_LINK"


class HarnessConfig(BaseSettings):
    """Validated environment contract shared by Compose and local runs."""

    model_config = SettingsConfigDict(extra="ignore")

    run_id: str = Field("local", validation_alias="BENCH_RUN_ID", min_length=1)
    artifact_dir: Path = Field(Path("artifacts/local"), validation_alias="BENCH_ARTIFACT_DIR")
    fanout: int = Field(10, validation_alias="BENCH_FANOUT", ge=1)
    hops: int = Field(3, validation_alias="BENCH_HOPS", ge=1)
    page_size: int = Field(5, validation_alias="BENCH_PAGE_SIZE", ge=1)
    samples: int = Field(20, validation_alias="BENCH_LATENCY_SAMPLES", ge=1)
    concurrency: int = Field(4, validation_alias="BENCH_CONCURRENCY", ge=1)
    execution_mode: Literal["isolated", "parallel"] = Field(
        "isolated", validation_alias="BENCH_EXECUTION_MODE"
    )
    connect_timeout_s: int = Field(120, validation_alias="BENCH_CONNECT_TIMEOUT_S", ge=1)
    turbolay_bolt_uri: str = Field(
        "bolt://turbolay:7687", validation_alias="TURBOLAY_BOLT_URI", min_length=1
    )
    turbolay_database: str = Field("benchmark", validation_alias="TURBOLAY_DATABASE", min_length=1)
    turbolay_principal: str = Field("neo4j", validation_alias="TURBOLAY_PRINCIPAL", min_length=1)
    turbolay_token: str = Field(
        "benchmark-local-auth-token-32chars", validation_alias="TURBOLAY_TOKEN", min_length=32
    )
    falkor_host: str = Field("falkor", validation_alias="FALKOR_HOST", min_length=1)
    falkor_port: int = Field(6379, validation_alias="FALKOR_PORT", ge=1, le=65535)
    falkor_graph: str = Field("benchmark", validation_alias="FALKOR_GRAPH", min_length=1)


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    return sorted(values)[math.ceil(p / 100 * len(values)) - 1]


def normalise(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: normalise(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [normalise(item) for item in value]
    return str(value)


@dataclass(frozen=True)
class Endpoint:
    name: str
    uri: str
    auth: tuple[str, str]
    database: str | None


class QueryClient(Protocol):
    async def wait_ready(self, timeout_s: int) -> None: ...

    async def run(
        self, query: str, parameters: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int]: ...

    async def close(self) -> None: ...


class TurbolayClient:
    def __init__(self, endpoint: Endpoint, pool_size: int) -> None:
        self.endpoint = endpoint
        self.driver = AsyncGraphDatabase.driver(
            endpoint.uri,
            auth=endpoint.auth,
            encrypted=False,
            max_connection_pool_size=pool_size,
        )

    async def wait_ready(self, timeout_s: int) -> None:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                await self.driver.verify_connectivity()
                return
            except Exception as error:  # server startup is expected to race us
                last_error = error
                await asyncio.sleep(1)
        raise RuntimeError(
            f"{self.endpoint.name} did not accept Bolt within {timeout_s}s: {last_error}"
        )

    async def run(self, query: str, parameters: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        started = time.perf_counter_ns()
        async with self.driver.session(database=self.endpoint.database) as session:
            result = await session.run(query, parameters)
            rows = [normalise(record.data()) async for record in result]
            await result.consume()
        return rows, time.perf_counter_ns() - started

    async def close(self) -> None:
        await self.driver.close()


class FalkorClient:
    """FalkorDB 4.20.2 speaks RESP/GRAPH.QUERY, not Bolt.

    The workload remains portable Cypher and runs in parallel with the Turbolay
    Bolt request. Keeping the protocol adaptation here prevents a failed port
    mapping from turning into a misleading cross-engine comparison.
    """

    def __init__(self, host: str, port: int, graph_name: str, pool_size: int) -> None:
        self.host = host
        self.port = port
        self.graph_name = graph_name
        self.pool_size = pool_size
        self.database: FalkorDB | None = None
        self.graph: Any | None = None

    def open(self) -> None:
        # FalkorDB's client probes INFO synchronously in its constructor to
        # distinguish a standalone node from a cluster. Construct it only from
        # the readiness loop, so normal Compose startup races are retried.
        if self.database is None:
            self.database = FalkorDB(
                host=self.host, port=self.port, max_connections=self.pool_size
            )
            self.graph = self.database.select_graph(self.graph_name)

    async def wait_ready(self, timeout_s: int) -> None:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.open()
                assert self.graph is not None
                await self.graph.query("RETURN 1")
                return
            except Exception as error:  # server startup is expected to race us
                last_error = error
                self.database = None
                self.graph = None
                await asyncio.sleep(1)
        raise RuntimeError(f"falkor did not accept GRAPH.QUERY within {timeout_s}s: {last_error}")

    async def run(self, query: str, parameters: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        self.open()
        assert self.graph is not None
        started = time.perf_counter_ns()
        result = await self.graph.query(query, params=parameters)
        columns = [column[1] for column in result.header]
        rows = [
            {column: normalise(value) for column, value in zip(columns, record, strict=True)}
            for record in result.result_set
        ]
        return rows, time.perf_counter_ns() - started

    async def close(self) -> None:
        if self.database is not None:
            await self.database.aclose()


class Reporter:
    fields = (
        "run_id",
        "phase",
        "test",
        "backend",
        "execution_mode",
        "fanout",
        "hops",
        "concurrency",
        "samples",
        "p50_us",
        "p95_us",
        "p99_us",
        "mean_us",
        "qps",
        "status",
    )

    def __init__(self, root: Path, run_id: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.run_id = run_id
        self.events = (root / "correctness.jsonl").open("w", encoding="utf-8")
        self.csv_file = (root / "comparison.csv").open("w", newline="", encoding="utf-8")
        self.csv = csv.DictWriter(self.csv_file, fieldnames=self.fields)
        self.csv.writeheader()

    def event(self, payload: dict[str, Any]) -> None:
        payload["run_id"] = self.run_id
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.events.write(json.dumps(payload, sort_keys=True) + "\n")
        self.events.flush()

    def row(self, **payload: Any) -> None:
        self.csv.writerow({field: payload.get(field, "") for field in self.fields})
        self.csv_file.flush()

    def summary(self, payload: dict[str, Any]) -> None:
        (self.root / "summary.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def close(self) -> None:
        self.events.close()
        self.csv_file.close()


class Harness:
    def __init__(self) -> None:
        try:
            self.config = HarnessConfig()
        except ValidationError as error:
            raise SystemExit(f"invalid benchmark configuration:\n{error}") from error
        self.run_id = self.config.run_id
        self.fanout = self.config.fanout
        self.hops = self.config.hops
        self.page_size = self.config.page_size
        self.samples = self.config.samples
        self.concurrency = self.config.concurrency
        self.mode = self.config.execution_mode
        self.root = 1
        self.clients: dict[str, QueryClient] = {
            "turbolay": TurbolayClient(
                Endpoint(
                    "turbolay",
                    self.config.turbolay_bolt_uri,
                    (
                        self.config.turbolay_principal,
                        self.config.turbolay_token,
                    ),
                    self.config.turbolay_database,
                ),
                self.concurrency + 4,
            ),
            "falkor": FalkorClient(
                self.config.falkor_host,
                self.config.falkor_port,
                self.config.falkor_graph,
                self.concurrency + 4,
            ),
        }
        self.report = Reporter(self.config.artifact_dir, self.run_id)
        self.checks = 0

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients.values()))
        self.report.close()

    async def paired(
        self,
        phase: str,
        test: str,
        query: str | dict[str, str],
        parameters: dict[str, Any],
        expected: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        queries = (
            {backend: query for backend in self.clients}
            if isinstance(query, str)
            else query
        )
        if set(queries) != set(self.clients):
            raise RuntimeError(f"{test}: backend-specific query map must cover every backend")
        outcomes = await asyncio.gather(
            *(self.clients[backend].run(queries[backend], parameters) for backend in self.clients),
            return_exceptions=True,
        )
        by_backend = dict(zip(self.clients, outcomes, strict=True))
        errors = {
            name: f"{type(result).__name__}: {result}"
            for name, result in by_backend.items()
            if isinstance(result, Exception)
        }
        if errors:
            self.report.event(
                {
                    "phase": phase,
                    "test": test,
                    "query": queries,
                    "parameters": parameters,
                    "errors": errors,
                    "status": "error",
                }
            )
            raise RuntimeError(f"{test}: backend error: {errors}")

        rows = {name: result[0] for name, result in by_backend.items()}  # type: ignore[index]
        latencies = {name: result[1] for name, result in by_backend.items()}  # type: ignore[index]
        matches = rows["turbolay"] == rows["falkor"]
        expectation_matches = expected is None or all(value == expected for value in rows.values())
        status = "ok" if matches and expectation_matches else "mismatch"
        self.report.event(
            {
                "phase": phase,
                "test": test,
                "query": queries,
                "parameters": parameters,
                "expected": expected,
                "results": rows,
                "latency_us": {name: value / 1_000 for name, value in latencies.items()},
                "status": status,
            }
        )
        self.checks += 1
        if status != "ok":
            raise RuntimeError(
                f"{test}: expected={expected}, turbolay={rows['turbolay']}, falkor={rows['falkor']}"
            )
        return latencies

    def edges(self) -> list[tuple[int, int]]:
        edges: list[tuple[int, int]] = []
        for branch in range(self.fanout):
            source = self.root
            for depth in range(1, self.hops + 1):
                destination = depth * 1_000_000 + branch + 1
                edges.append((source, destination))
                source = destination
        return edges

    async def seed(self) -> None:
        # Falkor's relationship-pattern MERGE is all-or-nothing: when the edge
        # is new it also creates a new `a`, despite an existing same-id node.
        # Turbolay's `id` is vertex identity, so its compact edge MERGE is
        # correct there. These two seed statements materialise the same graph;
        # all read/write checks below use identical portable Cypher.
        query = {
            "turbolay": f"MERGE (a {{id: $src}})-[:{EDGE_TYPE}]->(b {{id: $dst}})",
            "falkor": (
                "MERGE (a {id: $src}) "
                "MERGE (b {id: $dst}) "
                f"MERGE (a)-[:{EDGE_TYPE}]->(b)"
            ),
        }
        for source, destination in self.edges():
            await self.paired("seed", f"edge-{source}-{destination}", query, {"src": source, "dst": destination}, [])

    async def verify(self) -> None:
        await self.seed()
        count_query = (
            f"MATCH (u {{id: $root}})-[:{EDGE_TYPE}*1..{self.hops}]->(v) "
            "RETURN count(*) AS total"
        )
        await self.paired(
            "correctness", "range-count", count_query, {"root": self.root}, [{"total": self.fanout * self.hops}]
        )
        exact_query = (
            f"MATCH (u {{id: $root}})-[:{EDGE_TYPE}*{self.hops}..{self.hops}]->(v) "
            "RETURN count(*) AS total"
        )
        await self.paired(
            "correctness", "exact-hop-count", exact_query, {"root": self.root}, [{"total": self.fanout}]
        )
        rows_query = (
            f"MATCH (u {{id: $root}})-[:{EDGE_TYPE}*1..{self.hops}]->(v) "
            f"RETURN v.id AS id ORDER BY v.id LIMIT {self.page_size}"
        )
        expected_rows = [
            {"id": depth * 1_000_000 + branch + 1}
            for depth in range(1, self.hops + 1)
            for branch in range(self.fanout)
        ]
        expected_rows.sort(key=lambda row: row["id"])
        await self.paired("correctness", "ordered-page", rows_query, {"root": self.root}, expected_rows[: self.page_size])

        write_src, write_dst = 90_000_001, 90_000_002
        create = f"CREATE (a {{id: $src}})-[:{WRITE_EDGE_TYPE}]->(b {{id: $dst}})"
        await self.paired("correctness", "create", create, {"src": write_src, "dst": write_dst}, [])
        write_count = (
            f"MATCH (a {{id: $src}})-[:{WRITE_EDGE_TYPE}]->(b {{id: $dst}}) "
            "RETURN count(*) AS total"
        )
        await self.paired("correctness", "create-visible", write_count, {"src": write_src, "dst": write_dst}, [{"total": 1}])

        merge_src, merge_dst = 90_000_011, 90_000_012
        merge = f"MERGE (a {{id: $src}})-[:{MERGE_EDGE_TYPE}]->(b {{id: $dst}})"
        await self.paired("correctness", "merge-create", merge, {"src": merge_src, "dst": merge_dst}, [])
        await self.paired("correctness", "merge-idempotent", merge, {"src": merge_src, "dst": merge_dst}, [])
        merge_count = (
            f"MATCH (a {{id: $src}})-[:{MERGE_EDGE_TYPE}]->(b {{id: $dst}}) "
            "RETURN count(*) AS total"
        )
        await self.paired("correctness", "merge-visible-once", merge_count, {"src": merge_src, "dst": merge_dst}, [{"total": 1}])

        delete = (
            f"MATCH (a {{id: $src}})-[e:{WRITE_EDGE_TYPE}]->(b {{id: $dst}}) DELETE e"
        )
        await self.paired("correctness", "delete", delete, {"src": write_src, "dst": write_dst}, [])
        await self.paired("correctness", "delete-not-visible", write_count, {"src": write_src, "dst": write_dst}, [{"total": 0}])

    async def measure_query(
        self, client: QueryClient, query: str
    ) -> tuple[int, list[dict[str, Any]]]:
        rows, duration = await client.run(query, {"root": self.root})
        return duration, rows

    async def latency(self) -> None:
        query = (
            f"MATCH (u {{id: $root}})-[:{EDGE_TYPE}*1..{self.hops}]->(v) "
            "RETURN count(*) AS total"
        )
        expected = [{"total": self.fanout * self.hops}]
        durations: dict[str, list[int]] = {name: [] for name in self.clients}
        started = time.perf_counter_ns()

        async def paired_sample() -> None:
            outcomes = await asyncio.gather(
                *(self.measure_query(client, query) for client in self.clients.values())
            )
            for backend, (duration, rows) in zip(self.clients, outcomes, strict=True):
                if rows != expected:
                    raise RuntimeError(f"latency result mismatch for {backend}: {rows}")
                durations[backend].append(duration)

        async def isolated_sample(backend: str) -> None:
            duration, rows = await self.measure_query(self.clients[backend], query)
            if rows != expected:
                raise RuntimeError(f"latency result mismatch for {backend}: {rows}")
            durations[backend].append(duration)

        batches = math.ceil(self.samples / self.concurrency)
        for batch in range(batches):
            remaining = min(self.concurrency, self.samples - batch * self.concurrency)
            if self.mode == "parallel":
                await asyncio.gather(*(paired_sample() for _ in range(remaining)))
            else:
                # Alternate backend order per batch to avoid always giving one
                # database a warmer host and a lower frequency-throttle state.
                order = ("turbolay", "falkor") if batch % 2 == 0 else ("falkor", "turbolay")
                for backend in order:
                    await asyncio.gather(*(isolated_sample(backend) for _ in range(remaining)))
        elapsed_ns = time.perf_counter_ns() - started

        for backend, values in durations.items():
            micros = [value / 1_000 for value in values]
            qps = len(values) / max(elapsed_ns / 1_000_000_000, sys.float_info.min)
            self.report.row(
                run_id=self.run_id,
                phase="latency",
                test="range-count",
                backend=backend,
                execution_mode=self.mode,
                fanout=self.fanout,
                hops=self.hops,
                concurrency=self.concurrency,
                samples=len(values),
                p50_us=f"{percentile(micros, 50):.3f}",
                p95_us=f"{percentile(micros, 95):.3f}",
                p99_us=f"{percentile(micros, 99):.3f}",
                mean_us=f"{statistics.fmean(micros):.3f}",
                qps=f"{qps:.3f}",
                status="ok",
            )

    async def write_latency(self) -> None:
        create_q = f"CREATE (a {{id: $src}})-[:{WRITE_EDGE_TYPE}]->(b {{id: $dst}})"
        merge_q = f"MERGE (a {{id: $src}})-[:{MERGE_EDGE_TYPE}]->(b {{id: $dst}})"
        delete_q = f"MATCH (a {{id: $src}})-[e:{WRITE_EDGE_TYPE}]->(b {{id: $dst}}) DELETE e"
        count_write = (
            f"MATCH (a {{id: $src}})-[:{WRITE_EDGE_TYPE}]->(b {{id: $dst}}) "
            "RETURN count(*) AS total"
        )
        count_merge = (
            f"MATCH (a {{id: $src}})-[:{MERGE_EDGE_TYPE}]->(b {{id: $dst}}) "
            "RETURN count(*) AS total"
        )

        durations: dict[str, list[int]] = {name: [] for name in self.clients}
        started = time.perf_counter_ns()

        async def write_cycle(backend: str, cycle_id: int) -> None:
            client = self.clients[backend]
            base = 80_000_000 + cycle_id * 10
            c_src, c_dst = base + 1, base + 2
            m_src, m_dst = base + 3, base + 4

            ops: list[tuple[str, dict[str, Any], str, dict[str, Any], list[dict[str, Any]]]] = [
                (create_q, {"src": c_src, "dst": c_dst}, count_write, {"src": c_src, "dst": c_dst}, [{"total": 1}]),
                (merge_q, {"src": m_src, "dst": m_dst}, count_merge, {"src": m_src, "dst": m_dst}, [{"total": 1}]),
                (merge_q, {"src": m_src, "dst": m_dst}, count_merge, {"src": m_src, "dst": m_dst}, [{"total": 1}]),
                (delete_q, {"src": c_src, "dst": c_dst}, count_write, {"src": c_src, "dst": c_dst}, [{"total": 0}]),
            ]
            for write_query, write_params, verify_query, verify_params, expected in ops:
                _, dur = await client.run(write_query, write_params)
                durations[backend].append(dur)
                rows, _ = await client.run(verify_query, verify_params)
                if rows != expected:
                    raise RuntimeError(
                        f"write verification failed for {backend}: "
                        f"query={write_query}, got {rows}, expected {expected}"
                    )
                self.checks += 1

        batches = math.ceil(self.samples / self.concurrency)
        for batch in range(batches):
            remaining = min(self.concurrency, self.samples - batch * self.concurrency)
            if self.mode == "parallel":
                await asyncio.gather(*(
                    write_cycle(backend, batch * self.concurrency + i)
                    for i in range(remaining)
                    for backend in self.clients
                ))
            else:
                order = ("turbolay", "falkor") if batch % 2 == 0 else ("falkor", "turbolay")
                for backend in order:
                    await asyncio.gather(*(
                        write_cycle(backend, batch * self.concurrency + i)
                        for i in range(remaining)
                    ))
        elapsed_ns = time.perf_counter_ns() - started

        for backend, values in durations.items():
            micros = [value / 1_000 for value in values]
            qps = len(values) / max(elapsed_ns / 1_000_000_000, sys.float_info.min)
            self.report.row(
                run_id=self.run_id,
                phase="latency",
                test="write-mixed",
                backend=backend,
                execution_mode=self.mode,
                fanout=self.fanout,
                hops=self.hops,
                concurrency=self.concurrency,
                samples=len(values),
                p50_us=f"{percentile(micros, 50):.3f}",
                p95_us=f"{percentile(micros, 95):.3f}",
                p99_us=f"{percentile(micros, 99):.3f}",
                mean_us=f"{statistics.fmean(micros):.3f}",
                qps=f"{qps:.3f}",
                status="ok",
            )

    async def run(self, command: str) -> None:
        await asyncio.gather(
            *(client.wait_ready(self.config.connect_timeout_s) for client in self.clients.values())
        )
        await self.verify()
        if command == "bench":
            await self.latency()
            await self.write_latency()
        self.report.summary(
            {
                "run_id": self.run_id,
                "command": command,
                "execution_mode": self.mode if command == "bench" else None,
                "fanout": self.fanout,
                "hops": self.hops,
                "checks": self.checks,
                "status": "ok",
            }
        )


async def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"verify", "bench"}:
        print("usage: paired_bolt_bench.py [verify|bench]", file=sys.stderr)
        return 2
    harness = Harness()
    try:
        await harness.run(sys.argv[1])
        print(f"paired benchmark passed; artifacts={harness.report.root}")
        return 0
    finally:
        await harness.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
