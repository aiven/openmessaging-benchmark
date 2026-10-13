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
package io.openmessaging.benchmark;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.Arrays;
import java.util.List;
import org.junit.jupiter.api.Test;

class WorkloadGeneratorTopicNamesTest {

    @Test
    void rejectsTopicNamesThatDoNotMatchTopicsCount() {
        Workload workload = workload(3, Arrays.asList("topic-a", "topic-b"));

        assertThatThrownBy(() -> new WorkloadGenerator("driver", workload, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("'topicNames' has 2 entries but 'topics' is 3");
    }

    @Test
    void rejectsRepeatedTopicNames() {
        Workload workload = workload(2, Arrays.asList("topic-a", "topic-a"));

        assertThatThrownBy(() -> new WorkloadGenerator("driver", workload, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("duplicate entries: [topic-a]");
    }

    @Test
    void rejectsNullTopicName() {
        Workload workload = workload(2, Arrays.asList("topic-a", null));

        assertThatThrownBy(() -> new WorkloadGenerator("driver", workload, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("null or blank entry at index 1");
    }

    @Test
    void rejectsBlankTopicName() {
        Workload workload = workload(2, Arrays.asList("topic-a", "  "));

        assertThatThrownBy(() -> new WorkloadGenerator("driver", workload, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("null or blank entry at index 1");
    }

    @Test
    void acceptsMatchingTopicNamesAndMissingTopicNames() {
        assertThatCode(
                        () ->
                                new WorkloadGenerator(
                                        "driver", workload(2, Arrays.asList("topic-a", "topic-b")), null))
                .doesNotThrowAnyException();
        assertThatCode(() -> new WorkloadGenerator("driver", workload(2, null), null))
                .doesNotThrowAnyException();
    }

    private static Workload workload(int topics, List<String> topicNames) {
        Workload workload = new Workload();
        workload.topics = topics;
        workload.partitionsPerTopic = 1;
        workload.topicNames = topicNames;
        return workload;
    }
}
