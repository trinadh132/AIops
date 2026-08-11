package com.opsagent.mock.service;

import com.opsagent.mock.model.FailureMode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * Drives the two failure modes that unfold over time rather than per-request:
 * MEMORY_LEAK (gradual growth) and OOM_KILL (the eventual crash it causes).
 * Also resets the sliding window RETRY_STORM counts against.
 */
@Component
public class BackgroundSimulator {

    private static final Logger log = LoggerFactory.getLogger(BackgroundSimulator.class);

    private final FailureStateService state;

    public BackgroundSimulator(FailureStateService state) {
        this.state = state;
    }

    @Scheduled(fixedRate = 5000)
    public void simulateMemoryLeak() {
        if (!state.isActive(FailureMode.MEMORY_LEAK) && !state.isActive(FailureMode.OOM_KILL)) {
            return;
        }

        long usage = state.growMemory();
        MDC.put("failure_mode", FailureMode.MEMORY_LEAK.name());
        MDC.put("event_type", "memory_growth");
        try {
            log.warn("memory_usage_mb={} threshold_mb={} message=\"Simulated heap usage growing\"",
                    usage, state.getOomThresholdMb());
        } finally {
            MDC.remove("failure_mode");
            MDC.remove("event_type");
        }

        if (usage >= state.getOomThresholdMb()) {
            MDC.put("failure_mode", FailureMode.OOM_KILL.name());
            MDC.put("event_type", "process_killed");
            try {
                log.error("memory_usage_mb={} message=\"Simulated OOM: process killed by OS, container restarting\"", usage);
            } finally {
                MDC.remove("failure_mode");
                MDC.remove("event_type");
            }
            // A real restart clears the leak; model that here.
            state.deactivate(FailureMode.MEMORY_LEAK);
            state.deactivate(FailureMode.OOM_KILL);
        }
    }

    @Scheduled(fixedRate = 10000)
    public void resetRetryStormWindow() {
        state.resetWindow();
    }
}
