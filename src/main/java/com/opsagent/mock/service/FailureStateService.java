package com.opsagent.mock.service;

import com.opsagent.mock.model.FailureMode;
import org.springframework.stereotype.Service;

import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Holds all mutable "world state" for the mock service: which failure modes
 * are currently injected, plus the handful of simulated counters (memory
 * usage, in-flight requests, request-window count) that the failure
 * behaviors read and write.
 *
 * Single source of truth, deliberately not persisted — restarting the app
 * resets the world, which is the correct behavior for a toy service.
 */
@Service
public class FailureStateService {

    private static final long BASELINE_MEMORY_MB = 256L;
    private static final long MEMORY_LEAK_INCREMENT_MB = 40L;
    private static final long OOM_THRESHOLD_MB = 1024L;
    private static final int THREAD_POOL_CAPACITY = 20;
    private static final int RETRY_STORM_THRESHOLD_PER_WINDOW = 50;

    private final Map<FailureMode, Boolean> activeFailures = new ConcurrentHashMap<>();
    private final AtomicLong simulatedMemoryUsageMb = new AtomicLong(BASELINE_MEMORY_MB);
    private final AtomicLong inFlightRequests = new AtomicLong(0L);
    private final AtomicLong requestsInWindow = new AtomicLong(0L);

    public void activate(FailureMode mode) {
        activeFailures.put(mode, Boolean.TRUE);
    }

    public void deactivate(FailureMode mode) {
        activeFailures.remove(mode);
        if (mode == FailureMode.MEMORY_LEAK || mode == FailureMode.OOM_KILL) {
            simulatedMemoryUsageMb.set(BASELINE_MEMORY_MB);
        }
    }

    public void resetAll() {
        activeFailures.clear();
        simulatedMemoryUsageMb.set(BASELINE_MEMORY_MB);
        inFlightRequests.set(0L);
        requestsInWindow.set(0L);
    }

    public boolean isActive(FailureMode mode) {
        return activeFailures.getOrDefault(mode, Boolean.FALSE);
    }

    public Set<FailureMode> activeModes() {
        return activeFailures.keySet();
    }

    public long getSimulatedMemoryUsageMb() {
        return simulatedMemoryUsageMb.get();
    }

    public long growMemory() {
        return simulatedMemoryUsageMb.addAndGet(MEMORY_LEAK_INCREMENT_MB);
    }

    public long getOomThresholdMb() {
        return OOM_THRESHOLD_MB;
    }

    public int getThreadPoolCapacity() {
        return THREAD_POOL_CAPACITY;
    }

    public long incrementInFlight() {
        return inFlightRequests.incrementAndGet();
    }

    public long decrementInFlight() {
        return inFlightRequests.decrementAndGet();
    }

    public long getInFlightRequests() {
        return inFlightRequests.get();
    }

    public long recordRequestAndGetWindowCount() {
        return requestsInWindow.incrementAndGet();
    }

    public void resetWindow() {
        requestsInWindow.set(0L);
    }

    public int getRetryStormThreshold() {
        return RETRY_STORM_THRESHOLD_PER_WINDOW;
    }
}
