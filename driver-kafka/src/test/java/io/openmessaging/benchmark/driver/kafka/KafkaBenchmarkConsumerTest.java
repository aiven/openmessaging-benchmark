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

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import io.openmessaging.benchmark.driver.ConsumerCallback;
import java.time.Duration;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import org.apache.kafka.clients.consumer.ConsumerRecords;
import org.apache.kafka.clients.consumer.InvalidOffsetException;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.consumer.NoOffsetForPartitionException;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.clients.consumer.OffsetOutOfRangeException;
import org.apache.kafka.common.TopicPartition;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.mockito.InOrder;

class KafkaBenchmarkConsumerTest {
    private static final TopicPartition P0 = new TopicPartition("t", 0);
    private static final TopicPartition P1 = new TopicPartition("t", 1);

    @SuppressWarnings("unchecked")
    private final KafkaConsumer<String, byte[]> consumer = mock(KafkaConsumer.class);

    @Test
    void seekToEndWaitsForAssignmentThenSeeksAndCommitsEndOffsets() {
        Set<TopicPartition> assigned = new HashSet<>();
        assigned.add(P0);
        assigned.add(P1);
        when(consumer.assignment())
                .thenReturn(Collections.emptySet(), Collections.emptySet(), assigned);
        Map<TopicPartition, Long> endOffsets = new LinkedHashMap<>();
        endOffsets.put(P0, 10L);
        endOffsets.put(P1, 20L);
        when(consumer.endOffsets(assigned)).thenReturn(endOffsets);

        KafkaBenchmarkConsumer.seekToEnd(consumer, 1, Duration.ofSeconds(5));

        Map<TopicPartition, OffsetAndMetadata> committed = new HashMap<>();
        committed.put(P0, new OffsetAndMetadata(10L));
        committed.put(P1, new OffsetAndMetadata(20L));
        InOrder order = inOrder(consumer);
        order.verify(consumer, times(2)).poll(Duration.ofMillis(1));
        order.verify(consumer).pause(assigned);
        order.verify(consumer).seek(P0, 10L);
        order.verify(consumer).seek(P1, 20L);
        order.verify(consumer).commitSync(committed);
        order.verify(consumer).resume(assigned);
    }

    @Test
    void seekToEndContinuesWhenPollHasNoOffsetForTheAssignment() {
        seekToEndContinuesWhenPollThrows(new NoOffsetForPartitionException(P0));
    }

    @Test
    void seekToEndContinuesWhenPollFindsAnOffsetOutOfRange() {
        seekToEndContinuesWhenPollThrows(
                new OffsetOutOfRangeException(Collections.singletonMap(P0, 5L)));
    }

    private void seekToEndContinuesWhenPollThrows(InvalidOffsetException exception) {
        Set<TopicPartition> assigned = Collections.singleton(P0);
        when(consumer.assignment()).thenReturn(Collections.emptySet(), assigned);
        when(consumer.poll(any(Duration.class))).thenThrow(exception);
        when(consumer.endOffsets(assigned)).thenReturn(Collections.singletonMap(P0, 10L));

        KafkaBenchmarkConsumer.seekToEnd(consumer, 1, Duration.ofSeconds(5));

        verify(consumer).seek(P0, 10L);
        verify(consumer).commitSync(Collections.singletonMap(P0, new OffsetAndMetadata(10L)));
    }

    @Test
    @Timeout(10)
    void seekToEndFailsWhenNoPartitionIsAssigned() {
        when(consumer.assignment()).thenReturn(Collections.emptySet());

        assertThatThrownBy(() -> KafkaBenchmarkConsumer.seekToEnd(consumer, 1, Duration.ofMillis(50)))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("no partition assigned");
        verify(consumer, never()).commitSync(any(Map.class));
    }

    @Test
    void consumerCommitsEndOffsetsBeforeThePollingThreadPolls() throws Exception {
        Set<TopicPartition> assigned = Collections.singleton(P0);
        when(consumer.assignment()).thenReturn(assigned);
        when(consumer.endOffsets(assigned)).thenReturn(Collections.singletonMap(P0, 10L));
        when(consumer.poll(any(Duration.class))).thenReturn(ConsumerRecords.empty());

        KafkaBenchmarkConsumer benchmarkConsumer =
                new KafkaBenchmarkConsumer(
                        consumer, new Properties(), mock(ConsumerCallback.class), 1L, true);
        verify(consumer, timeout(1000).atLeastOnce()).poll(any(Duration.class));
        benchmarkConsumer.close();

        InOrder order = inOrder(consumer);
        order.verify(consumer).commitSync(Collections.singletonMap(P0, new OffsetAndMetadata(10L)));
        order.verify(consumer, atLeastOnce()).poll(any(Duration.class));
    }

    @Test
    void consumerDoesNotSeekWithoutResetToLatest() throws Exception {
        when(consumer.poll(any(Duration.class))).thenReturn(ConsumerRecords.empty());
        ConsumerCallback callback = mock(ConsumerCallback.class);

        new KafkaBenchmarkConsumer(consumer, new Properties(), callback, 1L, false).close();

        verify(consumer, never()).endOffsets(any());
        verify(consumer, never()).seek(any(TopicPartition.class), any(Long.class));
        verify(consumer, never()).commitSync(any(Map.class));
    }
}
