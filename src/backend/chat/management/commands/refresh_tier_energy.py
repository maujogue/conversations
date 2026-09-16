"""Refresh `RoutingTierSettings.tier_energy` from the last 30 days of answers."""

import json

from django.core.management.base import BaseCommand

from chat.tier_energy import WINDOW_DAYS, collect_tier_energy, refresh_tier_energy


class Command(BaseCommand):
    """Store the mean Wh per answer of each tier (router spec section 10).

    Run weekly (Celery beat entry ``refresh-tier-energy``). See
    ``chat/tier_energy.py`` for where the number comes from and why it is computed
    from our own CO2 figures rather than pulled from Langfuse.
    """

    help = "Recompute the mean Wh per answer of each routing tier and store it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=WINDOW_DAYS,
            help=f"Size of the observation window in days (default {WINDOW_DAYS}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the payload without writing it to the tier settings.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if options["dry_run"]:
            payload = collect_tier_energy(days=days)
        else:
            payload = refresh_tier_energy(days=days)
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        for tier, entry in sorted(payload.items()):
            self.stdout.write(
                f"{tier}: {entry['wh_per_answer']} Wh per answer "
                f"({entry['source']}, n={entry['n']})"
            )
