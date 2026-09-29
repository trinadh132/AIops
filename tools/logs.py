"""
Fetch the log lines behind a CloudWatch alarm.

A CloudWatch alarm notification carries metric data but no log lines, so the
agent pulls them itself with FilterLogEvents, filtered to the alarm's
failure_mode over a window ending at the alarm's state change.
"""

from datetime import datetime

# Per RAGcourps/oom_kill.md: the growth ticks leading up to an OOM kill are
# tagged MEMORY_LEAK and only the final process_killed line is OOM_KILL, so
# filtering on OOM_KILL alone would drop the whole growth phase.
RELATED_FAILURE_MODES = {
    "OOM_KILL": ["MEMORY_LEAK"],
}

# Bounds on a single fetch. FilterLogEvents can return empty pages with a
# nextToken while it scans, so the page cap is what guarantees termination.
MAX_PAGES = 20
DEFAULT_MAX_EVENTS = 300


def build_filter_pattern(failure_type: str) -> str:
    modes = [failure_type, *RELATED_FAILURE_MODES.get(failure_type, [])]
    clauses = " || ".join(f'$.failure_mode = "{mode}"' for mode in modes)
    return "{ " + clauses + " }"


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
