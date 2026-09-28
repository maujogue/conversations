"""Shared constants for the demo seed: tier profiles, prices and benchmark anchors.

Demo tooling only. Nothing here is read by the application at runtime; it exists
so ``seed_router_demo`` (Django rows) and ``seed_langfuse_demo`` (Langfuse traces)
tell the *same* story with the same numbers.

Where the numbers come from
---------------------------

Prices are **not** invented: they are read from the LLM configuration
(``input_price_eur_per_mtok`` / ``output_price_eur_per_mtok``), the same values
the arena freezes on every comparison. The cost figures the dashboards show are
therefore ordinary arithmetic on our own configured prices, not an estimate.

Win rates are anchored to published third-party standings, recorded in
``BENCHMARK_ANCHORS`` with their source. Several models in the configuration are
versions with no published evaluation yet; each is anchored to its nearest
published ancestor and that substitution is written down rather than hidden. A
seeded win rate is a *plausible reconstruction of a public standing*, never a
measurement of our own: every Django row it produces carries ``is_seed=True`` and
every Langfuse trace lands in a separate environment (see ``PRODUCTION_ENVIRONMENT``).
The environments are named for the traffic they *model*, not for the fact that they
are seeded - the dashboards read as production because that is the month they
describe. What makes them seeded is written here and on every Django row.
"""

from chat.enums import RoutingDomain, RoutingTask, RoutingTier

# Langfuse environments. The modelled month never shares an environment with this
# deployment's own traffic (`development`), and the counterfactual never shares one
# with the routed answers: the savings widget is a group-by on exactly this dimension.
PRODUCTION_ENVIRONMENT = "production"
BASELINE_ENVIRONMENT = "production-baseline"
# The arena's challenger answers are extra inference the router did not order: they
# are what the experiment costs, not what serving costs. Keeping them in their own
# environment stops them inflating the routed total the savings are measured against,
# while leaving them visible - the arena is not free and the dashboard should say so.
ARENA_ENVIRONMENT = "production-arena"

# The single model an unrouted deployment would put on every turn, and against
# which the savings are measured. The premium Mistral: the honest strawman, since
# picking one model that is safe for the hardest turn is what you do without a router.
BASELINE_MODEL_HRID = "mistral-medium-3-5"

# Tier -> the model the router sends that tier to, mirroring seed_routing_tiers.
TIER_MODEL = {
    RoutingTier.SIMPLE: "ministral-3-8b",
    RoutingTier.STANDARD: "mistral-small-3-2",
    RoutingTier.COMPLEX: "gpt-oss-120b",
}

# The classifier runs on every turn, so its overhead has to be seeded honestly:
# these are the real sizes, not a flattering guess. DEFAULT_ROUTER_PROMPT measures
# 5,184 characters (about 1,296 tokens) and the classifier appends the user message
# truncated at MESSAGE_MAX_CHARS = 2,000 characters; a typical message adds ~100
# tokens. Nothing in chat/router/ enables provider-side prompt caching, so that
# system prompt is re-sent on every turn.
#
# This is the one place the seed contradicts the spec: section 8 targets "router
# overhead under 2 percent of total tokens", and 1,400 tokens against a mean turn
# of roughly 1,700 is nowhere near it. The dashboards show the real figure.
ROUTER_MODEL_HRID = "ministral-3-8b"
ROUTER_PROMPT_TOKENS = 1400
ROUTER_COMPLETION_TOKENS = 18

# Share of turns that skip the classifier entirely. This is real behaviour, not a
# discount invented for the demo: chat/router/classifier.py reuses the previous
# turn's labels when a follow-up is under SHORTCUT_MAX_WORDS = 6 words, and tags
# the turn `routed:shortcut`. The 6-word bar is narrow ("merci", "plus court",
# "et ensuite ?"), so this is an assumption, not a measurement - stated here so it
# can be replaced with the real figure once production traffic exists.
SHORTCUT_SHARE = 0.18

# Share of turns the router sends to each tier. Anchored on the spec's own
# benchmark set (docs/llm-router-spec.md section 6), where most public-service
# questions classify simple and the complex tier is the exception.
TIER_MIX = {
    RoutingTier.SIMPLE: 0.55,
    RoutingTier.STANDARD: 0.35,
    RoutingTier.COMPLEX: 0.10,
}

