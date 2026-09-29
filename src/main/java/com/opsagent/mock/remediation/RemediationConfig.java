package com.opsagent.mock.remediation;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.opsagent.mock.service.FailureStateService;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.SqsClientBuilder;

import java.net.URI;
import java.time.Clock;
import java.time.Duration;

/**
 * Wires the remediation executor only when a queue URL is configured
 * (REMEDIATION_QUEUE_URL). Locally and in tests it's absent, so the service
 * runs exactly as before with no AWS dependency at runtime.
 *
 * Credentials and region come from the SDK's default chain: the ECS task
 * role and AWS_REGION on Fargate, or env vars locally.
 */
@Configuration
@ConditionalOnExpression("!'${remediation.queue-url:}'.isEmpty()")
public class RemediationConfig {

    @Bean(destroyMethod = "close")
    SqsClient remediationSqsClient(@Value("${remediation.sqs-endpoint:}") String endpoint) {
        SqsClientBuilder builder = SqsClient.builder();
        if (!endpoint.isEmpty()) {
            // Local SQS-compatible server (ElasticMQ/LocalStack) for testing.
            builder.endpointOverride(URI.create(endpoint));
        }
        return builder.build();
    }

    @Bean
    RemediationExecutor remediationExecutor(SqsClient remediationSqsClient,
                                            @Value("${remediation.queue-url}") String queueUrl,
                                            @Value("${remediation.max-age-seconds:3600}") long maxAgeSeconds,
                                            FailureStateService state,
                                            ObjectMapper mapper,
                                            MeterRegistry meters) {
        return new RemediationExecutor(remediationSqsClient, queueUrl, state, mapper, meters,
                Clock.systemUTC(), Duration.ofSeconds(maxAgeSeconds));
    }
}
