"""Unit tests for the vote acknowledgement block (llm-router spec, section 11.1)."""

# pylint: disable=redefined-outer-name, unused-argument

import pytest

from core.factories import UserFactory

from chat import arena
from chat.enums import ArenaComparisonStatus, ArenaVoteOutcome
from chat.factories import ArenaComparisonFactory, ArenaExperimentFactory

pytestmark = pytest.mark.django_db


def _voted(**kwargs):
    return ArenaComparisonFactory(status=ArenaComparisonStatus.VOTED, **kwargs)


def test_acknowledgement_counts_user_and_experiment_votes():
    """``user_votes`` spans every experiment of the user; ``experiment_votes`` every user."""
    user = UserFactory()
    experiment = ArenaExperimentFactory()
    other_experiment = ArenaExperimentFactory()
    comparison = _voted(user=user, experiment=experiment)
    _voted(user=user, experiment=experiment)
    _voted(user=user, experiment=other_experiment)
    _voted(user=UserFactory(), experiment=experiment)
    # Not votes: pending, abandoned, errored, and a redacted (user-less) vote.
    ArenaComparisonFactory(user=user, experiment=experiment)
    ArenaComparisonFactory(user=user, experiment=experiment, status=ArenaComparisonStatus.ABANDONED)
    ArenaComparisonFactory(user=user, experiment=experiment, status=ArenaComparisonStatus.ERRORED)
    _voted(user=None, experiment=other_experiment)

    block = arena.build_acknowledgement(comparison, user)

    assert block == {
        "user_votes": 3,
        "experiment_votes": 3,
        "tier_label": None,
        "task_label": None,
        "domain_label": None,
        "milestone": None,
    }


@pytest.mark.parametrize(
    ("votes", "milestone"),
    [(1, "first_vote"), (2, None), (10, "tenth_vote"), (11, None), (100, "hundredth_vote")],
)
def test_acknowledgement_milestones(votes, milestone):
    """Milestones fire on the first, tenth and hundredth vote only."""
    user = UserFactory()
    experiment = ArenaExperimentFactory()
    comparison = _voted(user=user, experiment=experiment)
    for _ in range(votes - 1):
        _voted(user=user, experiment=experiment)

    block = arena.build_acknowledgement(comparison, user)

    assert block["user_votes"] == votes
    assert block["milestone"] == milestone


@pytest.mark.parametrize("outcome", [ArenaVoteOutcome.TIE, ArenaVoteOutcome.BOTH_BAD])
def test_acknowledgement_for_a_draw(outcome):
    """Draws are votes: they get the block."""
    comparison = _voted(winner=outcome.value)

    block = arena.build_acknowledgement(comparison, comparison.user)

    assert block is not None
    assert block["user_votes"] == 1
    assert block["milestone"] == "first_vote"


@pytest.mark.parametrize("status", [ArenaComparisonStatus.ABANDONED, ArenaComparisonStatus.ERRORED])
def test_no_acknowledgement_without_a_vote(status):
    """Abandonment (or a comparison closed as errored) returns no block."""
    comparison = ArenaComparisonFactory(status=status)

    assert arena.build_acknowledgement(comparison, comparison.user) is None


def test_acknowledgement_labels_none_when_routing_fields_missing():
    """A comparison drawn before the router (no ``tier``/``task``/``domain``) has no labels."""
    comparison = _voted()
    assert not comparison.tier

    block = arena.build_acknowledgement(comparison, comparison.user)

    assert block["tier_label"] is None
    assert block["task_label"] is None
    assert block["domain_label"] is None


def test_acknowledgement_labels_built_from_routing_fields():
    """Present routing fields become i18n keys; empty ones stay null."""
    comparison = _voted(tier="standard", task="writing", domain="")

    block = arena.build_acknowledgement(comparison, comparison.user)

    assert block["tier_label"] == "router.tier.standard"
    assert block["task_label"] == "router.task.writing"
    assert block["domain_label"] is None
