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
| 5 | Arena votes: challenger side / champion side (2 widgets) | scores-categorical, pivot table by `stringValue` | count of `arena_preference` scores per value on `arena:<role>` traces. Filter further on `model:<hrid>` or `arena_experiment:<uuid>` to read one challenger or one experiment |

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

On the two traces of an arena comparison only (see
`AIAgentService._arena_trace_tags`):

- `arena:champion` | `arena:challenger`
- `arena_experiment:<experiment uuid>`
- `arena_origin:draw` | `arena_origin:manual`
- `model:<hrid>` - the side's own model, the row key of the admin scoreboard

The last three exist so the `arena_preference` score below can be sliced per
challenger and per experiment: the score is written on the trace, and
`scores-categorical` accepts `tags` as a filter but the only groupable
dimensions are trace and score attributes (`traceName`, `tags`, `userId`,
`sessionId`, `observationName`, `observationModelName`, `stringValue`, ...), so
"votes of one challenger" is a *filter* on `model:<hrid>`, never a group-by.
Grouping by `tags` groups by the whole tag array, as on the observations view.

Metadata (same keys without the prefix, string values): `tier`, `tier_source`,
`domain`, `task`, `router_reason`, `router_confidence`, and on arena traces
`arena`, `arena_experiment`, `arena_origin`, `model`. Not used by the current
widgets but available for ad-hoc filtering in the UI.

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

## What stays in the Django admin

The arena results page (`admin/chat/arenaexperiment/results.html`) is the
*promotion decision*, not a metrics page. Its challenger table holds only what
the decision is made of: decisive votes, win rate, the 95% Wilson interval, the
cost ratio, the Wh ratio and the verdict.

Everything that is observability moved here:

| Was a column of the scoreboard | Now |
|---|---|
| Both good / Both bad / Abandoned | `Arena votes: <role> side` widgets (`tie`, `both_bad`, `abandoned`), and the KPI tiles at the top of the results page |
| Errored | the `failures / attempts` lines above the table, and the error observations in Langfuse |
| Cost / answer, Champion cost / answer | `Cost per model` widget |
| Wh / answer | a single champion KPI tile; the per-challenger figure is the ratio |
| Time to vote | a single KPI tile: it is a per-comparison metric, identical on both rows |
| Second opinions | its own table under the scoreboard, since it is a different population |

Two figures deliberately did **not** move. The cost ratio is computed from the
EUR `price_snapshot` frozen on each comparison and is restricted to the turns
of that comparison; Langfuse costs are USD estimates over all traffic. And
Langfuse cannot compute Wh at all, for the reason given in "What it shows"
above.

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

## The modelled month and the `Router · Production` dashboard

`production-dashboard.json` is a second dashboard, built for one claim: *adequate
intelligence for the user, savings for the state*. It reads only seeded traffic.

### The volume it models

One month of the tool as it actually runs, from `chat/demo_seed.py`:

