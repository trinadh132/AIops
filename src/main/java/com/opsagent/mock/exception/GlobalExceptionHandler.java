package com.opsagent.mock.exception;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.server.ResponseStatusException;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/**
 * Translates simulated failures into (a) a structured log line the RAG
 * corpus / retrieval layer can eventually be pointed at, (b) a Prometheus
 * counter tagged by failure mode, and (c) an HTTP response.
 */
@RestControllerAdvice
@SuppressWarnings("null")
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    private final MeterRegistry meterRegistry;

    public GlobalExceptionHandler(MeterRegistry meterRegistry) {
        this.meterRegistry = meterRegistry;
    }

    @ExceptionHandler(SimulatedFailureException.class)
    public ResponseEntity<Map<String, Object>> handleSimulatedFailure(SimulatedFailureException ex) {
        MDC.put("failure_mode", ex.getFailureMode().name());
        MDC.put("event_type", "simulated_failure");
        try {
            log.error("failure_mode={} http_status={} message=\"{}\"",
                    ex.getFailureMode(), ex.getHttpStatus().value(), ex.getMessage());
        } finally {
            MDC.remove("failure_mode");
            MDC.remove("event_type");
        }

        Counter.builder("ops_mock_request_errors_total")
                .tag("failure_mode", ex.getFailureMode().name())
                .register(meterRegistry)
                .increment();

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("timestamp", Instant.now().toString());
        body.put("failureMode", ex.getFailureMode());
        body.put("status", ex.getHttpStatus().value());
        body.put("error", ex.getHttpStatus().getReasonPhrase());
        body.put("message", ex.getMessage());
        
        HttpStatus status = Objects.requireNonNullElse(ex.getHttpStatus(), HttpStatus.INTERNAL_SERVER_ERROR);
        return ResponseEntity.status(status).body(body);
    }

    @ExceptionHandler(ResponseStatusException.class)
    public ResponseEntity<Map<String, Object>> handleBadRequest(ResponseStatusException ex) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("timestamp", Instant.now().toString());
        body.put("status", ex.getStatusCode().value());
        body.put("message", ex.getReason());
        return ResponseEntity.status(ex.getStatusCode()).body(body);
    }
}
