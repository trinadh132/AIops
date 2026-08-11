package com.opsagent.mock.exception;

import com.opsagent.mock.model.FailureMode;
import org.springframework.http.HttpStatus;

/**
 * Thrown by the business endpoints when an active failure mode decides a
 * given request should fail. Carries enough context for the global handler
 * to emit a structured log line and a sensible HTTP response.
 */
public class SimulatedFailureException extends RuntimeException {

    private final FailureMode failureMode;
    private final HttpStatus httpStatus;

    public SimulatedFailureException(FailureMode failureMode, HttpStatus httpStatus, String message) {
        super(message);
        this.failureMode = failureMode;
        this.httpStatus = httpStatus;
    }

    public FailureMode getFailureMode() {
        return failureMode;
    }

    public HttpStatus getHttpStatus() {
        return httpStatus;
    }
}