| | |
|---|---|
| users | 100,000 / month |
| turns | 1,000,000 / month (`MONTHLY_TURNS`) |
| arena draws | 100,000 (`SAMPLING_RATE` 10%, the experiments' setting) |
| human choices | 5,000 (`ARENA_VOTE_RATE` 5%) |

The vote rate is not a dial to make a number look good: it is pinned by the 5,000
choices a month the tool really collects, given a 10 percent sample. It is low
because most people never vote, which is the reason the arena needs a sample that
large in the first place.

What the seeded month comes to, in euros at the configured prices:

| | EUR / month |
|---|---|
| routed, classifier included | 277 |
| the same turns on `mistral-medium-3-5` alone | 5,114 |
| **saved** | **4,838 (x18.5, 94.6%)** |

Distinct users are not a dashboard tile: the widget API exposes a `uniq`
aggregation whose semantics could not be verified here (every Langfuse read API is
disabled in `events_only` mode, so widget output cannot be checked from a script).
Langfuse's own Users page has the figure.

Of the routed 277 EUR, 110 is the router classifier - see the findings below.

### Seeding

```bash
# Django: one arena experiment per tier, 10% sampling, 100 draws/user/day.
docker compose exec -T app-dev python manage.py seed_routing_tiers
docker compose exec -T app-dev python manage.py seed_router_demo --explain   # sources
docker compose exec -T app-dev python manage.py seed_router_demo

# Langfuse: 30 days of routed traffic plus the single-model counterfactual.
docker compose exec -T app-dev python manage.py seed_langfuse_demo --dry-run
docker compose exec -T app-dev python manage.py seed_langfuse_demo   # ~80 min, ~5M spans

docker compose exec -T app-dev python manage.py push_langfuse_dashboard --definition - \
  < docs/langfuse/production-dashboard.json
```

A full month is about 5 million spans and takes roughly 70 minutes; `--turns`
cuts it down for a quick check, at the cost of every absolute euro figure on the
dashboard. The synchronous flush dominates that runtime, so `--batch` is worth
about 3x: 200 gives ~4,500 turns a minute against ~14,000 at the default 2000,
with no dropped spans at either. Watch the log for `Queue full, dropping Span`
if you raise it much further, and check afterwards that the `answer-*` count
equals the `conversation` count - they match exactly when nothing was dropped.

**Langfuse drops silently under sustained load.** The emitter is roughly three
times faster than the backend persists, and when the backlog grows past some
point Langfuse accepts spans, answers 200, and never writes them - no error in
the SDK log, nothing in the worker log, an empty Redis queue. A 1,000,000-turn
run in one shot landed 257,510 turns and reported success. Never trust the
emitter's own count: compare it against the rows, and seed in chunks with a
verified drain between them, so the backend is never more than one chunk behind:

```bash
docker exec langfuse-local-langfuse-clickhouse-1 clickhouse-client \
  --user clickhouse --password clickhouse --query \
  "SELECT countIf(name='conversation') FROM default.events_core WHERE environment='production'"
```

Chunks of about 40,000 turns land ~99.5 percent each, and re-reading the true
count before every chunk makes the loop self-correcting.

**Stopping a run takes two steps.** `docker compose exec` on the host is only a
client: killing it leaves the command running *inside* the container, still
emitting. A seed that appears to have been cancelled but whose row counts keep
climbing is this. Check and kill it properly:

```bash
docker compose exec -T app-dev ps aux | grep "manage.py seed_langfuse_demo"
docker compose exec -T app-dev pkill -9 -f "manage.py seed_langfuse_demo"
```

Re-seeding means clearing the old rows first, and Langfuse v4 in `events_only`
mode exposes no delete for traces, so it is done in ClickHouse directly:

```bash
docker exec langfuse-local-langfuse-clickhouse-1 clickhouse-client \
  --user clickhouse --password clickhouse --query \
  "DELETE FROM default.events_core WHERE environment LIKE 'production%'"
```

Repeat for `events_full`. Allow the ingestion backlog to drain first: OTLP
acceptance is asynchronous, so stopping the command does **not** stop the writes -
Langfuse has already queued whatever was sent and its worker keeps replaying it at
roughly 600 rows a second for as long as the backlog lasts. Poll the row count
until it stops rising, then delete; deleting while the worker is still replaying
just makes the rows come back.

### Three environments, never mixed

| Environment | What is in it |
|---|---|
| `production` | the turns as the router served them, classifier included. Every "what we spend" figure reads this and only this |
| `production-baseline` | the *same* turns, the *same* token counts, served by `mistral-medium-3-5` alone. The counterfactual |
| `production-arena` | the extra challenger answers the arena ordered. Real inference the router did not ask for, kept out of `production` so it cannot inflate the routed total |

They are named for the traffic they *model*, not for being seeded; this deployment's
own traffic stays in `development` and is never mixed in.

Langfuse cannot compute "what would this have cost on another model" - it only
aggregates what it is given - so the counterfactual is *emitted*, not derived.
That is the only way a savings widget can exist here.

### How the tier pies group by tier

Grouping by `tags` groups by the whole tag array, so no widget can split by tier
that way (see "Why per tier is three widgets" above), and grouping by
`providedModelName` gives a legend of model names rather than tiers. So the tier
is put where Langfuse *can* group: the observation name. The answer generation of
each turn is named `answer-simple`, `answer-standard` or `answer-complex`
(`demo_seed.answer_span_name`), and both pies group by `name` filtered to those
three. The legend then reads simple / standard / complex, and the
`router-classifier` generation cannot be mistaken for tier-1 traffic.

### How the leaderboard counts a vote once

The `arena_preference` score is written on the **answer observation**, not on the
trace. A trace-level score is counted once per observation of its trace, so a
champion trace - which also carries a `router-classifier` generation - would have
its vote counted twice, and filtering by observation name did not fix it (the
widget came back empty). Scoring the observation makes the join exact, and the
leaderboard is a plain group-by on `observationModelName`.

### Reading the numbers

Seeded traffic is not a measurement. Before quoting anything from this dashboard:

- **win rates** reconstruct published third-party standings, recorded with their
  source in `chat/demo_seed.py` (`BENCHMARK_ANCHORS`). `seed_router_demo --explain`
  prints the table. Several configured models have no published evaluation and are
  anchored to their nearest published ancestor; the substitution is written down;
- **costs** are not invented: they are arithmetic on `input_price_eur_per_mtok` /
  `output_price_eur_per_mtok` from the LLM configuration, in **euros**. Langfuse
  labels cost columns with a dollar sign whatever the unit, so the widget names
  say EUR;
- **energy** comes from EcoLogits on each model's real parameter counts, the same
  call the application makes at write time;
- **token counts and the tier mix** are assumptions, documented at the top of
  `chat/demo_seed.py`.

Two findings the seed surfaced, both visible on the dashboard rather than hidden:

- the **router costs about 40 percent of the routed total**. The classifier
  re-sends a ~1,300-token system prompt (`DEFAULT_ROUTER_PROMPT`) on every turn
  and nothing in `chat/router/` enables provider-side prompt caching, so on the
  simple tier the decision costs more than the answer. Section 8 of the router
  spec targets "under 2 percent of total tokens"; the current design is nowhere
  near it, and prompt caching is the obvious fix;
- **Mistral is not uniformly behind**. `mistral-small-3-2` genuinely beats Gemma 3
  27B on 6 of 7 shared benchmarks and stays the standard-tier champion. The saving
  does not come from Mistral being weak, it comes from not putting the *premium*
  Mistral on every turn.

## Model prices (cost tracking)

Langfuse only fills in `totalCost` for a generation when its model name
matches a **model definition** with a price; router models are not in
Langfuse's built-in list, so the Usage dashboard shows $0 for all of them
until we register one each. `model-prices.json` is a hand-maintained list of
public per-token USD prices (Mistral's own API pricing for its proprietary
models, OpenRouter's listed price for the open-weight ones we don't bill
directly), pushed with the `push_langfuse_model_prices` command:

```bash
docker compose exec -T app-dev python manage.py push_langfuse_model_prices --dry-run --prices - \
  < docs/langfuse/model-prices.json
docker compose exec -T app-dev python manage.py push_langfuse_model_prices --prices - \
  < docs/langfuse/model-prices.json
```

Outside Docker:

```bash
python manage.py push_langfuse_model_prices
```

It matches by `modelName`, but unlike the dashboard command it cannot update
in place: the models API has **no update verb** (a `PATCH` answers 405) and
refuses a `POST` whose name already exists in the project. Re-pushing an entry
the project owns therefore deletes it and creates it again, which is why the
command is still idempotent. Langfuse's own built-in definitions are never
deleted (`isLangfuseManaged: true`, and the API would refuse); when one of our
names collides with a built-in, the project definition simply shadows it, and
the command says `shadow the built-in model '...'` instead of `create`. Note
that the API returns no `projectId` on a model definition, so
`isLangfuseManaged` is the only way to tell the two apart.

Each entry's `matchPattern` is a case-insensitive regex applied
to the model string Langfuse actually sees on a generation - tune it (and
re-run with `--dry-run`) once you've confirmed the exact model names your
deployment emits, since these can include a provider prefix (e.g.
`mistralai/...`) depending on which inference backend is configured. Prices
are estimates for observability, not real billing: `chat/llm_configuration.py`
(`input_price_eur_per_mtok` / `output_price_eur_per_mtok`) remains the source
of truth for anything the app itself charges (e.g. the Arena feature).

### Format of `model-prices.json`

```json
[
  {
    "modelName": "gpt-oss-120b",
    "matchPattern": "(?i)^(openai/)?gpt-oss-120b$",
    "unit": "TOKENS",
    "inputPrice": 0.00000003,
    "outputPrice": 0.00000017,
    "source": "https://openrouter.ai/openai/gpt-oss-120b"
  }
]
```

`inputPrice`/`outputPrice` are USD per token (Langfuse's model-definition
unit), so a public "$X per million tokens" price becomes `X / 1_000_000`.
`source` is documentation only, dropped before the entry is sent to Langfuse.

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
