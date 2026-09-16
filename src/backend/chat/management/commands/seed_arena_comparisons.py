"""Seed synthetic arena comparisons so the results page has something to show.

Demo tooling only. Every row is flagged ``is_seed`` and the results page shows a
banner and an "exclude seed data" toggle whenever such rows exist.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from chat.arena import candidate_prices
from chat.enums import ArenaComparisonStatus, ArenaContextTag, ArenaRole, ArenaSide
from chat.models import ArenaComparison, ArenaExperiment

TAG_WEIGHTS = {
    ArenaContextTag.PLAIN: 0.6,
    ArenaContextTag.WEB_SEARCH: 0.15,
    ArenaContextTag.ATTACHMENT: 0.15,
    ArenaContextTag.PROJECT: 0.10,
}


def _random_tags(rng: random.Random) -> list[str]:
    tag = rng.choices(list(TAG_WEIGHTS), weights=list(TAG_WEIGHTS.values()))[0]
    return [tag.value]


class Command(BaseCommand):
    """Create synthetic, clearly flagged arena comparisons for an experiment."""

    help = "Seed synthetic arena comparisons (flagged is_seed) for a demo of the results page."

    def add_arguments(self, parser):
        parser.add_argument("--experiment", required=True, help="Experiment id or exact name.")
        parser.add_argument("--count", type=int, default=300, help="Comparisons to create.")
        parser.add_argument(
            "--challenger-win-rate",
            type=float,
            default=0.55,
            help="Probability that a voted comparison goes to the challenger.",
        )
        parser.add_argument(
            "--vote-rate",
            type=float,
            default=0.35,
            help="Share of comparisons that receive a vote (the rest are abandoned).",
        )
        parser.add_argument("--error-rate", type=float, default=0.02)
        parser.add_argument("--days", type=int, default=30, help="Spread draws over N days.")
        parser.add_argument("--seed", type=int, default=None, help="Random seed.")

    def handle(self, *args, **options):
        experiment = (
            ArenaExperiment.objects.filter(pk=options["experiment"]).first()
            if _looks_like_uuid(options["experiment"])
            else ArenaExperiment.objects.filter(name=options["experiment"]).first()
        )
        if experiment is None:
            raise CommandError(f"No experiment matches {options['experiment']!r}.")
        challengers = list(experiment.challengers.values_list("model_hrid", flat=True))
        if not challengers:
            raise CommandError("The experiment has no challenger.")

        rng = random.Random(options["seed"])  # noqa: S311  # demo data, not security
        now = timezone.now()
        price_reference = ArenaComparison(experiment=experiment)
        champion_prices = candidate_prices(price_reference, ArenaRole.CHAMPION)
        challenger_prices = {}
        for hrid in challengers:
            price_reference.challenger_model_hrid = hrid
            challenger_prices[hrid] = candidate_prices(price_reference, ArenaRole.CHALLENGER)
        rows = []
        for _ in range(options["count"]):
            drawn_at = now - timedelta(seconds=rng.uniform(0, options["days"] * 86400))
            champion_side = rng.choice([ArenaSide.LEFT, ArenaSide.RIGHT])
            challenger = rng.choice(challengers)
            roll = rng.random()
            if roll < options["error_rate"]:
                status = ArenaComparisonStatus.ERRORED
            elif roll < options["error_rate"] + options["vote_rate"]:
                status = ArenaComparisonStatus.VOTED
            else:
                status = ArenaComparisonStatus.ABANDONED

            champion_completion = rng.randint(120, 900)
            challenger_completion = rng.randint(120, 900)
            comparison = ArenaComparison(
                experiment=experiment,
                is_seed=True,
                turn=rng.randint(1, 6),
                context_tags=_random_tags(rng),
                champion_model_hrid=experiment.champion_model_hrid,
                challenger_model_hrid=challenger,
                champion_side=champion_side,
                status=status,
                drawn_at=drawn_at,
                price_snapshot={
                    ArenaRole.CHAMPION: champion_prices,
                    ArenaRole.CHALLENGER: challenger_prices[challenger],
                },
                champion_prompt_tokens=rng.randint(400, 3000),
                champion_completion_tokens=champion_completion,
                champion_latency_ms=int(champion_completion * rng.uniform(18, 35)),
                champion_first_token_ms=rng.randint(300, 1500),
                champion_co2_impact=champion_completion * rng.uniform(1e-6, 3e-6),
                champion_finished_at=drawn_at + timedelta(seconds=rng.uniform(4, 25)),
                challenger_prompt_tokens=rng.randint(400, 3000),
                challenger_completion_tokens=challenger_completion,
                challenger_latency_ms=int(challenger_completion * rng.uniform(12, 30)),
                challenger_first_token_ms=rng.randint(200, 1200),
                challenger_co2_impact=challenger_completion * rng.uniform(0.6e-6, 2e-6),
                challenger_finished_at=drawn_at + timedelta(seconds=rng.uniform(4, 25)),
            )
            if status == ArenaComparisonStatus.VOTED:
                comparison.winner = (
                    ArenaRole.CHALLENGER
                    if rng.random() < options["challenger_win_rate"]
                    else ArenaRole.CHAMPION
                )
                comparison.time_to_vote_ms = rng.randint(3000, 60000)
                comparison.voted_at = comparison.challenger_finished_at + timedelta(
                    milliseconds=comparison.time_to_vote_ms
                )
            elif status == ArenaComparisonStatus.ERRORED:
                comparison.closed_reason = "candidate_failed"
                comparison.challenger_error = "model_connection_error"
                comparison.challenger_payload = None
            rows.append(comparison)

        ArenaComparison.objects.bulk_create(rows)
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(rows)} comparisons on '{experiment.name}' "
                f"({sum(1 for r in rows if r.status == ArenaComparisonStatus.VOTED)} voted)."
            )
        )


def _looks_like_uuid(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4
