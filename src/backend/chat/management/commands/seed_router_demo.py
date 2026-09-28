"""Seed the Django side of the router demo: one arena experiment per tier.

Demo tooling only. Every comparison it creates is flagged ``is_seed``, so the
results page shows its banner and its "exclude seed data" toggle.

The three experiments run at once, which the model allows: the uniqueness rule is
one active experiment *per tier* (``ArenaExperiment.clean``), not one overall.

Win rates come from ``chat.demo_seed.CHALLENGER_WIN_RATES``, which records the
published standing each one reconstructs. Run ``--explain`` to print that table
with its sources before quoting any number from the results page.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from chat.demo_seed import (
    ARENA_VOTE_RATE,
    BENCHMARK_ANCHORS,
    CHALLENGER_WIN_RATES,
    DAILY_CAP_PER_USER,
    MONTHLY_TURNS,
    SAMPLING_RATE,
    TIER_MODEL,
    comparisons_per_tier,
)
from chat.enums import RoutingTier
from chat.models import ArenaChallenger, ArenaComparison, ArenaExperiment, RoutingTierSettings

# Comparisons follow the traffic: 100,000 turns a month, 10 percent sampled, split
# by the tier mix. At a 50 percent vote rate that is the 5,000 human choices a month
# the tool actually collects, and every challenger clears the 100-vote threshold by
# a wide margin.
ERROR_RATE = 0.015


class Command(BaseCommand):
    """Create one seeded arena experiment per tier, with benchmark-anchored win rates."""

    help = "Seed one arena experiment per tier (flagged is_seed) for the router demo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=None,
            help=(
                "Comparisons per tier. The default follows the traffic mix from "
                f"{MONTHLY_TURNS:,} turns a month at {SAMPLING_RATE:.0%} sampling: "
                + ", ".join(
                    f"{tier.value} {count:,}" for tier, count in comparisons_per_tier().items()
                )
                + "."
            ),
        )
        parser.add_argument("--days", type=int, default=30, help="Spread draws over N days.")
        parser.add_argument("--seed", type=int, default=20260917, help="Random seed.")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete the seeded comparisons of these experiments before reseeding.",
        )
        parser.add_argument(
            "--deactivate-others",
            action="store_true",
            help=(
                "Deactivate any other active experiment on these tiers. Without it the "
                "command stops rather than taking a running experiment off the air."
            ),
        )
        parser.add_argument(
            "--explain",
            action="store_true",
            help="Print the benchmark anchors behind each win rate and exit.",
        )

    def handle(self, *args, **options):
        if options["explain"]:
            self._explain()
            return

        tier_settings = RoutingTierSettings.get_solo()
        for tier in RoutingTier:
            if not tier_settings.model_for(tier):
                raise CommandError(
                    f"The {tier.value} tier has no model. Run `seed_routing_tiers` first."
                )

        self._clear_the_tiers(options["deactivate_others"])

        for tier in RoutingTier:
            experiment = self._experiment_for(tier, tier_settings)
            challengers = self._challengers_for(tier, experiment, tier_settings)
            if not challengers:
                self.stdout.write(
                    self.style.WARNING(
                        f"{tier.value}: no challenger configured for this tier, skipped."
                    )
                )
                continue
            if options["reset"]:
                deleted, _ = ArenaComparison.objects.filter(
                    experiment=experiment, is_seed=True
                ).delete()
                self.stdout.write(f"{tier.value}: removed {deleted} seeded rows.")

            win_rate_args = []
            for hrid in challengers:
                rate = CHALLENGER_WIN_RATES[tier].get(hrid)
                if rate is not None:
                    win_rate_args += ["--win-rate", f"{hrid}={rate}"]

            count = options["count"] or comparisons_per_tier()[tier]
            call_command(
                "seed_arena_comparisons",
                "--experiment",
                str(experiment.pk),
                "--count",
                str(count),
                "--vote-rate",
                str(ARENA_VOTE_RATE),
                "--error-rate",
                str(ERROR_RATE),
                "--days",
                str(options["days"]),
                "--seed",
                str(options["seed"] + hash(tier.value) % 1000),
                *win_rate_args,
                stdout=self.stdout,
            )

        votes = ArenaComparison.objects.filter(
            experiment__name__startswith="Demo ", is_seed=True, status="voted"
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded the three tier experiments: {votes:,} human choices, against the "
                f"{round(MONTHLY_TURNS * SAMPLING_RATE * ARENA_VOTE_RATE):,} a month the "
                "tool collects. Run with --explain for the benchmark anchors."
            )
        )

    def _clear_the_tiers(self, deactivate: bool):
        """Make room for the demo experiments, never silently.

        One experiment can be active per tier, so the demo cannot start while another
        one holds a tier. Those experiments may carry real votes, so the command
        refuses to touch them unless it is told to, and says what it would affect.
        """
        names = [f"Demo {tier.value}" for tier in RoutingTier]
        conflicting = ArenaExperiment.objects.filter(is_active=True).exclude(name__in=names)
        if not conflicting.exists():
            return

        described = []
        for experiment in conflicting:
            real = ArenaComparison.objects.filter(experiment=experiment, is_seed=False).count()
            described.append(f"{experiment.name!r} ({experiment.tier}, {real} real comparisons)")

        if not deactivate:
            raise CommandError(
                "These experiments are active and would block the demo: "
                + "; ".join(described)
                + ". Their comparisons are kept either way, but taking an experiment off "
                "the air stops it drawing new ones. Re-run with --deactivate-others to "
                "deactivate them, or deactivate the ones you want in the admin."
            )

        count = conflicting.update(is_active=False)
        self.stdout.write(
            self.style.WARNING(f"Deactivated {count} experiment(s): " + "; ".join(described))
        )

    def _experiment_for(self, tier: RoutingTier, tier_settings) -> ArenaExperiment:
        """Get or create the demo experiment of one tier, at the demo's settings."""
        experiment, created = ArenaExperiment.objects.get_or_create(
            name=f"Demo {tier.value}",
            defaults={
                "tier": tier.value,
                "description": (
                    f"Seeded demo experiment on the {tier.value} tier. Win rates "
                    "reconstruct published standings; see chat/demo_seed.py."
                ),
            },
        )
        experiment.tier = tier.value
        experiment.sampling_rate = SAMPLING_RATE
        experiment.daily_cap_per_user = DAILY_CAP_PER_USER
        experiment.is_active = True
        experiment.save()
        self.stdout.write(
            f"{tier.value}: {'created' if created else 'updated'} experiment "
            f"'{experiment.name}' (champion {tier_settings.model_for(tier)}, "
            f"sampling {SAMPLING_RATE:.0%}, cap {DAILY_CAP_PER_USER}/user/day)"
        )
        return experiment

    def _challengers_for(self, tier: RoutingTier, experiment, tier_settings) -> list[str]:
        """Attach every alternative of the tier that we have a seeded win rate for."""
        alternatives = tier_settings.alternatives_for(tier)
        wanted = [hrid for hrid in alternatives if hrid in CHALLENGER_WIN_RATES[tier]]
        for hrid in wanted:
            ArenaChallenger.objects.get_or_create(experiment=experiment, model_hrid=hrid)
        return wanted

    def _explain(self):
        """Print every seeded win rate next to the published standing it reconstructs."""
        self.stdout.write("Seeded win rates and the published standing behind each one.\n")
        for tier in RoutingTier:
            champion = TIER_MODEL[tier]
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{tier.value} - champion {champion}"))
            for hrid, rate in CHALLENGER_WIN_RATES[tier].items():
                anchor = BENCHMARK_ANCHORS.get(hrid, {})
                self.stdout.write(f"  {hrid}: challenger wins {rate:.0%} of decisive votes")
                self.stdout.write(f"    anchor:    {anchor.get('anchor', '(none)')}")
                self.stdout.write(f"    published: {anchor.get('published', '(none)')}")
                if anchor.get("source"):
                    self.stdout.write(f"    source:    {anchor['source']}")
                self.stdout.write(f"    note:      {anchor.get('note', '')}")
        self.stdout.write(
            "\nThese rates are a reconstruction of public standings, not a measurement "
            "of our own. Every row they produce is flagged is_seed."
        )
