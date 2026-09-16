# LLM router v1: three complexity tiers, tagged experiments

Status: v1 specification, final. Builds on `arena-mvp-spec.md` (branch
`arena-mvp`). Owner: assistant-ia team. Earlier revisions of this document
explored per-domain routing, user profiles and open-data contribution; all
three are deferred and recorded in section 16 so they are not re-debated.

## 1. Goal

Every user message is routed to one of three models by how hard the
question is. Every arena vote is tagged with the domain and task of the
question so that a preference dataset accumulates for internal use. The
user sees the decision being made for them, in words they understand, and
never a vendor or model name.

| Tier | Slug | Model class | Label shown to users |
|---|---|---|---|
| 1 | `simple` | small, fast | Modèle rapide |
| 2 | `standard` | medium, efficient | Modèle équilibré |
| 3 | `complex` | large reasoning model | Modèle de raisonnement |

Optimised, in this order: the answer is good enough that the user does
not leave for ChatGPT; energy and latency per answer go down; the user
trusts the choice because it is visible and explained.

Success looks like: "Reformule ce mail" answers on the small model in
under a second with the caption "Auto · Modèle rapide"; a multi-step
analysis lands on the reasoning model with a visible "Réflexion en cours…";
the admin results page says "on writing tasks in the standard tier,
challenger X wins 62 percent of votes"; the Router dashboard shows energy
per answer falling.

## 2. Principles

- **Route per turn.** Each message is classified on its own, with the
  previous turn's labels as a hint. The full history is passed whatever
  the tier.
- **Never route below capability.** Image attachments, tools, context
  length and forced web search are hard constraints checked before the
  tier model is picked. If the tier model cannot do it, walk up.
- **Compare within a tier only.** The router decides how much model a
  question needs; the arena decides which model of that size users prefer.
  A challenger is always an alternative of the same tier. One flagged
  exception exists to tune the classifier (section 5.4) and never counts
  for promotion.
- **One variable at a time.** Every model, every alternative and the
  reasoning model run the same system prompt and tool list. Only the model
  changes, and in tier 3 the reasoning effort.
- **Tags are stored, not used.** Domain and task tags are written on every
  arena comparison and sent as Langfuse tags on every trace. Nothing
  routes on them in v1.
- **Users see tiers, admins see models.** The model selector offers Auto
  and the three tiers. The full model list exists only in the Django
  admin and, behind a staff-only flag, on dev and staging.
- **Auto is the default, manual is allowed.** A user may pin a tier for a
  conversation. The interface frames Auto as the responsible choice and
  says what a heavier tier costs before it is picked.
- **The footprint is shown after the choice.** Arena candidates carry no
  energy leaf; the committed answer shows its own leaf like any answer.
- **Voting should feel like contributing.** The vote is answered with real
  numbers and the chosen answer moves into the conversation instead of
  the loser vanishing.
- **Second opinions are the user's to spend.** A dedicated button runs an
  alternative model of the same tier on demand, with no cap.
- **Only humans vote.** Model preference comes from real users. No model
  scores another model's answer: a machine verdict is not evidence of what
  civil servants prefer, and mixing the two would blur the one number the
  arena exists to produce. Footprint is measured, not judged, and reported
  next to the human win rate so a costly answer that people barely prefer
  is visible as such.
- **Langfuse observes, Django decides.** Descriptive data (energy, cost,
  latency, distributions, classifier quality) lives in Langfuse. Anything
  that changes runtime behaviour or is a system of record (tier settings,
  votes, promotions) lives in Django.

## 3. Models on Albert and tier assignment

Albert (`https://albert.api.etalab.gouv.fr/v1/models`, probed 2026-09-16)
serves eight chat models, all billed at zero, so the footprint axis is
energy and latency; euro prices stay configurable for other providers.

| Albert id | Params (total / active, B) | Vision | Context | Reasoning control |
|---|---|---|---|---|
| `ministral-3-8b-instruct-2512` | 8 dense | yes | 262k | none |
| `mistral-small-3-2-24b-instruct-2506` | 24 dense | yes | 128k | none |
| `gemma-4-31b-it` | 31 dense | yes | 262k | none |
| `mistral-medium-3-5` | 128 dense (open weights, April 2026) | yes | 262k | none; `reasoning_effort` accepted and ignored |
| `gpt-oss-120b` | 117 / 5.1 MoE | no | 131k | `reasoning_effort` low / medium / high honoured; reasoning streamed in `delta.reasoning` |
| `deepseek-v4-flash-0731` | 284 / 13 MoE | no | 131k | on/off only: `reasoning_effort: none` disables; `low` still thinks |
| `qwen3-coder-30b-a3b-instruct` | 30 / 3 MoE | no | 262k | none; code specialist |
| `lightonocr-2-1b` | 1 | OCR only | 16k | not a chat model |

Parameter counts are official (Hugging Face cards, DeepSeek tech report),
cross-checked with Compar:IA's registry. `mistral-medium-3-5` also carries
the alias `mistral-medium-2508` (Medium 3.1, closed, size undisclosed); we
assume the open 3.5 weights and configure 128. The difference with the
alternative estimate of 123 is under 5 percent.

Three probe facts drive the design:

