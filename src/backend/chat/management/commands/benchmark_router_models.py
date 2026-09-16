"""Compare candidate models for the router classifier.

The classifier runs before every answer, so its model is chosen on latency
first and accuracy second. This command replays the gold set
(``chat/router/gold_set.json``) through each candidate and reports latency,
label accuracy and an EcoLogits energy estimate.

    python manage.py benchmark_router_models
    python manage.py benchmark_router_models --models ministral-3-8b,gemma-4-31b
    python manage.py benchmark_router_models --repeat 2 --json /tmp/bench.json
"""

import asyncio
import json
import logging
import statistics
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings

from chat.footprint import estimate_co2_kg
from chat.router.classifier import build_classifier_agent, get_router_prompt

GOLD_SET_PATH = Path(__file__).resolve().parents[2] / "router" / "gold_set.json"

# A classification is worthless if it does not come back in time, so the
# benchmark uses a generous ceiling and reports the real latency instead.
BENCHMARK_TIMEOUT_S = 30.0

logger = logging.getLogger(__name__)


def load_gold_set() -> list[dict]:
    """Read the gold set from disk."""
    with GOLD_SET_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)["items"]


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile, which needs no interpolation guesswork."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return ordered[index]


async def run_one(agent, prompt: str) -> tuple[object | None, float, int]:
    """Return (labels or None, elapsed seconds, output tokens)."""
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(agent.run(prompt), timeout=BENCHMARK_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - a candidate that errors is a result
        return None, time.perf_counter() - started, 0
    elapsed = time.perf_counter() - started
    tokens = 0
    try:
        # `usage` is a property on this pydantic-ai version; older ones expose
        # it as a method, so accept both rather than pinning to one shape.
        usage = result.usage() if callable(result.usage) else result.usage
        tokens = int(getattr(usage, "output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001 - usage is best effort
        logger.debug("No usage reported for this call", exc_info=True)
    return result.output, elapsed, tokens


async def benchmark_model(hrid: str, gold: list[dict], repeat: int) -> dict:
    """Replay the gold set through one candidate model."""
    system_prompt, _ = get_router_prompt()
    with override_settings(LLM_ROUTER_MODEL_HRID=hrid):
        agent = build_classifier_agent(system_prompt)
        # One warm-up call: the first request of a process pays lazy imports
        # and connection setup, which is not what we are measuring.
        await run_one(agent, gold[0]["text"])

        latencies: list[float] = []
        output_tokens: list[int] = []
        valid = tier_hits = domain_hits = task_hits = off_by_two = 0
        confusion: dict[str, int] = {}
        total = 0

        for _ in range(repeat):
            for item in gold:
                labels, elapsed, tokens = await run_one(agent, item["text"])
                total += 1
                latencies.append(elapsed)
                output_tokens.append(tokens)
                if labels is None:
                    continue
                valid += 1
                got_tier = labels.complexity.value
                expected_tier = item["complexity"]
                if got_tier == expected_tier:
                    tier_hits += 1
                else:
                    key = f"{expected_tier}->{got_tier}"
                    confusion[key] = confusion.get(key, 0) + 1
                    order = ["simple", "standard", "complex"]
                    if abs(order.index(got_tier) - order.index(expected_tier)) == 2:
                        off_by_two += 1
                domain_hits += labels.domain.value == item["domain"]
                task_hits += labels.task.value == item["task"]

    configuration = settings.LLM_CONFIGURATIONS[hrid]
    mean_tokens = statistics.fmean(output_tokens) if output_tokens else 0.0
    wh = None
    co2 = estimate_co2_kg(configuration, int(mean_tokens), statistics.fmean(latencies))
    if co2 is not None:
        # EcoLogits gives kgCO2eq; report mgCO2eq per call, which is readable.
        wh = co2 * 1_000_000

    return {
        "model": hrid,
        "runs": total,
        "valid_pct": 100 * valid / total if total else 0,
        "tier_pct": 100 * tier_hits / total if total else 0,
        "domain_pct": 100 * domain_hits / total if total else 0,
        "task_pct": 100 * task_hits / total if total else 0,
        "off_by_two": off_by_two,
        "p50_ms": 1000 * percentile(latencies, 0.5),
        "p95_ms": 1000 * percentile(latencies, 0.95),
        "mean_ms": 1000 * statistics.fmean(latencies) if latencies else 0,
        "mean_output_tokens": mean_tokens,
        "tokens_per_s": mean_tokens / statistics.fmean(latencies) if latencies else 0,
        "mg_co2e_per_call": wh,
        "confusion": confusion,
    }


class Command(BaseCommand):
    """Benchmark candidate router models on the gold set."""

    help = "Compare candidate models for the router classifier (latency and accuracy)."

    def add_arguments(self, parser):
        """Declare the command options."""
        parser.add_argument(
            "--models",
            default="",
            help="Comma-separated HRIDs. Defaults to every active chat model.",
        )
        parser.add_argument("--repeat", type=int, default=1, help="Passes over the gold set.")
        parser.add_argument("--json", dest="json_path", default="", help="Write results as JSON.")

    def handle(self, *args, **options):
        """Run the benchmark and print a table."""
        gold = load_gold_set()
        if options["models"]:
            hrids = [h.strip() for h in options["models"].split(",") if h.strip()]
        else:
            hrids = [
                hrid
                for hrid, configuration in settings.LLM_CONFIGURATIONS.items()
                if configuration.is_active and getattr(configuration, "role", "chat") == "chat"
            ]
        unknown = [h for h in hrids if h not in settings.LLM_CONFIGURATIONS]
        if unknown:
            raise CommandError(f"Unknown model hrid(s): {', '.join(unknown)}")

        self.stdout.write(
            f"Gold set: {len(gold)} turns x {options['repeat']} pass(es) on {len(hrids)} model(s)\n"
        )
        results = []
        for hrid in hrids:
            self.stdout.write(f"  running {hrid} …")
            results.append(asyncio.run(benchmark_model(hrid, gold, options["repeat"])))

        results.sort(key=lambda row: (-row["tier_pct"], row["p50_ms"]))
        header = (
            f"\n{'model':22} {'valid%':>7} {'tier%':>7} {'domain%':>8} {'task%':>7} "
            f"{'p50 ms':>8} {'p95 ms':>8} {'tok/s':>7} {'mgCO2e':>8}  far-miss"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for row in results:
            co2 = f"{row['mg_co2e_per_call']:.3f}" if row["mg_co2e_per_call"] is not None else "-"
            self.stdout.write(
                f"{row['model']:22} {row['valid_pct']:7.0f} {row['tier_pct']:7.0f} "
                f"{row['domain_pct']:8.0f} {row['task_pct']:7.0f} {row['p50_ms']:8.0f} "
                f"{row['p95_ms']:8.0f} {row['tokens_per_s']:7.1f} {co2:>8}  {row['off_by_two']}"
            )
        self.stdout.write("\nTier confusion (expected -> got), errors only:")
        for row in results:
            if row["confusion"]:
                pairs = ", ".join(f"{k} x{v}" for k, v in sorted(row["confusion"].items()))
                self.stdout.write(f"  {row['model']:22} {pairs}")

        if options["json_path"]:
            Path(options["json_path"]).write_text(json.dumps(results, indent=2), encoding="utf-8")
            self.stdout.write(f"\nWrote {options['json_path']}")
