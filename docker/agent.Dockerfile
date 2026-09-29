# syntax=docker/dockerfile:1
# Build from the repo root:  docker build -f docker/agent.Dockerfile .
#
# One image for every Python entrypoint: the CLI scripts today, and later the
# Lambda handler (via awslambdaric) and the MCP server — each is just a
# different command.

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --create-home app
WORKDIR /app

# Dependencies first so code edits don't reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py init.sql eval_cases.json chunks.json ./
COPY RAGcourps ./RAGcourps
COPY tools ./tools

USER app
# No ENTRYPOINT on purpose: locally, `docker compose run agent python ...`
# picks the script. Lambda overrides it via image config (see
# lambda_handler.py): python -m awslambdaric lambda_handler.handler
CMD ["python", "run_real_alert.py", "--help"]
