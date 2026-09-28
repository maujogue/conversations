"""LLM configuration view: the tier selector entries, plus the raw model list for staff."""

from django.conf import settings

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.feature_flags.helpers import is_feature_enabled

from chat import models, serializers
from chat.enums import RoutingTier
from chat.serializers import TIER_AUTO

TIER_LEAVES = {RoutingTier.SIMPLE: 1, RoutingTier.STANDARD: 2, RoutingTier.COMPLEX: 3}

# EcoLogits starting values for typical answers (router spec section 10), used until
# `tier_energy` holds measured figures: tier 2 about 5 times and tier 3 about 10
# times tier 1.
DEFAULT_ENERGY_RATIOS = {
    RoutingTier.SIMPLE: 1.0,
    RoutingTier.STANDARD: 5.0,
    RoutingTier.COMPLEX: 10.0,
}


def _wh_per_answer(tier_energy: dict, tier: RoutingTier) -> float | None:
    entry = tier_energy.get(tier.value) if isinstance(tier_energy, dict) else None
    value = entry.get("wh_per_answer") if isinstance(entry, dict) else None
    try:
        value = float(value)
    except TypeError, ValueError:
        return None
    return value if value > 0 else None


def energy_ratio(tier_energy: dict, tier: RoutingTier) -> float:
    """Energy per answer of ``tier`` relative to the simple tier, from ``tier_energy``.

    Falls back to the EcoLogits starting values when either figure is missing.
    """
    simple = _wh_per_answer(tier_energy, RoutingTier.SIMPLE)
    value = _wh_per_answer(tier_energy, tier)
    if simple is None or value is None:
        return DEFAULT_ENERGY_RATIOS[tier]
    return round(value / simple, 1)


def tier_entries(tier_settings: models.RoutingTierSettings) -> list[dict]:
    """Selector entries: Auto always, then every tier whose model is configured and active."""
    entries = [{"slug": TIER_AUTO, "label_key": f"router.tier.{TIER_AUTO}", "recommended": True}]
    for tier in RoutingTier:
        configuration = settings.LLM_CONFIGURATIONS.get(tier_settings.model_for(tier))
        if configuration is None or not configuration.is_active:
            continue
        entries.append(
            {
                "slug": tier.value,
                "label_key": f"router.tier.{tier.value}",
                "leaves": TIER_LEAVES[tier],
                "energy_ratio": energy_ratio(tier_settings.tier_energy or {}, tier),
            }
        )
    return entries


class LLMConfigurationView(APIView):
    """View listing what the compose-box selector offers."""

    permission_classes = [
        permissions.IsAuthenticated,
    ]

    @extend_schema(responses=serializers.LLMConfigurationSerializer)
    def get(self, request):
        """Return the tier selector entries (router spec section 6).

        No model hrid, name, icon or provider is in the payload for regular users.
        The raw ``models`` list (chat models only) is added for staff when the
        ``dev_model_picker`` feature flag is on, for dev and staging debugging.
        """
        tier_settings = models.RoutingTierSettings.get_solo()
        payload = {"mode": "tiers", "tiers": tier_entries(tier_settings)}
        if request.user.is_staff and is_feature_enabled(request.user, "dev_model_picker"):
            payload["models"] = [
                model for model in settings.LLM_CONFIGURATIONS.values() if model.role == "chat"
            ]
        serializer = serializers.LLMConfigurationSerializer(payload)
        return Response(serializer.data, status=status.HTTP_200_OK)
