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

/**
 * A topic configuration with an associated weight for proportional distribution across topics. When
 * multiple entries are specified, topics are allocated to each config proportionally based on the
 * relative weights. Each entry's config is merged on top of the base {@code topicConfig} — so
 * shared defaults go in {@code topicConfig} and per-group overrides go here.
 */
public class WeightedTopicConfig {
    public double weight;
    public String config;

    public WeightedTopicConfig() {}

    public WeightedTopicConfig(double weight, String config) {
        this.weight = weight;
        this.config = config;
    }
}
