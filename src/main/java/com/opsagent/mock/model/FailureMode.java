package com.opsagent.mock.model;

/**
 * Mirrors the "Failure Mode" column of the Failure Taxonomy sheet in
 * self_healing_ops_agent_plan.xlsx. Keep these two in sync: if you add a
 * failure mode here, add the corresponding row (and eventually a runbook,
 * Phase 1) in that sheet too.
 */
public enum FailureMode {
    CONNECTION_POOL_EXHAUSTION,
    DB_DEADLOCK,
    SLOW_DOWNSTREAM_DEPENDENCY,
    MEMORY_LEAK,
    DISK_FULL,
    THREAD_POOL_EXHAUSTION,
    BAD_DEPLOY_ERROR_SPIKE,
    CONFIG_DRIFT,
    RETRY_STORM,
    OOM_KILL
}
