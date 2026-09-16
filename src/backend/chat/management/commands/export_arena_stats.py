"""Export closed arena comparisons as metrics and tags only (router spec section 13)."""

import hashlib
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

import pandas

from chat import models
from chat.enums import ArenaComparisonStatus, ArenaRole

# Columns, in this order, aligned with the metadata shape of the `comparia-fr-arena`
# dataset so a later publication is a formatting job and not a project. Nothing here
# identifies a person or a conversation: no content, no user id, no conversation id,
# no Langfuse trace id, and the comparison id is hashed.
COLUMNS = [
    "comparison_id",  # sha256 of the row's UUID: stable, non-reversible join key
    "date",  # day precision, never the exact timestamp
    "experiment_id",
    "tier",
    "tier_source",
    "domain",
    "task",
    "context_tags",
    "origin",
    "is_control",
    "champion_model",
    "challenger_model",
    "outcome",
    "champion_prompt_tokens",
    "champion_completion_tokens",
    "challenger_prompt_tokens",
    "challenger_completion_tokens",
    "champion_latency_ms",
    "challenger_latency_ms",
    "champion_co2_kg",
    "champion_co2_source",
    "challenger_co2_kg",
    "challenger_co2_source",
    "champion_reasoning_tokens",
    "champion_reasoning_effort",
    "challenger_reasoning_tokens",
    "challenger_reasoning_effort",
    "time_to_vote_ms",
]

FORMATS = ("csv", "parquet")

# Rows that describe a comparison a human actually closed. Seed rows are demo data
# and errored rows never produced two comparable answers.
EXPORTED_STATUSES = (ArenaComparisonStatus.VOTED, ArenaComparisonStatus.ABANDONED)

OUTCOME_ABANDONED = "abandoned"


def hash_id(value) -> str:
    """Non-reversible, stable pseudonym of a comparison id."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def outcome_of(comparison) -> str:
    """``champion`` / ``challenger`` / ``tie`` / ``both_bad``, else ``abandoned``."""
    if comparison.status == ArenaComparisonStatus.VOTED and comparison.winner:
        return comparison.winner
    return OUTCOME_ABANDONED


def comparison_rows(since=None):
    """Exportable rows, oldest first, as dicts keyed by ``COLUMNS``."""
    queryset = models.ArenaComparison.objects.filter(
        status__in=EXPORTED_STATUSES, is_seed=False
    ).order_by("drawn_at")
    if since is not None:
        queryset = queryset.filter(drawn_at__gte=since)

    control_hrids = set(
        models.ArenaChallenger.objects.filter(is_control=True).values_list(
            "experiment_id", "model_hrid"
        )
    )

    rows = []
    for comparison in queryset.defer(
        "champion_payload", "challenger_payload", "input_snapshot"
    ).iterator(chunk_size=500):
        row = {
            "comparison_id": hash_id(comparison.pk),
            "date": comparison.drawn_at.date().isoformat(),
            "experiment_id": str(comparison.experiment_id),
            "tier": comparison.tier,
            "tier_source": comparison.tier_source,
            "domain": comparison.domain,
            "task": comparison.task,
            "context_tags": "|".join(comparison.context_tags or []),
            "origin": comparison.origin,
            "is_control": (comparison.experiment_id, comparison.challenger_model_hrid)
            in control_hrids,
            "champion_model": comparison.champion_model_hrid,
            "challenger_model": comparison.challenger_model_hrid,
            "outcome": outcome_of(comparison),
            "time_to_vote_ms": comparison.time_to_vote_ms,
        }
        for role in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER):
            row[f"{role}_prompt_tokens"] = getattr(comparison, f"{role}_prompt_tokens")
            row[f"{role}_completion_tokens"] = getattr(comparison, f"{role}_completion_tokens")
            row[f"{role}_latency_ms"] = getattr(comparison, f"{role}_latency_ms")
            row[f"{role}_co2_kg"] = getattr(comparison, f"{role}_co2_impact")
            row[f"{role}_co2_source"] = getattr(comparison, f"{role}_co2_source")
            row[f"{role}_reasoning_tokens"] = getattr(comparison, f"{role}_reasoning_tokens")
            row[f"{role}_reasoning_effort"] = getattr(comparison, f"{role}_reasoning_effort")
        rows.append({column: row[column] for column in COLUMNS})
    return rows


def build_dataframe(since=None) -> pandas.DataFrame:
    """The export as a DataFrame, with the full column set even when there is no row."""
    return pandas.DataFrame(comparison_rows(since=since), columns=COLUMNS)


class Command(BaseCommand):
    """Write closed comparisons to CSV or Parquet, metrics and tags only.

    For the Albert team and, later, for publication: the file carries no message
    content, no user, no conversation and no trace id (router spec section 13).
    """

    help = "Export closed arena comparisons (metrics and tags only) to CSV or Parquet."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=FORMATS, default="csv")
        parser.add_argument("--output", required=True, help="Path of the file to write.")
        parser.add_argument(
            "--since",
            help="Only comparisons drawn on or after this day (YYYY-MM-DD).",
        )

    def handle(self, *args, **options):
        since = None
        if options["since"]:
            try:
                day = datetime.strptime(options["since"], "%Y-%m-%d")
            except ValueError as exc:
                raise CommandError("--since must be a YYYY-MM-DD date.") from exc
            since = timezone.make_aware(datetime.combine(day, time.min))

        frame = build_dataframe(since=since)
        output = options["output"]
        if options["format"] == "csv":
            frame.to_csv(output, index=False)
        else:
            try:
                frame.to_parquet(output, index=False)
            except ImportError as exc:
                # pandas is in the image as a transitive dependency but no Parquet
                # engine is: keep CSV working and say what to install.
                raise CommandError(
                    "Parquet needs a pandas engine: install pyarrow"
                    " (`uv add pyarrow`) or export with --format csv."
                ) from exc
        self.stdout.write(f"{len(frame)} comparisons exported to {output}.")
