"""
Fetch the log lines behind a CloudWatch alarm.

A CloudWatch alarm notification carries metric data but no log lines, so the
agent pulls them itself with FilterLogEvents, filtered to the alarm's
failure_mode over a window ending at the alarm's state change.
"""

from datetime import datetime
from pathlib import Path

# Per RAGcourps/oom_kill.md: the growth ticks leading up to an OOM kill are
# tagged MEMORY_LEAK and only the final process_killed line is OOM_KILL, so
# filtering on OOM_KILL alone would drop the whole growth phase.
RELATED_FAILURE_MODES = {
    "OOM_KILL": ["MEMORY_LEAK"],
}

# Injection events are the admin API saying "failure mode X activated". They
# carry failure_mode, so a failure_mode-only filter picks them up, and they
# name the answer outright. Keeping them out of what the agent sees is what
# keeps a diagnosis a diagnosis rather than reading the label off the log.
EXCLUDED_EVENT_TYPES = ("failure_injection",)

# Bounds on a single fetch. FilterLogEvents can return empty pages with a
# nextToken while it scans, so the page cap is what guarantees termination.
MAX_PAGES = 20
DEFAULT_MAX_EVENTS = 300


def build_filter_pattern(failure_type: str) -> str:
    modes = [failure_type, *RELATED_FAILURE_MODES.get(failure_type, [])]
    mode_clause = " || ".join(f'$.failure_mode = "{mode}"' for mode in modes)
    exclusions = " && ".join(f'$.event_type != "{e}"' for e in EXCLUDED_EVENT_TYPES)
    return "{ (" + mode_clause + ") && " + exclusions + " }"


def strip_excluded_events(lines: list[str]) -> list[str]:
    """Same exclusion as build_filter_pattern, for logs read from disk
    (captured-logs fixtures) instead of CloudWatch."""
    markers = tuple(f'"event_type":"{e}"' for e in EXCLUDED_EVENT_TYPES)
    return [line for line in lines if not any(m in line for m in markers)]


def fetch_alarm_logs(
    logs_client,
    log_group: str,
    failure_type: str,
    start: datetime,
    end: datetime,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> list[str]:
    """Returns raw log messages oldest-first, keeping the most recent
    max_events when there are more (the lines nearest the alarm matter most;
    the agent condenses repeats anyway)."""
    kwargs = {
        "logGroupName": log_group,
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "filterPattern": build_filter_pattern(failure_type),
    }
    events = []
    for _ in range(MAX_PAGES):
        page = logs_client.filter_log_events(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token

    events.sort(key=lambda e: e["timestamp"])
    return [e["message"].rstrip("\n") for e in events[-max_events:]]


def read_fixture_logs(fixtures_dir, failure_type: str, max_events: int = DEFAULT_MAX_EVENTS) -> list[str]:
    """Local stand-in for fetch_alarm_logs: the captured-logs/ fixture for a
    failure mode. Used when no CloudWatch log group is configured."""
    path = Path(fixtures_dir) / f"{failure_type.lower()}.log"
    if not path.exists():
        return []
    lines = strip_excluded_events(path.read_text(encoding="utf-8").splitlines())
    return lines[-max_events:]
