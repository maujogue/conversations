"""Seed Langfuse with a month of synthetic router traffic, plus its counterfactual.

Demo tooling only. Two things make this safe to run against a project that also
holds real traces:

- every seeded span lands in a dedicated Langfuse *environment*
  (``demo`` for the routed answers, ``demo-baseline`` for the counterfactual), so
  no widget that filters on environment can mix seeded and real traffic;
- nothing is written to the database. This command only talks to Langfuse.

Why OTLP and not the ingestion API
----------------------------------

Langfuse v4 running in ``events_only`` mode rejects ``trace-create`` and
``generation-create`` on ``/api/public/ingestion`` - that endpoint only accepts
scores. Traces must arrive over OTLP. The SDK's ``start_observation`` has no
``start_time``, so a month of history cannot be built with it; we drive the
underlying OTel tracer directly, which does accept explicit start and end times,
and use Langfuse's own attribute helpers so the spans are indistinguishable from
SDK-produced ones.

The counterfactual
------------------

Langfuse cannot compute "what would this have cost on another model": it only
aggregates what it is given. So each seeded turn is emitted twice - once as the
router actually served it, and once as the same turn would have been served by
``demo_seed.BASELINE_MODEL_HRID`` on identical token counts. The savings widget
is then an ordinary group-by on ``environment``.

Costs are in **euros**, computed from ``input_price_eur_per_mtok`` /
``output_price_eur_per_mtok`` in the LLM configuration - the same prices the
arena freezes on a comparison. Langfuse labels cost columns with a dollar sign
regardless; the widget names say EUR.
"""

import datetime
import random
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from chat.demo_seed import (
    ARENA_ENVIRONMENT,
    ARENA_VOTE_RATE,
    BASELINE_ENVIRONMENT,
    BASELINE_MODEL_HRID,
    CHALLENGER_WIN_RATES,
    DOMAIN_WEIGHTS,
    MONTHLY_TURNS,
    MONTHLY_USERS,
    PRODUCTION_ENVIRONMENT,
    ROUTER_COMPLETION_TOKENS,
    ROUTER_MODEL_HRID,
    ROUTER_PROMPT_TOKENS,
    SAMPLING_RATE,
    SHORTCUT_SHARE,
    TASK_WEIGHTS,
    TIER_LATENCY_MS,
    TIER_MIX,
    TIER_MODEL,
    TIER_TOKENS,
    answer_cost_eur,
    answer_span_name,
)
from chat.enums import RoutingTier

ARENA_SCORE_NAME = "arena_preference"
ROUTED_SPAN = "conversation"
CLASSIFIER_SPAN = "router-classifier"
DAY_SECONDS = 86400


