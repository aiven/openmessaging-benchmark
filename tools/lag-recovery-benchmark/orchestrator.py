#!/usr/bin/env python3
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Orchestrator for multi-phase consumer lag/recovery benchmark.

Uses OMB worker REST API to control dedicated workers per consumer group,
enabling independent lifecycle management (start/stop) of each group.

Test levels:
  L1: Produce for two phases; group A (optional) consumes. No consumer stops
  L2: Stop and restart group B while A keeps reading; wait for B to catch up
  L3: Stop B and C; restart B after the lag duration and C after twice that
  L4: A, B and C read at the head; group D starts late from offset 0
  L5: Seeded topic (--reuse-topic): reader A starts from offset 0, then B

Usage:
  python orchestrator.py \\
    --producer-workers http://localhost:8080 \\
    --consumer-a-workers http://localhost:8081 \\
    --driver-config driver.yaml \\
    --topics 1 --partitions 8 \\
    --message-size 1024 --target-rate-mb 1 \\
    --phase-duration-minutes 2 \\
    --kafka-bootstrap localhost:9092 \\
    --output results.json
"""

import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# Lag recovery core -- pure logic: no network, subprocess or sleeping.
# Covered by test_orchestrator.py. Scenarios drive a `ctx` object (implemented
# by LagRecoveryBenchmark) so their step order can be tested with a fake.
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as XmYs (e.g., 167m02s)."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s:02d}s"


class CounterRates:
    """Per-second rates from cumulative, monotonic counters.

    The first sample for a key has no baseline and a counter that went
    backwards (worker restart / stats reset) re-seeds the baseline; both
    return None so no negative or huge rate is reported.
    """

    def __init__(self):
        self._prev: dict[str, int] = {}

    def rate(self, key: str, current: int, dt: float) -> Optional[float]:
        prev = self._prev.get(key)
        self._prev[key] = current
        if prev is None or current < prev or dt <= 0:
            return None
        return (current - prev) / dt

    def forget(self, key: str):
        self._prev.pop(key, None)


STALL_MIN_RATE = 1.0  # msg/s


class StallDetector:
    """Flags a stalled producer: rate below min_rate for `polls` consecutive polls."""

    def __init__(self, min_rate: float = STALL_MIN_RATE, polls: int = 3):
        self.min_rate = min_rate
        self.polls = polls
        self.count = 0

    def observe(self, rate: Optional[float]) -> bool:
        """Record one poll. A None rate (no baseline yet) is ignored.

        Returns True once `polls` consecutive polls were below min_rate.
        """
        if rate is None:
            return False
        if rate < self.min_rate:
            self.count += 1
            return self.count >= self.polls
        self.count = 0
        return False


def parse_describe_rows(output: str) -> list[dict]:
    """Parse `kafka-consumer-groups.sh --describe` output into partition rows.

    Skips headers and rows without a numeric log-end offset. A partition without a
    committed offset gets committed=None and lag=log_end: the group has not consumed
    anything there yet.
    """
    rows = []
    for line in output.strip().split("\n"):
        if not line or line.startswith("GROUP") or line.startswith("Consumer group"):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            committed = None if parts[3] == "-" else int(parts[3])
            log_end = int(parts[4])
            lag = log_end if committed is None else int(parts[5])
            rows.append({"topic": parts[1], "partition": int(parts[2]),
                         "committed": committed, "log_end": log_end, "lag": lag})
        except ValueError:
            continue
    return rows


def summarize_offsets(rows: list[dict]) -> dict:
    """Totals over partition rows. Negative lag values are left out of total_lag.

    A missing committed offset (None) counts as 0 in total_committed.
    """
    return {
        "total_lag": sum(r["lag"] for r in rows if r["lag"] >= 0),
        "total_committed": sum(r["committed"] or 0 for r in rows),
        "total_log_end": sum(r["log_end"] for r in rows),
        "partitions": rows,
    }


def consumed_fraction(offsets: Optional[dict]) -> Optional[float]:
    """Fraction of the log a group has consumed, or None when it is unknown or the log is empty."""
    if not offsets or offsets["total_log_end"] <= 0:
        return None
    return 1 - offsets["total_lag"] / offsets["total_log_end"]


def is_caught_up(offsets: Optional[dict], threshold: float, expected_partitions: int) -> bool:
    """True when consumed fraction >= threshold over all expected partitions.

    Unknown offsets (None) are not caught up; an empty log is. During a rebalance,
    --describe can leave out partitions without a commit, so fewer rows than
    expected_partitions are not caught up.
    """
    if offsets is None or len(offsets["partitions"]) < expected_partitions:
        return False
    fraction = consumed_fraction(offsets)
    return fraction is None or fraction >= threshold


@dataclass
class Timing:
    """Scenario timings in seconds."""
    phase_s: float            # warm-up and cool-down
    lag_s: float              # how long consumers stay stopped / data accumulates
    catchup_timeout_s: float  # safety limit for each catch-up wait


def remaining_lag_wait(elapsed_s: float, t: Timing) -> float:
    """Seconds group C must stay stopped so its outage totals 2 * lag_s (stopped at phase_s)."""
    return max(0, 2 * t.lag_s - (elapsed_s - t.phase_s))


def scenario_steady_state(ctx, t: Timing, groups: list):
    """Level 1: produce and consume for 2 phases, no consumer lifecycle events."""
    ctx.collect_for(2 * t.phase_s)
    ctx.stop_all()


def scenario_lag_recovery(ctx, t: Timing, groups: list):
    """Level 2: warm-up -> stop B -> lag builds -> restart B -> catch-up -> cool-down."""
    ctx.collect_for(t.phase_s)
    ctx.stop_groups("stop_group_B", ["group-B"])
    ctx.collect_for(t.lag_s)
    ctx.start_group("start_group_B", "group-B")
    ctx.wait_catchup(["group-B"], cooldown_s=t.phase_s)
    ctx.stop_all()


def scenario_multi_group(ctx, t: Timing, groups: list):
    """Level 3: stop B and C; restart B after lag_s, C after 2 * lag_s; each catches up."""
    ctx.collect_for(t.phase_s)
    ctx.stop_groups("stop_groups_B_C", ["group-B", "group-C"])
    ctx.collect_for(t.lag_s)
    ctx.start_group("start_group_B", "group-B")
    ctx.wait_catchup(["group-B"])
    remaining = remaining_lag_wait(ctx.elapsed(), t)
    if remaining > 0:
        log.info("Waiting %s more for C lag accumulation", _format_elapsed(remaining))
        ctx.collect_for(remaining)
    ctx.start_group("start_group_C", "group-C")
    ctx.wait_catchup(["group-C"], cooldown_s=t.phase_s)
    ctx.stop_all()


def scenario_multi_layer(ctx, t: Timing, groups: list):
    """Level 4: hot consumers A, B, C read throughout; group-D joins late from offset 0.

    After lag_s of accumulation (should exceed local.retention.ms so tiered data
    exists), D reads tiered -> local -> head while the hot consumers are monitored.
    """
    log.info("L4 Phase 1: warm-up with hot consumers (A, B, C) for %s", _format_elapsed(t.phase_s))
    ctx.collect_for(t.phase_s)
    log.info("L4 Phase 2: data accumulation for %s (building tiered + local layers)",
             _format_elapsed(t.lag_s))
    ctx.collect_for(t.lag_s)
    ctx.start_group("start_group_D_from_beginning", "group-D")
    log.info("L4 Phase 3: group-D catching up (tiered -> local -> cache), "
             "monitoring hot consumer impact")
    ctx.wait_catchup(["group-D"], cooldown_s=t.phase_s)
    ctx.stop_all()


def scenario_staged_readers(ctx, t: Timing, groups: list):
    """Level 5: two late readers against a seeded topic, started one after the other.

    Producers run at head. Reader 1 (first group) starts from offset 0 with a
    fresh group ID. Reader 2 starts only if reader 1 catches up within the
    timeout, so a stall in the tiered-read path ends the run early.
    """
    reader1, reader2 = groups[0], groups[1]
    log.info("L5 Phase 0: producer-only warm-up for %s", _format_elapsed(t.phase_s))
    ctx.collect_for(t.phase_s)

    log.info("L5 Phase 1: starting reader-1 (%s) from beginning -- "
             "expect tiered -> local -> head traversal", reader1)
    ctx.start_group(f"start_{reader1}_from_beginning", reader1)
    if not ctx.wait_catchup([reader1], cooldown_s=t.phase_s):
        log.error("L5: reader-1 (%s) did NOT catch up within %s -- "
                  "tiered-read path likely stalled. Skipping reader-2, stopping.",
                  reader1, _format_elapsed(t.catchup_timeout_s))
        ctx.stop_all()
        return

    log.info("L5 Phase 2: reader-1 caught up -- starting reader-2 (%s) from beginning", reader2)
    ctx.start_group(f"start_{reader2}_from_beginning", reader2)
    ctx.wait_catchup([reader2], cooldown_s=t.phase_s)
    ctx.stop_all()


SCENARIOS = {
    1: scenario_steady_state,
    2: scenario_lag_recovery,
    3: scenario_multi_group,
    4: scenario_multi_layer,
    5: scenario_staged_readers,
}


def deferred_groups(level: int, group_names: list) -> set:
    """Groups whose consumers are created mid-run by the scenario, not at setup."""
    if level == 4:
        return {"group-D"}
    if level == 5:
        return set(group_names)
    return set()


WORKER_ROLES = ["producer_workers", "consumer_a_workers", "consumer_b_workers",
                "consumer_c_workers", "consumer_d_workers"]


def split_workers(workers: list, counts: list) -> dict:
    """Assign workers to roles in order: counts[i] workers go to WORKER_ROLES[i].

    Raises ValueError when fewer than 2 counts are given, a count is negative, or the
    counts exceed the workers. A count of 0 leaves a role empty.
    """
    if len(counts) < 2:
        raise ValueError(f"--worker-split needs at least 2 values: P,A (got {len(counts)})")
    if any(count < 0 for count in counts):
        raise ValueError(f"--worker-split counts must not be negative (got {counts})")
    if sum(counts) > len(workers):
        raise ValueError(
            f"--worker-split total ({sum(counts)}) exceeds available workers ({len(workers)})")
    assignment = {}
    offset = 0
    for role, count in zip(WORKER_ROLES, counts):
        assignment[role] = workers[offset:offset + count]
        offset += count
    return assignment


def validate_args(args) -> list:
    """Check that the workers and flags fit the level. Returns a list of errors."""
    errors = []
    if not args.producer_workers:
        errors.append("No producer workers specified. "
                      "Use --producer-workers or --workers-file + --worker-split")
    roles_by_url = {}
    for role in WORKER_ROLES:
        for url in getattr(args, role):
            roles_by_url.setdefault(url.rstrip("/"), []).append(role)
    for url, roles in roles_by_url.items():
        if len(roles) > 1:
            errors.append(f"Worker {url} is used {len(roles)} times ({', '.join(roles)}). "
                          "Each worker must serve one role only")
    if args.level >= 2 and not args.consumer_a_workers:
        errors.append(f"Level {args.level} requires --consumer-a-workers")
    if args.level == 2 and not args.consumer_b_workers:
        errors.append("Level 2 requires --consumer-b-workers (the group that stops and restarts)")
    if args.level == 3:
        if not args.consumer_b_workers:
            errors.append("Level 3 requires --consumer-b-workers")
        if not args.consumer_c_workers:
            errors.append("Level 3 requires --consumer-c-workers")
    if args.level == 4:
        if not args.consumer_b_workers:
            errors.append("Level 4 requires --consumer-b-workers")
        if not args.consumer_c_workers:
            errors.append("Level 4 requires --consumer-c-workers")
        if not args.consumer_d_workers:
            errors.append("Level 4 requires --consumer-d-workers (late-joining consumer)")
    if args.level == 5:
        if not args.consumer_b_workers:
            errors.append("Level 5 requires --consumer-b-workers (reader-2)")
        if not args.reuse_topic:
            errors.append("Level 5 requires --reuse-topic (attach to a seeded topic)")
    if args.reuse_topic and not args.topic_prefix:
        errors.append("--reuse-topic requires --topic-prefix (the seeded topic prefix)")
    for name in ("topics", "partitions", "message_size", "poll_interval"):
        if getattr(args, name) < 1:
            errors.append(f"--{name.replace('_', '-')} must be at least 1")
    for name in ("target_rate_mb", "phase_duration_minutes", "catchup_timeout_minutes",
                 "lag_duration_minutes"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            errors.append(f"--{name.replace('_', '-')} must be greater than 0")
    if not 0 < args.catchup_threshold <= 1:
        errors.append("--catchup-threshold must be greater than 0 and at most 1")
    if args.target_rate_mb > 0 and args.message_size > 0:
        expected_rate = args.target_rate_mb * 1_048_576 / args.message_size
        if expected_rate < STALL_MIN_RATE:
            errors.append(f"--target-rate-mb {args.target_rate_mb:g} with --message-size "
                          f"{args.message_size} gives {expected_rate:.2f} msg/s, below the "
                          f"{STALL_MIN_RATE:g} msg/s that the stall detector needs")
    return errors


# ---------------------------------------------------------------------------
# OMB Worker Client
# ---------------------------------------------------------------------------

class OMBWorkerClient:
    """HTTP client for a single OMB benchmark-worker."""

    def __init__(self, base_url: str, timeout: int = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, data: bytes = b"", content_type: str = None) -> bytes:
        """POST request, returns response body bytes."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=data, method="POST")
        if content_type:
            req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def _post_json(self, path: str, obj) -> bytes:
        """POST JSON payload, returns response body bytes."""
        data = json.dumps(obj).encode("utf-8")
        return self._post(path, data=data, content_type="application/json")

    def _get(self, path: str) -> bytes:
        """GET request, returns response body bytes."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def _get_json(self, path: str) -> dict:
        """GET request, returns parsed JSON."""
        return json.loads(self._get(path))

    def initialize_driver(self, driver_config_bytes: bytes):
        """POST /initialize-driver with raw YAML bytes."""
        self._post("/initialize-driver", data=driver_config_bytes)

    def create_topics(self, topic_names: list[str], partitions: int) -> list[str]:
        """POST /create-topics, returns list of created topic names."""
        payload = {
            "numberOfTopics": len(topic_names),
            "numberOfPartitionsPerTopic": partitions,
            "topicNames": topic_names,
        }
        resp = self._post_json("/create-topics", payload)
        return json.loads(resp)

    def create_producers(self, topics: list[str]):
        """POST /create-producers with list of topic names."""
        self._post_json("/create-producers", topics)

    def create_consumers(self, topic_subscriptions: list[dict]):
        """POST /create-consumers with ConsumerAssignment payload.

        topic_subscriptions: [{"topic": "t", "subscription": "group-A"}, ...]
        """
        payload = {"topicsSubscriptions": topic_subscriptions}
        self._post_json("/create-consumers", payload)

    def start_load(self, publish_rate: float, message_size: int):
        """POST /start-load with ProducerWorkAssignment."""
        payload_bytes = os.urandom(message_size)
        payload = {
            "publishRate": publish_rate,
            "payloadData": [base64.b64encode(payload_bytes).decode("ascii")],
            "keyDistributorType": "NO_KEY",
        }
        self._post_json("/start-load", payload)

    def stop_all(self):
        """POST /stop-all."""
        self._post("/stop-all")

    def get_counters_stats(self) -> dict:
        """GET /counters-stats."""
        return self._get_json("/counters-stats")

    def __repr__(self):
        return f"OMBWorkerClient({self.base_url})"


# ---------------------------------------------------------------------------
# Worker Group -- manages multiple workers assigned to the same role
# ---------------------------------------------------------------------------

class WorkerGroup:
    """A logical group of OMB workers (e.g., all producer workers or all group-A consumer workers)."""

    def __init__(self, name: str, urls: list[str]):
        self.name = name
        # LagRecoveryBenchmark adds the run ID.
        self.kafka_group_id = name
        self.clients = [OMBWorkerClient(url) for url in urls]

    def initialize_driver(self, driver_config_bytes: bytes):
        for c in self.clients:
            log.info("Initializing driver on %s (%s)", self.name, c.base_url)
            c.initialize_driver(driver_config_bytes)

    def create_topics(self, topic_names: list[str], partitions: int) -> list[str]:
        # Only create on the first worker (leader)
        leader = self.clients[0]
        log.info("Creating %d topics on %s (%s)", len(topic_names), self.name, leader.base_url)
        return leader.create_topics(topic_names, partitions)

    def create_producers(self, topics: list[str]):
        for c in self.clients:
            log.info("Creating producers on %s (%s)", self.name, c.base_url)
            c.create_producers(topics)

    def create_consumers(self, topic_subscriptions: list[dict]):
        for c in self.clients:
            log.info("Creating consumers on %s (%s) -- %d subscriptions", self.name, c.base_url, len(topic_subscriptions))
            c.create_consumers(topic_subscriptions)

    def start_load(self, publish_rate_per_worker: float, message_size: int):
        for c in self.clients:
            log.info("Starting load on %s (%s) at %.0f msg/s", self.name, c.base_url, publish_rate_per_worker)
            c.start_load(publish_rate_per_worker, message_size)

    def stop_all(self):
        """Stop every worker of the group. Raises after all attempts if any failed."""
        errors = []
        for c in self.clients:
            log.info("Stopping all on %s (%s)", self.name, c.base_url)
            try:
                c.stop_all()
            except Exception as e:
                log.error("Stop failed on %s (%s): %s", self.name, c.base_url, e)
                errors.append(f"{c.base_url}: {e}")
        if errors:
            raise RuntimeError(f"Stop failed on {len(errors)} of {len(self.clients)} workers "
                               f"of {self.name}: {'; '.join(errors)}")

    def get_counters_stats(self) -> list[dict]:
        return [c.get_counters_stats() for c in self.clients]

    def aggregate_counters(self) -> dict:
        """Aggregate counters across all workers in this group."""
        total = {"messagesSent": 0, "messagesReceived": 0, "messageSendErrors": 0}
        for stats in self.get_counters_stats():
            total["messagesSent"] += stats.get("messagesSent", 0)
            total["messagesReceived"] += stats.get("messagesReceived", 0)
            total["messageSendErrors"] += stats.get("messageSendErrors", 0)
        return total


# ---------------------------------------------------------------------------
# Kafka Lag Monitor -- uses kafka-consumer-groups.sh
# ---------------------------------------------------------------------------

def read_config_block(driver_config_path: str, block: str) -> dict:
    """Read the key=value lines of a block, such as commonConfig, of a driver YAML file."""
    props = {}
    in_block = False
    with open(driver_config_path) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith(f"{block}:"):
                in_block = True
                continue
            if in_block:
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, val = stripped.partition("=")
                    props[key.strip()] = val.strip()
                elif stripped and not stripped.startswith("#") and not stripped.startswith("|"):
                    # Hit next YAML key
                    in_block = False
    return props


def validate_driver_config(args) -> list:
    """Errors for settings of the driver file that the level needs. Returns a list of errors."""
    errors = []
    if args.level in (4, 5) or args.reuse_topic:
        try:
            reset = (read_config_block(args.driver_config, "consumerConfig").get("auto.offset.reset")
                     or read_config_block(args.driver_config, "commonConfig").get("auto.offset.reset"))
        except OSError as e:
            return [f"Cannot read the driver config {args.driver_config}: {e}"]
        if reset != "earliest":
            errors.append(f"Level {args.level}{' with --reuse-topic' if args.reuse_topic else ''} "
                          f"starts new consumer groups that must read from the "
                          f"beginning. Set auto.offset.reset=earliest in consumerConfig of "
                          f"{args.driver_config} (found: {reset or 'not set'})")
    return errors


def parse_driver_config(driver_config_path: str,
                        write_command_config: bool = True) -> Tuple[str, Optional[str]]:
    """Parse driver YAML to extract bootstrap.servers and write a command-config properties file.

    Returns (bootstrap_servers, command_config_path).
    command_config_path is None if no SSL/auth properties are needed or if
    write_command_config is False.
    """
    common_config = read_config_block(driver_config_path, "commonConfig")

    bootstrap = common_config.pop("bootstrap.servers", None)
    if not bootstrap:
        log.warning("No bootstrap.servers found in driver config")
        bootstrap = ""

    # Write remaining connection properties (SSL, auth) to a temp file
    # for kafka-consumer-groups.sh --command-config
    conn_props = {k: v for k, v in common_config.items()
                  if k.startswith("security.") or k.startswith("ssl.") or k.startswith("sasl.")}
    command_config_path = None
    if conn_props and write_command_config:
        import tempfile
        fd, command_config_path = tempfile.mkstemp(prefix="kafka-cmd-", suffix=".properties")
        with os.fdopen(fd, "w") as pf:
            for k, v in conn_props.items():
                pf.write(f"{k}={v}\n")
        log.info("Wrote kafka command-config to %s (%d properties)", command_config_path, len(conn_props))

    return bootstrap, command_config_path


def resolve_lag_connection(kafka_bootstrap: Optional[str], docker_container: str,
                           driver_config_path: str) -> Tuple[str, str]:
    """Pick the bootstrap servers and the command-config path for the lag queries.

    An explicit bootstrap wins. Without one, the address in the driver config applies,
    then localhost:9092. The command-config file is a host path, so it is written only
    when the queries run on the host, not in a Docker container.
    """
    parsed_bootstrap, command_config_path = parse_driver_config(
        driver_config_path, write_command_config=not docker_container)
    bootstrap = kafka_bootstrap or parsed_bootstrap or "localhost:9092"
    return bootstrap, command_config_path or ""


class PreflightError(Exception):
    """The Kafka CLI cannot reach the cluster, or a topic that the run needs does not exist."""


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


class KafkaLagMonitor:
    """Monitors consumer group lag via kafka-consumer-groups.sh.

    Supports two modes:
    - Direct: runs kafka-consumer-groups.sh on the host
    - Docker: runs kafka-consumer-groups.sh inside a Docker container via 'docker exec'
    """

    def __init__(self, bootstrap_servers: str, kafka_bin: str = "",
                 docker_container: str = "", command_config: str = ""):
        self.bootstrap_servers = bootstrap_servers
        self.docker_container = docker_container
        self.command_config = command_config
        self._failed_groups = set()

        if docker_container:
            # Use docker exec to run inside the kafka container
            self.cmd_prefix = [
                "docker", "exec", docker_container,
                "/opt/kafka/bin/kafka-consumer-groups.sh",
            ]
            log.info("Lag monitor: using docker exec into %s", docker_container)
        elif kafka_bin:
            self.cmd_prefix = [os.path.join(kafka_bin, "kafka-consumer-groups.sh")]
        else:
            self.cmd_prefix = ["kafka-consumer-groups.sh"]

    def check(self):
        """Run kafka-consumer-groups.sh --list once.

        Raises PreflightError when the tool is missing or cannot reach the cluster.
        Without this check, every catch-up wait would run to its timeout.
        """
        cmd = self.cmd_prefix + ["--bootstrap-server", self.bootstrap_servers, "--list"]
        if self.command_config:
            cmd += ["--command-config", self.command_config]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise PreflightError(f"Cannot run {' '.join(self.cmd_prefix)}: {e}. "
                                 "Set --kafka-bin or --kafka-docker-container")
        if result.returncode != 0:
            raise PreflightError(f"kafka-consumer-groups.sh --list failed (bootstrap "
                                 f"{self.bootstrap_servers}): {_first_line(result.stderr)}")

    def _run_describe(self, group_id: str) -> Optional[str]:
        """Run --describe for a group, return stdout or None."""
        try:
            cmd = self.cmd_prefix + [
                "--bootstrap-server", self.bootstrap_servers,
                "--group", group_id, "--describe",
            ]
            if self.command_config:
                cmd += ["--command-config", self.command_config]
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                self._report_failure(group_id, _first_line(result.stderr))
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, OSError) as e:
            self._report_failure(group_id, str(e))
            return None

    def _report_failure(self, group_id: str, detail: str):
        """Warn about the first failure of a group. Later failures of it go to debug level."""
        if group_id in self._failed_groups:
            log.debug("kafka-consumer-groups failed for %s: %s", group_id, detail)
        else:
            self._failed_groups.add(group_id)
            log.warning("kafka-consumer-groups failed for %s (later failures are logged "
                        "at debug level): %s", group_id, detail)

    def describe_topic(self, topic: str) -> int:
        """Return the partition count of an existing topic.

        Raises PreflightError when the topic does not exist or the check cannot run.
        """
        script = self.cmd_prefix[-1]
        tool = self.cmd_prefix[:-1] + [os.path.join(
            os.path.dirname(script),
            os.path.basename(script).replace("kafka-consumer-groups", "kafka-topics"))]
        cmd = tool + ["--bootstrap-server", self.bootstrap_servers, "--describe", "--topic", topic]
        if self.command_config:
            cmd += ["--command-config", self.command_config]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise PreflightError(f"Cannot check topic {topic}: {e}")
        if result.returncode != 0:
            raise PreflightError(f"Topic {topic} was not found or cannot be read: "
                                  f"{_first_line(result.stderr)}")
        match = re.search(r"PartitionCount:\s*(\d+)", result.stdout)
        if match:
            return int(match.group(1))
        count = len(re.findall(r"\bPartition:\s*\d+", result.stdout))
        if count:
            return count
        raise PreflightError(f"Cannot read the partition count of topic {topic}")

    def get_group_offsets(self, group_id: str) -> Optional[dict]:
        """Get detailed per-partition offsets for a consumer group.

        Returns {"total_lag": N, "total_committed": N, "total_log_end": N,
                 "partitions": [{"topic":..., "partition":..., "committed":..., "log_end":..., "lag":...}]}
        """
        output = self._run_describe(group_id)
        if output is None:
            return None
        rows = parse_describe_rows(output)
        if not rows:
            # A group without a committed offset has no numeric row yet.
            return None
        return summarize_offsets(rows)


# ---------------------------------------------------------------------------
# Metrics Collector
# ---------------------------------------------------------------------------

def _fmt(value: Optional[float], spec: str) -> str:
    """Format a rate; None (no baseline yet) prints as '?'."""
    return "?" if value is None else format(value, spec)


class WorkloadStalledError(Exception):
    """Raised when producers are expected to be running but the workload has
    stalled (producer rate ~0 for several consecutive polls) -- typically a dead
    or unresponsive broker. Lets the orchestrator abort instead of running blind."""


class MetricsCollector:
    """Collects and stores time-series metrics from worker groups and Kafka."""

    def __init__(self, producer_group: WorkerGroup, consumer_groups: dict[str, WorkerGroup],
                 lag_monitor: KafkaLagMonitor, message_size: int):
        self.producer_group = producer_group
        self.consumer_groups = consumer_groups
        self.lag_monitor = lag_monitor
        self.message_size = message_size
        self.start_time = None
        self.snapshots: list[dict] = []
        self.events: list[dict] = []
        # Track which groups are currently active
        self.active_groups: set[str] = set()
        # Rates keyed by "producer" / group name. A group's baseline is dropped
        # while inactive so a restarted group's counter reset gives no bad rate.
        self.rates = CounterRates()
        # Stall detection runs only while producers are expected to be running.
        self.producing = False
        self.stall = StallDetector()
        self.producer_failures = 0

    def start(self):
        self.start_time = time.time()
        self._last_collect_time = self.start_time

    def record_event(self, action: str, **kwargs):
        event = {"time_s": self._elapsed(), "action": action, **kwargs}
        self.events.append(event)
        log.info("EVENT [T+%s]: %s %s", _format_elapsed(event["time_s"]), action, kwargs if kwargs else "")

    def collect(self):
        """Collect one snapshot of metrics from all active groups.

        Rates are derived from cumulative /counters-stats (monotonic) divided by
        the actual elapsed time -- the same approach Prometheus rate() uses --
        rather than the worker's self-resetting /period-stats window, which
        aliases against our variable poll interval. Throughput is rate * message
        size since CountersStats exposes no byte fields.
        """
        now = time.time()
        dt = now - self._last_collect_time
        self._last_collect_time = now
        mb_per_msg = self.message_size / 1_048_576
        elapsed = self._elapsed()
        snapshot = {"timestamp_s": round(elapsed, 1)}

        # Producer stats
        prod_rate = None
        try:
            pc = self.producer_group.aggregate_counters()
            self.producer_failures = 0
            prod_rate = self.rates.rate("producer", pc["messagesSent"], dt)
            err_rate = self.rates.rate("producer_errors", pc["messageSendErrors"], dt)
            snapshot["producer"] = {
                "rate_msg_s": round(prod_rate, 1) if prod_rate is not None else None,
                "throughput_mb_s": round(prod_rate * mb_per_msg, 2) if prod_rate is not None else None,
                "total_sent": pc["messagesSent"],
                "send_errors": pc["messageSendErrors"],
                "send_error_rate": round(err_rate, 1) if err_rate is not None else None,
            }
        except Exception as e:
            self.producer_failures += 1
            log.warning("Failed to collect producer stats (%d/%d): %s",
                        self.producer_failures, self.stall.polls, e)
            # The clock moved on, so the old baseline would give a rate that is too high.
            self.rates.forget("producer")
            self.rates.forget("producer_errors")
            snapshot["producer"] = {}

        # Per-group consumer stats
        for group_name, group in self.consumer_groups.items():
            group_stats = {}
            if group_name in self.active_groups:
                try:
                    cc = group.aggregate_counters()
                    rate = self.rates.rate(group_name, cc["messagesReceived"], dt)
                    group_stats["rate_msg_s"] = round(rate, 1) if rate is not None else None
                    group_stats["throughput_mb_s"] = round(rate * mb_per_msg, 2) if rate is not None else None
                    group_stats["total_received"] = cc["messagesReceived"]
                except Exception as e:
                    log.warning("Failed to collect stats for %s: %s", group_name, e)
                    self.rates.forget(group_name)
            else:
                # Drop stale baseline so a restarted group recomputes from scratch
                self.rates.forget(group_name)

            # Kafka Admin API lag (works even when group is stopped -- shows committed offset lag)
            offsets = self.lag_monitor.get_group_offsets(group.kafka_group_id)
            if offsets:
                group_stats["lag_messages"] = offsets["total_lag"]

            group_stats["active"] = group_name in self.active_groups
            snapshot[group_name] = group_stats

        self.snapshots.append(snapshot)
        self._print_snapshot(snapshot)

        # Stall detection -- only while producers are expected to be running.
        # prod_rate is None on the first poll (no baseline) -- skip those.
        if self.producing and self.producer_failures >= self.stall.polls:
            raise WorkloadStalledError(
                f"Producer stats unavailable for {self.producer_failures} consecutive polls at T+"
                f"{_format_elapsed(snapshot['timestamp_s'])} -- a producer worker may be down")
        if self.producing and prod_rate is not None:
            stalled = self.stall.observe(prod_rate)
            if self.stall.count:
                log.warning("Producer rate %.1f msg/s below %.1f (stall %d/%d) -- "
                            "broker may be unresponsive",
                            prod_rate, self.stall.min_rate,
                            self.stall.count, self.stall.polls)
            if stalled:
                raise WorkloadStalledError(
                    f"Producers stalled (<{self.stall.min_rate} msg/s) for "
                    f"{self.stall.count} consecutive polls at T+"
                    f"{_format_elapsed(snapshot['timestamp_s'])}")

    def _print_snapshot(self, s: dict):
        parts = [f"T+{_format_elapsed(s['timestamp_s'])}"]
        prod = s.get("producer", {})
        if prod:
            rate = prod.get('rate_msg_s')
            tp = prod.get('throughput_mb_s')
            pub = f"Pub: {_fmt(rate, '.0f')} msg/s ({_fmt(tp, '.2f')} MB/s)"
            err_rate = prod.get('send_error_rate', 0)
            if err_rate:
                pub += f" ERR {err_rate:.0f}/s"
            parts.append(pub)
        total_consumer_tp = 0.0
        for gname in self.consumer_groups:
            gs = s.get(gname, {})
            status = "ON" if gs.get("active") else "OFF"
            lag = gs.get("lag_messages", "?")
            rate = gs.get("rate_msg_s")
            tp = gs.get("throughput_mb_s")
            total_consumer_tp += tp or 0.0
            parts.append(f"{gname}[{status}]: {_fmt(rate, '.0f')} msg/s ({_fmt(tp, '.2f')} MB/s) lag={lag}")
        parts.append(f"Total Con: {total_consumer_tp:.2f} MB/s")
        log.info(" | ".join(parts))

    def _elapsed(self) -> float:
        return time.time() - self.start_time if self.start_time else 0

    def to_result(self, config: dict) -> dict:
        """Generate the final results JSON."""
        return {
            "config": config,
            "time_series": self.snapshots,
            "events": self.events,
        }


# ---------------------------------------------------------------------------
# Phase Runner -- imperative execution with catch-up awareness
# ---------------------------------------------------------------------------

class PhaseRunner:
    """Executes benchmark phases, collecting metrics between them.

    Supports both fixed-duration waits and catch-up-aware waits.
    """

    def __init__(self, collector: MetricsCollector, lag_monitor: KafkaLagMonitor,
                 poll_interval_s: int = 10):
        self.collector = collector
        self.lag_monitor = lag_monitor
        self.poll_interval_s = poll_interval_s

    def collect_for(self, duration_s: float):
        """Collect metrics for a fixed duration."""
        end_time = time.time() + duration_s
        while time.time() < end_time:
            try:
                self.collector.collect()
            except WorkloadStalledError:
                raise
            except Exception as e:
                log.warning("Metrics collection error: %s", e)
            sleep_time = min(self.poll_interval_s, end_time - time.time())
            if sleep_time > 0:
                time.sleep(sleep_time)

    def collect_until_catchup(self, groups: list, expected_partitions: int, threshold: float = 0.99,
                              timeout_s: float = 600, cooldown_s: float = 0):
        """Collect until all groups have caught up, then optionally collect for cooldown.

        The cool-down runs only after a successful catch-up.

        threshold: consumed fraction required (e.g., 0.99 = 99% of log-end consumed).
        Returns True if caught up within timeout, False if timed out.
        """
        start = time.time()
        caught_up = False

        while time.time() - start < timeout_s:
            try:
                self.collector.collect()
            except WorkloadStalledError:
                raise
            except Exception as e:
                log.warning("Metrics collection error: %s", e)

            all_ok = True
            for group_name in groups:
                gid = self.collector.consumer_groups[group_name].kafka_group_id
                offsets = self.lag_monitor.get_group_offsets(gid)
                fraction = consumed_fraction(offsets)
                if fraction is not None:
                    log.info("  CATCHUP %s: %.2f%% consumed (lag=%d, log_end=%d, %d of %d partitions)",
                             group_name, fraction * 100,
                             offsets["total_lag"], offsets["total_log_end"],
                             len(offsets["partitions"]), expected_partitions)
                if not is_caught_up(offsets, threshold, expected_partitions):
                    all_ok = False
            if all_ok:
                caught_up = True
                elapsed = time.time() - start
                log.info("All groups caught up in %s (threshold=%.0f%%)",
                         _format_elapsed(elapsed), threshold * 100)
                break

            sleep_time = min(self.poll_interval_s, timeout_s - (time.time() - start))
            if sleep_time > 0:
                time.sleep(sleep_time)

        if not caught_up:
            log.warning("Catch-up timeout (%s) reached for groups %s",
                        _format_elapsed(timeout_s), groups)
        self.collector.record_event("catchup", groups=list(groups), caught_up=caught_up,
                                    waited_s=time.time() - start)

        if caught_up and cooldown_s > 0:
            log.info("Cool-down: collecting for %s", _format_elapsed(cooldown_s))
            self.collect_for(cooldown_s)

        return caught_up

    def execute_phase(self, name: str, action_fn):
        """Log and execute a named phase action."""
        elapsed = self.collector._elapsed()
        log.info("=" * 60)
        log.info("PHASE: %s (T+%s)", name, _format_elapsed(elapsed))
        log.info("=" * 60)
        self.collector.record_event(name)
        try:
            action_fn()
        except Exception as e:
            log.error("Phase '%s' failed: %s", name, e)
            self.collector.record_event(f"{name}_failed", error=str(e))
            raise


# ---------------------------------------------------------------------------
# Benchmark Orchestrator
# ---------------------------------------------------------------------------

class LagRecoveryBenchmark:
    """Orchestrates the multi-phase consumer lag/recovery benchmark."""

    def __init__(self, args):
        self.args = args
        self.driver_config_bytes = Path(args.driver_config).read_bytes()
        self.run_id = uuid.uuid4().hex[:8]
        prefix = args.topic_prefix or f"lag-bench-{int(time.time())}-{self.run_id}"
        self.topic_names = [f"{prefix}-{i}" for i in range(args.topics)]
        self.message_size = args.message_size
        self.partitions = args.partitions
        self.phase_duration_s = args.phase_duration_minutes * 60
        self.lag_duration_s = (args.lag_duration_minutes or args.phase_duration_minutes) * 60

        # Calculate publish rate: target_rate_mb / message_size = msg/s
        total_rate_msg_s = (args.target_rate_mb * 1_048_576) / args.message_size

        # Worker groups
        self.producer_group = WorkerGroup("producers", args.producer_workers)
        self.rate_per_producer = total_rate_msg_s / len(self.producer_group.clients)

        self.consumer_groups: dict[str, WorkerGroup] = {}
        if args.consumer_a_workers:
            self.consumer_groups["group-A"] = WorkerGroup("group-A", args.consumer_a_workers)
        if args.consumer_b_workers:
            self.consumer_groups["group-B"] = WorkerGroup("group-B", args.consumer_b_workers)
        if args.consumer_c_workers:
            self.consumer_groups["group-C"] = WorkerGroup("group-C", args.consumer_c_workers)
        if args.consumer_d_workers:
            self.consumer_groups["group-D"] = WorkerGroup("group-D", args.consumer_d_workers)

        # Kafka keeps the committed offsets of a group, and --describe lists the topics
        # of all earlier runs. A run ID in the group ID keeps the runs apart. It stays the
        # same within the run, so a restarted group resumes from its committed offsets.
        # Internal keys stay as the logical names; only the Kafka-facing group ID changes.
        for gname, group in self.consumer_groups.items():
            group.kafka_group_id = f"{gname}-{self.run_id}"
            log.info("%s -> kafka group id %s", gname, group.kafka_group_id)

        # Lag monitor -- extract connection properties from driver config
        bootstrap, command_config = resolve_lag_connection(
            args.kafka_bootstrap, args.kafka_docker_container, args.driver_config)
        self._command_config_file = command_config
        self.lag_monitor = KafkaLagMonitor(
            bootstrap, args.kafka_bin,
            docker_container=args.kafka_docker_container,
            command_config=command_config,
        )

        # Catch-up settings
        self.catchup_timeout_s = args.catchup_timeout_minutes * 60
        self.catchup_threshold = args.catchup_threshold

        # Metrics
        self.collector = MetricsCollector(
            self.producer_group, self.consumer_groups, self.lag_monitor, self.message_size,
        )
        self.runner = PhaseRunner(self.collector, self.lag_monitor, poll_interval_s=args.poll_interval)

    def _make_subscriptions(self, group_name: str) -> list[dict]:
        """Build TopicSubscription list for a consumer group.

        The subscription name becomes the Kafka group ID, so use the group's
        kafka_group_id (which carries the run ID)."""
        gid = self.consumer_groups[group_name].kafka_group_id
        return [{"topic": t, "subscription": gid} for t in self.topic_names]

    def _init_and_create_consumers(self, group_name: str):
        """Create consumers for a group (workers must already be initialized)."""
        self._log_group_offsets(group_name, "before_restart")
        group = self.consumer_groups[group_name]
        group.create_consumers(self._make_subscriptions(group_name))
        self.collector.active_groups.add(group_name)
        time.sleep(2)  # Let consumer join and start fetching
        self._log_group_offsets(group_name, "after_restart")

    def _log_group_offsets(self, group_name: str, label: str):
        """Log committed offsets for a consumer group (for verification)."""
        gid = self.consumer_groups[group_name].kafka_group_id
        offsets = self.lag_monitor.get_group_offsets(gid)
        if offsets:
            log.info("OFFSETS [%s] %s: committed=%d log_end=%d lag=%d",
                     label, group_name, offsets["total_committed"],
                     offsets["total_log_end"], offsets["total_lag"])
            self.collector.record_event(
                f"offsets_{label}",
                group=group_name,
                committed=offsets["total_committed"],
                log_end=offsets["total_log_end"],
                lag=offsets["total_lag"],
                partitions=offsets["partitions"],
            )

    def _stop_consumer_group(self, group_name: str):
        """Stop all workers for a consumer group and re-initialize for later restart."""
        self._log_group_offsets(group_name, "before_stop")
        group = self.consumer_groups[group_name]
        group.stop_all()
        self.collector.active_groups.discard(group_name)
        # Re-initialize so workers are ready to create new consumers later
        time.sleep(1)
        group.initialize_driver(self.driver_config_bytes)
        # Log offsets after stop -- committed offset should be preserved
        self._log_group_offsets(group_name, "after_stop")

    def run(self):
        num_groups = len(self.consumer_groups)
        log.info("Starting lag recovery benchmark with %d consumer group(s)", num_groups)
        log.info("Topics: %s, Partitions: %d, Message size: %d bytes",
                 self.topic_names, self.args.partitions, self.message_size)
        log.info("Target rate: %g MB/s (%.0f msg/s per producer worker)",
                 self.args.target_rate_mb, self.rate_per_producer)
        log.info("Phase duration: %.1f min, lag duration: %.1f min, catchup timeout: %.1f min",
                 self.args.phase_duration_minutes, self.lag_duration_s / 60,
                 self.catchup_timeout_s / 60)

        # --- Setup ---
        self.lag_monitor.check()
        if self.args.reuse_topic:
            self._check_seeded_topics()
        deferred = deferred_groups(self.args.level, list(self.consumer_groups))

        all_groups = [self.producer_group] + list(self.consumer_groups.values())
        for g in all_groups:
            g.initialize_driver(self.driver_config_bytes)

        if self.args.reuse_topic:
            log.info("Reuse mode: attaching to existing topics %s", self.topic_names)
        else:
            created_topics = self.producer_group.create_topics(self.topic_names, self.args.partitions)
            log.info("Topics: %s", created_topics)
            # A worker build without topicNames creates random names. The producers would
            # then auto-create the requested names with the broker defaults.
            if sorted(created_topics) != sorted(self.topic_names):
                raise RuntimeError(f"The workers created the topics {created_topics}, not "
                                   f"{self.topic_names}. Use a worker build with topicNames")

        # Create producers
        self.producer_group.create_producers(self.topic_names)

        # Create consumer groups (except deferred ones)
        for group_name, group in self.consumer_groups.items():
            if group_name in deferred:
                log.info("Deferring consumer group %s (late-joining)", group_name)
                continue
            group.create_consumers(self._make_subscriptions(group_name))
            self.collector.active_groups.add(group_name)

        # Start producing
        self.producer_group.start_load(self.rate_per_producer, self.message_size)

        # --- Execute scenario ---
        self.collector.start()
        # Producers run continuously until the final stop_all; enable stall detection.
        self.collector.producing = True

        timing = Timing(self.phase_duration_s, self.lag_duration_s, self.catchup_timeout_s)
        scenario = SCENARIOS[self.args.level]
        log.info("Scenario: %s", scenario.__name__)
        try:
            scenario(self, timing, list(self.consumer_groups))
        except BaseException as e:
            # Keep the time series and the events of the aborted run for diagnosis.
            try:
                self._write_results("aborted", error=str(e) or type(e).__name__)
            except Exception as write_error:
                log.warning("Could not write partial results: %s", write_error)
            raise

        # Final collection
        try:
            self.collector.collect()
        except Exception:
            pass

        timed_out = any(e["action"] == "catchup" and not e["caught_up"]
                        for e in self.collector.events)
        return self._write_results("timeout" if timed_out else "completed")

    def _check_seeded_topics(self):
        """Check that every topic of reuse mode exists, before any worker is touched.

        Raises PreflightError for a missing topic or for topics with different partition
        counts. The catch-up check needs one partition count. A count that differs from
        --partitions is a warning: the count of the seeded topic is the one that counts.
        """
        counts = {name: self.lag_monitor.describe_topic(name) for name in self.topic_names}
        for name, count in counts.items():
            log.info("Seeded topic %s has %d partitions", name, count)
            if count != self.args.partitions:
                log.warning("Topic %s has %d partitions, not the %d of --partitions. "
                            "The topic is not changed", name, count, self.args.partitions)
        if len(set(counts.values())) > 1:
            raise PreflightError(
                "Seeded topics have different partition counts: "
                + ", ".join(f"{name}={count}" for name, count in counts.items())
                + ". Use topics with the same partition count.")
        self.partitions = next(iter(counts.values()))

    def _write_results(self, status: str, error: Optional[str] = None) -> dict:
        """Write the results JSON.

        status is "completed", "timeout" (a group did not catch up in time) or "aborted".
        """
        config = {
            "topics": self.args.topics,
            "partitions": self.partitions,
            "message_size_bytes": self.message_size,
            "target_rate_mb_s": self.args.target_rate_mb,
            "phase_duration_minutes": self.args.phase_duration_minutes,
            "lag_duration_minutes": self.lag_duration_s / 60,
            "catchup_timeout_minutes": self.catchup_timeout_s / 60,
            "catchup_threshold": self.catchup_threshold,
            "consumer_groups": list(self.consumer_groups.keys()),
            "kafka_group_ids": {name: group.kafka_group_id
                                for name, group in self.consumer_groups.items()},
            "run_id": self.run_id,
            "level": self.args.level,
        }
        result = self.collector.to_result(config)
        result["status"] = status
        if error:
            result["error"] = error

        with open(self.args.output, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info("Results written to %s (%s)", self.args.output, status)
        return result

    # --- Scenario context: the steps a scenario function drives ---

    def elapsed(self) -> float:
        return self.collector._elapsed()

    def collect_for(self, duration_s: float):
        self.runner.collect_for(duration_s)

    def stop_groups(self, event: str, group_names: list):
        """Stop the consumer groups of group_names as one named phase."""
        def action():
            for name in group_names:
                self._stop_consumer_group(name)
        self.runner.execute_phase(event, action)

    def start_group(self, event: str, group_name: str):
        self.runner.execute_phase(event, lambda: self._init_and_create_consumers(group_name))

    def wait_catchup(self, group_names: list, cooldown_s: float = 0) -> bool:
        return self.runner.collect_until_catchup(
            group_names, len(self.topic_names) * self.partitions, threshold=self.catchup_threshold,
            timeout_s=self.catchup_timeout_s, cooldown_s=cooldown_s)

    def stop_all(self):
        self.runner.execute_phase("stop_all", self._stop_all)

    def close(self):
        """Remove the command-config file that resolve_lag_connection wrote.

        The file holds the SSL and SASL properties of the driver config.
        """
        if self._command_config_file:
            try:
                os.remove(self._command_config_file)
            except FileNotFoundError:
                pass
            self._command_config_file = ""

    def _stop_all(self):
        """Stop everything."""
        self.collector.producing = False
        errors = []
        # Every group was initialized in setup, so stop the inactive ones too.
        for group in [self.producer_group] + list(self.consumer_groups.values()):
            try:
                group.stop_all()
            except Exception as e:
                errors.append(str(e))
        self.collector.active_groups.clear()
        if errors:
            raise RuntimeError("; ".join(errors))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Multi-phase consumer lag/recovery benchmark orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Worker URLs -- either via --workers-file + --worker-split, or explicit per-role args
    parser.add_argument("--workers-file", default=None,
                        help="Path to workers.yaml (flat list). Use with --worker-split.")
    parser.add_argument("--worker-split", default=None,
                        help="Comma-separated split of workers into roles: P,A[,B[,C[,D]]]. "
                             "E.g., '2,2,2,2' assigns first 2 to producers, next 2 to consumer-A, etc.")
    parser.add_argument("--producer-workers", nargs="+", default=[],
                        help="URLs of producer OMB workers (e.g., http://localhost:8080)")
    parser.add_argument("--consumer-a-workers", nargs="+", default=[],
                        help="URLs of consumer group A workers")
    parser.add_argument("--consumer-b-workers", nargs="+", default=[],
                        help="URLs of consumer group B workers")
    parser.add_argument("--consumer-c-workers", nargs="+", default=[],
                        help="URLs of consumer group C workers")
    parser.add_argument("--consumer-d-workers", nargs="+", default=[],
                        help="URLs of consumer group D workers (late-joining, reads from beginning)")

    # Benchmark configuration
    parser.add_argument("--driver-config", required=True,
                        help="Path to Kafka driver YAML config file")
    parser.add_argument("--topics", type=int, default=1,
                        help="Number of topics (default: 1)")
    parser.add_argument("--topic-prefix", default=None,
                        help="Topic name prefix (default: auto-generated with timestamp)")
    parser.add_argument("--partitions", type=int, default=8,
                        help="Partitions per topic (default: 8)")
    parser.add_argument("--message-size", type=int, default=1024,
                        help="Message size in bytes (default: 1024)")
    parser.add_argument("--target-rate-mb", type=float, default=1.0,
                        help="Target producer throughput in MB/s (default: 1.0)")

    # Phase timing
    parser.add_argument("--phase-duration-minutes", type=float, default=2.0,
                        help="Duration of each phase in minutes (default: 2.0)")
    parser.add_argument("--lag-duration-minutes", type=float, default=None,
                        help="How long consumer groups stay stopped in minutes. "
                             "Defaults to --phase-duration-minutes if not set. "
                             "Use to extend lag accumulation independently of phase timing.")
    parser.add_argument("--catchup-timeout-minutes", type=float, default=30,
                        help="Max time to wait for consumer catch-up after restart (default: 30)")
    parser.add_argument("--catchup-threshold", type=float, default=0.99,
                        help="Catch-up threshold as consumed fraction (e.g., 0.99 = 99%% consumed before moving on). Default: 0.99")
    parser.add_argument("--level", type=int, default=2, choices=[1, 2, 3, 4, 5],
                        help="Test level: 1=smoke, 2=lag/recovery, 3=multi-group, "
                             "4=multi-layer traversal (late consumer from beginning), "
                             "5=staged tiered readers (reuse seeded topic; A catches up, "
                             "then B) (default: 2)")
    parser.add_argument("--reuse-topic", action="store_true",
                        help="Attach to a pre-existing (seeded) topic instead of producing "
                             "a fresh backlog. Requires --topic-prefix. The consumer groups of the "
                             "run are new, so they replay from earliest. Used by level 5 "
                             "for fast tiered-read feedback loops.")

    # Kafka lag monitoring
    parser.add_argument("--kafka-bootstrap", default=None,
                        help="Kafka bootstrap servers for lag monitoring "
                             "(default: from the driver config, else localhost:9092)")
    parser.add_argument("--kafka-bin", default="",
                        help="Path to Kafka bin directory (for kafka-consumer-groups.sh)")
    parser.add_argument("--kafka-docker-container", default="",
                        help="Docker container name to exec kafka-consumer-groups.sh in (for local testing)")

    # Output
    parser.add_argument("--output", default="results.json",
                        help="Output file path (default: results.json)")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Metrics poll interval in seconds (default: 10)")

    return parser.parse_args(argv)


def load_workers_file(path: str) -> list[str]:
    """Load worker URLs from workers.yaml: one unquoted '- http...' list item per line."""
    urls = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("- http"):
                urls.append(line[2:].strip())
    return urls


def apply_worker_split(args):
    """Populate per-role worker args from --workers-file and --worker-split."""
    workers = load_workers_file(args.workers_file)
    try:
        try:
            counts = [int(x) for x in args.worker_split.split(",")]
        except ValueError:
            raise ValueError(f"--worker-split needs integers separated by commas "
                             f"(got '{args.worker_split}')") from None
        assignment = split_workers(workers, counts)
    except ValueError as e:
        log.error("%s", e)
        sys.exit(1)
    for role, urls in assignment.items():
        setattr(args, role, urls)
    log.info("Worker split from %s: %s", args.workers_file, assignment)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()

    # Apply workers file if provided
    if args.worker_split and not args.workers_file:
        log.error("--worker-split requires --workers-file")
        sys.exit(1)
    if args.workers_file:
        if not args.worker_split:
            log.error("--workers-file requires --worker-split")
            sys.exit(1)
        apply_worker_split(args)

    errors = validate_args(args) + validate_driver_config(args)
    if errors:
        for e in errors:
            log.error(e)
        sys.exit(1)

    benchmark = LagRecoveryBenchmark(args)
    try:
        result = benchmark.run()
        if result["status"] == "timeout":
            log.error("A consumer group did not catch up within %g min. See the catchup events in %s",
                      args.catchup_timeout_minutes, args.output)
            sys.exit(3)
    except KeyboardInterrupt:
        log.info("Interrupted -- stopping all workers...")
        try:
            benchmark._stop_all()
        except Exception as e:
            log.error("Stop failed: %s", e)
        sys.exit(1)
    except PreflightError as e:
        log.error("ABORT: %s", e)
        sys.exit(1)
    except WorkloadStalledError as e:
        log.error("ABORT: %s", e)
        log.error("Workload stalled -- broker likely unresponsive. "
                  "Check broker/worker logs around this time. Stopping all workers...")
        try:
            benchmark._stop_all()
        except Exception:
            pass
        sys.exit(2)
    except Exception:
        log.exception("Benchmark failed")
        try:
            benchmark._stop_all()
        except Exception:
            pass
        sys.exit(1)
    finally:
        benchmark.close()


if __name__ == "__main__":
    main()
