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


import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.yaml.YAMLFactory;
import io.openmessaging.benchmark.driver.BenchmarkConsumer;
import io.openmessaging.benchmark.driver.BenchmarkDriver;
import io.openmessaging.benchmark.driver.BenchmarkProducer;
import io.openmessaging.benchmark.driver.ConsumerCallback;
import java.io.File;
import java.io.IOException;
import java.io.StringReader;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.CompletableFuture;
import org.apache.bookkeeper.stats.StatsLogger;
import org.apache.kafka.clients.admin.AdminClient;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.serialization.ByteArrayDeserializer;
import org.apache.kafka.common.serialization.ByteArraySerializer;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.apache.kafka.common.serialization.StringSerializer;

public class KafkaBenchmarkDriver implements BenchmarkDriver {

    private static final String ZONE_ID_CONFIG = "zone.id";
    private static final String ZONE_ID_TEMPLATE = "{zone.id}";
    private static final String KAFKA_CLIENT_ID = "client.id";
    private static final String KAFKA_CLIENT_RACK = "client.rack";
    private Config config;

    private List<BenchmarkProducer> producers = Collections.synchronizedList(new ArrayList<>());
    private List<BenchmarkConsumer> consumers = Collections.synchronizedList(new ArrayList<>());

    // Visible for testing
    Properties topicProperties;
    Properties producerProperties;
    Properties consumerProperties;

    private AdminClient admin;