1. Albert's `usage.completion_tokens` excludes reasoning tokens: a
   `gpt-oss-120b` answer with 800 characters of reasoning and a one-word
   reply reports `completion_tokens: 1`.
2. Albert's `carbon` and `impacts` figures are computed from that count,
   so its CO2 undercounts reasoning models; a high-effort answer can
   report less CO2 than a low-effort one.
3. On the streaming path Albert sends the reasoning chunk by chunk in
   `delta.reasoning` (85 chunks for a one-word answer in the probe), and
   the final usage chunk still excludes those tokens. The tokens can be
   counted on our side.

### 3.1 Assignment

"Model" is what the tier runs; "alternatives" is the closed list from
which arena challengers for that tier are drawn.

| Tier | Model | Alternatives | Why |
|---|---|---|---|
| `simple` | `ministral-3-8b` | `mistral-small-3-2` | The only small model. Mistral Small is the fallback if 8B quality is not enough; both have vision. |
| `standard` | whatever `LLM_DEFAULT_MODEL_HRID` is in production | `mistral-small-3-2`, `gemma-4-31b`, `mistral-medium-3-5`, minus the model itself | Tier 2 is today's production model by construction, so nobody needs to know which one it is to configure the router. The medium-class siblings are its challengers. |
| `complex` | `gpt-oss-120b` | `mistral-medium-3-5`, `deepseek-v4-flash` | Dedicated reasoning model with 5.1B active parameters, so not much heavier per token than tier 2. Mistral Medium is the vision-capable fallback since both reasoning models are text-only. |
| router | `ministral-3-8b` | – | classifier, about 300 input tokens, structured output |

`qwen3-coder-30b` is not in a tier: it is a task specialist, and task
routing is deferred. Coding turns are tagged `task:coding`; the by-task
results table will show whether a coding branch is worth adding.

`gpt-oss-120b` has the smallest context of the three tiers (131k), so the
context constraint cannot push a very long conversation to tier 3; it
stays on tier 2. Accepted.

### 3.2 Reasoning effort is routed too

Only tier 3 has a reasoning control and only `gpt-oss-120b` has levels.

| Situation | Model | Effort |
|---|---|---|
| `complex`, confidence 0.7 to 0.9 | `gpt-oss-120b` | `medium` |
| `complex`, confidence >= 0.9, or task in {`reasoning`, `data_analysis`, `coding`} | `gpt-oss-120b` | `high` |
| `complex` with image constraint | `mistral-medium-3-5` | n/a |
| user pinned Raisonnement | `gpt-oss-120b` | `high` |
| experiment with `reasoning_effort` set | both sides | that value |

`low` is not used: at low effort the model is not cheaper than tier 2, and
the router would have chosen tier 2. `deepseek-v4-flash` runs with
thinking on.

Plumbing: `LLMSettings.extra_body` sets a static effort per configured
model; per-turn effort needs `model_settings` at run time, so
`AIAgentService` gains a `model_settings_override` built by the router
(`OpenAIChatModelSettings.openai_reasoning_effort` on the OpenAI path).
The effort used is recorded with the routing labels.

