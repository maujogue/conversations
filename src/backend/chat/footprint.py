"""Carbon footprint of an LLM answer: provider figure or EcoLogits estimate.

Albert reports a CO2 figure computed from `usage.completion_tokens`, which
excludes reasoning tokens, so its value undercounts reasoning models. This
module resolves the figure to keep for an answer and tags its source:

- ``provider``: the provider's own figure (Albert's ``impacts.kgCO2eq``).
- ``estimated``: EcoLogits estimate, the provider sent nothing.
- ``estimated_reasoning``: EcoLogits estimate on ``completion + reasoning``
  tokens, used for reasoning models whatever the provider sent.

The estimate calls ``ecologits.impacts.llm.compute_llm_impacts`` with the
parameter counts configured on the model (``total_params_b`` /
``active_params_b`` in the LLM configuration), the same low-level call
``ecologits.tracers.utils.llm_impacts`` and Compar:IA use.
"""

import logging
import math
from typing import Any, Literal, Mapping

from ecologits.electricity_mix_repository import electricity_mixes
from ecologits.impacts.llm import compute_llm_impacts
from ecologits.utils.range_value import RangeValue

from chat.llm_configuration import LLModel

logger = logging.getLogger(__name__)

CO2Source = Literal["provider", "estimated", "estimated_reasoning"]

# Albert runs in France; fall back to the world mix if the zone is unknown to
# the installed EcoLogits data.
ELECTRICITY_MIX_ZONE = "FRA"
ELECTRICITY_MIX_FALLBACK_ZONE = "WOR"

# Data center efficiency factors. EcoLogits has no entry for Albert, so we use
# its generic OpenAI-compatible defaults (PUE 1.20, WUE 0.569 L/kWh).
DATACENTER_PUE = 1.20
DATACENTER_WUE = 0.569

# Fallback ratio when only the reasoning text length is known: about four
# characters per token for Latin-script text.
CHARS_PER_TOKEN = 4


def _electricity_mix():
    """Return the EcoLogits electricity mix for France, else the world mix."""
    mix = electricity_mixes.find_electricity_mix(zone=ELECTRICITY_MIX_ZONE)
    if mix is None:
        mix = electricity_mixes.find_electricity_mix(zone=ELECTRICITY_MIX_FALLBACK_ZONE)
    return mix


def _scalar(value: Any) -> float:
    """Collapse an EcoLogits value-or-range to a float (mean of the range)."""
    if isinstance(value, RangeValue):
        return float(value.mean)
    return float(value)


def estimate_co2_kg(model: LLModel, output_tokens: int, latency_s: float | None) -> float | None:
    """Estimate the CO2 of one answer in kgCO2eq with EcoLogits.

    Returns None when the model has no configured parameter counts (the
    estimate cannot be computed) or when EcoLogits fails.
    """
    total_params = model.total_params_b
    active_params = model.active_params_b if model.active_params_b is not None else total_params
    if total_params is None or active_params is None:
        return None
    if output_tokens <= 0:
        return 0.0

    mix = _electricity_mix()
    if mix is None:
        logger.warning("EcoLogits electricity mix not found, cannot estimate CO2.")
        return None

    try:
        impacts = compute_llm_impacts(
            model_active_parameter_count=active_params,
            model_total_parameter_count=total_params,
            output_token_count=output_tokens,
            if_electricity_mix_adpe=mix.adpe,
            if_electricity_mix_pe=mix.pe,
            if_electricity_mix_gwp=mix.gwp,
            if_electricity_mix_wue=mix.wue,
            datacenter_pue=DATACENTER_PUE,
            datacenter_wue=DATACENTER_WUE,
            request_latency=latency_s if latency_s and latency_s > 0 else None,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("EcoLogits CO2 estimate failed for model %s", model.hrid)
        return None

    return _scalar(impacts.gwp.value)


def resolve_co2(
    model: LLModel,
    provider_co2_kg: float | None,
    completion_tokens: int,
    reasoning_tokens: int,
    latency_s: float | None,
) -> tuple[float | None, CO2Source]:
    """Pick the CO2 figure to keep for an answer and tag its source.

    - Reasoning answers (``reasoning_tokens > 0`` or a model with a reasoning
      control) get an EcoLogits estimate on completion + reasoning tokens,
      because the provider's figure ignores reasoning tokens. If the estimate
      is impossible (no parameter counts), the provider's figure is kept when
      present.
    - Otherwise the provider's figure wins when present.
    - Otherwise an EcoLogits estimate on completion tokens.
    """
    is_reasoning = reasoning_tokens > 0 or model.reasoning_control != "none"

    if is_reasoning:
        estimate = estimate_co2_kg(model, completion_tokens + reasoning_tokens, latency_s)
        if estimate is not None:
            return estimate, "estimated_reasoning"
        if provider_co2_kg is not None:
            return provider_co2_kg, "provider"
        return None, "estimated_reasoning"

    if provider_co2_kg is not None:
        return provider_co2_kg, "provider"

    return estimate_co2_kg(model, completion_tokens, latency_s), "estimated"


def reasoning_tokens_from_details(details: Mapping[str, Any] | None) -> int:
    """Return the reasoning token count carried by a usage ``details`` mapping.

    Prefers the provider-reported ``reasoning_tokens``; falls back to the
    ``reasoning_chars`` accumulated from streamed reasoning deltas divided by
    an average characters-per-token ratio.
    """
    if not details:
        return 0

    reasoning_tokens = details.get("reasoning_tokens")
    if reasoning_tokens:
        return int(reasoning_tokens)

    reasoning_chars = details.get("reasoning_chars") or 0
    if reasoning_chars <= 0:
        return 0
    return math.ceil(reasoning_chars / CHARS_PER_TOKEN)


def wh_from_co2_kg(co2_kg: float | None) -> float | None:
    """Energy of an answer in Wh, back-computed from its CO2 figure.

    Every answer is stored with a CO2 value, not an energy one, so the arena
    results derive Wh from it with the same electricity mix EcoLogits used to
    produce it (``gwp`` is kgCO2eq per kWh). Since both sides of a comparison run
    on the same mix, the *ratio* the promotion verdict looks at is exact whatever
    the mix; the absolute value is as good as the mix figure.
    """
    if co2_kg is None:
        return None
    mix = _electricity_mix()
    gwp = _scalar(mix.gwp) if mix is not None else None
    if not gwp:
        return None
    return float(co2_kg) / gwp * 1000
