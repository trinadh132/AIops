package com.opsagent.mock.remediation;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.opsagent.mock.model.FailureMode;
import com.opsagent.mock.service.FailureStateService;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.DeleteMessageRequest;
import software.amazon.awssdk.services.sqs.model.Message;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageResponse;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;

import static com.opsagent.mock.remediation.RemediationExecutor.Outcome.APPLIED;
import static com.opsagent.mock.remediation.RemediationExecutor.Outcome.REJECTED;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class RemediationExecutorTest {

    private static final Instant NOW = Instant.parse("2026-09-29T12:00:00Z");
    private static final String QUEUE = "https://sqs.us-east-1.amazonaws.com/123/remediation";

    private SqsClient sqs;
    private FailureStateService state;
    private SimpleMeterRegistry meters;
    private RemediationExecutor executor;

    @BeforeEach
    void setUp() {
        sqs = mock(SqsClient.class);
        state = new FailureStateService();
        meters = new SimpleMeterRegistry();
        executor = new RemediationExecutor(sqs, QUEUE, state, new ObjectMapper(), meters,
                Clock.fixed(NOW, ZoneOffset.UTC), Duration.ofHours(1));
    }

    private static String message(String action, String modesJson, long issuedAt) {
        return """
                {"run_id":"run-1","issued_at":%d,"action":"%s","failure_modes":%s}
                """.formatted(issuedAt, action, modesJson);
    }

    private static String valid(String modesJson) {
        return message("deactivate_failure_mode", modesJson, NOW.getEpochSecond() - 60);
    }

    @Test
    void appliesAllowlistedActionToEveryListedMode() {
        state.activate(FailureMode.OOM_KILL);
        state.activate(FailureMode.MEMORY_LEAK);
        state.growMemory();

        assertThat(executor.handle(valid("[\"OOM_KILL\",\"MEMORY_LEAK\"]"))).isEqualTo(APPLIED);

        assertThat(state.activeModes()).isEmpty();
        assertThat(state.getSimulatedMemoryUsageMb()).isEqualTo(256L); // leak cleared too
        assertThat(meters.counter("ops_mock_remediations_total", "result", "applied").count()).isEqualTo(1.0);
    }

    @Test
    void redeliveryOfAppliedMessageIsHarmless() {
        state.activate(FailureMode.DISK_FULL);
        assertThat(executor.handle(valid("[\"DISK_FULL\"]"))).isEqualTo(APPLIED);
        assertThat(executor.handle(valid("[\"DISK_FULL\"]"))).isEqualTo(APPLIED);
        assertThat(state.activeModes()).isEmpty();
    }

    @Test
    void rejectsActionsOutsideItsOwnAllowlist() {
        state.activate(FailureMode.DISK_FULL);
        assertThat(executor.handle(message("reset_all", "[\"DISK_FULL\"]", NOW.getEpochSecond()))).isEqualTo(REJECTED);
        assertThat(state.activeModes()).containsExactly(FailureMode.DISK_FULL);
    }

    @Test
    void oneUnknownModeRejectsTheWholeMessage() {
        state.activate(FailureMode.DISK_FULL);
        assertThat(executor.handle(valid("[\"DISK_FULL\",\"NOT_A_MODE\"]"))).isEqualTo(REJECTED);
        assertThat(state.activeModes()).containsExactly(FailureMode.DISK_FULL); // nothing partially applied
    }

    @Test
    void rejectsStaleMalformedAndEmptyMessages() {
        long twoHoursAgo = NOW.minus(Duration.ofHours(2)).getEpochSecond();
        assertThat(executor.handle(message("deactivate_failure_mode", "[\"DISK_FULL\"]", twoHoursAgo))).isEqualTo(REJECTED);
        assertThat(executor.handle(message("deactivate_failure_mode", "[\"DISK_FULL\"]", 0))).isEqualTo(REJECTED);
        assertThat(executor.handle("not json")).isEqualTo(REJECTED);
        assertThat(executor.handle(valid("[]"))).isEqualTo(REJECTED);
        assertThat(meters.counter("ops_mock_remediations_total", "result", "rejected").count()).isEqualTo(4.0);
    }

    @Test
    void pollDeletesAppliedAndRejectedMessages() {
        when(sqs.receiveMessage(any(ReceiveMessageRequest.class))).thenReturn(ReceiveMessageResponse.builder()
                .messages(
                        Message.builder().messageId("1").receiptHandle("r1").body(valid("[\"DISK_FULL\"]")).build(),
                        Message.builder().messageId("2").receiptHandle("r2").body("not json").build())
                .build());

        assertThat(executor.pollOnce()).isEqualTo(2);

        verify(sqs, times(2)).deleteMessage(any(DeleteMessageRequest.class));
    }

    @Test
    void pollLeavesMessageOnQueueWhenHandlingThrows() {
        FailureStateService broken = mock(FailureStateService.class);
        org.mockito.Mockito.doThrow(new IllegalStateException("boom")).when(broken).deactivate(any());
        RemediationExecutor failing = new RemediationExecutor(sqs, QUEUE, broken, new ObjectMapper(), meters,
                Clock.fixed(NOW, ZoneOffset.UTC), Duration.ofHours(1));
        when(sqs.receiveMessage(any(ReceiveMessageRequest.class))).thenReturn(ReceiveMessageResponse.builder()
                .messages(Message.builder().messageId("1").receiptHandle("r1").body(valid("[\"DISK_FULL\"]")).build())
                .build());

        failing.pollOnce();

        // Not deleted: SQS redelivers it, then dead-letters it after maxReceiveCount.
        verify(sqs, never()).deleteMessage(any(DeleteMessageRequest.class));
    }
}
