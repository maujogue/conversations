# Langfuse `Router` dashboard

`router-dashboard.json` is the versioned definition of the **Router** dashboard
described in `docs/llm-router-spec.md` (section 12). It is pushed with the
`push_langfuse_dashboard` management command, which creates or updates the
dashboard and its widgets in place (matched by name) and never deletes
anything else in the project.

## What it shows

| Row | Widget | View / chart | How |
|---|---|---|---|
| 1 | Tier share: simple / standard / complex (3 widgets) | observations, bar time series | count of root `conversation` spans whose trace is tagged `tier:<tier>` |
| 2 | Tier `<tier>`: tokens & latency (3 widgets) | observations, pivot table by observation `name` | count, sum of output and total tokens (energy proxy), p50 and p95 latency (ms) on `tier:<tier>` traces. The `conversation` row is end-to-end turn latency; generation rows carry the tokens |
| 3 | Overhead: tokens by observation | observations (generations), pie by `name` | share of total tokens per generation name; the `router-classifier` slice is the routing overhead (spec target: under 2 percent) |
| 3 | Cost per model | observations (generations), horizontal bar by `providedModelName` | sum of `totalCost` (needs model definitions with prices in Langfuse) |
| 3 | Constraint fallbacks | observations, bar time series | count of root `conversation` spans tagged `routed:constraint_fallback` |
| 4 | Arena votes | scores-categorical, bar time series by `stringValue` | count of `arena_preference` scores per value |

Langfuse cannot compute Wh: tokens per tier are the proxy, the Wh conversion
stays in Django (`chat/footprint.py`).

For the same reason `RoutingTierSettings.tier_energy` is **not** pulled from the
Metrics API: Langfuse would only return tokens per tier, which we would then convert
with the very EcoLogits model we already apply to each answer at write time, with its
real parameter counts and reasoning tokens. `refresh_tier_energy` therefore reads our
own CO2 figures (arena comparisons, and the routing metadata of committed assistant
messages) and converts them with `footprint.wh_from_co2_kg`. See `chat/tier_energy.py`.

### Why "per tier" is three widgets

Langfuse dashboards (v4, widget API) only accept a fixed set of group-by
dimensions on the observations view (`name`, `type`, `providedModelName`,
`tags`, `traceName`, `environment`, ...). Grouping by `tags` groups by the
*whole* tag array (`["tier:simple","domain:general",...]`), so it cannot split
by tier, and metadata is not a dimension at all (it is filter-only, as a
`stringObject` filter with a `key`). Tier is therefore applied as a widget
filter (`tags any of ["tier:<tier>"]`), one widget per tier. The filters would
work identically on `metadata.tier`; tags are used because they are the
primary attribute in the spec.

### Trace-level score counts

In Langfuse v4 a trace-level score is counted once per observation of its
trace unless the query joins traces. The arena widget adds the filter
`traceName is not null` to force that join; keep it when editing the widget.

## Trace attributes the dashboard expects

Set with `langfuse.propagate_attributes(tags=..., metadata=...)` so that they
land on the trace and every observation of the turn.

Tags (arrays of strings, all optional except the tier ones):

- `tier:simple` | `tier:standard` | `tier:complex`
- `tier_source:router` | `tier_source:user` | `tier_source:constraint`
- `domain:<slug>`, `task:<slug>`
- `routed:<reason>`, in particular `routed:constraint_fallback`
- `arena:champion` | `arena:challenger`

Metadata (same keys without the prefix, string values): `tier`, `tier_source`,
`domain`, `task`, `routed`, `arena`. Not used by the current widgets but
available for ad-hoc filtering in the UI.

Observations:

- the root span of a turn is named `conversation` (see
  `chat/clients/pydantic_ai.py`); the "traces" counts are counts of that span;
- the classifier generation is named `router-classifier`;
- generations carry the model name and `usage_details` (tokens); cost needs a
  model definition in Langfuse.

Scores: categorical `arena_preference`, one score per side of a comparison, pushed by
`chat/arena_scores.py` when the comparison closes. Values, from that side's point of
view: `won`, `lost`, `tie`, `both_bad`, `abandoned`. The score is idempotent by
`score_id = "<comparison id>-<role>"`, and its comment carries the comparison id and
the origin (`draw` or `manual`). No model ever scores an answer: only humans vote.

## Pushing the dashboard

The command uses `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` (basic auth) and requires `LANGFUSE_ENABLED=true`.

From the repo root, against the local Langfuse (only `src/backend` is mounted
in the container, so the JSON is piped on stdin):

```bash
docker compose exec -T app-dev python manage.py push_langfuse_dashboard --dry-run --definition - \
  < docs/langfuse/router-dashboard.json
docker compose exec -T app-dev python manage.py push_langfuse_dashboard --definition - \
  < docs/langfuse/router-dashboard.json
```

Outside Docker (repo checked out, default path resolves):

```bash
python manage.py push_langfuse_dashboard
```

The command is idempotent: run it again after editing the JSON and the
existing widgets and dashboard are updated in place. It prints the dashboard
URL (`<LANGFUSE_HOST>/project/<projectId>/dashboards/<id>`; replace
`host.docker.internal` with `localhost` when the host was set for Docker).

To rename a widget, delete the old one in the Langfuse UI (or via the API)
after the push; the command deliberately never deletes.

## Format of `router-dashboard.json`

```json
{
  "name": "Router",
  "description": "...",
  "filters": [],
  "widgets": [
    {
      "placement": {"id": "tier-share-simple", "x": 0, "y": 0, "width": 4, "height": 5},
      "widget": {
        "name": "Router · Tier share: simple",
        "description": "...",
        "view": "observations",
        "dimensions": [],
        "metrics": [{"measure": "count", "agg": "count"}],
        "filters": [{"column": "tags", "operator": "any of", "value": ["tier:simple"], "type": "arrayOptions"}],
        "chartType": "BAR_TIME_SERIES"
      }
    }
  ]
}
```

`widget` is the body of `POST /api/public/unstable/dashboard-widgets`;
`placement` is a tile on the 12-column grid of
`/api/public/unstable/dashboards` (`definition.widgets`). Widget names must be
unique: they are the idempotency key.