    @Override
    public void initialize(File configurationFile, StatsLogger statsLogger) throws IOException {
        config = mapper.readValue(configurationFile, Config.class);

        Properties commonProperties = new Properties();
        commonProperties.load(new StringReader(config.commonConfig));

        applyZoneIdIfNeeded(commonProperties, KAFKA_CLIENT_ID);
        applyZoneIdIfNeeded(commonProperties, KAFKA_CLIENT_RACK);

        producerProperties = new Properties();
        commonProperties.forEach((key, value) -> producerProperties.put(key, value));
        producerProperties.load(new StringReader(config.producerConfig));

        applyZoneIdIfNeeded(producerProperties, KAFKA_CLIENT_ID);
        applyZoneIdIfNeeded(producerProperties, KAFKA_CLIENT_RACK);

        producerProperties.put(
                ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
        producerProperties.put(
                ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, ByteArraySerializer.class.getName());

        consumerProperties = new Properties();
        commonProperties.forEach((key, value) -> consumerProperties.put(key, value));
        consumerProperties.load(new StringReader(config.consumerConfig));

        applyZoneIdIfNeeded(consumerProperties, KAFKA_CLIENT_ID);
        applyZoneIdIfNeeded(consumerProperties, KAFKA_CLIENT_RACK);

        consumerProperties.put(
                ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class.getName());
        consumerProperties.put(
                ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, ByteArrayDeserializer.class.getName());

        topicProperties = new Properties();
        topicProperties.load(new StringReader(config.topicConfig));

        admin = AdminClient.create(commonProperties);
    }

    private static void applyZoneIdIfNeeded(Properties props, String propKey) {
        if (props.containsKey(propKey)) {
            props.put(
                    propKey, applyZoneId(props.getProperty(propKey), System.getProperty(ZONE_ID_CONFIG)));
        }
    }

    @Override
    public String getTopicNamePrefix() {
        return "test-topic";
    }

    @Override
    public CompletableFuture<Void> createTopic(String topic, int partitions) {
        return createTopics(Collections.singletonList(new TopicInfo(topic, partitions)));
    }

    @Override
    public CompletableFuture<Void> createTopics(List<TopicInfo> topicInfos) {
        if (config.weightedTopicConfigs == null || config.weightedTopicConfigs.isEmpty()) {
            return createAllWithConfig(topicInfos, baseTopicConfigs());
        }
        return createWithWeightedConfigs(topicInfos);
    }

    private CompletableFuture<Void> createAllWithConfig(
            List<TopicInfo> topicInfos, Map<String, String> topicConfigs) {
        return new KafkaTopicCreator(admin, topicConfigs, config.replicationFactor).create(topicInfos);
    }

    private CompletableFuture<Void> createWithWeightedConfigs(List<TopicInfo> topicInfos) {
        int[] counts = allocateByWeight(topicInfos.size(), config.weightedTopicConfigs);

        List<CompletableFuture<Void>> futures = new ArrayList<>();
        int offset = 0;
        for (int i = 0; i < config.weightedTopicConfigs.size(); i++) {
            int count = counts[i];
            if (count == 0) {
                continue;
            }
            List<TopicInfo> group = topicInfos.subList(offset, offset + count);
            offset += count;

            Map<String, String> merged = mergeConfigs(config.weightedTopicConfigs.get(i).config);
            futures.add(createAllWithConfig(group, merged));
        }
        return CompletableFuture.allOf(futures.toArray(new CompletableFuture[0]));
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private Map<String, String> baseTopicConfigs() {
        return new HashMap<>((Map) topicProperties);
    }

    private Map<String, String> mergeConfigs(String overrideConfig) {
        Map<String, String> merged = baseTopicConfigs();
        if (overrideConfig == null || overrideConfig.trim().isEmpty()) {
            return merged;
        }
        Properties overrides = new Properties();
        try {
            overrides.load(new StringReader(overrideConfig));
        } catch (IOException e) {
            throw new RuntimeException("Failed to parse topic config override", e);
        }
        overrides.forEach((k, v) -> merged.put((String) k, (String) v));
        return merged;
    }

    static int[] allocateByWeight(int total, List<WeightedTopicConfig> configs) {
        double totalWeight = 0;
        for (WeightedTopicConfig c : configs) {
            totalWeight += c.weight;
        }
        if (totalWeight <= 0) {
            throw new IllegalArgumentException("Total weight must be positive");
        }

        double[] exact = new double[configs.size()];
        int[] counts = new int[configs.size()];
        int allocated = 0;

        for (int i = 0; i < configs.size(); i++) {
            exact[i] = (configs.get(i).weight / totalWeight) * total;
            counts[i] = (int) Math.floor(exact[i]);
            allocated += counts[i];
        }

        int remaining = total - allocated;
        while (remaining > 0) {
            double maxFrac = -1;
            int maxIdx = 0;
            for (int i = 0; i < configs.size(); i++) {
                double frac = exact[i] - counts[i];
                if (frac > maxFrac) {
                    maxFrac = frac;
                    maxIdx = i;
                }
            }
            counts[maxIdx]++;
            exact[maxIdx] = counts[maxIdx];
            remaining--;
        }

        return counts;
    }

    @Override
    public CompletableFuture<BenchmarkProducer> createProducer(String topic) {
        KafkaProducer<String, byte[]> kafkaProducer = new KafkaProducer<>(producerProperties);
        BenchmarkProducer benchmarkProducer = new KafkaBenchmarkProducer(kafkaProducer, topic);
        try {
            // Add to producer list to close later
            producers.add(benchmarkProducer);
            return CompletableFuture.completedFuture(benchmarkProducer);
        } catch (Throwable t) {
            kafkaProducer.close();
            CompletableFuture<BenchmarkProducer> future = new CompletableFuture<>();
            future.completeExceptionally(t);
            return future;
        }
    }

    @Override
    public CompletableFuture<BenchmarkConsumer> createConsumer(
            String topic, String subscriptionName, ConsumerCallback consumerCallback) {
        Properties properties = new Properties();
        consumerProperties.forEach((key, value) -> properties.put(key, value));
        properties.put(ConsumerConfig.GROUP_ID_CONFIG, subscriptionName);
        KafkaConsumer<String, byte[]> consumer = new KafkaConsumer<>(properties);
        try {
            consumer.subscribe(Arrays.asList(topic));
            return CompletableFuture.completedFuture(
                    new KafkaBenchmarkConsumer(consumer, consumerProperties, consumerCallback));
        } catch (Throwable t) {
            consumer.close();
            CompletableFuture<BenchmarkConsumer> future = new CompletableFuture<>();
            future.completeExceptionally(t);
            return future;
        }
    }

    @Override
    public void close() throws Exception {
        for (BenchmarkProducer producer : producers) {
            producer.close();
        }

        for (BenchmarkConsumer consumer : consumers) {
            consumer.close();
        }
        admin.close();
    }

    private static String applyZoneId(String propValue, String zoneId) {
        return propValue.replace(ZONE_ID_TEMPLATE, zoneId);
    }

    // Visible for testing
    static final ObjectMapper mapper =
            new ObjectMapper(new YAMLFactory())
                    .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);
}
