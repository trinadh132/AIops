package com.opsagent.mock.remediation;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.opsagent.mock.model.FailureMode;
import com.opsagent.mock.service.FailureStateService;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.context.SmartLifecycle;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.DeleteMessageRequest;
import software.amazon.awssdk.services.sqs.model.Message;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * Pulls approved remediation actions from SQS and applies them.
 *
 * Pull, not push: the service runs on Fargate Spot with no load balancer, so
 * it has no stable address for the agent to call, and polling means the
 * admin surface never has to be reachable from outside at all.
 *
 * The agent already validates actions before queueing them; this class
 * validates again against its own allowlist (defense in depth), so a bug or
 * compromise on the agent side still can't make the service run anything
 * outside that list.
 *
 * Runs on its own thread: a 20s long-poll on Spring's single default
 * scheduler thread would stall BackgroundSimulator's memory-leak ticks.
 */
public class RemediationExecutor implements SmartLifecycle {

    private static final Logger log = LoggerFactory.getLogger(RemediationExecutor.class);

    static final Set<String> ALLOWED_ACTIONS = Set.of("deactivate_failure_mode");
    private static final Duration ERROR_BACKOFF = Duration.ofSeconds(5);

    enum Outcome { APPLIED, REJECTED }

    private final SqsClient sqs;
    private final String queueUrl;
    private final FailureStateService state;
    private final ObjectMapper mapper;
    private final MeterRegistry meters;
    private final Clock clock;
    private final Duration maxAge;

    private volatile boolean running;
    private Thread worker;

    public RemediationExecutor(SqsClient sqs, String queueUrl, FailureStateService state, ObjectMapper mapper,
                               MeterRegistry meters, Clock clock, Duration maxAge) {
        this.sqs = sqs;
        this.queueUrl = queueUrl;
        this.state = state;
        this.mapper = mapper;
        this.meters = meters;
        this.clock = clock;
        this.maxAge = maxAge;
    }

    // ---- lifecycle: stop polling promptly on SIGTERM (e.g. a Spot interruption)

    @Override
    public void start() {
        running = true;
        worker = new Thread(this::pollLoop, "remediation-executor");
        worker.setDaemon(true);
        worker.start();
        log.info("event_type=remediation_executor_started queue_url={}", queueUrl);
    }

    @Override
    public void stop() {
        running = false;
        if (worker != null) {
            worker.interrupt();
            try {
                worker.join(Duration.ofSeconds(5).toMillis());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    @Override
    public boolean isRunning() {
        return running;
    }

    private void pollLoop() {
        while (running) {
            try {
                pollOnce();
            } catch (RuntimeException e) {
                if (!running) {
                    break;
                }
                log.warn("event_type=remediation_poll_error message=\"{}\"", e.toString());
                try {
                    Thread.sleep(ERROR_BACKOFF.toMillis());
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }
    }

    /** One long-poll round. Returns how many messages were received. */
    int pollOnce() {
        List<Message> messages = sqs.receiveMessage(ReceiveMessageRequest.builder()
                .queueUrl(queueUrl)
                .maxNumberOfMessages(10)
                .waitTimeSeconds(20)
                .build()).messages();

        for (Message message : messages) {
            try {
                handle(message.body());
            } catch (RuntimeException e) {
                // Unexpected failure: leave the message on the queue. It
                // becomes visible again after the visibility timeout and
                // lands in the dead-letter queue after maxReceiveCount.
                log.error("event_type=remediation_error message_id={} message=\"{}\"", message.messageId(), e.toString());
                continue;
            }
            // Applied or rejected, it's final either way: a rejected message
            // (bad action, unknown mode, too old) will never become valid,
            // so redelivering it would only repeat the rejection.
            sqs.deleteMessage(DeleteMessageRequest.builder()
                    .queueUrl(queueUrl)
                    .receiptHandle(message.receiptHandle())
                    .build());
        }
        return messages.size();
    }

    Outcome handle(String body) {
        JsonNode node;
        try {
            node = mapper.readTree(body);
        } catch (JsonProcessingException e) {
            return reject("unknown", "malformed_json");
        }

        String runId = node.path("run_id").asText("unknown");
        String action = node.path("action").asText("");
        if (!ALLOWED_ACTIONS.contains(action)) {
            return reject(runId, "action_not_allowed");
        }

        long issuedAt = node.path("issued_at").asLong(0);
        if (issuedAt <= 0 || Instant.ofEpochSecond(issuedAt).plus(maxAge).isBefore(clock.instant())) {
            // An approval from hours ago describes an old situation; don't
            // apply it to whatever the service is doing now.
            return reject(runId, "stale_or_missing_issued_at");
        }

        JsonNode modesNode = node.path("failure_modes");
        if (!modesNode.isArray() || modesNode.isEmpty()) {
            return reject(runId, "no_failure_modes");
        }
        // Validate every mode before applying any: all-or-nothing.
        List<FailureMode> modes = new ArrayList<>();
        for (JsonNode m : modesNode) {
            try {
                modes.add(FailureMode.valueOf(m.asText()));
            } catch (IllegalArgumentException e) {
                return reject(runId, "unknown_failure_mode");
            }
        }

        for (FailureMode mode : modes) {
            // Idempotent: deactivating an inactive mode is a no-op, so an
            // SQS redelivery of an already-applied message is harmless.
            state.deactivate(mode);
            withMdc(runId, mode.name(), "remediation_applied", () ->
                    log.warn("run_id={} action={} failure_mode={} message=\"Remediation applied\"", runId, action, mode));
        }
        meters.counter("ops_mock_remediations_total", "result", "applied").increment();
        return Outcome.APPLIED;
    }

    private Outcome reject(String runId, String reason) {
        withMdc(runId, null, "remediation_rejected", () ->
                log.error("run_id={} reason={} message=\"Remediation message rejected\"", runId, reason));
        meters.counter("ops_mock_remediations_total", "result", "rejected").increment();
        return Outcome.REJECTED;
    }

    private static void withMdc(String runId, String failureMode, String eventType, Runnable body) {
        MDC.put("run_id", runId);
        MDC.put("event_type", eventType);
        if (failureMode != null) {
            MDC.put("failure_mode", failureMode);
        }
        try {
            body.run();
        } finally {
            MDC.remove("run_id");
            MDC.remove("event_type");
            MDC.remove("failure_mode");
        }
    }
}
