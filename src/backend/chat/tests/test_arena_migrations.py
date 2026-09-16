"""Upgrade existing Arena records without retaining orphaned conversation content."""

import pytest


@pytest.mark.django_db(transaction=True)
def test_arena_reliability_migration_repairs_legacy_records(migrator):
    old = migrator.apply_initial_migration(("chat", "0017_arena_draw_votes"))
    users = old.apps.get_model("core", "User")
    experiments = old.apps.get_model("chat", "ArenaExperiment")
    comparisons = old.apps.get_model("chat", "ArenaComparison")
    conversations = old.apps.get_model("chat", "ChatConversation")
    user = users.objects.create()
    conversation = conversations.objects.create(owner=user)
    experiment = experiments.objects.create(name="Legacy", champion_model_hrid="champion")
    fields = {
        "experiment": experiment,
        "champion_model_hrid": "champion",
        "challenger_model_hrid": "challenger",
        "champion_side": "left",
    }
    orphan = comparisons.objects.create(
        **fields,
        user=user,
        champion_payload={"prompt": "private"},
        challenger_payload={"answer": "private"},
        champion_trace_id="private-trace",
    )
    failed = comparisons.objects.create(
        **fields,
        conversation=conversation,
        status="abandoned",
        challenger_error="provider_error",
    )
    pending = comparisons.objects.create(**fields, conversation=conversation)
    voted = comparisons.objects.create(
        **fields, conversation=conversation, status="voted", winner="champion"
    )
    new = migrator.apply_tested_migration(("chat", "0018_arena_reliability"))
    rows = new.apps.get_model("chat", "ArenaComparison").objects
    redacted = rows.get(pk=orphan.pk)
    assert redacted.champion_payload is None and redacted.challenger_payload is None
    assert redacted.user_id is None and redacted.champion_trace_id == ""
    assert rows.get(pk=pending.pk).closed_reason == "legacy_interrupted"
    assert rows.get(pk=failed.pk).status == "errored"
    assert rows.get(pk=voted.pk).status == "voted"
    assert rows.get(pk=voted.pk).price_snapshot == {}
