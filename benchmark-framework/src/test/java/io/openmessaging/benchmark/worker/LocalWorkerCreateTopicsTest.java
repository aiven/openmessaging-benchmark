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
package io.openmessaging.benchmark.worker;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import io.openmessaging.benchmark.driver.BenchmarkConsumer;
import io.openmessaging.benchmark.driver.BenchmarkDriver;
import io.openmessaging.benchmark.driver.BenchmarkProducer;
import io.openmessaging.benchmark.driver.ConsumerCallback;
import io.openmessaging.benchmark.worker.commands.TopicsInfo;
import java.io.File;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import org.apache.bookkeeper.stats.StatsLogger;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class LocalWorkerCreateTopicsTest {

    private static final String PREFIX = "test-prefix";

    @TempDir File tempDir;

    private LocalWorker worker;

    @BeforeEach
    void setUp() throws Exception {
        RecordingDriver.created.clear();
        File config = new File(tempDir, "driver.yaml");
        Files.write(
                config.toPath(),
                ("name: test\ndriverClass: " + RecordingDriver.class.getName() + "\n").getBytes());
        worker = new LocalWorker();
        worker.initializeDriver(config);
    }

    @AfterEach
    void tearDown() throws Exception {
        worker.close();
    }

    @Test
    void usesGivenTopicNames() {
        List<String> topics =
                worker.createTopics(new TopicsInfo(2, 4, Arrays.asList("topic-a", "topic-b")));

        assertThat(topics).containsExactly("topic-a", "topic-b");
        assertThat(RecordingDriver.created).containsExactly("topic-a:4", "topic-b:4");
    }

    @Test
    void generatesRandomNamesWhenNoNamesAreGiven() {
        List<String> topics = worker.createTopics(new TopicsInfo(2, 1));

        assertThat(topics).hasSize(2);
        assertThat(topics.get(0)).startsWith(PREFIX + "-0000000-");
        assertThat(topics.get(1)).startsWith(PREFIX + "-0000001-");
    }

    @Test
    void failsWhenFewerNamesThanTopics() {
        assertThatThrownBy(
                        () -> worker.createTopics(new TopicsInfo(3, 1, Collections.singletonList("topic-a"))))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("'topicNames' has 1 entries but 'topics' is 3");
        assertThat(RecordingDriver.created).isEmpty();
    }

    @Test
    void failsWhenMoreNamesThanTopics() {
        assertThatThrownBy(
                        () -> worker.createTopics(new TopicsInfo(1, 1, Arrays.asList("topic-a", "topic-b"))))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("'topicNames' has 2 entries but 'topics' is 1");
        assertThat(RecordingDriver.created).isEmpty();
    }

    @Test
    void failsWhenNamesRepeat() {
        assertThatThrownBy(
                        () ->
                                worker.createTopics(
                                        new TopicsInfo(
                                                4, 1, Arrays.asList("topic-a", "topic-b", "topic-a", "topic-b"))))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("duplicate entries: [topic-a, topic-b]");
        assertThat(RecordingDriver.created).isEmpty();
    }

    /** Driver that records the topics it is asked to create. Instantiated by class name. */
    public static class RecordingDriver implements BenchmarkDriver {
        static final List<String> created = new ArrayList<>();

        @Override
        public void initialize(File configurationFile, StatsLogger statsLogger) {}

        @Override
        public String getTopicNamePrefix() {
            return PREFIX;
        }

        @Override
        public CompletableFuture<Void> createTopic(String topic, int partitions) {
            created.add(topic + ":" + partitions);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletableFuture<BenchmarkProducer> createProducer(String topic) {
            throw new UnsupportedOperationException();
        }

        @Override
        public CompletableFuture<BenchmarkConsumer> createConsumer(
                String topic, String subscriptionName, ConsumerCallback consumerCallback) {
            throw new UnsupportedOperationException();
        }

        @Override
        public void close() {}
    }
}
