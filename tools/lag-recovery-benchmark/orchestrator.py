#!/usr/bin/env python3
"""
Orchestrator for multi-phase consumer lag/recovery benchmark.

Uses OMB worker REST API to control dedicated workers per consumer group,
enabling independent lifecycle management (start/stop) of each group.

Progressive test levels:
  L1: Single producer + single consumer, basic cycle
  L2: Single consumer stop/restart with lag recovery
  L3: Two consumer groups with staggered restart
  Full: Three consumer groups with staggered restart (target scenario)

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
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("orchestrator")


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

    def get_period_stats(self) -> dict:
        """GET /period-stats."""
        return self._get_json("/period-stats")

    def get_counters_stats(self) -> dict:
        """GET /counters-stats."""
        return self._get_json("/counters-stats")

    def reset_stats(self):
        """POST /reset-stats."""
        self._post("/reset-stats")

    def __repr__(self):
        return f"OMBWorkerClient({self.base_url})"


# ---------------------------------------------------------------------------
# Worker Group — manages multiple workers assigned to the same role
# ---------------------------------------------------------------------------

class WorkerGroup:
    """A logical group of OMB workers (e.g., all producer workers or all group-A consumer workers)."""

    def __init__(self, name: str, urls: list[str]):
        self.name = name
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
            log.info("Creating consumers on %s (%s) — %d subscriptions", self.name, c.base_url, len(topic_subscriptions))
            c.create_consumers(topic_subscriptions)

    def start_load(self, publish_rate_per_worker: float, message_size: int):
        for c in self.clients:
            log.info("Starting load on %s (%s) at %.0f msg/s", self.name, c.base_url, publish_rate_per_worker)
            c.start_load(publish_rate_per_worker, message_size)

    def stop_all(self):
        for c in self.clients:
            log.info("Stopping all on %s (%s)", self.name, c.base_url)
            c.stop_all()

    def get_period_stats(self) -> list[dict]:
        return [c.get_period_stats() for c in self.clients]

    def get_counters_stats(self) -> list[dict]:
        return [c.get_counters_stats() for c in self.clients]

    def reset_stats(self):
        for c in self.clients:
            c.reset_stats()

    def aggregate_counters(self) -> dict:
        """Aggregate counters across all workers in this group."""
        total = {"messagesSent": 0, "messagesReceived": 0, "messageSendErrors": 0}
        for stats in self.get_counters_stats():
            total["messagesSent"] += stats.get("messagesSent", 0)
            total["messagesReceived"] += stats.get("messagesReceived", 0)
            total["messageSendErrors"] += stats.get("messageSendErrors", 0)
        return total

    def aggregate_period_stats(self) -> dict:
        """Aggregate period stats across all workers in this group."""
        total = {
            "messagesSent": 0, "bytesSent": 0,
            "messagesReceived": 0, "bytesReceived": 0,
            "totalMessagesSent": 0, "totalMessagesReceived": 0,
        }
        for stats in self.get_period_stats():
            for key in total:
                total[key] += stats.get(key, 0)
        return total


# ---------------------------------------------------------------------------
# Kafka Lag Monitor — uses kafka-consumer-groups.sh
# ---------------------------------------------------------------------------

def parse_driver_config(driver_config_path: str) -> Tuple[str, Optional[str]]:
    """Parse driver YAML to extract bootstrap.servers and write a command-config properties file.

    Returns (bootstrap_servers, command_config_path).
    command_config_path is None if no SSL/auth properties are needed.
    """
    common_config = {}
    in_common = False
    with open(driver_config_path) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("commonConfig:"):
                in_common = True
                continue
            if in_common:
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, val = stripped.partition("=")
                    common_config[key.strip()] = val.strip()
                elif stripped and not stripped.startswith("#") and not stripped.startswith("|"):
                    # Hit next YAML key
                    in_common = False

    bootstrap = common_config.pop("bootstrap.servers", None)
    if not bootstrap:
        log.warning("No bootstrap.servers found in driver config")
        return "", None

    # Write remaining connection properties (SSL, auth) to a temp file
    # for kafka-consumer-groups.sh --command-config
    conn_props = {k: v for k, v in common_config.items()
                  if k.startswith("security.") or k.startswith("ssl.") or k.startswith("sasl.")}
    command_config_path = None
    if conn_props:
        import tempfile
        fd, command_config_path = tempfile.mkstemp(prefix="kafka-cmd-", suffix=".properties")
        with os.fdopen(fd, "w") as pf:
            for k, v in conn_props.items():
                pf.write(f"{k}={v}\n")
        log.info("Wrote kafka command-config to %s (%d properties)", command_config_path, len(conn_props))

    return bootstrap, command_config_path


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
            # Try common locations
            for candidate in [
                "kafka-consumer-groups.sh",
                "/opt/kafka/bin/kafka-consumer-groups.sh",
                "/usr/local/bin/kafka-consumer-groups",
            ]:
                if self._cmd_exists(candidate):
                    self.cmd_prefix = [candidate]
                    break
            else:
                self.cmd_prefix = None
                log.warning("kafka-consumer-groups.sh not found — lag monitoring disabled")

    @staticmethod
    def _cmd_exists(cmd: str) -> bool:
        try:
            subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _run_describe(self, group_id: str) -> Optional[str]:
        """Run --describe for a group, return stdout or None."""
        if not self.cmd_prefix:
            return None
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
                log.debug("kafka-consumer-groups failed for %s: %s", group_id, result.stderr)
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("kafka-consumer-groups failed for %s: %s", group_id, e)
            return None

    def get_group_lag(self, group_id: str) -> Optional[dict]:
        """Get total lag for a consumer group."""
        output = self._run_describe(group_id)
        if output is None:
            return None
        return self._parse_describe(output)

    def get_group_offsets(self, group_id: str) -> Optional[dict]:
        """Get detailed per-partition offsets for a consumer group.

        Returns {"total_lag": N, "total_committed": N, "total_log_end": N,
                 "partitions": [{"topic":..., "partition":..., "committed":..., "log_end":..., "lag":...}]}
        """
        output = self._run_describe(group_id)
        if output is None:
            return None

        total_lag = 0
        total_committed = 0
        total_log_end = 0
        partitions = []

        for line in output.strip().split("\n"):
            if not line or line.startswith("GROUP") or line.startswith("Consumer group"):
                continue
            parts = line.split()
            if len(parts) >= 6:
                try:
                    topic = parts[1]
                    partition = int(parts[2])
                    committed = int(parts[3])
                    log_end = int(parts[4])
                    lag = int(parts[5])
                    partitions.append({
                        "topic": topic, "partition": partition,
                        "committed": committed, "log_end": log_end, "lag": lag,
                    })
                    if lag >= 0:
                        total_lag += lag
                    total_committed += committed
                    total_log_end += log_end
                except (ValueError, IndexError):
                    continue

        return {
            "total_lag": total_lag,
            "total_committed": total_committed,
            "total_log_end": total_log_end,
            "partitions": partitions,
        }

    @staticmethod
    def _parse_describe(output: str) -> dict:
        total_lag = 0
        for line in output.strip().split("\n"):
            if not line or line.startswith("GROUP") or line.startswith("Consumer group"):
                continue
            parts = line.split()
            if len(parts) >= 6:
                try:
                    lag = int(parts[5])
                    if lag >= 0:
                        total_lag += lag
                except (ValueError, IndexError):
                    continue
        return {"lag_messages": total_lag}


# ---------------------------------------------------------------------------
# Metrics Collector
# ---------------------------------------------------------------------------

@dataclass
class MetricsSnapshot:
    timestamp_s: float
    producer: dict = field(default_factory=dict)
    consumer_groups: dict = field(default_factory=dict)  # group_name -> stats
    events: list = field(default_factory=list)


class MetricsCollector:
    """Collects and stores time-series metrics from worker groups and Kafka."""

    def __init__(self, producer_group: WorkerGroup, consumer_groups: dict[str, WorkerGroup],
                 lag_monitor: KafkaLagMonitor, message_size: int, poll_interval: int = 10):
        self.producer_group = producer_group
        self.consumer_groups = consumer_groups
        self.lag_monitor = lag_monitor
        self.message_size = message_size
        self.poll_interval = poll_interval
        self.start_time = None
        self.snapshots: list[dict] = []
        self.events: list[dict] = []
        # Track which groups are currently active
        self.active_groups: set[str] = set()

    def start(self):
        self.start_time = time.time()
        self._last_collect_time = self.start_time

    def record_event(self, action: str, **kwargs):
        event = {"time_s": self._elapsed(), "action": action, **kwargs}
        self.events.append(event)
        log.info("EVENT [T+%s]: %s %s", _format_elapsed(event["time_s"]), action, kwargs if kwargs else "")

    def collect(self):
        """Collect one snapshot of metrics from all active groups."""
        now = time.time()
        self._actual_period = now - self._last_collect_time
        self._last_collect_time = now
        elapsed = self._elapsed()
        snapshot = {"timestamp_s": round(elapsed, 1)}

        # Producer stats
        try:
            pstats = self.producer_group.aggregate_period_stats()
            snapshot["producer"] = {
                "rate_msg_s": pstats["messagesSent"],
                "throughput_mb_s": round(pstats["bytesSent"] / 1_048_576, 2),
                "total_sent": pstats["totalMessagesSent"],
            }
        except Exception as e:
            log.debug("Failed to collect producer stats: %s", e)
            snapshot["producer"] = {}

        # Per-group consumer stats
        for group_name, group in self.consumer_groups.items():
            group_stats = {}
            if group_name in self.active_groups:
                try:
                    cstats = group.aggregate_period_stats()
                    group_stats["rate_msg_s"] = cstats["messagesReceived"]
                    group_stats["throughput_mb_s"] = round(cstats["bytesReceived"] / 1_048_576, 2)
                    group_stats["total_received"] = cstats["totalMessagesReceived"]
                except Exception as e:
                    log.debug("Failed to collect stats for %s: %s", group_name, e)

            # Kafka Admin API lag (works even when group is stopped — shows committed offset lag)
            lag = self.lag_monitor.get_group_lag(group_name)
            if lag:
                group_stats["lag_messages"] = lag["lag_messages"]

            group_stats["active"] = group_name in self.active_groups
            snapshot[group_name] = group_stats

        self.snapshots.append(snapshot)
        self._print_snapshot(snapshot)

    def _print_snapshot(self, s: dict):
        period = self._actual_period if self._actual_period > 0 else 1
        parts = [f"T+{_format_elapsed(s['timestamp_s'])}"]
        prod = s.get("producer", {})
        if prod:
            rate = prod.get('rate_msg_s', 0) / period
            tp = prod.get('throughput_mb_s', 0) / period
            parts.append(f"Pub: {rate:.0f} msg/s ({tp:.2f} MB/s)")
        total_consumer_tp = 0.0
        for gname in self.consumer_groups:
            gs = s.get(gname, {})
            status = "ON" if gs.get("active") else "OFF"
            lag = gs.get("lag_messages", "?")
            rate = gs.get("rate_msg_s", 0) / period
            tp = gs.get("throughput_mb_s", 0) / period
            total_consumer_tp += tp
            parts.append(f"{gname}[{status}]: {rate:.0f} msg/s ({tp:.2f} MB/s) lag={lag}")
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
# Phase Runner — imperative execution with catch-up awareness
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as XmYs (e.g., 167m02s)."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s:02d}s"


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
            except Exception as e:
                log.warning("Metrics collection error: %s", e)
            sleep_time = min(self.poll_interval_s, end_time - time.time())
            if sleep_time > 0:
                time.sleep(sleep_time)

    def collect_until_catchup(self, groups: list, threshold: float = 0.99,
                              timeout_s: float = 600, cooldown_s: float = 0):
        """Collect until all groups have caught up, then optionally collect for cooldown.

        threshold: consumed fraction required (e.g., 0.99 = 99% of log-end consumed).
        Returns True if caught up within timeout, False if timed out.
        """
        start = time.time()
        caught_up = False

        while time.time() - start < timeout_s:
            try:
                self.collector.collect()
            except Exception as e:
                log.warning("Metrics collection error: %s", e)

            # Check if all groups are caught up (consumed / log_end >= threshold)
            all_ok = True
            for group_name in groups:
                offsets = self.lag_monitor.get_group_offsets(group_name)
                if offsets and offsets["total_log_end"] > 0:
                    consumed_pct = 1 - (offsets["total_lag"] / offsets["total_log_end"])
                    log.info("  CATCHUP %s: %.2f%% consumed (lag=%d, log_end=%d)",
                             group_name, consumed_pct * 100,
                             offsets["total_lag"], offsets["total_log_end"])
                    if consumed_pct < threshold:
                        all_ok = False
                elif offsets is None:
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

        if cooldown_s > 0:
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


# ---------------------------------------------------------------------------
# Benchmark Orchestrator
# ---------------------------------------------------------------------------

class LagRecoveryBenchmark:
    """Orchestrates the multi-phase consumer lag/recovery benchmark."""

    def __init__(self, args):
        self.args = args
        self.driver_config_bytes = Path(args.driver_config).read_bytes()
        prefix = args.topic_prefix or f"lag-bench-{int(time.time())}"
        self.topic_names = [f"{prefix}-{i}" for i in range(args.topics)]
        self.message_size = args.message_size
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

        # Lag monitor — extract connection properties from driver config
        bootstrap = args.kafka_bootstrap
        command_config = ""
        if not args.kafka_docker_container:
            parsed_bootstrap, command_config_path = parse_driver_config(args.driver_config)
            if not bootstrap or bootstrap == "localhost:9092":
                bootstrap = parsed_bootstrap or bootstrap
            command_config = command_config_path or ""

        self.lag_monitor = KafkaLagMonitor(
            bootstrap, args.kafka_bin,
            docker_container=args.kafka_docker_container,
            command_config=command_config,
        )

        # Catch-up settings
        self.catchup_timeout_s = (args.catchup_timeout_minutes or 30) * 60
        self.catchup_threshold = args.catchup_threshold

        # Metrics
        self.collector = MetricsCollector(
            self.producer_group, self.consumer_groups, self.lag_monitor, self.message_size,
            poll_interval=args.poll_interval,
        )
        self.runner = PhaseRunner(self.collector, self.lag_monitor, poll_interval_s=args.poll_interval)

    def _make_subscriptions(self, group_name: str) -> list[dict]:
        """Build TopicSubscription list for a consumer group."""
        return [{"topic": t, "subscription": group_name} for t in self.topic_names]

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
        offsets = self.lag_monitor.get_group_offsets(group_name)
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
        # Log offsets after stop — committed offset should be preserved
        self._log_group_offsets(group_name, "after_stop")

    def run(self):
        num_groups = len(self.consumer_groups)
        lag_dur_min = self.args.lag_duration_minutes or self.args.phase_duration_minutes
        log.info("Starting lag recovery benchmark with %d consumer group(s)", num_groups)
        log.info("Topics: %s, Partitions: %d, Message size: %d bytes",
                 self.topic_names, self.args.partitions, self.message_size)
        log.info("Target rate: %d MB/s (%.0f msg/s per producer worker)",
                 self.args.target_rate_mb, self.rate_per_producer)
        log.info("Phase duration: %.1f min, lag duration: %.1f min, catchup timeout: %.1f min",
                 self.args.phase_duration_minutes, lag_dur_min,
                 self.catchup_timeout_s / 60)

        # --- Setup ---
        # For level 4, group-D is late-joining — don't create its consumers yet
        deferred_groups = set()
        if self.args.level == 4:
            deferred_groups.add("group-D")

        all_groups = [self.producer_group] + list(self.consumer_groups.values())
        for g in all_groups:
            g.initialize_driver(self.driver_config_bytes)

        # Create topics (on producer leader)
        created_topics = self.producer_group.create_topics(self.topic_names, self.args.partitions)
        log.info("Created topics: %s", created_topics)

        # Create producers
        self.producer_group.create_producers(self.topic_names)

        # Create consumer groups (except deferred ones)
        for group_name, group in self.consumer_groups.items():
            if group_name in deferred_groups:
                log.info("Deferring consumer group %s (late-joining)", group_name)
                continue
            group.create_consumers(self._make_subscriptions(group_name))
            self.collector.active_groups.add(group_name)

        # Start producing
        self.producer_group.start_load(self.rate_per_producer, self.message_size)

        # --- Execute phases ---
        pd = self.phase_duration_s
        ld = self.lag_duration_s
        self.collector.start()

        if self.args.level == 4:
            # Level 4: Multi-layer diskless traversal
            # Hot consumers (A, B, C) read from diskless cache throughout.
            # After data accumulates past local.retention.ms, start group-D
            # from offset 0 — it traverses: tiered → local (TS-consolidated) → diskless cache.
            # Validates: hot consumers maintain throughput, late joiner reads all layers.

            # 1. Warm-up: all hot consumers reading from cache
            log.info("L4 Phase 1: warm-up with hot consumers (A, B, C) for %s",
                     _format_elapsed(pd))
            self.runner.collect_for(pd)

            # 2. Data accumulation: produce until enough data spans all storage layers.
            #    lag_duration_minutes controls how long to accumulate before late join.
            #    Should be > local.retention.ms to ensure tiered data exists.
            log.info("L4 Phase 2: data accumulation for %s (building tiered + local layers)",
                     _format_elapsed(ld))
            self.runner.collect_for(ld)

            # 3. Start late-joining consumer D from beginning (offset 0, fresh group ID)
            self.runner.execute_phase("start_group_D_from_beginning",
                                     lambda: self._init_and_create_consumers("group-D"))

            # 4. Wait for D to catch up while monitoring hot consumer throughput
            log.info("L4 Phase 3: group-D catching up (tiered → local → cache), "
                     "monitoring hot consumer impact")
            self.runner.collect_until_catchup(
                ["group-D"], threshold=self.catchup_threshold,
                timeout_s=self.catchup_timeout_s, cooldown_s=pd)

            # 5. Stop all
            self.runner.execute_phase("stop_all", self._stop_all)

        elif num_groups == 1:
            # L1: warm-up then stop
            self.runner.collect_for(2 * pd)
            self.runner.execute_phase("stop_all", self._stop_all)

        elif num_groups == 2:
            if self.args.level >= 2:
                # L2: warm-up → stop B → lag accumulation → restart B → catch-up → cool-down → stop
                self.runner.collect_for(pd)
                self.runner.execute_phase("stop_group_B", lambda: self._stop_consumer_group("group-B"))
                self.runner.collect_for(ld)
                self.runner.execute_phase("start_group_B", lambda: self._init_and_create_consumers("group-B"))
                self.runner.collect_until_catchup(
                    ["group-B"], threshold=self.catchup_threshold,
                    timeout_s=self.catchup_timeout_s, cooldown_s=pd)
                self.runner.execute_phase("stop_all", self._stop_all)
            else:
                self.runner.collect_for(2 * pd)
                self.runner.execute_phase("stop_all", self._stop_all)

        else:
            # Full scenario: 3 groups (level 3)
            # 1. Warm-up
            self.runner.collect_for(pd)

            # 2. Stop B and C
            self.runner.execute_phase("stop_groups_B_C", self._stop_bc)

            # 3. Lag accumulation for B (ld)
            self.runner.collect_for(ld)

            # 4. Restart B, wait for catch-up
            self.runner.execute_phase("start_group_B", lambda: self._init_and_create_consumers("group-B"))
            self.runner.collect_until_catchup(
                ["group-B"], threshold=self.catchup_threshold,
                timeout_s=self.catchup_timeout_s)

            # 5. Wait remaining lag time for C (C stopped for 2*ld total)
            #    Time elapsed since stop: ld + B_catchup_time. Remaining: ld - B_catchup_time (or 0).
            elapsed_since_stop = self.collector._elapsed() - pd
            remaining_for_c = max(0, 2 * ld - elapsed_since_stop)
            if remaining_for_c > 0:
                log.info("Waiting %s more for C lag accumulation", _format_elapsed(remaining_for_c))
                self.runner.collect_for(remaining_for_c)

            # 6. Restart C, wait for catch-up + cool-down
            self.runner.execute_phase("start_group_C", lambda: self._init_and_create_consumers("group-C"))
            self.runner.collect_until_catchup(
                ["group-C"], threshold=self.catchup_threshold,
                timeout_s=self.catchup_timeout_s, cooldown_s=pd)

            # 7. Stop all
            self.runner.execute_phase("stop_all", self._stop_all)

        # Final collection
        try:
            self.collector.collect()
        except Exception:
            pass

        # --- Results ---
        config = {
            "topics": self.args.topics,
            "partitions": self.args.partitions,
            "message_size_bytes": self.message_size,
            "target_rate_mb_s": self.args.target_rate_mb,
            "phase_duration_minutes": self.args.phase_duration_minutes,
            "lag_duration_minutes": lag_dur_min,
            "catchup_timeout_minutes": self.catchup_timeout_s / 60,
            "consumer_groups": list(self.consumer_groups.keys()),
            "level": self.args.level,
        }
        result = self.collector.to_result(config)

        # Write output
        output_path = self.args.output
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info("Results written to %s", output_path)

        return result

    def _stop_bc(self):
        """Stop consumer groups B and C."""
        if "group-B" in self.consumer_groups:
            self._stop_consumer_group("group-B")
        if "group-C" in self.consumer_groups:
            self._stop_consumer_group("group-C")

    def _stop_all(self):
        """Stop everything."""
        self.producer_group.stop_all()
        for group_name in list(self.collector.active_groups):
            self.consumer_groups[group_name].stop_all()
            self.collector.active_groups.discard(group_name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Multi-phase consumer lag/recovery benchmark orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Worker URLs — either via --workers-file + --worker-split, or explicit per-role args
    parser.add_argument("--workers-file", default=None,
                        help="Path to workers.yaml (flat list). Use with --worker-split.")
    parser.add_argument("--worker-split", default=None,
                        help="Comma-separated split of workers into roles: P,A[,B[,C]]. "
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
    parser.add_argument("--level", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Test level: 1=smoke, 2=lag/recovery, 3=multi-group, "
                             "4=multi-layer traversal (late consumer from beginning) (default: 2)")

    # Kafka lag monitoring
    parser.add_argument("--kafka-bootstrap", default="localhost:9092",
                        help="Kafka bootstrap servers for lag monitoring")
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


def _load_workers_file(path: str) -> list[str]:
    """Load worker URLs from a workers.yaml file (flat list format)."""
    import yaml  # noqa: try stdlib-compatible parsing first
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["workers"]


def _load_workers_file_simple(path: str) -> list[str]:
    """Load worker URLs from workers.yaml without PyYAML dependency."""
    urls = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("- http"):
                urls.append(line[2:].strip())
    return urls


def load_workers_file(path: str) -> list[str]:
    """Load worker URLs from workers.yaml, trying PyYAML first then fallback."""
    try:
        return _load_workers_file(path)
    except ImportError:
        return _load_workers_file_simple(path)


def apply_worker_split(args):
    """Populate per-role worker args from --workers-file and --worker-split."""
    workers = load_workers_file(args.workers_file)
    counts = [int(x) for x in args.worker_split.split(",")]
    if len(counts) < 2:
        log.error("--worker-split needs at least 2 values: P,A (got %d)", len(counts))
        sys.exit(1)
    if sum(counts) > len(workers):
        log.error("--worker-split total (%d) exceeds available workers (%d)", sum(counts), len(workers))
        sys.exit(1)

    roles = ["producer_workers", "consumer_a_workers", "consumer_b_workers", "consumer_c_workers", "consumer_d_workers"]
    offset = 0
    for i, count in enumerate(counts):
        if i >= len(roles):
            break
        setattr(args, roles[i], workers[offset:offset + count])
        offset += count

    log.info("Worker split from %s: %s", args.workers_file,
             {roles[i]: getattr(args, roles[i]) for i in range(len(counts)) if i < len(roles)})


def main():
    args = parse_args()

    # Apply workers file if provided
    if args.workers_file:
        if not args.worker_split:
            log.error("--workers-file requires --worker-split")
            sys.exit(1)
        apply_worker_split(args)

    if not args.producer_workers:
        log.error("No producer workers specified. Use --producer-workers or --workers-file + --worker-split")
        sys.exit(1)

    # Validate worker configuration matches level
    if args.level >= 2 and not args.consumer_a_workers:
        log.error("Level %d requires --consumer-a-workers", args.level)
        sys.exit(1)
    if args.level >= 2 and not args.consumer_b_workers:
        log.warning("Level %d typically uses --consumer-b-workers for lag/recovery testing", args.level)
    if args.level >= 3 and not args.consumer_b_workers:
        log.error("Level 3 requires --consumer-b-workers")
        sys.exit(1)
    if args.level >= 4 and not args.consumer_d_workers:
        log.error("Level 4 requires --consumer-d-workers (late-joining consumer)")
        sys.exit(1)

    benchmark = LagRecoveryBenchmark(args)
    try:
        benchmark.run()
    except KeyboardInterrupt:
        log.info("Interrupted — stopping all workers...")
        benchmark._stop_all()
        sys.exit(1)
    except Exception:
        log.exception("Benchmark failed")
        try:
            benchmark._stop_all()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
