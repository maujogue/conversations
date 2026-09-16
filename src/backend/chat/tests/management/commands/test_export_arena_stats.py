"""Tests for the `export_arena_stats` command: column set, exclusions, privacy."""

# pylint: disable=redefined-outer-name, unused-argument

from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

import pandas
import pytest

from chat import models
from chat.enums import ArenaComparisonStatus, ArenaOrigin, ArenaRole, RoutingTier
from chat.factories import ArenaComparisonFactory
from chat.management.commands.export_arena_stats import COLUMNS, hash_id

pytestmark = pytest.mark.django_db


@pytest.fixture
def voted():
    """One closed, voted, fully measured comparison."""
    return ArenaComparisonFactory(
        status=ArenaComparisonStatus.VOTED,
        winner=ArenaRole.CHALLENGER,
        tier=RoutingTier.STANDARD.value,
        tier_source="router",
        domain="legal",
        task="writing",
        context_tags=["plain", "web_search"],
        origin=ArenaOrigin.DRAW.value,
        champion_prompt_tokens=100,
        champion_completion_tokens=200,
        champion_latency_ms=1200,
        champion_co2_impact=0.0001,
        champion_co2_source="provider",
        challenger_prompt_tokens=110,
        challenger_completion_tokens=220,
        challenger_latency_ms=900,
        challenger_co2_impact=0.0002,
        challenger_co2_source="estimated_reasoning",
        challenger_reasoning_tokens=300,
        challenger_reasoning_effort="high",
        time_to_vote_ms=4200,
    )


def _export(tmp_path, name="arena.csv", **options):
    path = tmp_path / name
    call_command("export_arena_stats", "--output", str(path), **options)
    return pandas.read_csv(path) if name.endswith(".csv") else pandas.read_parquet(path)


def test_export_columns_are_metrics_and_tags_only(tmp_path, voted):
    """Exactly the agreed columns: no content, no user, no conversation, no trace id."""
    frame = _export(tmp_path)

    assert list(frame.columns) == COLUMNS
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["comparison_id"] == hash_id(voted.pk)
    assert str(voted.pk) not in str(row.to_dict())
    assert row["date"] == voted.drawn_at.date().isoformat()
    assert row["tier"] == "standard"
    assert row["domain"] == "legal"
    assert row["task"] == "writing"
    assert row["context_tags"] == "plain|web_search"
    assert row["origin"] == "draw"
    assert not row["is_control"]
    assert row["outcome"] == "challenger"
    assert row["challenger_reasoning_tokens"] == 300
    assert row["challenger_reasoning_effort"] == "high"
    assert row["time_to_vote_ms"] == 4200
    forbidden = {"user", "conversation", "trace", "payload", "content", "prompt_text"}
    assert not [column for column in COLUMNS if any(word in column for word in forbidden)]


def test_seed_errored_and_pending_rows_are_excluded(tmp_path, voted):
    """Only what a human closed: no seed, no errored, no pending."""
    ArenaComparisonFactory(status=ArenaComparisonStatus.VOTED, winner="champion", is_seed=True)
    ArenaComparisonFactory(status=ArenaComparisonStatus.ERRORED)
    ArenaComparisonFactory(status=ArenaComparisonStatus.PENDING)
    abandoned = ArenaComparisonFactory(status=ArenaComparisonStatus.ABANDONED)

    frame = _export(tmp_path)

    assert sorted(frame["comparison_id"]) == sorted([hash_id(voted.pk), hash_id(abandoned.pk)])
    assert set(frame["outcome"]) == {"challenger", "abandoned"}


def test_since_filters_on_the_draw_day(tmp_path, voted):
    old = ArenaComparisonFactory(status=ArenaComparisonStatus.ABANDONED)
    models.ArenaComparison.objects.filter(pk=old.pk).update(
        drawn_at=timezone.now() - timedelta(days=40)
    )

    since = (timezone.now() - timedelta(days=2)).date().isoformat()
    frame = _export(tmp_path, since=since)

    assert list(frame["comparison_id"]) == [hash_id(voted.pk)]


def test_control_challengers_are_flagged(tmp_path, voted):
    # bulk_create skips the model's full_clean, which would ask for a configured model.
    models.ArenaChallenger.objects.bulk_create(
        [
            models.ArenaChallenger(
                experiment=voted.experiment,
                model_hrid=voted.challenger_model_hrid,
                is_control=True,
            )
        ]
    )

    frame = _export(tmp_path)

    assert bool(frame.iloc[0]["is_control"])


def test_parquet_export_has_the_same_columns(tmp_path, voted):
    pytest.importorskip("pyarrow", reason="Parquet needs the pyarrow engine.")
    frame = _export(tmp_path, name="arena.parquet", format="parquet")

    assert list(frame.columns) == COLUMNS
    assert len(frame) == 1


def test_an_empty_export_still_has_every_column(tmp_path):
    frame = _export(tmp_path)

    assert list(frame.columns) == COLUMNS
    assert frame.empty
