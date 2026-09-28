"""Fill the RoutingTierSettings singleton with the spec's tier assignment (section 3.1).

Models missing from the LLM configuration are skipped, so the command is safe on
any environment: it only writes what the configuration can actually run.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from chat.enums import RoutingTier
from chat.models import RoutingTierSettings

TIER_ASSIGNMENT = {
    RoutingTier.SIMPLE: ("ministral-3-8b", ["mistral-small-3-2"]),
    RoutingTier.STANDARD: ("mistral-small-3-2", ["gemma-4-31b", "mistral-medium-3-5"]),
    RoutingTier.COMPLEX: ("gpt-oss-120b", ["mistral-medium-3-5", "deepseek-v4-flash"]),
}


def _is_chat_model(hrid: str) -> bool:
    configuration = settings.LLM_CONFIGURATIONS.get(hrid)
    return configuration is not None and configuration.role == "chat"


class Command(BaseCommand):
    """Seed the routing tier settings from the spec, skipping unconfigured models."""

    help = "Seed RoutingTierSettings with the spec's tier models and alternatives."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite tiers already set in the admin (default: only fill blanks).",
        )

    def handle(self, *args, **options):
        tier_settings = RoutingTierSettings.get_solo()
        for tier, (model_hrid, alternatives) in TIER_ASSIGNMENT.items():
            model_field = f"{tier.value}_model_hrid"
            alternatives_field = f"{tier.value}_alternatives"

            if getattr(tier_settings, model_field) and not options["force"]:
                self.stdout.write(f"{tier.value}: already set, skipped (use --force)")
                continue

            if _is_chat_model(model_hrid):
                setattr(tier_settings, model_field, model_hrid)
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"{tier.value}: model {model_hrid} not configured, kept blank"
                    )
                )
            kept = [hrid for hrid in alternatives if _is_chat_model(hrid)]
            skipped = sorted(set(alternatives) - set(kept))
            setattr(tier_settings, alternatives_field, kept)
            self.stdout.write(
                f"{tier.value}: model={getattr(tier_settings, model_field) or '(setting)'}"
                f" alternatives={kept}" + (f" skipped={skipped}" if skipped else "")
            )

        tier_settings.full_clean()
        tier_settings.save()
        self.stdout.write(self.style.SUCCESS("Routing tier settings saved."))
