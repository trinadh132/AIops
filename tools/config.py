"""
Resolve secrets from SSM Parameter Store into the process environment.

Locally, secrets come from .env via python-dotenv and this module does
nothing. In Lambda, SSM_PARAMETER_PREFIX (e.g. "/ops-agent") is set and the
parameters under it are loaded once per cold start.

Why write into os.environ instead of returning a settings object: llm.py,
searchllm.py and query_retrieval.py already read os.environ, and llm.py does
it at import time. Populating the environment before they're imported keeps
those modules unchanged. The cost is that load_config() must run before the
first import of llm.py — lambda_handler.py does this at module load.
"""

import logging
import os

logger = logging.getLogger("self_healing_ops.config")

REQUIRED_KEYS = ("OPENROUTER_API", "DATABASE_URL")


def load_config(ssm_client=None) -> None:
    prefix = os.environ.get("SSM_PARAMETER_PREFIX")
    if not prefix:
        return

    missing = [k for k in REQUIRED_KEYS if not os.environ.get(k)]
    if not missing:
        return

    if ssm_client is None:
        import boto3  # only needed in AWS; keeps local runs free of boto3 setup
        ssm_client = boto3.client("ssm")

    loaded = []
    kwargs = {"Path": prefix.rstrip("/"), "WithDecryption": True, "Recursive": False}
    while True:
        page = ssm_client.get_parameters_by_path(**kwargs)
        for param in page["Parameters"]:
            key = param["Name"].rsplit("/", 1)[-1]
            # An explicitly set env var wins, so a single value can be
            # overridden for debugging without editing SSM.
            if not os.environ.get(key):
                os.environ[key] = param["Value"]
                loaded.append(key)
        if "NextToken" not in page:
            break
        kwargs["NextToken"] = page["NextToken"]

    # Log names only, never values.
    logger.info("Loaded %d parameter(s) from SSM under %s: %s", len(loaded), prefix, sorted(loaded))

    still_missing = [k for k in REQUIRED_KEYS if not os.environ.get(k)]
    if still_missing:
        raise RuntimeError(f"Missing required config {still_missing} (checked env and SSM {prefix})")
