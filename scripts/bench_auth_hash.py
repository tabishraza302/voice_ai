#!/usr/bin/env python3
"""Measure local password hashing latency and peak process memory.

This benchmark uses the production PasswordHasher implementation
(platform_api.auth.passwords.PasswordHasher) configured with bounded scrypt
parameters (N=131072, r=8, p=1, maxmem=256MB).

It executes real hashing work across synthetic non-secret test passwords under
configurable worker concurrency, measuring:
  - Latency distribution (p50, p95, min, max, mean, stddev)
  - Peak resident memory (RSS) using platform resource stats
  - System metadata (OS, architecture, Python version, CPU count)
  - Hasher configuration metadata

No secrets or passwords are read from environment, files, or persistent storage.
Outputs structured JSON and human-readable summaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import statistics
import sys
import time
from typing import Any

# Ensure platform_api is importable if run directly from repository root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from platform_api.auth.passwords import (  # noqa: E402
    PasswordHasher,
    SCRYPT_DKLEN,
    SCRYPT_MAXMEM,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SALT_BYTES,
)


def get_peak_rss_mb() -> float:
    """Return process peak resident set size in megabytes.

    On macOS (Darwin), ru_maxrss is returned in bytes.
    On Linux, ru_maxrss is returned in kilobytes.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF)
    maxrss = usage.ru_maxrss
    if sys.platform == "darwin":
        return round(maxrss / (1024 * 1024), 2)
    return round(maxrss / 1024, 2)


def generate_synthetic_passwords(count: int) -> list[str]:
    """Generate deterministic synthetic passwords meeting contract requirements.

    Requirements: 15–128 Unicode codepoints, max 512 UTF-8 bytes.
    These are disposable benchmark payloads and contain no sensitive secrets.
    """
    return [f"BenchmarkSyntheticPassword-{i:06d}-TestVector" for i in range(count)]


async def run_benchmark(
    concurrency: int,
    iterations: int,
    warmup: int = 1,
) -> dict[str, Any]:
    hasher = PasswordHasher(concurrency=concurrency)
    warmup_pw = "BenchmarkWarmupPassword-000000-Initial"

    # Optional warmup to stabilize memory allocations and thread pool start
    for _ in range(warmup):
        await hasher.hash_password(warmup_pw)

    passwords = generate_synthetic_passwords(iterations)
    latencies: list[float] = []

    sem = asyncio.Semaphore(concurrency)

    async def _worker(pw: str) -> None:
        async with sem:
            t0 = time.perf_counter()
            _ = await hasher.hash_password(pw)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)

    start_wall = time.perf_counter()
    tasks = [asyncio.create_task(_worker(pw)) for pw in passwords]
    await asyncio.gather(*tasks)
    total_wall_seconds = time.perf_counter() - start_wall

    hasher.shutdown()

    sorted_latencies = sorted(latencies)
    count = len(sorted_latencies)

    def percentile(p: float) -> float:
        if count == 1:
            return round(sorted_latencies[0], 2)
        # Using linear interpolation similar to numpy / quantiles
        index = (count - 1) * p
        lower = int(index)
        upper = min(lower + 1, count - 1)
        weight = index - lower
        val = sorted_latencies[lower] * (1.0 - weight) + sorted_latencies[upper] * weight
        return round(val, 2)

    p50 = percentile(0.50)
    p95 = percentile(0.95)
    mean_lat = round(statistics.mean(sorted_latencies), 2) if sorted_latencies else 0.0
    min_lat = round(sorted_latencies[0], 2) if sorted_latencies else 0.0
    max_lat = round(sorted_latencies[-1], 2) if sorted_latencies else 0.0
    stddev_lat = (
        round(statistics.pstdev(sorted_latencies), 2) if count > 1 else 0.0
    )

    peak_rss_mb = get_peak_rss_mb()

    result = {
        "metadata": {
            "platform_os": platform.system(),
            "platform_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": sys.version.split()[0],
            "cpu_count": os.cpu_count() or 1,
            "hasher_implementation": "platform_api.auth.passwords.PasswordHasher",
            "scrypt_parameters": {
                "N": SCRYPT_N,
                "r": SCRYPT_R,
                "p": SCRYPT_P,
                "dklen": SCRYPT_DKLEN,
                "maxmem_bytes": SCRYPT_MAXMEM,
                "salt_bytes": SALT_BYTES,
            },
        },
        "workload": {
            "concurrency": concurrency,
            "iterations": iterations,
            "warmup_iterations": warmup,
            "total_wall_seconds": round(total_wall_seconds, 3),
            "throughput_hashes_per_second": round(iterations / total_wall_seconds, 2)
            if total_wall_seconds > 0
            else 0.0,
        },
        "latency_ms": {
            "p50": p50,
            "p95": p95,
            "min": min_lat,
            "max": max_lat,
            "mean": mean_lat,
            "stddev": stddev_lat,
        },
        "memory": {
            "peak_rss_mb": peak_rss_mb,
            "peak_rss_gb": round(peak_rss_mb / 1024.0, 3),
        },
    }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark platform auth password hashing latency and peak memory."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Thread concurrency pool limit (matches PLATFORM_AUTH_HASH_CONCURRENCY, default: 2)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Total number of password hashes to execute (default: 10)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Warmup iterations before collecting timings (default: 1)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON format only",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to write JSON benchmark artifact (e.g. data/auth_hash_benchmark.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.concurrency < 1:
        sys.stderr.write("Error: --concurrency must be at least 1\n")
        sys.exit(1)
    if args.iterations < 1:
        sys.stderr.write("Error: --iterations must be at least 1\n")
        sys.exit(1)

    result = asyncio.run(
        run_benchmark(
            concurrency=args.concurrency,
            iterations=args.iterations,
            warmup=args.warmup,
        )
    )

    if args.output:
        out_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        meta = result["metadata"]
        work = result["workload"]
        lat = result["latency_ms"]
        mem = result["memory"]
        print("=" * 60)
        print("Platform Auth Password Hashing Benchmark")
        print("=" * 60)
        print(f"Platform:       {meta['platform_os']} ({meta['architecture']}), Python {meta['python_version']}")
        print(f"CPU count:      {meta['cpu_count']}")
        print(f"Algorithm:      scrypt (N={meta['scrypt_parameters']['N']}, r={meta['scrypt_parameters']['r']}, p={meta['scrypt_parameters']['p']})")
        print(f"Concurrency:    {work['concurrency']} worker(s)")
        print(f"Iterations:     {work['iterations']} hashes (+{work['warmup_iterations']} warmup)")
        print(f"Wall time:      {work['total_wall_seconds']}s ({work['throughput_hashes_per_second']} hash/s)")
        print("-" * 60)
        print(f"Latency p50:    {lat['p50']} ms")
        print(f"Latency p95:    {lat['p95']} ms")
        print(f"Latency min:    {lat['min']} ms / max: {lat['max']} ms (mean: {lat['mean']} ms, stddev: {lat['stddev']} ms)")
        print(f"Peak RSS:       {mem['peak_rss_mb']} MB ({mem['peak_rss_gb']} GB)")
        print("=" * 60)
        if args.output:
            print(f"Wrote benchmark results to {args.output}")


if __name__ == "__main__":
    main()
