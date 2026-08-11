package com.opsagent.mock;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * Entry point for the mock service used as the failure-injection target for
 * the Self-Healing Ops Agent project (Phase 0).
 *
 * This service exposes:
 *  - /api/orders/{id}   a "real" business endpoint whose behavior degrades
 *                       according to whichever failure modes are active
 *  - /admin/failures/*  an admin-only surface to activate/deactivate failure
 *                       modes on demand, so the agent has something real to
 *                       diagnose
 *
 * See README.md for the full list of failure modes and how to trigger them.
 */
@SpringBootApplication
@EnableScheduling
public class MockServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(MockServiceApplication.class, args);
    }
}