# Typical answer shape per tier, from the spec's section 10 figures (the same
# 150 / 400 / 500 + 2,000 reasoning tokens the tier energy estimate is built on).
TIER_TOKENS = {
    RoutingTier.SIMPLE: {"prompt": 800, "completion": 150, "reasoning": 0},
    RoutingTier.STANDARD: {"prompt": 1500, "completion": 400, "reasoning": 0},
    RoutingTier.COMPLEX: {"prompt": 2500, "completion": 500, "reasoning": 2000},
}

# Plausible end-to-end latency per tier, in milliseconds, used for the p50/p95 tiles.
TIER_LATENCY_MS = {
    RoutingTier.SIMPLE: (900, 1800),
    RoutingTier.STANDARD: (1800, 3600),
    RoutingTier.COMPLEX: (6000, 14000),
}

# Domain and task mix, so the tag tables and the Langfuse filters have something
# to slice. Weighted towards what a public-service assistant actually gets asked.
DOMAIN_WEIGHTS = {
    RoutingDomain.ADMINISTRATIVE: 0.30,
    RoutingDomain.GENERAL: 0.20,
    RoutingDomain.LEGAL: 0.12,
    RoutingDomain.HR: 0.10,
    RoutingDomain.FINANCE: 0.08,
    RoutingDomain.IT_SOFTWARE: 0.08,
    RoutingDomain.HEALTH: 0.06,
    RoutingDomain.SCIENCE_EDUCATION: 0.06,
}

TASK_WEIGHTS = {
    RoutingTask.QA_KNOWLEDGE: 0.28,
    RoutingTask.WRITING: 0.20,
    RoutingTask.SUMMARIZATION: 0.14,
    RoutingTask.DOCUMENT_QA: 0.12,
    RoutingTask.REASONING: 0.10,
    RoutingTask.TRANSLATION: 0.06,
    RoutingTask.CODING: 0.05,
    RoutingTask.CLASSIFICATION_EXTRACTION: 0.05,
}


# --------------------------------------------------------------------------- #
# Benchmark anchors
# --------------------------------------------------------------------------- #

# Each configured model, the published model its seeded standing is taken from,
# and the source. `published` is verbatim from the source; `note` says what the
# substitution costs us. Read this table before quoting any seeded win rate.
BENCHMARK_ANCHORS = {
    "gpt-oss-120b": {
        "anchor": "gpt-oss-120b (high)",
        "published": "Artificial Analysis Intelligence Index 12; $0.15 in / $0.595 out; 174 tok/s",
        "source": "https://artificialanalysis.ai/models/comparisons/gpt-oss-120b-vs-mistral-medium-3",
        "note": "Same model as configured; no substitution.",
    },
    "mistral-medium-3-5": {
        "anchor": "Mistral Medium 3",
        "published": (
            "Artificial Analysis Intelligence Index 9 (estimated); $0.40 in / $2.00 out; 142 tok/s"
        ),
        "source": "https://artificialanalysis.ai/models/comparisons/gpt-oss-120b-vs-mistral-medium-3",
        "note": "Configured as 3.5; anchored to Medium 3, the latest with a published index.",
    },
    "mistral-small-3-2": {
        "anchor": "Mistral Small 3.2 24B Instruct",
        "published": (
            "LLM-Stats score 8.6 vs Gemma 3 27B's 8.4; wins 6 of 7 shared benchmarks "
            "(AI2D, ChartQA, DocVQA, GPQA, MMLU-Pro, SimpleQA)"
        ),
        "source": (
            "https://llm-stats.com/models/compare/"
            "gemma-3-27b-it-vs-mistral-small-3.2-24b-instruct-2506"
        ),
        "note": "Same model as configured; no substitution.",
    },
    "gemma-4-31b": {
        "anchor": "Gemma 3 27B IT",
        "published": "LLM-Stats score 8.4; loses 6 of 7 shared benchmarks to Mistral Small 3.2",
        "source": (
            "https://llm-stats.com/models/compare/"
            "gemma-3-27b-it-vs-mistral-small-3.2-24b-instruct-2506"
        ),
        "note": "Configured as Gemma 4 31B; anchored to Gemma 3 27B, its published ancestor.",
    },
    "ministral-3-8b": {
        "anchor": "Ministral 3 (3B Base 2512)",
        "published": "MMLU 70.7",
        "source": (
            "https://llm-stats.com/models/compare/ministral-3-3b-base-2512-vs-qwen3-vl-8b-thinking"
        ),
        "note": (
            "Configured as the 8B; the published Ministral 3 figure is for the 3B base. "
            "The 8B is assumed at least as strong, so this anchor is conservative."
        ),
    },
    "deepseek-v4-flash": {
        "anchor": "DeepSeek V3-class flash tier",
        "published": "No like-for-like published index for this version",
        "source": "",
        "note": (
            "No published evaluation found. Seeded as roughly at parity with the "
            "complex-tier champion, which is the weakest claim we can make about it."
        ),
    },
}

