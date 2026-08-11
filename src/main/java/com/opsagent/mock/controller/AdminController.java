package com.opsagent.mock.controller;

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
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Admin-only surface (Phase 0, Step 2) for injecting and clearing failure
 * modes on demand. Nothing here is protected by auth — this is a local toy
 * service, not a real admin panel; don't expose it publicly.
 *
 * Example:
 *   curl -X POST http://localhost:8080/admin/failures/memory_leak/activate
 *   curl http://localhost:8080/admin/failures
 *   curl -X POST http://localhost:8080/admin/failures/memory_leak/deactivate
 */
@RestController
@RequestMapping("/admin/failures")
public class AdminController {

    private static final Logger log = LoggerFactory.getLogger(AdminController.class);

    private final FailureStateService failureStateService;
    private final MeterRegistry meterRegistry;

    public AdminController(FailureStateService failureStateService, MeterRegistry meterRegistry) {
        this.failureStateService = failureStateService;
        this.meterRegistry = meterRegistry;
        meterRegistry.gauge("ops_mock_active_failure_modes", failureStateService,
                s -> s.activeModes().size());
    }

    @GetMapping
    public ResponseEntity<Map<String, Object>> listActive() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("activeFailureModes", failureStateService.activeModes());
        body.put("simulatedMemoryUsageMb", failureStateService.getSimulatedMemoryUsageMb());
        body.put("inFlightRequests", failureStateService.getInFlightRequests());
        return ResponseEntity.ok(body);
    }

    @PostMapping("/{mode}/activate")
    public ResponseEntity<Map<String, Object>> activate(@PathVariable String mode) {
        FailureMode failureMode = parse(mode);
        failureStateService.activate(failureMode);
        recordInjection(failureMode, "activate");
        return ResponseEntity.ok(Map.of("failureMode", failureMode, "status", "activated"));
    }

    @PostMapping("/{mode}/deactivate")
    public ResponseEntity<Map<String, Object>> deactivate(@PathVariable String mode) {
        FailureMode failureMode = parse(mode);
        failureStateService.deactivate(failureMode);
        recordInjection(failureMode, "deactivate");
        return ResponseEntity.ok(Map.of("failureMode", failureMode, "status", "deactivated"));
    }

    @PostMapping("/reset")
    public ResponseEntity<Map<String, String>> reset() {
        failureStateService.resetAll();
        log.info("event_type=admin_action action=reset_all message=\"All failure modes cleared\"");
        return ResponseEntity.ok(Map.of("status", "reset"));
    }

    private FailureMode parse(String mode) {
        try {
            return FailureMode.valueOf(mode.toUpperCase());
        } catch (IllegalArgumentException ex) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Unknown failure mode: " + mode);
        }
    }

    private void recordInjection(FailureMode mode, String action) {
        MDC.put("failure_mode", mode.name());
        MDC.put("event_type", "failure_injection");
        try {
            log.warn("action={} failure_mode={} message=\"Failure mode {}d\"", action, mode, action);
        } finally {
            MDC.remove("failure_mode");
            MDC.remove("event_type");
        }

        Counter.builder("ops_mock_failure_injections_total")
                .tag("failure_mode", mode.name())
                .tag("action", action)
                .register(meterRegistry)
                .increment();
    }
}
