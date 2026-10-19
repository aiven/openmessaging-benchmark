/*
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package io.openmessaging.benchmark.driver.kafka;


import io.openmessaging.benchmark.driver.BenchmarkConsumer;
import io.openmessaging.benchmark.driver.ConsumerCallback;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.ConsumerRecords;
import org.apache.kafka.clients.consumer.InvalidOffsetException;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.common.TopicPartition;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class KafkaBenchmarkConsumer implements BenchmarkConsumer {

    private static final Logger log = LoggerFactory.getLogger(KafkaBenchmarkConsumer.class);

    private static final Duration ASSIGNMENT_TIMEOUT = Duration.ofMinutes(2);

    private final KafkaConsumer<String, byte[]> consumer;

    private final ExecutorService executor;
    private final Future<?> consumerTask;
    private volatile boolean closing = false;
    private boolean autoCommit;

    public KafkaBenchmarkConsumer(
            KafkaConsumer<String, byte[]> consumer,
            Properties consumerConfig,
            ConsumerCallback callback) {
        this(consumer, consumerConfig, callback, 100L, false);
    }

    public KafkaBenchmarkConsumer(
            KafkaConsumer<String, byte[]> consumer,
            Properties consumerConfig,
            ConsumerCallback callback,
            boolean resetToLatest) {
        this(consumer, consumerConfig, callback, 100L, resetToLatest);
    }

    public KafkaBenchmarkConsumer(
            KafkaConsumer<String, byte[]> consumer,
            Properties consumerConfig,
            ConsumerCallback callback,
            long pollTimeoutMs) {
        this(consumer, consumerConfig, callback, pollTimeoutMs, false);
    }

    public KafkaBenchmarkConsumer(
            KafkaConsumer<String, byte[]> consumer,
            Properties consumerConfig,
            ConsumerCallback callback,
            long pollTimeoutMs,
            boolean resetToLatest) {
        this.consumer = consumer;
        this.autoCommit =
                Boolean.valueOf(
                        (String)
                                consumerConfig.getOrDefault(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, "false"));

        if (resetToLatest) {
            seekToEnd(consumer, pollTimeoutMs, ASSIGNMENT_TIMEOUT);
        }

        this.executor = Executors.newSingleThreadExecutor();
        this.consumerTask =
                this.executor.submit(
                        () -> {
                            while (!closing) {
                                try {
                                    ConsumerRecords<String, byte[]> records =
                                            consumer.poll(Duration.ofMillis(pollTimeoutMs));

                                    Map<TopicPartition, OffsetAndMetadata> offsetMap = new HashMap<>();
                                    for (ConsumerRecord<String, byte[]> record : records) {
                                        callback.messageReceived(record.value(), record.timestamp());

                                        offsetMap.put(
                                                new TopicPartition(record.topic(), record.partition()),
                                                new OffsetAndMetadata(record.offset() + 1));
                                    }

                                    if (!autoCommit && !offsetMap.isEmpty()) {
                                        // Async commit all messages polled so far
                                        consumer.commitAsync(offsetMap, null);
                                    }
                                } catch (Exception e) {
                                    log.error("exception occur while consuming message", e);
                                }
                            }
                        });
    }

    /**
     * Waits for a partition assignment, then moves the position of each assigned partition to the end
     * offset and commits it. The commit makes the position survive a rebalance: a partition that
     * stays with this consumer after another consumer joins resumes from the end offset, not from the
     * group offset reset policy.
     *
     * <p>If no partition is assigned within the timeout, the method throws {@link
     * IllegalStateException}. A consumer without partitions, for example when consumers outnumber
     * partitions, fails this way.
     *
     * @param consumer the subscribed consumer
     * @param pollTimeoutMs the poll timeout while the method waits for the assignment
     * @param timeout the longest time to wait for the assignment
     */
    static void seekToEnd(
            KafkaConsumer<String, byte[]> consumer, long pollTimeoutMs, Duration timeout) {
        long deadline = System.nanoTime() + timeout.toNanos();
        Set<TopicPartition> assignment = consumer.assignment();
        while (assignment.isEmpty()) {
            if (System.nanoTime() - deadline >= 0) {
                throw new IllegalStateException(
                        "resetToLatest: no partition assigned within "
                                + timeout
                                + ", check the number of consumers");
            }
            try {
                consumer.poll(Duration.ofMillis(pollTimeoutMs));
            } catch (InvalidOffsetException e) {
                // auto.offset.reset=none: the seek below replaces the missing or stale offset
            }
            assignment = consumer.assignment();
        }
        consumer.pause(assignment);
        Map<TopicPartition, OffsetAndMetadata> endOffsets = new HashMap<>();
        for (Map.Entry<TopicPartition, Long> entry : consumer.endOffsets(assignment).entrySet()) {
            log.info("Seeking {} to end offset {}", entry.getKey(), entry.getValue());
            consumer.seek(entry.getKey(), entry.getValue());
            endOffsets.put(entry.getKey(), new OffsetAndMetadata(entry.getValue()));
        }
        consumer.commitSync(endOffsets);
        consumer.resume(assignment);
    }

    @Override
    public void close() throws Exception {
        closing = true;
        executor.shutdown();
        consumerTask.get();
        consumer.close();
    }
}
