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
package io.openmessaging.benchmark.worker.commands;


import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

public class TopicsInfo {
    public int numberOfTopics;
    public int numberOfPartitionsPerTopic;
    public List<String> topicNames;

    public TopicsInfo() {}

    public TopicsInfo(int numberOfTopics, int numberOfPartitionsPerTopic) {
        this.numberOfTopics = numberOfTopics;
        this.numberOfPartitionsPerTopic = numberOfPartitionsPerTopic;
    }

    public TopicsInfo(int numberOfTopics, int numberOfPartitionsPerTopic, List<String> topicNames) {
        this.numberOfTopics = numberOfTopics;
        this.numberOfPartitionsPerTopic = numberOfPartitionsPerTopic;
        this.topicNames = topicNames;
    }

    /**
     * Checks that {@code topicNames}, when set, has exactly one distinct name per topic.
     *
     * @throws IllegalArgumentException if the number of names differs from {@code numberOfTopics}, a
     *     name is null or blank, or a name appears more than once
     */
    public void validate() {
        if (topicNames == null) {
            return;
        }
        if (topicNames.size() != numberOfTopics) {
            throw new IllegalArgumentException(
                    String.format(
                            "'topicNames' has %d entries but 'topics' is %d. "
                                    + "Provide exactly one name per topic, or remove 'topicNames' to generate names.",
                            topicNames.size(), numberOfTopics));
        }
        for (int i = 0; i < topicNames.size(); i++) {
            String name = topicNames.get(i);
            if (name == null || name.trim().isEmpty()) {
                throw new IllegalArgumentException(
                        String.format(
                                "'topicNames' has a null or blank entry at index %d. Provide a name for each topic.",
                                i));
            }
        }
        Set<String> seen = new HashSet<>();
        Set<String> duplicates = new LinkedHashSet<>();
        for (String name : topicNames) {
            if (!seen.add(name)) {
                duplicates.add(name);
            }
        }
        if (!duplicates.isEmpty()) {
            throw new IllegalArgumentException(
                    String.format(
                            "'topicNames' has duplicate entries: %s. Provide a distinct name per topic.",
                            duplicates));
        }
    }
}
