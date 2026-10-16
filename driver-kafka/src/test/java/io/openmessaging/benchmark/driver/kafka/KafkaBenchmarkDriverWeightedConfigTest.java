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

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.Arrays;
import java.util.List;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

class KafkaBenchmarkDriverWeightedConfigTest {

    @Nested
    class AllocateByWeight {

        @Test
        void exactEvenSplit() {
            List<WeightedTopicConfig> configs =
                    Arrays.asList(new WeightedTopicConfig(0.5, "a"), new WeightedTopicConfig(0.5, "b"));

            int[] counts = KafkaBenchmarkDriver.allocateByWeight(10, configs);

            assertThat(counts).containsExactly(5, 5);
        }

        @Test
        void unevenWeights() {
            List<WeightedTopicConfig> configs =
                    Arrays.asList(new WeightedTopicConfig(0.7, "a"), new WeightedTopicConfig(0.3, "b"));

            int[] counts = KafkaBenchmarkDriver.allocateByWeight(10, configs);

            assertThat(counts).containsExactly(7, 3);
        }

        @Test
        void singleTopicGoesToFirstConfig() {
            List<WeightedTopicConfig> configs =
                    Arrays.asList(new WeightedTopicConfig(0.5, "a"), new WeightedTopicConfig(0.5, "b"));

            int[] counts = KafkaBenchmarkDriver.allocateByWeight(1, configs);

            assertThat(counts).containsExactly(1, 0);
        }

        @Test
        void threeWaySplit() {
            List<WeightedTopicConfig> configs =
                    Arrays.asList(
                            new WeightedTopicConfig(1, "a"),
                            new WeightedTopicConfig(1, "b"),
                            new WeightedTopicConfig(1, "c"));

            int[] counts = KafkaBenchmarkDriver.allocateByWeight(10, configs);

            assertThat(Arrays.stream(counts).sum()).isEqualTo(10);
            for (int count : counts) {
                assertThat(count).isBetween(3, 4);
            }
        }

        @Test
        void weightsAreNormalized() {
            List<WeightedTopicConfig> configs =
                    Arrays.asList(new WeightedTopicConfig(2.0, "a"), new WeightedTopicConfig(2.0, "b"));

            int[] counts = KafkaBenchmarkDriver.allocateByWeight(6, configs);

            assertThat(counts).containsExactly(3, 3);
        }

        @Test
        void throwsOnZeroTotalWeight() {
            List<WeightedTopicConfig> configs =
                    Arrays.asList(new WeightedTopicConfig(0, "a"), new WeightedTopicConfig(0, "b"));

            assertThatThrownBy(() -> KafkaBenchmarkDriver.allocateByWeight(10, configs))
                    .isInstanceOf(IllegalArgumentException.class);
        }
    }
}
