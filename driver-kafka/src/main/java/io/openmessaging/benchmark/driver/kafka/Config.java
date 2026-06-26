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


import java.util.ArrayList;
import java.util.List;

public class Config {
    public short replicationFactor;

    public String topicConfig;

    /**
     * Optional weighted topic configurations for mixed workloads. Each entry's {@code config} is
     * merged on top of {@link #topicConfig} — put shared defaults in {@link #topicConfig} and
     * per-group overrides here. Topics are distributed proportionally by weight.
     */
    public List<WeightedTopicConfig> weightedTopicConfigs = new ArrayList<>();

    public String commonConfig;

    public String producerConfig;

    public String consumerConfig;
}
