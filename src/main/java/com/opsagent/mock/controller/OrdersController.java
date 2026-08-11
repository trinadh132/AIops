package com.opsagent.mock.controller;

import com.opsagent.mock.exception.SimulatedFailureException;
import com.opsagent.mock.model.FailureMode;
import com.opsagent.mock.service.FailureStateService;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Stand-in "real" traffic endpoint. Every request runs through each active
 * failure mode's check; a mode either throws (caught by
 * GlobalExceptionHandler, which does the structured logging) or degrades the
 * request in place (e.g. added latency) and logs inline.
 *
 * This is intentionally a single fat endpoint rather than a full domain
 * model — the point is to give the agent something realistic to alert on,
 * not to build a real orders service.
 */
@RestController
@RequestMapping("/api/orders")
public class OrdersController {

    private static final Logger log = LoggerFactory.getLogger(OrdersController.class);

    private final FailureStateService state;
    private final MeterRegistry meterRegistry;

    public OrdersController(FailureStateService state, MeterRegistry meterRegistry) {
        this.state = state;
        this.meterRegistry = meterRegistry;
    }

    @GetMapping("/{id}")
    public ResponseEntity<Map<String, Object>> getOrder(@PathVariable String id) throws InterruptedException {
        Counter.builder("ops_mock_requests_total").register(meterRegistry).increment();

        long inFlight = state.incrementInFlight();
        MDC.put("request_id", UUID.randomUUID().toString());
        try {
            maybeThreadPoolExhaustion(inFlight);
            maybeConnectionPoolExhaustion();
            maybeDbDeadlock();
            maybeSlowDownstream();
            maybeDiskFull();
            maybeConfigDrift();
            maybeBadDeploySpike();
            maybeRetryStorm();

            log.info("event_type=request_success order_id={} message=\"Order retrieved\"", id);

            Map<String, Object> body = new LinkedHashMap<>();
            body.put("orderId", id);
            body.put("status", "FULFILLED");
            return ResponseEntity.ok(body);
        } finally {
            state.decrementInFlight();
            MDC.remove("request_id");
        }
    }

    private void maybeThreadPoolExhaustion(long inFlight) {
        if (state.isActive(FailureMode.THREAD_POOL_EXHAUSTION) && inFlight > state.getThreadPoolCapacity()) {
            throw new SimulatedFailureException(FailureMode.THREAD_POOL_EXHAUSTION,
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "Thread pool exhausted: " + inFlight + " concurrent requests exceed capacity "
                            + state.getThreadPoolCapacity());
        }
    }

    private void maybeConnectionPoolExhaustion() {
        if (state.isActive(FailureMode.CONNECTION_POOL_EXHAUSTION)
                && ThreadLocalRandom.current().nextInt(100) < 70) {
            throw new SimulatedFailureException(FailureMode.CONNECTION_POOL_EXHAUSTION,
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "No available connections in pool");
        }
    }

    private void maybeDbDeadlock() {
        if (state.isActive(FailureMode.DB_DEADLOCK) && ThreadLocalRandom.current().nextInt(100) < 40) {
            throw new SimulatedFailureException(FailureMode.DB_DEADLOCK,
                    HttpStatus.INTERNAL_SERVER_ERROR,
                    "Lock wait timeout exceeded on orders table");
        }
    }

    private void maybeSlowDownstream() throws InterruptedException {
        if (state.isActive(FailureMode.SLOW_DOWNSTREAM_DEPENDENCY)) {
            long delayMs = 2000 + ThreadLocalRandom.current().nextInt(3000);
            Thread.sleep(delayMs);

            MDC.put("failure_mode", FailureMode.SLOW_DOWNSTREAM_DEPENDENCY.name());
            MDC.put("event_type", "high_latency");
            try {
                log.warn("delay_ms={} message=\"Downstream dependency degraded\"", delayMs);
            } finally {
                MDC.remove("failure_mode");
                MDC.remove("event_type");
            }
        }
    }

    private void maybeDiskFull() {
        if (state.isActive(FailureMode.DISK_FULL) && ThreadLocalRandom.current().nextInt(100) < 50) {
            throw new SimulatedFailureException(FailureMode.DISK_FULL,
                    HttpStatus.INTERNAL_SERVER_ERROR,
                    "Write failed: no space left on device");
        }
    }

    private void maybeConfigDrift() {
        if (state.isActive(FailureMode.CONFIG_DRIFT) && ThreadLocalRandom.current().nextInt(100) < 30) {
            throw new SimulatedFailureException(FailureMode.CONFIG_DRIFT,
                    HttpStatus.INTERNAL_SERVER_ERROR,
                    "Missing or invalid required configuration: ORDERS_DB_URL");
        }
    }

    private void maybeBadDeploySpike() {
        if (state.isActive(FailureMode.BAD_DEPLOY_ERROR_SPIKE) && ThreadLocalRandom.current().nextInt(100) < 60) {
            throw new SimulatedFailureException(FailureMode.BAD_DEPLOY_ERROR_SPIKE,
                    HttpStatus.INTERNAL_SERVER_ERROR,
                    "NullPointerException in OrderService (regression from latest deploy)");
        }
    }

    private void maybeRetryStorm() {
        if (state.isActive(FailureMode.RETRY_STORM)) {
            long windowCount = state.recordRequestAndGetWindowCount();
            if (windowCount > state.getRetryStormThreshold()) {
                throw new SimulatedFailureException(FailureMode.RETRY_STORM,
                        HttpStatus.SERVICE_UNAVAILABLE,
                        "Request rate " + windowCount
                                + "/window exceeds gateway capacity; clients are retrying and amplifying load");
            }
        }
    }
}