class Command(BaseCommand):
    """Emit a month of synthetic routed turns, and the single-model counterfactual."""

    help = "Seed Langfuse with demo router traffic and its single-model counterfactual."

    def add_arguments(self, parser):
        parser.add_argument(
            "--turns",
            type=int,
            default=MONTHLY_TURNS,
            help=f"Turns to emit (default: {MONTHLY_TURNS:,}, one month of traffic).",
        )
        parser.add_argument("--days", type=int, default=30, help="Spread them over N days.")
        parser.add_argument("--seed", type=int, default=20260917, help="Random seed.")
        parser.add_argument(
            "--batch",
            type=int,
            default=2000,
            help=(
                "Flush the OTLP exporter every N turns (default: 2000). The flush is "
                "synchronous and dominates the runtime: 200 gives about 4,500 turns a "
                "minute, 2000 about 14,000, with no dropped spans at either."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the cost and tier summary without emitting anything.",
        )

    def handle(self, *args, **options):
        if not settings.LANGFUSE_ENABLED:
            raise CommandError("LANGFUSE_ENABLED is false; nothing to seed.")

        configurations = {}
        for hrid in {*TIER_MODEL.values(), BASELINE_MODEL_HRID, ROUTER_MODEL_HRID}:
            configuration = settings.LLM_CONFIGURATIONS.get(hrid)
            if configuration is None:
                raise CommandError(f"Model {hrid!r} is not in the LLM configuration.")
            if configuration.input_price_eur_per_mtok is None:
                raise CommandError(f"Model {hrid!r} has no configured price; cost would be 0.")
            configurations[hrid] = configuration

        rng = random.Random(options["seed"])  # noqa: S311  # demo data, not security
        plan = self._plan(rng, options["turns"], configurations)
        self._report(plan, options["turns"])
        if options["dry_run"]:
            return

        self._emit(plan, options, configurations)

    def _plan(self, rng, turns: int, configurations) -> list[dict]:  # pylint: disable=too-many-locals
        """Draw every turn up front: tier, labels, tokens, and both costs."""
        tiers = list(TIER_MIX)
        tier_weights = [TIER_MIX[tier] for tier in tiers]
        domains = list(DOMAIN_WEIGHTS)
        domain_weights = [DOMAIN_WEIGHTS[d] for d in domains]
        tasks = list(TASK_WEIGHTS)
        task_weights = [TASK_WEIGHTS[t] for t in tasks]
        baseline = configurations[BASELINE_MODEL_HRID]

        # 100,000 users over 1,000,000 turns: about ten turns each, drawn so a few
        # people are heavy users and most are not, which is what the Users page shows.
        users = [f"user-{index:06d}" for index in range(MONTHLY_USERS)]

        plan = []
        for _ in range(turns):
            tier = rng.choices(tiers, weights=tier_weights)[0]
            classified = rng.random() >= SHORTCUT_SHARE
            shape = TIER_TOKENS[tier]
            # A little spread per turn, so the pies are not made of identical rows.
            prompt = max(1, int(shape["prompt"] * rng.uniform(0.6, 1.6)))
            completion = max(1, int(shape["completion"] * rng.uniform(0.6, 1.6)))
            reasoning = int(shape["reasoning"] * rng.uniform(0.6, 1.6))
            billed_output = completion + reasoning
            routed_model = TIER_MODEL[tier]
            p50, p95 = TIER_LATENCY_MS[tier]
            plan.append(
                {
                    "tier": tier,
                    "user_id": users[min(int(rng.triangular(0, len(users), 0)), len(users) - 1)],
                    "domain": rng.choices(domains, weights=domain_weights)[0],
                    "task": rng.choices(tasks, weights=task_weights)[0],
                    "model": routed_model,
                    "prompt": prompt,
                    "completion": completion,
                    "reasoning": reasoning,
                    "billed_output": billed_output,
                    "latency_ms": int(rng.triangular(p50 * 0.6, p95, p50)),
                    "routed_cost": answer_cost_eur(
                        configurations[routed_model], prompt, billed_output
                    ),
                    "classified": classified,
                    "router_cost": (
                        answer_cost_eur(
                            configurations[ROUTER_MODEL_HRID],
                            ROUTER_PROMPT_TOKENS,
                            ROUTER_COMPLETION_TOKENS,
                        )
                        if classified
                        else 0.0
                    ),
                    "baseline_cost": answer_cost_eur(baseline, prompt, billed_output),
                    "age_s": rng.uniform(0, 1) ** 0.8,  # scaled to --days at emit time
                    **_arena_draw(rng, tier),
                }
            )
        return plan

    def _report(self, plan: list[dict], turns: int):
        """Print what the dashboards will show, so the pitch numbers are checkable."""
        routed = sum(row["routed_cost"] + row["router_cost"] for row in plan)
        baseline = sum(row["baseline_cost"] for row in plan)
        self.stdout.write(f"\n{turns} turns, costs in EUR at the configured prices.\n")
        for tier in RoutingTier:
            rows = [row for row in plan if row["tier"] == tier]
            if not rows:
                continue
            cost = sum(row["routed_cost"] for row in rows)
            self.stdout.write(
                f"  {tier.value:<9} {len(rows):>5} turns ({len(rows) / turns:>5.1%})  "
                f"{TIER_MODEL[tier]:<20} EUR {cost:>8.4f} "
                f"({cost / routed:>5.1%} of the routed cost)"
            )
        drawn = sum(1 for row in plan if row["arena_challenger"])
        voted = sum(1 for row in plan if row["arena_voted"])
        self.stdout.write(f"\n  arena: {drawn:,} comparisons drawn, {voted:,} human choices")
        overhead = sum(row["router_cost"] for row in plan)
        classified = sum(1 for row in plan if row["classified"])
        self.stdout.write(
            f"\n  classifier ran on {classified}/{turns} turns ({classified / turns:.0%}); "
            f"the rest took the shortcut path"
        )
        self.stdout.write(
            f"  router classifier overhead: EUR {overhead:.4f} ({overhead / routed:.2%} "
            "of the routed cost)"
        )
        self.stdout.write(f"  routed total:               EUR {routed:.4f}")
        self.stdout.write(
            f"  all on {BASELINE_MODEL_HRID}: EUR {baseline:.4f} "
            f"(x{baseline / routed:.1f}, saving {1 - routed / baseline:.1%})\n"
        )

    @staticmethod
    def _trace_id_of(span) -> str:
        """The 32-character Langfuse trace id of an OTel span."""
        return format(span.get_span_context().trace_id, "032x")

    def _emit_arena(self, emitter, row, turn):
        """Emit the challenger's answer and the vote, as one comparison would.

        The champion's answer is the routed trace we already wrote; this adds the
        challenger's own trace and writes the categorical score on both, from each
        side's point of view - the shape chat/arena_scores.py produces in production.
        """
        start_ns = turn["start_ns"]
        duration_ns = turn["duration_ns"]
        tags = turn["tags"]
        challenger = row["arena_challenger"]
        configuration = turn["configurations"].get(challenger)
        if configuration is None or configuration.input_price_eur_per_mtok is None:
            return

        challenger_tags = [
            *[tag for tag in tags if not tag.startswith("model:")],
            f"model:{challenger}",
            "arena:challenger",
        ]
        challenger_root = emitter.root(
            start_ns, tags=challenger_tags, session_id=turn["session_id"], row=row
        )
        challenger_answer = emitter.generation(
            answer_span_name(row["tier"]),
            (start_ns + 5_000_000, start_ns + duration_ns),
            parent=challenger_root,
            model=configuration.model_name,
            usage={"input": row["prompt"], "output": row["billed_output"]},
            cost=answer_cost_eur(configuration, row["prompt"], row["billed_output"]),
        )
        challenger_trace_id = self._trace_id_of(challenger_root)
        challenger_root.end(end_time=start_ns + duration_ns)

        if not row["arena_voted"]:
            return  # The two answers were served; nobody picked a side.

        won = row["challenger_won"]
        voted_at = datetime.datetime.fromtimestamp(
            (start_ns + duration_ns) / 1e9, tz=datetime.timezone.utc
        )
        for trace_id, observation_id, value in (
            (turn["champion_trace_id"], turn["champion_answer_id"], "lost" if won else "won"),
            (challenger_trace_id, challenger_answer, "won" if won else "lost"),
        ):
            # create_score takes no environment: the score inherits its trace's.
            # It does take a timestamp, so the vote is backdated with the turn.
            turn["client"].create_score(
                name=ARENA_SCORE_NAME,
                value=value,
                trace_id=trace_id,
                observation_id=observation_id,
                data_type="CATEGORICAL",
                timestamp=voted_at,
                comment=f"seed challenger={challenger} tier={row['tier'].value}",
            )

    def _emit(self, plan, options, configurations):  # pylint: disable=too-many-locals
        """Write every turn twice: as routed, and as the single-model counterfactual."""
        # Imported lazily: the module stays importable without the langfuse extra.
        # pylint: disable=import-outside-toplevel
        import langfuse  # noqa: PLC0415
        from langfuse._client.attributes import (  # noqa: PLC0415
            LangfuseOtelSpanAttributes as Attr,
        )
        from langfuse._client.attributes import create_generation_attributes  # noqa: PLC0415

        client = langfuse.get_client()
        # start_observation has no start_time, so the OTel tracer is the only way to
        # backdate; the attributes below are the ones the SDK itself writes.
        tracer = client._otel_tracer  # noqa: SLF001  # pylint: disable=protected-access
        routed = _Emitter(tracer, Attr, PRODUCTION_ENVIRONMENT, create_generation_attributes)
        arena = _Emitter(tracer, Attr, ARENA_ENVIRONMENT, create_generation_attributes)
        counterfactual = _Emitter(tracer, Attr, BASELINE_ENVIRONMENT, create_generation_attributes)

        now_ns = time.time_ns()
        window_ns = options["days"] * DAY_SECONDS * 1_000_000_000
        baseline = configurations[BASELINE_MODEL_HRID]

        for index, row in enumerate(plan, start=1):
            start_ns = now_ns - int(row["age_s"] * window_ns)
            duration_ns = row["latency_ms"] * 1_000_000
            session_id = str(uuid.uuid4())
            tags = [
                f"tier:{row['tier'].value}",
                "tier_source:router",
                f"routed:{'classified' if row['classified'] else 'shortcut'}",
                f"domain:{row['domain'].value}",
                f"task:{row['task'].value}",
                f"model:{row['model']}",
            ]

            # 1. The turn as the router actually served it.
            champion_tags = [*tags, "arena:champion"] if row["arena_challenger"] else tags
            root = routed.root(start_ns, tags=champion_tags, session_id=session_id, row=row)
            classifier_ns = start_ns + 5_000_000
            if row["classified"]:
                routed.generation(
                    CLASSIFIER_SPAN,
                    (classifier_ns, classifier_ns + 300_000_000),
                    parent=root,
                    model=configurations[ROUTER_MODEL_HRID].model_name,
                    usage={"input": ROUTER_PROMPT_TOKENS, "output": ROUTER_COMPLETION_TOKENS},
                    cost=row["router_cost"],
                )
            champion_answer = routed.generation(
                answer_span_name(row["tier"]),
                (classifier_ns + 320_000_000, start_ns + duration_ns),
                parent=root,
                model=configurations[row["model"]].model_name,
                usage={"input": row["prompt"], "output": row["billed_output"]},
                cost=row["routed_cost"],
            )
            root.end(end_time=start_ns + duration_ns)

            # 2. On the sampled share of turns, the arena also ran the tier's
            # challenger on the same question, and a human picked a side. Both sides
            # carry an `arena_preference` score, so the Langfuse leaderboard is
            # sliceable by the `model:` tag exactly like the admin scoreboard.
            if row["arena_challenger"]:
                self._emit_arena(
                    arena,
                    row,
                    {
                        "start_ns": start_ns,
                        "duration_ns": duration_ns,
                        "session_id": session_id,
                        "tags": tags,
                        "champion_trace_id": self._trace_id_of(root),
                        "champion_answer_id": champion_answer,
                        "client": client,
                        "configurations": configurations,
                    },
                )

            # 3. The same turn on a single premium model. Same tokens, same labels,
            # different price - that difference is the whole savings claim.
            baseline_duration_ns = int(duration_ns * 1.4)
            baseline_tags = [*tags[:-1], f"model:{BASELINE_MODEL_HRID}", "baseline:single_model"]
            baseline_root = counterfactual.root(
                start_ns, tags=baseline_tags, session_id=session_id, row=row
            )
            counterfactual.generation(
                answer_span_name(row["tier"]),
                (start_ns + 5_000_000, start_ns + baseline_duration_ns),
                parent=baseline_root,
                model=baseline.model_name,
                usage={"input": row["prompt"], "output": row["billed_output"]},
                cost=row["baseline_cost"],
            )
            baseline_root.end(end_time=start_ns + baseline_duration_ns)

            if index % options["batch"] == 0:
                client.flush()
                self.stdout.write(f"  emitted {index}/{len(plan)} turns")

        client.flush()
        self.stdout.write(
            self.style.SUCCESS(
                f"Emitted {len(plan)} routed turns and {len(plan)} counterfactual turns "
                f"into {PRODUCTION_ENVIRONMENT!r} and {BASELINE_ENVIRONMENT!r}; arena "
                f"challenger answers are in {ARENA_ENVIRONMENT!r}."
            )
        )


def _arena_draw(rng, tier) -> dict:
    """Decide whether this turn is an arena draw, and who wins if it is.

    Mirrors the experiments seeded on the Django side: the same sampling rate and
    the same benchmark-anchored win rates, so the Langfuse leaderboard and the
    admin scoreboard cannot disagree.
    """
    challengers = CHALLENGER_WIN_RATES.get(tier) or {}
    if not challengers or rng.random() >= SAMPLING_RATE:
        return {"arena_challenger": None, "arena_voted": False, "challenger_won": False}
    challenger = rng.choice(sorted(challengers))
    # A drawn comparison is two answers; it only becomes a *human choice* when
    # someone votes. Unvoted draws still cost inference, so they are emitted, but
    # they carry no score - that is what keeps the vote count honest.
    return {
        "arena_challenger": challenger,
        "arena_voted": rng.random() < ARENA_VOTE_RATE,
        "challenger_won": rng.random() < challengers[challenger],
    }


def _context_of(span):
    """The OTel context that makes ``span`` the parent of the next one.

    ``start_span`` parents from the *current* context, and nothing here is ever made
    current (that would need a `with` block per span, which is incompatible with the
    explicit start/end times a backdated span needs). Passing the context explicitly
    is what keeps a turn one trace instead of three unrelated ones.
    """
    if span is None:
        return None
    # Lazy import: the module must stay importable without the OTel extra.
    from opentelemetry import trace  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    return trace.set_span_in_context(span)


class _Emitter:
    """Backdated span writer for one Langfuse environment.

    Holds the tracer, the attribute names and the environment so the call sites
    stay readable: every span this writes belongs to the same environment.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, tracer, attr, environment: str, build_generation_attributes
    ):
        self._tracer = tracer
        self._attr = attr
        self._environment = environment
        self._build_generation_attributes = build_generation_attributes

    def root(self, start_ns: int, *, tags: list[str], session_id: str, row: dict):
        """Open the root `conversation` span, carrying the trace-level attributes."""
        attr = self._attr
        span = self._tracer.start_span(ROUTED_SPAN, start_time=start_ns)
        span.set_attribute(attr.TRACE_NAME, ROUTED_SPAN)
        span.set_attribute(attr.TRACE_TAGS, tags)
        span.set_attribute(attr.TRACE_SESSION_ID, session_id)
        span.set_attribute(attr.TRACE_USER_ID, row["user_id"])
        span.set_attribute(attr.ENVIRONMENT, self._environment)
        span.set_attribute(attr.OBSERVATION_TYPE, "span")
        for key in ("tier", "domain", "task"):
            span.set_attribute(f"{attr.TRACE_METADATA}.{key}", row[key].value)
        return span

    def generation(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, name: str, window: tuple[int, int], *, model, usage, cost, parent=None
    ):
        """One generation with explicit token usage and euro cost, backdated.

        Returns its observation id: an arena vote is scored on the *answer*, not on
        the trace. A trace-level score is counted once per observation of its trace,
        so grouping votes by model would double-count every trace that also carries a
        classifier generation. On the observation the join is exact.

        The attributes come from the SDK's own builder rather than hand-written keys:
        it serializes ``usage_details`` and ``cost_details`` as JSON strings on a
        single attribute, and dotted sub-keys are silently dropped (they land as
        zero tokens and zero cost, which is easy to miss until a widget is empty).
        """
        start_ns, end_ns = window
        span = self._tracer.start_span(name, context=_context_of(parent), start_time=start_ns)
        attributes = self._build_generation_attributes(
            name=name,
            model=model,
            usage_details={**usage, "total": usage["input"] + usage["output"]},
            cost_details={"total": cost},
        )
        for key, value in attributes.items():
            span.set_attribute(key, value)
        span.set_attribute(self._attr.ENVIRONMENT, self._environment)
        observation_id = format(span.get_span_context().span_id, "016x")
        span.end(end_time=end_ns)
        return observation_id