# P(the challenger's answer is preferred) per tier and challenger, derived from the
# anchors above. These reproduce real standings; they are not tuned to flatter any
# vendor, and two of them deliberately go against the demo's convenience.
#
# simple    ministral-3-8b (champion) vs mistral-small-3-2
#           A much larger model wins more often overall, but on turns the router
#           classified simple the gap largely collapses - which is the whole claim
#           of "adequate intelligence": 0.56, not 0.90.
# standard  mistral-small-3-2 (champion) vs gemma-4-31b and mistral-medium-3-5
#           Mistral Small genuinely beats Gemma 3 27B on 6 of 7 benchmarks, so the
#           Gemma challenger loses. Mistral Medium wins slightly - for about 20x the
#           euro cost per answer, which is the point of the standard-tier row.
# complex   gpt-oss-120b (champion) vs mistral-medium-3-5 and deepseek-v4-flash
#           Artificial Analysis puts gpt-oss-120b at 12 against Medium's 9, cheaper
#           and faster; the premium Mistral loses its own tier.
CHALLENGER_WIN_RATES = {
    RoutingTier.SIMPLE: {"mistral-small-3-2": 0.56},
    RoutingTier.STANDARD: {"gemma-4-31b": 0.44, "mistral-medium-3-5": 0.53},
    RoutingTier.COMPLEX: {"mistral-medium-3-5": 0.40, "deepseek-v4-flash": 0.48},
}

# The volume the tool actually serves, and what the arena gets out of it.
#
#   100,000 users/month, about 1,000,000 turns
#   x SAMPLING_RATE   0.10 = 100,000 comparisons drawn
#   x ARENA_VOTE_RATE 0.05 =   5,000 human choices
#
# Neither rate is free. The sampling rate is what the experiments are configured at;
# the vote rate is then pinned by the 5,000 choices a month the tool actually
# collects. It is low because most people never vote - which is exactly why the
# arena needs a 10 percent sample to produce a usable number of votes.
MONTHLY_USERS = 100_000
MONTHLY_TURNS = 1_000_000
SAMPLING_RATE = 0.10
ARENA_VOTE_RATE = 0.05
# A daily cap high enough that a tester can hit the arena repeatedly.
DAILY_CAP_PER_USER = 100


def comparisons_per_tier() -> dict:
    """How many comparisons each tier's experiment gets, following the traffic mix."""
    drawn = MONTHLY_TURNS * SAMPLING_RATE
    return {tier: round(drawn * share) for tier, share in TIER_MIX.items()}


def answer_cost_eur(configuration, prompt_tokens: int, completion_tokens: int) -> float:
    """Cost of one answer in euros, from the configured per-million-token prices."""
    return (
        prompt_tokens * float(configuration.input_price_eur_per_mtok or 0)
        + completion_tokens * float(configuration.output_price_eur_per_mtok or 0)
    ) / 1_000_000


# Langfuse can group observations by `name` but not by a single tag (grouping by
# tags groups by the whole array), so the tier has to *be* an observation name for
# a tier pie to exist. The answer generation is therefore named after its tier.
def answer_span_name(tier) -> str:
    """Observation name of the answer generation: the tier, so pies can group on it."""
    return f"answer-{tier.value}"


ANSWER_SPAN_NAMES = tuple(f"answer-{tier.value}" for tier in RoutingTier)


def tier_of(hrid: str) -> RoutingTier | None:
    """The tier a model is the router's pick for, or ``None`` when it is not one."""
    return next((tier for tier, model in TIER_MODEL.items() if model == hrid), None)