Tier 3 experiments run sequentially: first `gpt-oss-120b` versus
`mistral-medium-3-5` at fixed `medium` effort ("is a reasoning model worth
it over a large dense one"), then `gpt-oss-120b` at `high` versus
`deepseek-v4-flash` thinking on, with the level mismatch written in the
experiment description and both efforts stored per side.

## 4. Classification

One structured-output call on the router model:

```python
class RoutingLabels(BaseModel):
    complexity: Literal["simple", "standard", "complex"]
    domain: Literal[...]        # 4.1
    task: Literal[...]          # 4.2
    confidence: float           # 0..1, on complexity
```

Input: the user message, the previous turn's labels, the last 200 tokens
of the previous assistant answer, and whether attachments or project
context are present. No tools. Prompt versioned in Langfuse prompt
management as `router-classifier`, label `production`; the version id is
recorded with every decision.

Shortcuts that skip the call: message under 6 words with previous labels
(reuse them, task `conversation`); timeout or error (previous tier, else
`standard`, confidence 0, reason `fallback`).

The timeout (`LLM_ROUTER_TIMEOUT_S`) is 2.5 s. Measured on Albert with
`ministral-3-8b` and the production prompt: 0.9 to 1.7 s per call, the
first call of a process being the slowest. The earlier 400 ms budget was
written before measuring and made every turn fall back, which is a silent
way of switching the router off. Classification therefore adds about one
second before the first token; tier 1 answers still come back faster than
today's single model, and the Router dashboard tracks the overhead.

Domain and task cost about 15 extra output tokens on a call made anyway,
so they are always produced. No consent gate: no text leaves the platform
through them.

### 4.0 Choosing the router model (measured)

`benchmark_router_models` replays the 24-turn gold set
(`chat/router/gold_set.json`) through each candidate. Three passes per
model, 72 classifications each, on Albert, 2026-09-16:

| Model | tier | domain | task | p50 | p95 | mgCO2e/call |
|---|---|---|---|---|---|---|
| `deepseek-v4-flash` | 92 % | 93 % | 90 % | 421 ms | 1128 ms | n/a |
| `gemma-4-31b` | 92 % | 92 % | 88 % | 619 ms | 937 ms | n/a |
| `mistral-small-3-2` | 92 % | 79 % | 88 % | 869 ms | 1190 ms | n/a |
| **`ministral-3-8b`** | **90 %** | **78 %** | **85 %** | **332 ms** | **402 ms** | **0.20** |
| `mistral-medium-3-5` | 88 % | 79 % | 92 % | 491 ms | 1675 ms | 1.55 |
| `gpt-oss-120b` | 71 % | 83 % | 88 % | 805 ms | 1344 ms | n/a |

Every model returned valid structured output on every call, and only one
classification in the whole benchmark confused `simple` with `complex`:
errors are off by one tier, which costs a size step, not a wrong answer.

`ministral-3-8b` stays the router. Tier accuracy is what decides routing,
and 90 against 92 percent is inside the confidence interval of 72 samples,
while the small model is 2.8 times faster at p95 and about seven times
lighter. A router that itself burns a large model on every turn would eat
the saving the router exists to produce.

Its real weakness is tagging: 78 percent on domain against 93 for
DeepSeek. Tags do not route anything in v1, they only build the dataset, so
this is accepted for now and revisited when a tag slice is about to become
a branch. The cheap fix at that point is to re-tag arena comparisons in the
background with a stronger model, off the critical path, rather than to
slow every turn down.

Latency on Albert is noisy: a single pass of `ministral-3-8b` produced a
p95 of 4.7 s where three passes give 402 ms. Re-run the benchmark with at
least three passes before drawing any conclusion, and keep the 2.5 s
timeout, which exists for exactly those outliers.

Caveat on the table: Albert reports `output_tokens` for some models only,
so the energy column is blank where the provider sent no usage.

### 4.1 Domain tags

`general`, `administrative`, `legal`, `health`, `finance`, `hr`,
`it_software`, `science_education`, `communication`, `defense_security`,
`environment`, `culture_society`.

### 4.2 Task tags

`qa_knowledge`, `writing`, `summarization`, `translation`, `coding`,
`data_analysis`, `document_qa`, `research`, `reasoning`,
`brainstorm_creative`, `classification_extraction`, `conversation`.

### 4.3 What complexity means

- `simple`: one step, a few sentences, no reasoning. Rewrites, short
  factual questions, translating a sentence, greetings.
- `standard`: needs structure or domain knowledge, a few paragraphs.
  Drafting a note, summarising a document, explaining a procedure.
- `complex`: multi-step reasoning, trade-offs, maths, code in several
  parts, analysis of long input, anything where a wrong shortcut is
  costly.

The prompt carries six examples per tier from real anonymised turns.
Tiers 1 and 3 are chosen only with confidence >= 0.7, otherwise tier 2.
Tier 2 is today's production model, so a wrong classification degrades to
current behaviour.

## 5. Routing

```
labels = classify(message, previous_labels, context)
tier   = pinned_tier or (labels.complexity if labels.confidence >= 0.7 else "standard")
tier   = max(tier, minimum_tier_for(constraints))     # image, tools, context length, web search
model  = resolve(tier)  ->  health cascade (existing model_routing.py)
effort = effort_for(tier, labels, pinned_tier, experiment)
```

### 5.1 Constraint resolution

If the tier model lacks a capability, try the tier's alternatives that
have it, then the next tier up. Images on tier 3 land on
`mistral-medium-3-5`. If nothing fits, the default model is used with
reason `constraint_fallback` and an admin counter increments.

### 5.2 Settings and insertion point

`RoutingTierSettings` (section 9) is seeded from `LLM_TIER_*_MODEL_HRID`
settings that all default to `LLM_DEFAULT_MODEL_HRID`, so with the flag on
and nothing configured the router is a no-op. The existing fallback
settings remain the health cascade for each tier.

`post_conversation` (`chat/views/conversations.py`) pins
`conversation.model_hrid` once today through `resolve_effective_model_hrid`
(`chat/model_routing.py`). With the `router` flag on, the model is
resolved per turn and `conversation.model_hrid` becomes "model of the last
turn". Arena's `_pin_conversation_to_champion` pins to the experiment
tier's model.

### 5.3 Manual tier choice, Auto strongly preferred

The compose-box selector offers four entries and nothing else:

| Entry | Label | Sub-label | Leaves |
|---|---|---|---|
| `auto` | Auto | Choisit le modèle le plus sobre pour chaque question | – |
| `simple` | Rapide | Réponses courtes, reformulations | 1 |
| `standard` | Équilibré | Rédaction, synthèse, explications | 2 |
| `complex` | Raisonnement | Analyses, calculs, réponses plus longues | 3 |

Rules:

- Auto is the default on every new conversation, marked "Recommandé" with
  a leaf badge. The selector is collapsed to one chip showing "Auto".
- Leaves are ordinal and match the tier pictogram. `standard` and
  `complex` also show "Environ N fois plus d'énergie qu'une réponse
  rapide. L'assistant y recourt de lui-même quand la question le demande."
  N comes from `tier_energy` (section 10).
- A manual choice applies to the current conversation only and is stored
  as `ChatConversation.pinned_tier`.
- After three turns on a pinned tier where the router would have picked a
  lower one, a one-line non-blocking hint appears under the caption: "Le
  mode Auto aurait suffi pour cette question", with a "Revenir en Auto"
  link. At most once per conversation. Shown and followed counts feed a
  dashboard widget; reviewed after four weeks against 30 percent
  followed, wording changes before frequency.
- A pinned tier bypasses the complexity decision but not the classifier:
  domain and task are still produced and the router's own choice is
  stored as `router_would_pick`. Constraints still apply. Arena draws on a
  pinned tier are allowed and compare within that tier.
- The selector never shows a model name.

### 5.4 Threshold tuning, a procedure not a judgment

`confidence_threshold` (0.7) and `high_effort_threshold` (0.9) live in
`RoutingTierSettings` and are reviewed monthly from human votes and
traffic, never from a model's opinion:

- tier share and constraint fallbacks on the Router dashboard: a tier 1
  share under 25 percent of turns means the classifier is too cautious;
- a **control challenger**: `ministral-3-8b` on 5 percent of tier 2
  draws, flagged `is_control`. If it wins or draws more than 55 percent of
  decisive votes on turns tagged `standard`, the classifier is too
  conservative; lower by 0.05. Control votes are excluded from the
  promotion verdict and shown in their own results column.

One step per month at most, logged in `RoutingTierHistory` with reason
`threshold`.

### 5.5 What is recorded per turn

On the assistant message metadata (existing `conversation_metadata`
mechanism) and as Langfuse trace tags: `tier`, `tier_source`
(`router | user | constraint`), `router_would_pick`, `reasoning_effort`,
`reasoning_tokens`, `reasoning_seconds`, `domain`, `task`,
`router_confidence`, `router_reason`
(`classified | shortcut | constraint | fallback | user_pinned`),
`router_latency_ms`, `router_prompt_version`. No new table for non-arena
turns: Langfuse is the store for those, comparisons for votes.

## 6. Model selector and the LLM configuration endpoint

Today `GET /api/v1.0/llm-configuration/` returns every entry of the
configuration file and the frontend `ModelSelector` lists them all,
including non-chat entries such as the summarization model. In v1:

- The endpoint returns, for every authenticated user:

  ```json
  {"mode": "tiers",
   "tiers": [
     {"slug": "auto",     "label_key": "router.tier.auto",     "recommended": true},
     {"slug": "simple",   "label_key": "router.tier.simple",   "leaves": 1, "energy_ratio": 1.0},
     {"slug": "standard", "label_key": "router.tier.standard", "leaves": 2, "energy_ratio": 8.1},
     {"slug": "complex",  "label_key": "router.tier.complex",  "leaves": 3, "energy_ratio": 10.4}
   ]}
  ```

  A tier is omitted when its model is not configured or is marked
  unhealthy by the health cascade; Auto is always present. No model
  hrid, name, icon or provider is ever in the payload.
- The `models` list is returned only when the requester `is_staff` **and**
  the feature flag `dev_model_picker` is on (off in production). This keeps
  the raw picker for dev and staging debugging. The `model_hrid` query
  parameter on the send endpoint is accepted under the same two
  conditions and otherwise rejected with a 400.
- The frontend `ModelSelector` is replaced by `TierSelector`. The staff
  picker, when present, is a separate collapsed "Modèle (debug)" entry at
  the bottom of the same menu.
- Sending carries `tier` (`auto | simple | standard | complex`) instead of
  `model_hrid`; the backend stores it as `pinned_tier` when not `auto`.
- `LLModel` gains `role: chat | utility` in the configuration; `utility`
  entries (summarization, title generation, router) are never eligible as
  tier models or challengers and never listed to the staff picker.
- Django admin: `RoutingTierSettings` uses the existing configured-model
  dropdowns (`_configured_model_choices`) for tier models and multi-select
  for alternatives, filtered to `role: chat`.

## 7. Telling the user

### 7.1 The Auto decision, visible on every turn

1. While the router runs (about one second, 2.5 s worst case): the pending
   bubble shows a shimmer line "Choix du modèle…" in the caption slot.
2. As soon as the decision is known, before the first token, a transient
   stream part is emitted next to the existing `conversation_metadata`
   and `cooldown` parts (`chat/clients/pydantic_ai.py`, consumed in
   `useChat.tsx`):

   ```json
   {"type": "data-routing", "data": {
     "tier": "complex", "tier_label": "router.tier.complex",
     "tier_source": "router", "changed": true, "reasoning": true}}
   ```

3. The caption replaces the shimmer: tier pictogram (one to three bars)
   plus "Auto · Modèle de raisonnement". Constraint bumps say why: "Auto ·
   Modèle équilibré (image)". Pinned: "Modèle de raisonnement · choisi par
   vous". The caption stays on the message at 12px, same style as the
   leaf row, and the message info tooltip adds "L'assistant a choisi ce
   modèle pour cette question afin d'utiliser juste ce qu'il faut." On
   `changed`, the pictogram animates once from the previous tier (150 ms;
   none under reduced motion).
4. First time only, the intro line (existing `ArenaIntro` pattern, flag in
   the preferences store): "Pour chaque question, l'assistant choisit
   automatiquement le modèle le plus adapté et le plus sobre. Vous voyez
   ce choix au-dessus de chaque réponse."
5. Arena turns: "Deux modèles équilibrés sont comparés" with the tier
   label; the committed answer gets the normal caption after the vote.

### 7.2 Reasoning indicator

Reasoning models stream `ThinkingPart`s before any text; pydantic-ai 2.22
maps Albert's `delta.reasoning` chunks to them. The frontend ignores them
today, so the bubble would stay empty for 20 to 60 seconds. In
`MessageItem.tsx`:

- While thinking parts arrive and no text has started: "Réflexion en
  cours…" with an animated ellipsis, and an elapsed counter after 5
  seconds ("Réflexion en cours… 23 s"), under the tier caption.
- Once text starts: collapses to a chevron "Raisonnement (23 s)" that
  expands the reasoning text, collapsed by default, rendered plain and
  muted, never included in copy.
- On reload only "Raisonnement (23 s)" from `reasoning_seconds` is shown,
  without the text.
- Reduced motion: static ellipsis, counter still updates.

The backend's keepalive (every 55 seconds) already protects the
connection through proxies during silence; this section is about what
the user sees.

## 8. Arena on tiers, and the second-opinion button

### 8.1 Sampled draws

- `ArenaExperiment`: `champion_model_hrid` is replaced by `tier`. The
  champion at draw time is the tier's model, snapshotted on the comparison
  as today. At most one active experiment per tier. `clean()` refuses a
  challenger outside the tier's alternatives unless `is_control`.
- `ArenaExperiment.reasoning_effort`, optional, `medium | high`. When set
  on a tier 3 experiment both sides run at that effort where the model has
  levels; on/off models run with thinking on. Unset: the router's per-turn
  effort applies to both sides. Effort used is stored per side.
- `ArenaComparison`: `theme` is replaced by `tier`, `tier_source`,
  `domain`, `task`, `router_confidence`, `router_reason`,
  `router_model_hrid`, `router_prompt_version`, `origin` (`draw | manual`),
  and per side `reasoning_tokens`, `reasoning_effort`, `co2_source`.
  `context_tags` unchanged.
- Eligibility: the turn's tier equals the experiment's tier; the
  challenger satisfies the turn's constraints.
- Results (`arena_results.py`): header and challenger table unchanged, per
  experiment and therefore per tier. Context slices become tag slices:
  one table each for `domain`, `task`, `context_tags`, with n, win rate,
  Wilson interval and the "indicative" rule. Manual comparisons and
  control challengers each get their own columns and never enter the
  sampled win rate.
- Promotion: a **Promote** button on a challenger row writes it into
  `RoutingTierSettings` for that tier and a `RoutingTierHistory` row.
  Manual only in v1. Verdict: `promote` when n >= `min_votes_for_conclusion`
  and the Wilson lower bound is above 50 percent; `promote_for_footprint`
  when the interval includes 50 percent and the challenger is at least 30
  percent lighter in Wh per answer and not slower at p95; `keep`
  otherwise; `indicative` under threshold.

### 8.2 Second opinion on demand

Button "Essayer une autre réponse" in the assistant message toolbar,
shown on the last assistant message when its tier has at least one
alternative not yet tried on that turn that satisfies the turn's
constraints.

`POST chats/<id>/arena/manual/ {message_id}` creates a comparison with
`origin=manual`, champion = the model that answered, challenger = a random
untried eligible alternative of the same tier, `champion_payload` copied
from the existing message; returns the comparison id and side. The
frontend opens one stream for the challenger side (`claim_candidate`
already handles one side at a time), shows the existing answer as
"Réponse A" and the new one as "Réponse B" in the arena split with the
normal vote bar. B swaps the answer as in a draw; A or a draw keeps it.
The button reappears after the vote until the tier's alternatives are
exhausted.

No daily cap. The existing per-user stream concurrency limit and
`ChatCooldownSettings` are the only guards. Own feature flag
`arena_manual`. Caption: "Deux modèles équilibrés sont comparés".

## 9. Data model (app `chat`)

```
RoutingTierSettings  (singleton, same pattern as ModelHealthSettings)
  simple_model_hrid,   simple_alternatives    JSON list
  standard_model_hrid, standard_alternatives  JSON list
  complex_model_hrid,  complex_alternatives   JSON list
  router_model_hrid            nullable, overrides setting
  confidence_threshold         default 0.70
  high_effort_threshold        default 0.90
  tier_energy JSON             {tier: {wh_per_answer, n, source: estimated|measured, updated_at}}

RoutingTierHistory
  tier, changed_at, changed_by (nullable), reason in {promotion, threshold, manual}
  from_value, to_value, evidence JSON (experiment id, votes, win rate, ci, wh ratio)

ArenaExperiment   - champion_model_hrid  + tier, reasoning_effort (nullable)
ArenaChallenger   + is_control (default False)

ChatConversation  + pinned_tier (nullable; null = auto)
```

LLM configuration (`chat/llm_configuration.py`, `default.json`) gains per
model `role` (`chat | utility`), `input_price_eur_per_mtok`,
`output_price_eur_per_mtok`, `total_params_b`, `active_params_b`. Arena
price fields default from the configuration; the frozen `price_snapshot`
stays. Retention stays at 90 days with the existing redaction, extended
to Langfuse trace deletion through its API in the same signal.
`pinned_tier` is kept, it is not personal data.

## 10. Footprint

- **CO2 for non-reasoning models:** Albert's header when present
  (`co2_handling: albert`, existing); EcoLogits estimate when absent.
- **CO2 for reasoning models:** Albert's figure is replaced by an EcoLogits
  estimate from `compute_llm_impacts` with the configured parameter
  counts, `completion_tokens + reasoning_tokens` and latency, the same
  call Compar:IA uses. `reasoning_tokens` is accumulated from the streamed
  `delta.reasoning` chunks in `AlbertOpenAIStreamedResponse`
  (`chat/providers/albert_models.py`, next to the CO2 factor it already
  extracts), with the model's tokenizer when available, else `len / 4`.
- `co2_source` in {`provider`, `estimated`, `estimated_reasoning`}, shown
  as "estimation" in the leaf tooltip. New dependency `ecologits`. To be
  reported to the Albert team, whose dashboard undercounts the same way.
- **Cooldown counts reasoning:** `reasoning_tokens` are added to the
  completion tokens fed to the `ChatCooldownSettings` window. The user
  message is unchanged.
- **`tier_energy`:** management command `refresh_tier_energy`, weekly
  Celery beat, over the last 30 days. The source is our own stored CO2 per
  answer, not Langfuse: Langfuse holds tokens, not watt-hours, and cannot
  group by a single tag value, so pulling from it would mean re-deriving
  energy with the same EcoLogits model we already apply per answer, but
  with worse inputs (no per-answer parameter counts, no reasoning tokens).
  Below 500 answers a tier keeps an EcoLogits starting value computed from
  its configured model and a typical answer (150, 400, and 500 + 2,000
  reasoning tokens), flagged `estimated`; at or above 500 it is the
  measured mean, flagged `measured`.

  Measured on the configured tiers, the starting values are 0.040 Wh for
  tier 1 (`ministral-3-8b`), 0.131 Wh for tier 2 (`mistral-small-3-2`) and
  2.54 Wh for tier 3 (`gpt-oss-120b`), that is about 3 and 64 times tier 1.
  An earlier revision of this document guessed 8 and 10 without computing;
  the guess was wrong and the computation stands. The tier 3 figure is
  dominated by answer length, not by model size: a reasoning answer is
  assumed to spend 2,000 thinking tokens on top of its reply, so the
  selector's "Environ 64 fois plus d'énergie" compares a long reasoned
  answer with a one-line rewrite. That is the honest comparison for a user
  choosing a mode, and it is exactly the number the responsible-design
  framing exists to surface. Once 500 real answers per tier exist the
  sentence follows measured usage instead.
- Router overhead (its own latency and tokens) is stored separately and
  must stay under 2 percent of total tokens on the dashboard.

## 11. Vote acknowledgement and transition

### 11.1 Data returned by the vote

`POST chats/<id>/arena/<uuid>/vote/` returns the conversation as today plus:

```json
{"acknowledgement": {
  "user_votes": 7,                 // this user's votes on comparisons under 90 days
  "experiment_votes": 1342,
  "tier_label": "router.tier.standard",
  "task_label": "router.task.writing",
  "domain_label": "router.domain.administrative",
  "milestone": "first_vote" | "tenth_vote" | "hundredth_vote" | null
}}
```

Two aggregates on `ArenaComparison`. The copy says "vos avis récents"
because of the 90-day redaction. Draws return the block; abandonment does
not.

### 11.2 No leaf while choosing, the leaf after

Candidates keep `hideActions`: no leaf, copy, feedback, sources or
second-opinion button during the choice. The committed answer shows its
own leaf bottom right like any answer, with its CO2 corrected as in
section 10. The loser's footprint is stored, never shown.

### 11.3 What the user sees

1. Click. The vote bar disappears; both columns lock.
2. The unchosen column fades to 0 and its width animates to 0 (250 ms,
   ease-out). The chosen column slides to the left edge and grows to full
   width (300 ms); its "Réponse A/B" caption cross-fades into the normal
   header plus tier caption. The chosen column is the DOM node that
   becomes the committed message, so text does not reflow twice: render
   the committed message in place, swap the arena container on the next
   paint.
3. An acknowledgement card slides up under the message for about 4
   seconds then collapses: "Merci, votre avis compte. Vous avez donné 7
   avis récents, sur 1 342 pour cette évaluation. Ce vote aide à choisir
   le modèle équilibré pour la rédaction administrative." First vote:
   "Premier avis, merci !" Tenth and hundredth: a longer line and a
   one-off Lottie check mark (`lottie-react` already present).
4. `prefers-reduced-motion`: no width animation, a 150 ms cross-fade, the
   card appears without sliding.

Implementation: CSS transitions in `ArenaTurn.tsx`, new `ArenaThanks.tsx`,
`onVoted` delayed until the transition ends so `Chat.tsx` swaps in the
committed message after the visual move. fr/en i18n. Card is
`role="status"`. Keyboard path unchanged.

## 12. Langfuse versus custom

Moves to Langfuse:

| Need | Feature | Use |
|---|---|---|
| Slices by tier / domain / task / model / source | trace tags and metadata via `propagate_attributes` | tags `tier:complex`, `tier_source:user`, `domain:legal`, `task:writing`, `routed:classified`; set before the trace starts (tags are immutable) |
| Energy, tokens, latency, cost over time | model definitions, custom dashboards | one `Router` dashboard: tier share, Wh per tier, p95 latency per tier, router overhead, hint shown/followed, constraint fallbacks; JSON in `docs/langfuse/`, pushed with the public API |
| The vote as a score | categorical scores idempotent by `score_id` | `arena_preference` in {`won`, `lost`, `tie`, `both_bad`, `abandoned`} on both traces, `score_id = f"{comparison_id}-{role}"`, `origin` as comment |
| Router prompt | prompt management | `router-classifier`, version on each decision |
| Classifier regression tests | datasets + experiments through `chat/evals` | a fixed set of real turns with the tier the team agrees on, run before a prompt or threshold change |
| `tier_energy` source | Metrics API v2 | weekly pull by `refresh_tier_energy` |

Stays in Django: draw, manual comparison, streams, vote, commit;
`ArenaComparison` as system of record; results page with Wilson intervals,
tag tables, manual and control columns, verdict and Promote; tier settings
and history; labels and i18n; redaction. Langfuse dashboards cannot be
embedded, so the results page links to the pre-filtered dashboard and
readers get an org Viewer seat.

Removed from the Django results page once the dashboard exists: mean
latency, tokens and CO2 columns, cost totals, manual price fields.

## 13. Internal stats

No publication in v1. Kept: every closed comparison with tags, models,
outcome, tokens, latency, CO2 and source, price snapshot, in Postgres
(redacted of user and content after 90 days, metrics kept); every trace
with tags in Langfuse. Management command `export_arena_stats` produces
CSV or Parquet of comparisons with metrics and tags only, no content, no
ids, for the Albert team, with columns aligned on the `comparia-fr-arena`
metadata shape so a later publication is formatting, not a project.

## 14. Build order

Feature flags: `router` (tiers, captions, selector), `arena_manual`
(second opinion), `dev_model_picker` (staff raw picker, off in
production). With all off, current behaviour is unchanged. PRs 1 to 3 have
no flag dependency and can ship first.

| # | Scope | Title | Depends on |
|---|---|---|---|
| 0 | doc | 📝(doc) LLM router v1 spec | – |
| 1 | back | ✨(back) vote acknowledgement block | – |
| 2 | front | ✨(front) vote transition and thanks card | 1 |
| 3 | back | ✨(back) model `role`, prices, parameter counts in the LLM configuration; EcoLogits estimate and `co2_source` | – |
| 4 | back | ✨(back) classifier agent with shortcuts, timeout, Langfuse prompt | – |
| 5 | back | ✨(back) `RoutingTierSettings`, per-turn routing with constraints, reasoning effort override, `pinned_tier` | 3, 4 |
| 6 | back | ✨(back) tiers endpoint, staff-only model list behind `dev_model_picker`, `tier` on send | 5 |
| 7 | back | ✨(back) Langfuse tags, `data-routing` part, per-turn metadata | 5 |
| 8 | front | ✨(front) `TierSelector` replacing `ModelSelector`, Auto framing, energy sentence | 6 |
| 9 | front | ✨(front) Auto decision caption, shimmer, pictogram, first-time intro | 7 |
| 10 | full | ✨(full) reasoning indicator, `reasoning_seconds` and tokens in metadata | 7 |
| 11 | back | ✨(back) reasoning tokens in the CO2 estimate and the cooldown window | 3, 10 |
| 12 | back | ♻️(back) arena per tier, alternatives validation, control flag, tags on comparisons, tag tables | 5 |
| 13 | back | ✨(back) manual comparison endpoint, `origin`, separate columns | 12 |
| 14 | front | ✨(front) "Essayer une autre réponse", repeatable | 13 |
| 15 | back | ✨(back) promotion verdict, Promote action, tier history | 12 |
| 16 | back | ✨(back) votes as Langfuse scores, Router dashboard JSON | 12 |
| 17 | back | ✨(back) `refresh_tier_energy` command and beat task | 16 |
| 18 | front | ✨(front) wasteful-pin hint with counters | 9 |
| 19 | back | ✨(back) `export_arena_stats` command | 12 |
| 20 | back | 🔥(back) drop descriptive columns and manual prices from results | 16 |

Suggested demo cut: PRs 1 to 10, 12 and 14 give the full user-facing story
(Auto caption, reasoning indicator, tier selector, arena with thanks card,
second opinion). 15 to 20 are admin and hygiene.

## 15. Acceptance criteria

Routing

- With `router` on and tiers configured as in section 3, "Reformule ce
  mail" answers on `ministral-3-8b`, a multi-step analysis on
  `gpt-oss-120b`, each with the matching caption.
- With `router` on and nothing configured, every tier resolves to the
  default model and behaviour is unchanged except for captions.
- An image on a complex turn lands on `mistral-medium-3-5`, reason
  `constraint`, never on a text-only model.
- Classifier down: every turn answers on the standard model within the
  usual latency, reason `fallback`, no visible error.
- A `complex` classification at confidence 0.95 runs at `high`, at 0.75
  at `medium`; the effort is on the message metadata.
- Router overhead under 2 percent of total tokens over a week.

Selector and visibility

- A non-staff user's configuration payload contains no model hrid, name,
  icon or provider; the selector shows exactly Auto plus the configured
  tiers. A staff user with `dev_model_picker` on sees the debug entry.
- A `model_hrid` query parameter from a non-staff user is rejected.
- Every Auto answer carries "Auto · <tier label>" with the pictogram from
  the first token and after reload. Pinned answers say "choisi par vous".
- New conversations start in Auto; picking Raisonnement shows the energy
  sentence first; the return-to-Auto hint appears at most once per
  conversation.
- A reasoning answer shows "Réflexion en cours…" within one second of the
  first thinking delta, a counter after five seconds, and a collapsible
  "Raisonnement (N s)" once text starts; reload shows the duration only.

Arena

- No comparison pairs models from different tiers except a challenger
  flagged `is_control`; saving any other out-of-tier challenger is
  refused.
- An experiment with `reasoning_effort = high` runs both sides at high
  regardless of confidence, and per-side effort says so.
- Results show by-domain and by-task tables per challenger with the
  indicative rule, manual and control votes in their own columns, and the
  verdict; neither enters the sampled win rate or the promotion verdict.
- The second-opinion button appears only on the last assistant message
  when an untried same-tier alternative fits the turn, is repeatable, has
  no daily cap, and stores `origin=manual`.
- Candidates never show a leaf; the committed answer shows the chosen
  answer's leaf after the vote.
- After a vote the chosen answer visibly moves into the conversation, the
  card shows counts matching the database, reduced motion gives
  cross-fades only, screen readers announce the card once.

Footprint and data

- A `gpt-oss-120b` answer with long reasoning and a short reply reports
  more CO2 than the same reply without reasoning, source
  `estimated_reasoning`; every comparison has a CO2 value and source.
- After a reasoning answer the user's cooldown window includes the
  reasoning tokens.
- Every trace carries `tier:*`, `domain:*`, `task:*` tags; every closed
  comparison has the same fields filled or `unknown`, plus `origin`.
- The energy sentence shows EcoLogits values until 500 answers per tier
  exist, then measured ones.
- The frontend never receives a model name.

## 16. Decisions log

| Decision | Revisit |
|---|---|
| Per-domain and per-task champions deferred; tags collected now so they can be built later from real votes. | when a tag slice has >= 100 decisive votes and a clear winner |
| Per-user model profiles dropped with the branches. | with branches |
| No open-data contribution in v1; export kept in Compar:IA shape. | product and legal decision, not before v2 |
| Users see Auto plus three tiers; the model list is admin-only, staff picker behind `dev_model_picker`. | never needed |
| Tier 2 is the production default model by construction. | never needed |
| Same tools and prompt for every model. | when a specialist model is introduced |
| Mistral Medium configured as the open 128B dense model. | if Albert states otherwise |
| Tier 3 experiments sequential: gpt-oss vs Mistral Medium at medium, then gpt-oss high vs DeepSeek thinking on. | after the first reaches `min_votes_for_conclusion` |
| Thresholds 0.7 / 0.9, one step of 0.05 per month from tier share and the control challenger. | monthly, logged |
| Auto hint once per conversation after three wasteful turns; target 30 percent followed. | four weeks after `router` is on |
| Leaves ordinal 1 / 2 / 3; multiplier measured weekly from our stored CO2, EcoLogits values (about x3 and x64) until 500 answers per tier. | automatic |
| No daily cap on manual comparisons. | if provider rate limits are hit |
| Manual promotion only; `auto_promote` is a v2 toggle. | after the first promotion |
| Router model is `ministral-3-8b`: fastest and lightest, tier accuracy within noise of the best candidate (section 4.0). | when a tag slice becomes a branch, or on a new Albert model |
| No LLM-as-a-judge anywhere: model preference comes from human votes only, and the classifier threshold is tuned from traffic and the control challenger. | if human vote volume proves too low to conclude |
| Implementation of PRs 12, 13 and 15: the draw endpoint runs the router itself, since the experiment is selected by the turn's tier; the streaming request that follows is in arena mode and does not classify again. With the `router` flag off the single active experiment is used and the routing columns stay empty. | when the router flag is permanently on |
| A sampled draw is refused (and counted as a refused draw) when the routed model is not the tier model, i.e. the health cascade or the constraint walk moved the turn. Comparing a challenger against a fallback model would not measure the tier. | never needed |
| Wh per answer is derived from the stored CO2 figure with the EcoLogits electricity mix rather than stored as an energy column; both sides share the mix, so the ratio the footprint verdict uses is exact. | when an energy column is stored per answer |
| The manual comparison copies the committed answer with a zeroed usage block: a conversation only keeps running totals, so the champion's own token counts for that turn are not recoverable. Swapping in the second opinion therefore adds its usage without subtracting an invented figure. | if per-turn usage is persisted on messages |
| A second opinion needs an active experiment on the turn's tier, because a comparison belongs to one. | if manual comparisons are wanted without an experiment |
| Promoting a challenger keeps the former champion as an alternative of the tier, so the arena can go on comparing against it. The experiment is not closed automatically; the admin says to open a new one. | with `auto_promote` |
