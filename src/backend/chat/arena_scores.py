"""Arena outcomes pushed to Langfuse as categorical scores (router spec section 12).

Every closed comparison writes one ``arena_preference`` score on *both* sides'
traces, so a Langfuse reader can slice preferences by the tags the trace already
carries (tier, domain, task, model) without Django having to duplicate them.

The score is idempotent by ``score_id = f"{comparison_id}-{role}"``: replaying a
close (a retry, a re-run of a backfill) updates the same score instead of
creating a second one. Langfuse is observability, never a system of record: the
vote itself lives on ``ArenaComparison``, and any failure here is logged and
swallowed so a vote is never lost because the observability backend is down.
"""

import logging

from django.conf import settings

import langfuse

from chat.enums import ArenaComparisonStatus, ArenaRole, ArenaVoteOutcome

logger = logging.getLogger(__name__)

SCORE_NAME = "arena_preference"

# The five values of the categorical score, from the point of view of one side.
WON = "won"
LOST = "lost"
TIE = "tie"
BOTH_BAD = "both_bad"
ABANDONED = "abandoned"

SCORE_VALUES = (WON, LOST, TIE, BOTH_BAD, ABANDONED)

ROLES = (ArenaRole.CHAMPION, ArenaRole.CHALLENGER)


def score_value_for(comparison, role: str) -> str | None:
    """The score one side of a closed comparison earned, ``None`` while it is pending.

    A decisive vote gives ``won`` to the picked role and ``lost`` to the other; a
    draw gives both sides ``tie`` or ``both_bad``; a comparison closed without a
    vote (the user walked away, or a side failed) gives both sides ``abandoned``.
    """
    if comparison.status == ArenaComparisonStatus.PENDING:
        return None
    if comparison.status == ArenaComparisonStatus.VOTED:
        if comparison.winner == ArenaVoteOutcome.TIE:
            return TIE
        if comparison.winner == ArenaVoteOutcome.BOTH_BAD:
            return BOTH_BAD
        if comparison.winner in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER):
            return WON if comparison.winner == role else LOST
    return ABANDONED


def _comment(comparison, role: str) -> str:
    """What the score carries for a human reading it in Langfuse: the id and the origin."""
    parts = [f"comparison={comparison.pk}", f"origin={comparison.origin}", f"role={role}"]
    if comparison.closed_reason:
        parts.append(f"closed_reason={comparison.closed_reason}")
    return " ".join(parts)


def push_comparison_scores(comparison) -> None:
    """Write the ``arena_preference`` score of a closed comparison on both traces.

    Silent no-op when Langfuse is off, when the comparison is still pending, or for
    a side that has no trace id (a side that never started, or a seeded row).
    """
    if comparison is None or not settings.LANGFUSE_ENABLED:
        return
    for role in ROLES:
        value = score_value_for(comparison, role)
        trace_id = getattr(comparison, f"{role}_trace_id", "")
        if not value or not trace_id:
            continue
        try:
            langfuse.get_client().create_score(
                name=SCORE_NAME,
                value=value,
                trace_id=trace_id,
                score_id=f"{comparison.pk}-{role}",
                data_type="CATEGORICAL",
                comment=_comment(comparison, role),
            )
        except Exception:  # pylint: disable=broad-except  # noqa: BLE001
            # Observability must never break a vote.
            logger.warning(
                "Could not push the arena score of comparison %s (%s) to Langfuse.",
                comparison.pk,
                role,
                exc_info=True,
            )
