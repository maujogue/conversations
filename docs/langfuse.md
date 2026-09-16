# Langfuse tracing

The backend traces every conversation turn to [Langfuse](https://langfuse.com)
through pydantic-ai's OpenTelemetry instrumentation (see
`chat/clients/pydantic_ai.py`). Traces carry the user (`user_id`), the
conversation (`session_id`), router tags (`tier:*`, `domain:*`, `task:*`)
and, for arena turns, the vote as a categorical score.

Langfuse is not part of this project's Docker Compose stack. Point the app
at whichever instance you use: Langfuse Cloud, the team's self-hosted
instance, or a standalone local one.

## Connect the app

Set in `env.d/development/common` (the `.dist` file carries the same block
commented out), then recreate `app-dev` and `celery-dev`:

```
LANGFUSE_ENABLED=true
LANGFUSE_HOST=https://your-langfuse-host
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

From inside Docker, a Langfuse running on your own machine is reachable as
`http://host.docker.internal:<port>`; `app-dev` already declares that host
alias.

Check from the app container:

```bash
bin/compose exec app-dev python -c "from langfuse import get_client; print(get_client().auth_check())"
```

## Running Langfuse locally, standalone

Langfuse v4 self-hosts with the official
[docker-compose](https://langfuse.com/self-hosting/docker-compose) (web,
worker, PostgreSQL, ClickHouse, Redis, MinIO). Run it from its own directory
as its own compose project, on a port that does not collide with the
frontend (3000) or MinIO (9000, 9001). Set the `LANGFUSE_INIT_*` variables
so the organisation, project and API keys exist at first boot and can be
pasted into the env file above without clicking through the UI.

Metrics API v2 (used by the `refresh_tier_energy` command) requires
Langfuse v4; v3 only serves the legacy `/api/public/metrics`.

## What is in Langfuse versus the Django admin

See `docs/llm-router-spec.md`, section 12. Short version: Langfuse holds
traces, tags, costs, energy, latency, the vote scores, the router judge and
the `Router` dashboard; Django holds the tier settings, the arena
comparisons as system of record, the results page with the promotion
verdict, and the retention rules.

The `Router` dashboard definition is versioned in `docs/langfuse/` and
pushed with the public API; see that folder's README.
