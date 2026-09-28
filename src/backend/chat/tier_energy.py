"""Energy per answer and per tier, the figure the selector's "×8" sentence is built on.

Router spec section 10: ``RoutingTierSettings.tier_energy`` holds, per tier,
``{wh_per_answer, n, source, updated_at}``. It is refreshed by the
``refresh_tier_energy`` management command (weekly) and read by the tiers
endpoint (``chat/views/llm_config.energy_ratio``).

Where the number comes from, and why not from Langfuse
------------------------------------------------------

Langfuse knows tokens, latency and cost; it does not know watt-hours, and its
Metrics API cannot group by tier (tags group by the whole tag array), so a
Langfuse pull would be one query per tier returning *tokens*, which we would
then have to convert with the same EcoLogits model we already apply when we
store an answer. That conversion is exactly what ``chat.footprint`` does at
write time, with the model's real parameter counts and the real reasoning token
count of that answer — a strictly better input than a tier-wide token mean.

So the measured figure is computed from our own rows, which carry both the tier
and the resolved CO2 of each answer:

- ``ArenaComparison``: one answer per side, with ``tier`` and
  ``{role}_co2_impact`` (both sides are real answers of the tier's models);
- assistant messages of recent conversations, whose metadata carries the
  routing ``tier`` and the answer's ``co2_impact`` (the committed answers of
  every turn, arena or not — by far the larger sample).

CO2 is turned back into Wh with ``footprint.wh_from_co2_kg``, the same
electricity mix EcoLogits used to produce it.

Below ``MIN_ANSWERS_FOR_MEASURED`` answers for a tier, the sample is not worth
publishing and the EcoLogits starting values are kept (``source: "estimated"``),
computed from the configured parameter counts of the tier's model and the
typical answer lengths of the spec: 150, 400 and 500 + 2,000 reasoning tokens,
which give the familiar ratios of about 1, 8 and 10 times tier 1.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from chat import models
from chat.enums import ArenaRole, RoutingTier
from chat.footprint import estimate_co2_kg, wh_from_co2_kg

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30

# Under this many answers a tier keeps the EcoLogits starting values: a mean over
# a handful of answers says more about who chatted this month than about the tier.
MIN_ANSWERS_FOR_MEASURED = 500

SOURCE_MEASURED = "measured"
SOURCE_ESTIMATED = "estimated"

# Typical answer of each tier (router spec section 10), in output tokens. Tier 3
# also thinks: its reasoning tokens are billed to the same estimate.
TYPICAL_OUTPUT_TOKENS = {
    RoutingTier.SIMPLE: 150,
    RoutingTier.STANDARD: 400,
    RoutingTier.COMPLEX: 500,
}
TYPICAL_REASONING_TOKENS = {
    RoutingTier.SIMPLE: 0,
    RoutingTier.STANDARD: 0,
    RoutingTier.COMPLEX: 2000,
}

# Last-resort values, used only when the tier's model has no configured parameter
# counts and EcoLogits therefore cannot estimate anything. They keep the spec's
# ratios (1 / 8 / 10) so the selector sentence stays truthful about orders of
# magnitude even on a bare configuration.
FALLBACK_WH_SIMPLE = 0.5
FALLBACK_RATIOS = {
    RoutingTier.SIMPLE: 1.0,
    RoutingTier.STANDARD: 8.0,
    RoutingTier.COMPLEX: 10.0,
}


def estimated_wh_per_answer(tier: RoutingTier, tier_settings=None) -> float:
    """EcoLogits Wh of a typical answer of ``tier``, from the tier model's parameters."""
    tier_settings = tier_settings or models.RoutingTierSettings.get_solo()
    configuration = settings.LLM_CONFIGURATIONS.get(tier_settings.model_for(tier))
    if configuration is not None:
        tokens = TYPICAL_OUTPUT_TOKENS[tier] + TYPICAL_REASONING_TOKENS[tier]
        watt_hours = wh_from_co2_kg(estimate_co2_kg(configuration, tokens, None))
        if watt_hours:
            return watt_hours
    return FALLBACK_WH_SIMPLE * FALLBACK_RATIOS[tier]


def _add(totals: dict, tier: str, co2_kg) -> None:
    """Accumulate one answer's Wh under its tier, ignoring rows we cannot convert."""
    if tier not in totals or co2_kg is None:
        return
    watt_hours = wh_from_co2_kg(co2_kg)
    if watt_hours is None:
        return
    bucket = totals[tier]
    bucket["wh"] += watt_hours
    bucket["n"] += 1


def _collect_from_comparisons(totals: dict, since) -> None:
    """Both sides of every non-seed comparison drawn in the window."""
    rows = models.ArenaComparison.objects.filter(drawn_at__gte=since, is_seed=False).exclude(
        tier=""
    )
    fields = ["tier"] + [
        f"{role}_co2_impact" for role in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER)
    ]
    for row in rows.values(*fields).iterator():
        for role in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER):
            _add(totals, row["tier"], row[f"{role}_co2_impact"])


def _message_metadata(message):
    """Metadata of a stored UI message, whether it is a model object or raw JSON."""
    if isinstance(message, dict):
        return message.get("metadata") or {}, message.get("role")
    return getattr(message, "metadata", None) or {}, getattr(message, "role", None)


def _collect_from_messages(totals: dict, since) -> None:
    """Committed assistant answers of conversations touched in the window.

    Every routed turn writes ``tier`` and ``co2_impact`` in the assistant message
    metadata (``chat/clients/pydantic_ai.py``), so this is the whole traffic of the
    tier, not only its arena turns. Conversations are filtered on ``updated_at``:
    per-message timestamps are not stored, so the window is the conversation's.
    """
    conversations = models.ChatConversation.objects.filter(updated_at__gte=since).only("messages")
    for conversation in conversations.iterator(chunk_size=100):
        for message in conversation.messages or []:
            metadata, role = _message_metadata(message)
            if role != "assistant" or not metadata:
                continue
            _add(totals, metadata.get("tier"), metadata.get("co2_impact"))


def collect_tier_energy(days: int = WINDOW_DAYS, tier_settings=None) -> dict:
    """Return the ``tier_energy`` payload: one entry per tier, measured or estimated."""
    tier_settings = tier_settings or models.RoutingTierSettings.get_solo()
    since = timezone.now() - timedelta(days=days)
    totals = {tier.value: {"wh": 0.0, "n": 0} for tier in RoutingTier}

    _collect_from_comparisons(totals, since)
    _collect_from_messages(totals, since)

    now = timezone.now().isoformat()
    payload = {}
    for tier in RoutingTier:
        bucket = totals[tier.value]
        measured = bucket["n"] >= MIN_ANSWERS_FOR_MEASURED and bucket["wh"] > 0
        payload[tier.value] = {
            "wh_per_answer": round(
                bucket["wh"] / bucket["n"]
                if measured
                else estimated_wh_per_answer(tier, tier_settings),
                4,
            ),
            "n": bucket["n"],
            "source": SOURCE_MEASURED if measured else SOURCE_ESTIMATED,
            "updated_at": now,
            "window_days": days,
        }
    return payload


def refresh_tier_energy(days: int = WINDOW_DAYS) -> dict:
    """Recompute ``RoutingTierSettings.tier_energy`` and save it."""
    tier_settings = models.RoutingTierSettings.get_solo()
    payload = collect_tier_energy(days=days, tier_settings=tier_settings)
    tier_settings.tier_energy = payload
    tier_settings.save(update_fields=["tier_energy"])
    logger.info("tier_energy refreshed over %s days: %s", days, payload)
    return payload
