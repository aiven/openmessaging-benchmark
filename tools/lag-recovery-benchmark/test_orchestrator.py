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

"""Unit tests for the lag recovery core in orchestrator.py.

Run from this directory: python3 -m unittest -v test_orchestrator
"""

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import orchestrator as o


DESCRIBE_OUTPUT = """
GROUP    TOPIC   PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG  CONSUMER-ID  HOST  CLIENT-ID
group-A  topic-0 0          100             150             50   c1           /h    omb
group-A  topic-0 1          200             200             0    c1           /h    omb
group-A  topic-0 2          -               80              -    -            -     -
"""


def make_args(**overrides):
    defaults = dict(
        level=2, producer_workers=["p1"], consumer_a_workers=["a1"],
        consumer_b_workers=["b1"], consumer_c_workers=[], consumer_d_workers=[],
        reuse_topic=False, topic_prefix=None, worker_split=None, output="results.json",
        topics=1, partitions=8, message_size=1024, poll_interval=10,
        target_rate_mb=1.0, phase_duration_minutes=2.0, lag_duration_minutes=None,
        catchup_timeout_minutes=30, catchup_threshold=0.99, driver_config="driver.yaml",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class FakeCtx:
    """Records the steps a scenario drives. wait_catchup results are scripted."""

    def __init__(self, catchup_results=None, elapsed_s=0.0):
        self.calls = []
        self._catchup_results = list(catchup_results or [])
        self._elapsed_s = elapsed_s

    def elapsed(self):
        return self._elapsed_s

    def collect_for(self, duration_s):
        self.calls.append(("collect_for", duration_s))

    def stop_groups(self, event, group_names):
        self.calls.append(("stop_groups", event, group_names))

    def start_group(self, event, group_name):
        self.calls.append(("start_group", event, group_name))

    def wait_catchup(self, group_names, cooldown_s=0):
        self.calls.append(("wait_catchup", group_names, cooldown_s))
        return self._catchup_results.pop(0) if self._catchup_results else True

    def stop_all(self):
        self.calls.append(("stop_all",))


class FormatElapsedTest(unittest.TestCase):
    def test_formats_minutes_and_zero_padded_seconds(self):
        self.assertEqual(o._format_elapsed(0), "0m00s")
        self.assertEqual(o._format_elapsed(125), "2m05s")
        self.assertEqual(o._format_elapsed(10022), "167m02s")


class CounterRatesTest(unittest.TestCase):
    def test_first_sample_has_no_baseline(self):
        self.assertIsNone(o.CounterRates().rate("k", 100, 10))

    def test_rate_is_delta_over_elapsed_time(self):
        r = o.CounterRates()
        r.rate("k", 100, 10)
        self.assertEqual(r.rate("k", 300, 10), 20.0)

    def test_keys_are_independent(self):
        r = o.CounterRates()
        r.rate("a", 0, 1)
        self.assertIsNone(r.rate("b", 500, 1))

    def test_counter_going_backwards_reseeds_baseline(self):
        r = o.CounterRates()
        r.rate("k", 1000, 10)
        self.assertIsNone(r.rate("k", 50, 10))
        self.assertEqual(r.rate("k", 150, 10), 10.0)

    def test_non_positive_dt_gives_no_rate(self):
        r = o.CounterRates()
        r.rate("k", 0, 10)
        self.assertIsNone(r.rate("k", 10, 0))

    def test_forget_drops_baseline(self):
        r = o.CounterRates()
        r.rate("k", 100, 10)
        r.forget("k")
        self.assertIsNone(r.rate("k", 200, 10))

class StallDetectorTest(unittest.TestCase):
    def test_trips_after_consecutive_low_polls(self):
        d = o.StallDetector(min_rate=1.0, polls=3)
        self.assertFalse(d.observe(0.0))
        self.assertFalse(d.observe(0.5))
        self.assertTrue(d.observe(0.0))

    def test_healthy_poll_resets_the_count(self):
        d = o.StallDetector(min_rate=1.0, polls=3)
        d.observe(0.0)
        d.observe(0.0)
        self.assertFalse(d.observe(100.0))
        self.assertEqual(d.count, 0)
        self.assertFalse(d.observe(0.0))

    def test_rate_equal_to_min_is_not_a_stall(self):
        d = o.StallDetector(min_rate=1.0, polls=1)
        self.assertFalse(d.observe(1.0))

    def test_missing_rate_is_ignored(self):
        d = o.StallDetector(min_rate=1.0, polls=1)
        self.assertFalse(d.observe(None))
        self.assertEqual(d.count, 0)

class DescribeParsingTest(unittest.TestCase):
    def test_parses_rows_and_skips_headers_and_keeps_partitions_without_a_commit(self):
        rows = o.parse_describe_rows(DESCRIBE_OUTPUT)
        self.assertEqual(rows, [
            {"topic": "topic-0", "partition": 0, "committed": 100, "log_end": 150, "lag": 50},
            {"topic": "topic-0", "partition": 1, "committed": 200, "log_end": 200, "lag": 0},
            {"topic": "topic-0", "partition": 2, "committed": None, "log_end": 80, "lag": 80},
        ])

    def test_skips_rows_without_a_log_end_offset(self):
        out = "group-A  topic-0 0          -               -               -    -            -     -\n"
        self.assertEqual(o.parse_describe_rows(out), [])

    def test_skips_consumer_group_banner_and_short_lines(self):
        out = "Consumer group 'g' has no active members.\nshort line\n"
        self.assertEqual(o.parse_describe_rows(out), [])

    def test_empty_output(self):
        self.assertEqual(o.parse_describe_rows(""), [])

    def test_summary_totals(self):
        s = o.summarize_offsets(o.parse_describe_rows(DESCRIBE_OUTPUT))
        self.assertEqual(s["total_lag"], 130)
        self.assertEqual(s["total_committed"], 300)
        self.assertEqual(s["total_log_end"], 430)
        self.assertEqual(len(s["partitions"]), 3)

    def test_negative_lag_is_left_out_of_total_lag_only(self):
        rows = [
            {"topic": "t", "partition": 0, "committed": 10, "log_end": 5, "lag": -5},
            {"topic": "t", "partition": 1, "committed": 1, "log_end": 4, "lag": 3},
        ]
        s = o.summarize_offsets(rows)
        self.assertEqual(s["total_lag"], 3)
        self.assertEqual(s["total_committed"], 11)
        self.assertEqual(s["total_log_end"], 9)


class KafkaLagMonitorTest(unittest.TestCase):
    def setUp(self):
        self.monitor = o.KafkaLagMonitor("broker:9092", kafka_bin="/opt/kafka/bin")

    def test_group_offsets(self):
        with mock.patch.object(self.monitor, "_run_describe", return_value=DESCRIBE_OUTPUT):
            offsets = self.monitor.get_group_offsets("group-A")
        self.assertEqual(offsets["total_log_end"], 430)

    def test_describe_without_usable_rows_gives_none(self):
        output = ("GROUP    TOPIC   PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG  CONSUMER-ID  HOST  CLIENT-ID\n"
                  "group-D  topic-0 0          -               -               -    -            -     -\n")
        with mock.patch.object(self.monitor, "_run_describe", return_value=output):
            self.assertIsNone(self.monitor.get_group_offsets("group-D"))

    def test_new_group_without_commits_has_the_whole_log_as_lag_and_is_not_caught_up(self):
        output = ("GROUP    TOPIC   PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG  CONSUMER-ID  HOST  CLIENT-ID\n"
                  "group-D  topic-0 0          -               80              -    -            -     -\n"
                  "group-D  topic-0 1          -               120             -    -            -     -\n")
        with mock.patch.object(self.monitor, "_run_describe", return_value=output):
            offsets = self.monitor.get_group_offsets("group-D")
        self.assertEqual(offsets["total_lag"], 200)
        self.assertEqual(offsets["total_committed"], 0)
        self.assertFalse(o.is_caught_up(offsets, 0.99, 2))

    def test_partition_without_commit_keeps_a_group_that_is_behind_on_it_from_catching_up(self):
        output = ("GROUP    TOPIC   PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG  CONSUMER-ID  HOST  CLIENT-ID\n"
                  "group-D  topic-0 0          1000            1000            0    c1           /h    omb\n"
                  "group-D  topic-0 1          -               1000            -    -            -     -\n")
        with mock.patch.object(self.monitor, "_run_describe", return_value=output):
            offsets = self.monitor.get_group_offsets("group-D")
        self.assertFalse(o.is_caught_up(offsets, 0.99, 2))

    def test_unavailable_describe_gives_none(self):
        with mock.patch.object(self.monitor, "_run_describe", return_value=None):
            self.assertIsNone(self.monitor.get_group_offsets("g"))


class RunDescribeFailureTest(unittest.TestCase):
    def test_first_failure_of_each_group_warns_once(self):
        monitor = o.KafkaLagMonitor("broker:9092", kafka_bin="/opt/kafka/bin")
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Error: failed\n")
        with mock.patch.object(o.subprocess, "run", return_value=failed), \
                mock.patch.object(o, "log") as log:
            self.assertIsNone(monitor.get_group_offsets("group-A"))
            monitor.get_group_offsets("group-A")
            monitor.get_group_offsets("group-B")
        self.assertEqual(log.warning.call_count, 2)


class PreflightCheckTest(unittest.TestCase):
    """Without a working lag CLI, every catch-up wait would run to its timeout."""

    def check(self, **run):
        monitor = o.KafkaLagMonitor("broker:9092", kafka_bin="/opt/kafka/bin")
        with mock.patch.object(o.subprocess, "run", **run) as patched:
            monitor.check()
        return patched.call_args[0][0]

    def test_working_cli_passes(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        cmd = self.check(return_value=ok)
        self.assertEqual(cmd[0], "/opt/kafka/bin/kafka-consumer-groups.sh")
        self.assertIn("--list", cmd)

    def test_missing_cli_raises(self):
        with self.assertRaises(o.PreflightError) as ctx:
            self.check(side_effect=FileNotFoundError("kafka-consumer-groups.sh"))
        self.assertIn("--kafka-bin", str(ctx.exception))

    def test_unreachable_cluster_raises(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="",
                                             stderr="Timed out waiting for a node\n")
        with self.assertRaises(o.PreflightError) as ctx:
            self.check(return_value=failed)
        self.assertIn("broker:9092", str(ctx.exception))

    def test_default_is_the_cli_on_the_path(self):
        self.assertEqual(o.KafkaLagMonitor("broker:9092").cmd_prefix, ["kafka-consumer-groups.sh"])


class DescribeTopicTest(unittest.TestCase):
    DESCRIBE = ("Topic: seed-0\tTopicId: abc\tPartitionCount: 4\tReplicationFactor: 1\tConfigs: \n"
                "\tTopic: seed-0\tPartition: 0\tLeader: 1\tReplicas: 1\tIsr: 1\n")

    def completed(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def run_describe(self, monitor, result):
        with mock.patch.object(o.subprocess, "run", return_value=result) as run:
            count = monitor.describe_topic("seed-0")
        return count, run.call_args[0][0]

    def test_partition_count_is_read_from_the_output(self):
        monitor = o.KafkaLagMonitor("broker:9092", kafka_bin="/opt/kafka/bin")
        count, cmd = self.run_describe(monitor, self.completed(self.DESCRIBE))
        self.assertEqual(count, 4)
        self.assertEqual(cmd[0], "/opt/kafka/bin/kafka-topics.sh")

    def test_docker_mode_runs_kafka_topics_in_the_container(self):
        monitor = o.KafkaLagMonitor("kafka:9092", docker_container="kafka-container")
        _, cmd = self.run_describe(monitor, self.completed(self.DESCRIBE))
        self.assertEqual(cmd[:4], ["docker", "exec", "kafka-container", "/opt/kafka/bin/kafka-topics.sh"])

    def test_missing_topic_raises_with_the_first_error_line(self):
        monitor = o.KafkaLagMonitor("broker:9092", kafka_bin="/opt/kafka/bin")
        result = self.completed(stderr="Error while executing topic command: Topic 'seed-0' does not exist\n\tat x",
                                returncode=1)
        with mock.patch.object(o.subprocess, "run", return_value=result):
            with self.assertRaises(o.PreflightError) as ctx:
                monitor.describe_topic("seed-0")
        self.assertIn("seed-0", str(ctx.exception))
        self.assertIn("does not exist", str(ctx.exception))
        self.assertNotIn("\tat x", str(ctx.exception))

class ResolveLagConnectionTest(unittest.TestCase):
    """The lag queries use the bootstrap of the driver config unless the user sets one."""

    def setUp(self):
        fd, self.driver_path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write("driverClass: x\ncommonConfig: |\n  bootstrap.servers=kafka:9092\n"
                    "  security.protocol=SSL\n  ssl.truststore.password=secret\nproducerConfig: |\n  acks=all\n")
        self.written = []

    def tearDown(self):
        os.remove(self.driver_path)
        for path in self.written:
            if path and os.path.exists(path):
                os.remove(path)

    def resolve(self, bootstrap, docker_container):
        result = o.resolve_lag_connection(bootstrap, docker_container, self.driver_path)
        self.written.append(result[1])
        return result

    def test_docker_mode_takes_bootstrap_from_driver_config_and_writes_no_file(self):
        bootstrap, command_config = self.resolve(None, "kafka-container")
        self.assertEqual(bootstrap, "kafka:9092")
        self.assertEqual(command_config, "")

    def test_docker_mode_keeps_explicit_bootstrap(self):
        bootstrap, command_config = self.resolve("other:9093", "kafka-container")
        self.assertEqual(bootstrap, "other:9093")
        self.assertEqual(command_config, "")

    def test_host_mode_takes_bootstrap_and_writes_connection_properties(self):
        bootstrap, command_config = self.resolve(None, "")
        self.assertEqual(bootstrap, "kafka:9092")
        with open(command_config) as f:
            content = f.read()
        self.assertIn("security.protocol=SSL", content)
        self.assertNotIn("bootstrap.servers", content)
        self.assertNotIn("acks", content)

    def test_explicit_localhost_bootstrap_is_kept(self):
        for container in ("kafka-container", ""):
            with self.subTest(container=container):
                bootstrap, _ = self.resolve("localhost:9092", container)
                self.assertEqual(bootstrap, "localhost:9092")

    def test_driver_config_without_bootstrap_falls_back_to_localhost(self):
        with open(self.driver_path, "w") as f:
            f.write("driverClass: x\ncommonConfig: |\n  security.protocol=SSL\n")
        bootstrap, command_config = self.resolve(None, "")
        self.assertEqual(bootstrap, "localhost:9092")
        with open(command_config) as f:
            self.assertIn("security.protocol=SSL", f.read())

    def test_connection_properties_are_kept_without_a_bootstrap_line(self):
        with open(self.driver_path, "w") as f:
            f.write("driverClass: x\ncommonConfig: |\n  security.protocol=SSL\n"
                    "  ssl.truststore.password=secret\n")
        bootstrap, command_config = self.resolve("broker:9093", "")
        self.assertEqual(bootstrap, "broker:9093")
        with open(command_config) as f:
            content = f.read()
        self.assertIn("security.protocol=SSL", content)
        self.assertIn("ssl.truststore.password=secret", content)


class DriverConfigTest(unittest.TestCase):
    """Levels 4 and 5 start fresh consumer groups, so the driver file must read from earliest."""

    def driver_file(self, content):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        self.addCleanup(os.remove, path)
        return path

    def errors(self, content, level=4):
        return o.validate_driver_config(make_args(level=level, driver_config=self.driver_file(content)))

    def test_read_config_block_reads_only_the_named_block(self):
        path = self.driver_file(
            "name: x\ncommonConfig: |\n  bootstrap.servers=b:9092\n  # comment\n"
            "consumerConfig: |\n  auto.offset.reset=earliest\n  fetch.min.bytes=1\n"
            "producerConfig: |\n  acks=all\n")
        self.assertEqual(o.read_config_block(path, "commonConfig"), {"bootstrap.servers": "b:9092"})
        self.assertEqual(o.read_config_block(path, "consumerConfig"),
                         {"auto.offset.reset": "earliest", "fetch.min.bytes": "1"})
        self.assertEqual(o.read_config_block(path, "missingConfig"), {})

    def test_earliest_in_consumer_config_is_accepted(self):
        for level in (4, 5):
            with self.subTest(level=level):
                self.assertEqual(
                    self.errors("consumerConfig: |\n  auto.offset.reset=earliest\n", level), [])

    def test_earliest_in_common_config_is_accepted(self):
        self.assertEqual(self.errors("commonConfig: |\n  auto.offset.reset=earliest\n"), [])

    def test_latest_is_rejected_with_the_value_in_the_message(self):
        errors = self.errors("consumerConfig: |\n  auto.offset.reset=latest\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("auto.offset.reset=earliest", errors[0])
        self.assertIn("found: latest", errors[0])

    def test_missing_setting_is_rejected(self):
        for level in (4, 5):
            with self.subTest(level=level):
                errors = self.errors("consumerConfig: |\n  fetch.min.bytes=1\n", level)
                self.assertEqual(len(errors), 1)
                self.assertIn("found: not set", errors[0])

    def test_consumer_config_overrides_common_config(self):
        errors = self.errors("commonConfig: |\n  auto.offset.reset=earliest\n"
                             "consumerConfig: |\n  auto.offset.reset=latest\n")
        self.assertEqual(len(errors), 1)

    def test_other_levels_do_not_check_the_driver_file(self):
        for level in (1, 2, 3):
            with self.subTest(level=level):
                self.assertEqual(
                    self.errors("consumerConfig: |\n  auto.offset.reset=latest\n", level), [])

    def test_reuse_topic_needs_earliest_at_every_level(self):
        for level in (1, 2, 3):
            with self.subTest(level=level):
                args = make_args(level=level, reuse_topic=True, topic_prefix="seed",
                                 driver_config=self.driver_file("consumerConfig: |\n  auto.offset.reset=latest\n"))
                errors = o.validate_driver_config(args)
                self.assertEqual(len(errors), 1)
                self.assertIn("--reuse-topic", errors[0])

    def test_unreadable_driver_file_is_an_error_for_level_four(self):
        errors = o.validate_driver_config(make_args(level=4, driver_config="/nonexistent/driver.yaml"))
        self.assertEqual(len(errors), 1)
        self.assertIn("Cannot read the driver config", errors[0])

    def test_main_exits_before_building_the_benchmark(self):
        path = self.driver_file("consumerConfig: |\n  auto.offset.reset=latest\n")
        with mock.patch.object(o, "parse_args",
                               return_value=make_args(level=4, workers_file=None, driver_config=path)), \
                mock.patch.object(o, "validate_args", return_value=[]), \
                mock.patch.object(o, "LagRecoveryBenchmark") as benchmark_class:
            with self.assertRaises(SystemExit) as ctx:
                o.main()
        self.assertEqual(ctx.exception.code, 1)
        benchmark_class.assert_not_called()


class RateBaselineTest(unittest.TestCase):
    """A rate without a baseline is None, so a failed stats call gives no false spike or zero."""

    class Monitor:
        def get_group_offsets(self, group_id):
            return None

    class Producers:
        kafka_group_id = "producers"

    class Consumers:
        kafka_group_id = "group-A"

    def collector(self, producer_results, consumer_results=None):
        clock = [1000.0]
        producers = self.Producers()
        producers.aggregate_counters = mock.Mock(side_effect=producer_results)
        groups = {}
        if consumer_results is not None:
            consumers = self.Consumers()
            consumers.aggregate_counters = mock.Mock(side_effect=consumer_results)
            groups["group-A"] = consumers
        with mock.patch.object(o.time, "time", side_effect=lambda: clock[0]):
            c = o.MetricsCollector(producers, groups, self.Monitor(), 1024)
            c.active_groups = set(groups)
            c.start()
            snapshots = []
            for _ in range(3):
                clock[0] += 10
                c.collect()
                snapshots.append(c.snapshots[-1])
        return snapshots

    @staticmethod
    def sent(count):
        return {"messagesSent": count, "messagesReceived": 0, "messageSendErrors": 0}

    @staticmethod
    def received(count):
        return {"messagesSent": 0, "messagesReceived": count, "messageSendErrors": 0}

    def test_producer_rate_after_a_failed_poll_does_not_spike(self):
        snapshots = self.collector([self.sent(1000), OSError("down"), self.sent(3000)])
        self.assertEqual(snapshots[1]["producer"], {})
        self.assertIsNone(snapshots[2]["producer"]["rate_msg_s"])
        self.assertIsNone(snapshots[2]["producer"]["send_error_rate"])

    def test_producer_rate_without_a_failure_is_computed(self):
        snapshots = self.collector([self.sent(1000), self.sent(2000), self.sent(3000)])
        self.assertIsNone(snapshots[0]["producer"]["rate_msg_s"])
        self.assertEqual(snapshots[2]["producer"]["rate_msg_s"], 100.0)

    def test_consumer_rate_after_a_failed_poll_does_not_spike(self):
        snapshots = self.collector(
            [self.sent(1000)] * 3, [self.received(500), OSError("down"), self.received(1500)])
        self.assertNotIn("rate_msg_s", snapshots[1]["group-A"])
        self.assertIsNone(snapshots[2]["group-A"]["rate_msg_s"])


class ExecutePhaseTest(unittest.TestCase):
    def test_failed_action_is_recorded_and_raised(self):
        collector = mock.Mock()
        collector._elapsed.return_value = 5.0
        runner = o.PhaseRunner(collector, mock.Mock(), poll_interval_s=0)
        with self.assertRaises(RuntimeError):
            runner.execute_phase("stop_group_B", mock.Mock(side_effect=RuntimeError("worker down")))
        collector.record_event.assert_any_call("stop_group_B")
        collector.record_event.assert_any_call("stop_group_B_failed", error="worker down")

    def test_successful_action_records_only_the_start(self):
        collector = mock.Mock()
        collector._elapsed.return_value = 5.0
        runner = o.PhaseRunner(collector, mock.Mock(), poll_interval_s=0)
        runner.execute_phase("stop_all", mock.Mock())
        collector.record_event.assert_called_once_with("stop_all")


class StopAllTest(unittest.TestCase):
    def worker_group(self, results):
        group = o.WorkerGroup("group-X", ["http://w%d" % i for i in range(len(results))])
        for client, result in zip(group.clients, results):
            client.stop_all = mock.Mock(side_effect=result)
        return group

    def test_every_worker_is_stopped_when_the_first_fails(self):
        group = self.worker_group([OSError("dead"), None, None])
        with self.assertRaises(RuntimeError) as ctx:
            group.stop_all()
        for client in group.clients:
            client.stop_all.assert_called_once()
        self.assertIn("1 of 3 workers", str(ctx.exception))
        self.assertIn("dead", str(ctx.exception))

    def test_no_error_when_every_worker_stops(self):
        self.worker_group([None, None]).stop_all()

    def benchmark(self, groups, active):
        benchmark = o.LagRecoveryBenchmark.__new__(o.LagRecoveryBenchmark)
        benchmark.producer_group = mock.Mock()
        benchmark.consumer_groups = groups
        benchmark.collector = mock.Mock()
        benchmark.collector.active_groups = set(active)
        return benchmark

    def test_inactive_consumer_groups_are_stopped_too(self):
        groups = {"group-A": mock.Mock(), "group-B": mock.Mock()}
        benchmark = self.benchmark(groups, ["group-A"])
        benchmark._stop_all()
        benchmark.producer_group.stop_all.assert_called_once()
        groups["group-A"].stop_all.assert_called_once()
        groups["group-B"].stop_all.assert_called_once()
        self.assertEqual(benchmark.collector.active_groups, set())
        self.assertFalse(benchmark.collector.producing)

    def test_a_failing_group_does_not_prevent_stopping_the_others(self):
        groups = {"group-A": mock.Mock(), "group-B": mock.Mock()}
        groups["group-A"].stop_all.side_effect = RuntimeError("A failed")
        benchmark = self.benchmark(groups, ["group-A", "group-B"])
        benchmark.producer_group.stop_all.side_effect = RuntimeError("producers failed")
        with self.assertRaises(RuntimeError) as ctx:
            benchmark._stop_all()
        groups["group-B"].stop_all.assert_called_once()
        self.assertIn("producers failed", str(ctx.exception))
        self.assertIn("A failed", str(ctx.exception))
        self.assertEqual(benchmark.collector.active_groups, set())


class OffsetEventTest(unittest.TestCase):
    def test_event_has_the_offsets_of_each_partition(self):
        partitions = [
            {"topic": "t", "partition": 0, "committed": 5, "log_end": 9, "lag": 4},
            {"topic": "t", "partition": 1, "committed": 7, "log_end": 7, "lag": 0},
        ]
        benchmark = o.LagRecoveryBenchmark.__new__(o.LagRecoveryBenchmark)
        benchmark.consumer_groups = {"group-B": mock.Mock(kafka_group_id="group-B")}
        benchmark.lag_monitor = mock.Mock()
        benchmark.lag_monitor.get_group_offsets.return_value = o.summarize_offsets(partitions)
        benchmark.collector = mock.Mock()
        benchmark._log_group_offsets("group-B", "before_stop")
        benchmark.collector.record_event.assert_called_once_with(
            "offsets_before_stop", group="group-B", committed=12, log_end=16, lag=4,
            partitions=partitions)


def build_benchmark(test, extra_args=(), output=None):
    """A LagRecoveryBenchmark in Docker mode, built from CLI arguments. Cleans its temp files."""
    fd, driver = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write("commonConfig: |\n  bootstrap.servers=kafka:9092\n")
    test.addCleanup(os.remove, driver)
    args = o.parse_args([
        "--producer-workers", "http://p", "--consumer-a-workers", "http://a",
        "--consumer-b-workers", "http://b", "--driver-config", driver,
        "--kafka-docker-container", "kafka-container",
        "--output", output or os.devnull, *extra_args])
    return o.LagRecoveryBenchmark(args)


class GroupIdTest(unittest.TestCase):
    """Every run has its own Kafka group IDs, and they stay the same within the run."""

    def test_default_topic_names_differ_between_runs_started_in_the_same_second(self):
        with mock.patch.object(o.time, "time", return_value=1_000_000.0):
            first, second = build_benchmark(self), build_benchmark(self)
        self.assertTrue(set(first.topic_names).isdisjoint(second.topic_names))
        self.assertIn(first.run_id, first.topic_names[0])

    def test_explicit_topic_prefix_is_used_as_given(self):
        benchmark = build_benchmark(self, ["--topic-prefix", "seed", "--topics", "2"])
        self.assertEqual(benchmark.topic_names, ["seed-0", "seed-1"])

    def test_group_id_is_the_name_with_the_run_id(self):
        benchmark = build_benchmark(self)
        self.assertEqual(benchmark.consumer_groups["group-A"].kafka_group_id,
                         f"group-A-{benchmark.run_id}")
        self.assertEqual(benchmark.consumer_groups["group-B"].kafka_group_id,
                         f"group-B-{benchmark.run_id}")


class RunResultsTest(unittest.TestCase):
    """The result file is written for a completed run and for an aborted one."""

    COUNTERS = {"messagesSent": 10, "messagesReceived": 0, "messageSendErrors": 0}
    check_error = None
    created_topics = None

    def setUp(self):
        self.output = os.path.join(tempfile.mkdtemp(), "results.json")
        self.addCleanup(lambda: os.path.exists(self.output) and os.remove(self.output))

    def run_benchmark(self, scenario, output=None, extra_args=()):
        benchmark = build_benchmark(self, ["--level", "1", *extra_args], output=output or self.output)
        benchmark.collector.lag_monitor = mock.Mock()
        benchmark.collector.lag_monitor.get_group_offsets.return_value = None
        network = dict(initialize_driver=mock.DEFAULT, create_topics=mock.DEFAULT,
                       create_producers=mock.DEFAULT, create_consumers=mock.DEFAULT,
                       start_load=mock.DEFAULT, stop_all=mock.DEFAULT)
        with mock.patch.multiple(o.WorkerGroup, **network) as patched, \
                mock.patch.object(o.WorkerGroup, "aggregate_counters", return_value=self.COUNTERS), \
                mock.patch.object(o.KafkaLagMonitor, "check", side_effect=self.check_error), \
                mock.patch.dict(o.SCENARIOS, {1: scenario}):
            self.patched = patched
            patched["create_topics"].return_value = (
                benchmark.topic_names if self.created_topics is None else self.created_topics)
            return benchmark.run()

    def read_result(self):
        with open(self.output) as f:
            return json.load(f)

    def test_completed_run_has_status_completed(self):
        def scenario(ctx, timing, groups):
            ctx.collector.record_event("custom")
        self.run_benchmark(scenario)
        result = self.read_result()
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("error", result)
        self.assertIn("custom", [e["action"] for e in result["events"]])

    def test_run_with_a_catchup_timeout_has_status_timeout(self):
        def scenario(ctx, timing, groups):
            ctx.collector.record_event("catchup", groups=["group-B"], caught_up=False, waited_s=3)
        result = self.run_benchmark(scenario)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(self.read_result()["status"], "timeout")
        self.assertNotIn("error", result)

    def test_run_with_only_successful_catchups_has_status_completed(self):
        def scenario(ctx, timing, groups):
            ctx.collector.record_event("catchup", groups=["group-B"], caught_up=True, waited_s=3)
        self.assertEqual(self.run_benchmark(scenario)["status"], "completed")

    def test_result_config_has_the_run_settings(self):
        self.run_benchmark(lambda ctx, timing, groups: None,
                           extra_args=["--catchup-threshold", "0.9"])
        config = self.read_result()["config"]
        self.assertEqual(config["catchup_threshold"], 0.9)
        self.assertRegex(config["run_id"], "^[0-9a-f]{8}$")
        self.assertEqual(config["kafka_group_ids"], {
            "group-A": f"group-A-{config['run_id']}",
            "group-B": f"group-B-{config['run_id']}",
        })

    def test_failed_preflight_aborts_before_touching_a_worker(self):
        self.check_error = o.PreflightError("kafka-consumer-groups.sh not found")
        with self.assertRaises(o.PreflightError):
            self.run_benchmark(lambda ctx, timing, groups: None)
        self.patched["initialize_driver"].assert_not_called()
        self.assertFalse(os.path.exists(self.output))

    def test_normal_run_creates_the_topics(self):
        self.run_benchmark(lambda ctx, timing, groups: None)
        self.patched["create_topics"].assert_called_once()

    def test_run_stops_when_the_workers_create_other_topic_names(self):
        self.created_topics = ["test-topic-0000000-abc"]
        with self.assertRaises(RuntimeError) as ctx:
            self.run_benchmark(lambda ctx, timing, groups: None)
        self.assertIn("topicNames", str(ctx.exception))
        self.patched["create_producers"].assert_not_called()

    def test_reuse_mode_checks_the_topic_and_does_not_create_it(self):
        with mock.patch.object(o.KafkaLagMonitor, "describe_topic", return_value=4) as describe:
            self.run_benchmark(lambda ctx, timing, groups: None,
                               extra_args=["--reuse-topic", "--topic-prefix", "seed"])
        describe.assert_called_once_with("seed-0")
        self.patched["create_topics"].assert_not_called()
        self.patched["create_producers"].assert_called_once()
        self.assertEqual(self.read_result()["config"]["partitions"], 4)

    def test_reuse_mode_aborts_before_touching_a_worker_when_the_topic_is_missing(self):
        missing = o.PreflightError("Topic seed-0 was not found")
        with mock.patch.object(o.KafkaLagMonitor, "describe_topic", side_effect=missing):
            with self.assertRaises(o.PreflightError):
                self.run_benchmark(lambda ctx, timing, groups: None,
                                   extra_args=["--reuse-topic", "--topic-prefix", "seed"])
        self.patched["initialize_driver"].assert_not_called()
        self.patched["create_topics"].assert_not_called()
        self.assertFalse(os.path.exists(self.output))

    def test_reuse_mode_rejects_topics_with_different_partition_counts(self):
        with mock.patch.object(o.KafkaLagMonitor, "describe_topic", side_effect=[4, 6]):
            with self.assertRaises(o.PreflightError) as ctx:
                self.run_benchmark(lambda ctx, timing, groups: None,
                                   extra_args=["--reuse-topic", "--topic-prefix", "seed",
                                               "--topics", "2"])
        self.assertIn("seed-0=4", str(ctx.exception))
        self.assertIn("seed-1=6", str(ctx.exception))
        self.patched["initialize_driver"].assert_not_called()
        self.assertFalse(os.path.exists(self.output))

    def test_stalled_run_writes_partial_results_and_raises(self):
        def scenario(ctx, timing, groups):
            ctx.collector.collect()
            ctx.collector.record_event("custom")
            raise o.WorkloadStalledError("producers stalled")
        with self.assertRaises(o.WorkloadStalledError):
            self.run_benchmark(scenario)
        result = self.read_result()
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["error"], "producers stalled")
        self.assertEqual(len(result["time_series"]), 1)
        self.assertIn("custom", [e["action"] for e in result["events"]])

    def test_failed_phase_writes_partial_results_and_raises(self):
        def scenario(ctx, timing, groups):
            ctx.runner.execute_phase("stop_group_B", mock.Mock(side_effect=RuntimeError("worker down")))
        with self.assertRaises(RuntimeError):
            self.run_benchmark(scenario)
        result = self.read_result()
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["error"], "worker down")
        self.assertIn("stop_group_B_failed", [e["action"] for e in result["events"]])

    def test_interrupt_writes_partial_results_and_raises(self):
        def scenario(ctx, timing, groups):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_benchmark(scenario)
        result = self.read_result()
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["error"], "KeyboardInterrupt")

    def test_unwritable_output_does_not_hide_the_failure(self):
        def scenario(ctx, timing, groups):
            raise o.WorkloadStalledError("producers stalled")
        missing = os.path.join(os.path.dirname(self.output), "missing-dir", "results.json")
        with self.assertRaises(o.WorkloadStalledError):
            self.run_benchmark(scenario, output=missing)


class CloseTest(unittest.TestCase):
    """The command-config file holds connection secrets, so the run removes it."""

    def benchmark_with_file(self, path):
        benchmark = o.LagRecoveryBenchmark.__new__(o.LagRecoveryBenchmark)
        benchmark._command_config_file = path
        return benchmark

    def test_close_removes_the_command_config_file(self):
        fd, path = tempfile.mkstemp(suffix=".properties")
        os.close(fd)
        benchmark = self.benchmark_with_file(path)
        benchmark.close()
        self.assertFalse(os.path.exists(path))

    def test_close_twice_and_without_file_do_nothing(self):
        fd, path = tempfile.mkstemp(suffix=".properties")
        os.close(fd)
        benchmark = self.benchmark_with_file(path)
        benchmark.close()
        benchmark.close()
        self.benchmark_with_file("").close()

    def test_main_closes_and_maps_each_outcome_to_an_exit_code(self):
        cases = [
            ("completed", dict(return_value={"status": "completed"}), None),
            ("timeout", dict(return_value={"status": "timeout"}), 3),
            ("failure", dict(side_effect=RuntimeError("boom")), 1),
            ("preflight", dict(side_effect=o.PreflightError("Topic seed-0 was not found")), 1),
            ("interrupt", dict(side_effect=KeyboardInterrupt()), 1),
            ("stall", dict(side_effect=o.WorkloadStalledError("stalled")), 2),
        ]
        for name, run, code in cases:
            with self.subTest(outcome=name), \
                    mock.patch.object(o, "parse_args", return_value=make_args(workers_file=None)), \
                    mock.patch.object(o, "validate_args", return_value=[]), \
                    mock.patch.object(o, "LagRecoveryBenchmark") as benchmark_class, \
                    mock.patch.object(o, "log"):
                benchmark = benchmark_class.return_value
                benchmark.run.configure_mock(**run)
                exit_code = None
                try:
                    o.main()
                except SystemExit as e:
                    exit_code = e.code
                self.assertEqual(exit_code, code)
                benchmark.close.assert_called_once()


class CatchupTest(unittest.TestCase):
    @staticmethod
    def offsets(lag, log_end, partitions=1):
        return {"total_lag": lag, "total_committed": log_end - lag, "total_log_end": log_end,
                "partitions": [{}] * partitions}

    def test_consumed_fraction(self):
        self.assertEqual(o.consumed_fraction(self.offsets(25, 100)), 0.75)

    def test_consumed_fraction_unknown_for_missing_or_empty_log(self):
        self.assertIsNone(o.consumed_fraction(None))
        self.assertIsNone(o.consumed_fraction(self.offsets(0, 0)))

    def test_caught_up_at_or_above_threshold(self):
        self.assertTrue(o.is_caught_up(self.offsets(1, 100), 0.99, 1))
        self.assertTrue(o.is_caught_up(self.offsets(0, 100), 0.99, 1))

    def test_not_caught_up_below_threshold(self):
        self.assertFalse(o.is_caught_up(self.offsets(2, 100), 0.99, 1))

    def test_unknown_offsets_are_not_caught_up(self):
        self.assertFalse(o.is_caught_up(None, 0.99, 1))

    def test_empty_log_counts_as_caught_up(self):
        self.assertTrue(o.is_caught_up(self.offsets(0, 0), 0.99, 1))

    def test_fewer_partitions_than_expected_are_not_caught_up(self):
        # During a rebalance, --describe can leave out partitions without a commit.
        self.assertFalse(o.is_caught_up(self.offsets(0, 100, partitions=7), 0.99, 8))
        self.assertTrue(o.is_caught_up(self.offsets(0, 100, partitions=8), 0.99, 8))


class PhaseRunnerCatchupTest(unittest.TestCase):
    class Collector:
        def __init__(self):
            self.consumer_groups = {"group-B": mock.Mock(kafka_group_id="group-B-run")}
            self.collects = 0
            self.events = []

        def collect(self):
            self.collects += 1

        def record_event(self, action, **kwargs):
            self.events.append({"action": action, **kwargs})

    class Monitor:
        def __init__(self, offsets):
            self._offsets = offsets

        def get_group_offsets(self, group_id):
            return self._offsets

    def runner(self, offsets):
        return o.PhaseRunner(self.Collector(), self.Monitor(offsets), poll_interval_s=0)

    @staticmethod
    def offsets(lag, log_end):
        return {"total_lag": lag, "total_committed": log_end - lag, "total_log_end": log_end,
                "partitions": [{}]}

    def test_returns_true_when_group_is_caught_up(self):
        r = self.runner(self.offsets(0, 10))
        self.assertTrue(r.collect_until_catchup(["group-B"], 1, threshold=0.99, timeout_s=5))
        self.assertEqual(r.collector.collects, 1)

    def test_returns_false_on_timeout(self):
        r = self.runner(self.offsets(90, 100))
        self.assertFalse(r.collect_until_catchup(["group-B"], 1, threshold=0.99, timeout_s=0.05))

    def test_cooldown_runs_after_a_successful_catchup(self):
        r = self.runner(self.offsets(0, 10))
        r.collect_for = mock.Mock()
        self.assertTrue(r.collect_until_catchup(["group-B"], 1, timeout_s=5, cooldown_s=7))
        r.collect_for.assert_called_once_with(7)

    def test_cooldown_is_skipped_after_a_timeout(self):
        r = self.runner(self.offsets(90, 100))
        r.collect_for = mock.Mock()
        self.assertFalse(r.collect_until_catchup(["group-B"], 1, timeout_s=0.05, cooldown_s=7))
        r.collect_for.assert_not_called()

    def test_records_a_catchup_event_when_the_group_caught_up(self):
        r = self.runner(self.offsets(0, 10))
        r.collect_until_catchup(["group-B"], 1, threshold=0.99, timeout_s=5)
        [event] = r.collector.events
        self.assertEqual((event["action"], event["groups"], event["caught_up"]),
                         ("catchup", ["group-B"], True))
        self.assertGreaterEqual(event["waited_s"], 0)

    def test_records_a_catchup_event_on_timeout(self):
        r = self.runner(self.offsets(90, 100))
        r.collect_until_catchup(["group-B"], 1, threshold=0.99, timeout_s=0.05)
        [event] = r.collector.events
        self.assertEqual((event["action"], event["groups"], event["caught_up"]),
                         ("catchup", ["group-B"], False))

    def test_returns_false_when_offsets_are_unavailable(self):
        r = self.runner(None)
        self.assertFalse(r.collect_until_catchup(["group-B"], 1, threshold=0.99, timeout_s=0.05))

    def test_stall_error_is_not_swallowed(self):
        r = self.runner(None)
        r.collector.collect = mock.Mock(side_effect=o.WorkloadStalledError("stalled"))
        with self.assertRaises(o.WorkloadStalledError):
            r.collect_until_catchup(["group-B"], 1, timeout_s=5)


class MetricsCollectorStallTest(unittest.TestCase):
    class Producers:
        kafka_group_id = "producers"

        def aggregate_counters(self):
            return {"messagesSent": 1000, "messagesReceived": 0, "messageSendErrors": 0}

    class Monitor:
        def get_group_offsets(self, group_id):
            return None

    def collector(self):
        c = o.MetricsCollector(self.Producers(), {}, self.Monitor(), 1024)
        c.start()
        return c

    def test_flat_producer_counter_aborts_after_three_stalled_polls(self):
        c = self.collector()
        c.producing = True
        c.collect()  # first poll: no baseline
        c.collect()
        c.collect()
        with self.assertRaises(o.WorkloadStalledError):
            c.collect()

    def test_unreadable_producer_stats_abort_after_three_polls(self):
        c = self.collector()
        c.producer_group.aggregate_counters = mock.Mock(side_effect=OSError("down"))
        c.producing = True
        c.collect()
        c.collect()
        with self.assertRaises(o.WorkloadStalledError):
            c.collect()

    def test_a_good_producer_poll_resets_the_failure_count(self):
        c = self.collector()
        c.producing = True
        failing = mock.Mock(side_effect=OSError("down"))
        c.producer_group.aggregate_counters = failing
        c.collect()
        c.collect()
        sent = {"messagesSent": 5000, "messagesReceived": 0, "messageSendErrors": 0}
        c.producer_group.aggregate_counters = mock.Mock(return_value=sent)
        c.collect()
        self.assertEqual(c.producer_failures, 0)
        c.producer_group.aggregate_counters = failing
        c.collect()
        c.collect()
        self.assertEqual(c.producer_failures, 2)

    def test_unreadable_producer_stats_do_not_abort_when_producers_are_not_expected(self):
        c = self.collector()
        c.producer_group.aggregate_counters = mock.Mock(side_effect=OSError("down"))
        c.producing = False
        for _ in range(6):
            c.collect()

    def test_no_abort_when_producers_are_not_expected_to_run(self):
        c = self.collector()
        c.producing = False
        for _ in range(6):
            c.collect()


class RemainingLagWaitTest(unittest.TestCase):
    T = o.Timing(phase_s=60, lag_s=600, catchup_timeout_s=1800)

    def test_c_stays_stopped_for_twice_the_lag(self):
        # B stopped at 60s, restarted and caught up by 760s -> C needs until 60 + 1200.
        self.assertEqual(o.remaining_lag_wait(760, self.T), 500)

    def test_never_negative(self):
        self.assertEqual(o.remaining_lag_wait(5000, self.T), 0)


class SelectScenarioTest(unittest.TestCase):
    def test_each_level_has_its_scenario(self):
        self.assertEqual(o.SCENARIOS, {
            1: o.scenario_steady_state,
            2: o.scenario_lag_recovery,
            3: o.scenario_multi_group,
            4: o.scenario_multi_layer,
            5: o.scenario_staged_readers,
        })

    def test_deferred_groups(self):
        names = ["group-A", "group-B", "group-C", "group-D"]
        self.assertEqual(o.deferred_groups(3, names), set())
        self.assertEqual(o.deferred_groups(4, names), {"group-D"})
        self.assertEqual(o.deferred_groups(5, names[:2]), {"group-A", "group-B"})


class ScenarioFlowTest(unittest.TestCase):
    T = o.Timing(phase_s=60, lag_s=600, catchup_timeout_s=1800)

    def test_steady_state(self):
        ctx = FakeCtx()
        o.scenario_steady_state(ctx, self.T, ["group-A"])
        self.assertEqual(ctx.calls, [("collect_for", 120), ("stop_all",)])

    def test_lag_recovery(self):
        ctx = FakeCtx()
        o.scenario_lag_recovery(ctx, self.T, ["group-A", "group-B"])
        self.assertEqual(ctx.calls, [
            ("collect_for", 60),
            ("stop_groups", "stop_group_B", ["group-B"]),
            ("collect_for", 600),
            ("start_group", "start_group_B", "group-B"),
            ("wait_catchup", ["group-B"], 60),
            ("stop_all",),
        ])

    def test_multi_group_waits_for_remaining_lag_before_restarting_c(self):
        ctx = FakeCtx(elapsed_s=760)
        o.scenario_multi_group(ctx, self.T, ["group-A", "group-B", "group-C"])
        self.assertEqual(ctx.calls, [
            ("collect_for", 60),
            ("stop_groups", "stop_groups_B_C", ["group-B", "group-C"]),
            ("collect_for", 600),
            ("start_group", "start_group_B", "group-B"),
            ("wait_catchup", ["group-B"], 0),
            ("collect_for", 500),
            ("start_group", "start_group_C", "group-C"),
            ("wait_catchup", ["group-C"], 60),
            ("stop_all",),
        ])

    def test_multi_group_restarts_c_at_once_when_lag_already_elapsed(self):
        ctx = FakeCtx(elapsed_s=5000)
        o.scenario_multi_group(ctx, self.T, ["group-A", "group-B", "group-C"])
        self.assertNotIn(("collect_for", 0), ctx.calls)
        self.assertEqual(ctx.calls[5][0], "start_group")
        self.assertEqual(ctx.calls[5][2], "group-C")

    def test_multi_layer(self):
        ctx = FakeCtx()
        o.scenario_multi_layer(ctx, self.T, ["group-A", "group-B", "group-C", "group-D"])
        self.assertEqual(ctx.calls, [
            ("collect_for", 60),
            ("collect_for", 600),
            ("start_group", "start_group_D_from_beginning", "group-D"),
            ("wait_catchup", ["group-D"], 60),
            ("stop_all",),
        ])

    def test_staged_readers_start_reader_two_after_reader_one_catches_up(self):
        ctx = FakeCtx(catchup_results=[True, True])
        o.scenario_staged_readers(ctx, self.T, ["group-A", "group-B"])
        self.assertEqual(ctx.calls, [
            ("collect_for", 60),
            ("start_group", "start_group-A_from_beginning", "group-A"),
            ("wait_catchup", ["group-A"], 60),
            ("start_group", "start_group-B_from_beginning", "group-B"),
            ("wait_catchup", ["group-B"], 60),
            ("stop_all",),
        ])

    def test_staged_readers_skip_reader_two_when_reader_one_stalls(self):
        ctx = FakeCtx(catchup_results=[False])
        o.scenario_staged_readers(ctx, self.T, ["group-A", "group-B"])
        self.assertEqual(ctx.calls, [
            ("collect_for", 60),
            ("start_group", "start_group-A_from_beginning", "group-A"),
            ("wait_catchup", ["group-A"], 60),
            ("stop_all",),
        ])


class SplitWorkersTest(unittest.TestCase):
    WORKERS = ["w1", "w2", "w3", "w4", "w5"]

    def test_assigns_in_role_order(self):
        self.assertEqual(o.split_workers(self.WORKERS, [2, 1, 2]), {
            "producer_workers": ["w1", "w2"],
            "consumer_a_workers": ["w3"],
            "consumer_b_workers": ["w4", "w5"],
        })

    def test_fewer_than_two_counts_is_an_error(self):
        with self.assertRaises(ValueError):
            o.split_workers(self.WORKERS, [5])

    def test_counts_exceeding_workers_is_an_error(self):
        with self.assertRaises(ValueError):
            o.split_workers(self.WORKERS, [3, 3])

class WorkerSplitOptionsTest(unittest.TestCase):
    def workers_file(self):
        path = os.path.join(tempfile.mkdtemp(), "workers.yaml")
        with open(path, "w") as f:
            f.write("workers:\n  - http://w1\n  - http://w2\n  - http://w3\n")
        return path

    def test_malformed_split_exits_with_one_and_no_traceback(self):
        args = make_args(workers_file=self.workers_file(), worker_split="2,x")
        with mock.patch.object(o, "log"):
            with self.assertRaises(SystemExit) as ctx:
                o.apply_worker_split(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_valid_split_assigns_the_roles(self):
        args = make_args(workers_file=self.workers_file(), worker_split="2,1")
        o.apply_worker_split(args)
        self.assertEqual(args.producer_workers, ["http://w1", "http://w2"])
        self.assertEqual(args.consumer_a_workers, ["http://w3"])

    def test_main_rejects_a_split_without_a_workers_file(self):
        args = make_args(workers_file=None, worker_split="2,2")
        with mock.patch.object(o, "parse_args", return_value=args), \
                mock.patch.object(o, "LagRecoveryBenchmark") as benchmark_class, \
                mock.patch.object(o, "log") as log:
            with self.assertRaises(SystemExit) as ctx:
                o.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("--worker-split requires --workers-file", str(log.error.call_args))
        benchmark_class.assert_not_called()


class SplitCountsTest(unittest.TestCase):
    def test_negative_count_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            o.split_workers(["w1", "w2", "w3"], [2, -1])
        self.assertIn("negative", str(ctx.exception))

    def test_zero_count_leaves_a_role_empty(self):
        assignment = o.split_workers(["w1", "w2", "w3"], [2, 0, 1])
        self.assertEqual(assignment["consumer_a_workers"], [])
        self.assertEqual(assignment["consumer_b_workers"], ["w3"])


class ValidateArgsTest(unittest.TestCase):
    def test_valid_level_two(self):
        self.assertEqual(o.validate_args(make_args()), [])

    def test_missing_producers(self):
        errors = o.validate_args(make_args(producer_workers=[]))
        self.assertEqual(len(errors), 1)
        self.assertIn("producer", errors[0])

    def test_level_one_needs_no_consumers(self):
        self.assertEqual(
            o.validate_args(make_args(level=1, consumer_a_workers=[], consumer_b_workers=[])),
            [])

    def test_level_two_requires_consumer_a(self):
        errors = o.validate_args(make_args(consumer_a_workers=[]))
        self.assertIn("--consumer-a-workers", errors[0])

    def test_level_two_requires_consumer_b(self):
        errors = o.validate_args(make_args(consumer_b_workers=[]))
        self.assertEqual(len(errors), 1)
        self.assertIn("--consumer-b-workers", errors[0])

    def test_level_three_requires_consumer_b(self):
        errors = o.validate_args(
            make_args(level=3, consumer_b_workers=[], consumer_c_workers=["c1"]))
        self.assertEqual(len(errors), 1)
        self.assertIn("--consumer-b-workers", errors[0])

    def test_level_three_requires_consumer_c(self):
        errors = o.validate_args(make_args(level=3))
        self.assertEqual(len(errors), 1)
        self.assertIn("--consumer-c-workers", errors[0])

    def test_level_three_valid(self):
        self.assertEqual(o.validate_args(make_args(level=3, consumer_c_workers=["c1"])), [])

    def test_counts_below_one_are_rejected(self):
        for name in ("topics", "partitions", "message_size", "poll_interval"):
            with self.subTest(name=name):
                errors = o.validate_args(make_args(**{name: 0}))
                self.assertEqual(len(errors), 1)
                self.assertIn("--" + name.replace("_", "-"), errors[0])

    def test_rates_and_durations_at_or_below_zero_are_rejected(self):
        for name in ("target_rate_mb", "phase_duration_minutes", "catchup_timeout_minutes",
                     "lag_duration_minutes"):
            for value in (0, -1):
                with self.subTest(name=name, value=value):
                    errors = o.validate_args(make_args(**{name: value}))
                    self.assertEqual(len(errors), 1)
                    self.assertIn("--" + name.replace("_", "-"), errors[0])

    def test_catchup_threshold_must_be_above_zero_and_at_most_one(self):
        for value in (0, -0.5, 1.01):
            with self.subTest(value=value):
                errors = o.validate_args(make_args(catchup_threshold=value))
                self.assertEqual(len(errors), 1)
                self.assertIn("--catchup-threshold", errors[0])
        for value in (0.01, 1):
            with self.subTest(value=value):
                self.assertEqual(o.validate_args(make_args(catchup_threshold=value)), [])

    def test_level_four_requires_consumer_d(self):
        errors = o.validate_args(make_args(level=4, consumer_c_workers=["c1"]))
        self.assertEqual(len(errors), 1)
        self.assertIn("--consumer-d-workers", errors[0])

    def test_level_four_requires_consumer_b_and_c(self):
        errors = o.validate_args(
            make_args(level=4, consumer_b_workers=[], consumer_d_workers=["d1"]))
        self.assertEqual(len(errors), 2)
        self.assertIn("--consumer-b-workers", errors[0])
        self.assertIn("--consumer-c-workers", errors[1])

    def test_level_four_valid(self):
        args = make_args(level=4, consumer_c_workers=["c1"], consumer_d_workers=["d1"])
        self.assertEqual(o.validate_args(args), [])

    def test_target_rate_below_the_stall_limit_is_rejected(self):
        errors = o.validate_args(make_args(target_rate_mb=0.5, message_size=1_048_576))
        self.assertEqual(len(errors), 1)
        self.assertIn("0.50 msg/s", errors[0])
        self.assertIn("stall detector", errors[0])

    def test_target_rate_at_the_stall_limit_is_accepted(self):
        args = make_args(target_rate_mb=1.0, message_size=1_048_576)
        self.assertEqual(o.validate_args(args), [])

    def test_duplicate_worker_url_inside_a_role_is_rejected(self):
        errors = o.validate_args(make_args(consumer_a_workers=["http://a", "http://a"]))
        self.assertEqual(len(errors), 1)
        self.assertIn("http://a", errors[0])

    def test_worker_url_shared_across_roles_is_rejected(self):
        errors = o.validate_args(
            make_args(producer_workers=["http://w"], consumer_a_workers=["http://w/"]))
        self.assertEqual(len(errors), 1)
        self.assertIn("producer_workers", errors[0])
        self.assertIn("consumer_a_workers", errors[0])

    def test_distinct_worker_urls_are_accepted(self):
        args = make_args(producer_workers=["http://p1", "http://p2"],
                         consumer_a_workers=["http://a1", "http://a2"])
        self.assertEqual(o.validate_args(args), [])

    def test_level_five_requires_consumer_b_and_reuse_topic(self):
        errors = o.validate_args(make_args(level=5, consumer_b_workers=[]))
        self.assertEqual(len(errors), 2)

    def test_reuse_topic_requires_prefix(self):
        errors = o.validate_args(make_args(reuse_topic=True))
        self.assertIn("--topic-prefix", errors[0])

    def test_level_five_valid(self):
        args = make_args(level=5, reuse_topic=True, topic_prefix="seed")
        self.assertEqual(o.validate_args(args), [])

    def test_all_errors_are_reported_together(self):
        errors = o.validate_args(make_args(level=3, producer_workers=[], consumer_b_workers=[]))
        self.assertEqual(len(errors), 3)


if __name__ == "__main__":
    unittest.main()
