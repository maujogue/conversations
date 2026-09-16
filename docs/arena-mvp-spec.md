# Arena: in-app A/B testing of LLM models (hackathon MVP spec)

Status: draft for hackathon, revision 7 (as implemented on branch arena-mvp). Owner: assistant-ia team.

## 1. Goal

Give DINUM measurable evidence on which LLM civil servants actually prefer,
at what cost, on real work in the assistant. The first experiment compares the
production Mistral model against one cheaper open-weight challenger.

Success looks like one admin page saying, with real numbers:
"challenger X was preferred over the production model in P percent of N
blind votes, at C times lower cost per answer".

## 2. Principles (decided during brainstorm)

- **Champion versus challenger.** Every arena turn pits the conversation's
  pinned production model (the champion) against one challenger drawn from
  the experiment. This matches the real question ("is X better than what we
  run today"), gives a natural rule when the user does not vote, and makes a
  scoreboard trivial with several challengers.
- **Blind, and stays blind.** Model names are never shown to the user, before
  or after the vote. Model choice is an admin concern, not a user one.
- **Four-way vote, LM Arena style.** "A is better", "Both good", "Both bad",
  "B is better" in one bar under the two answers. A draw ("both good" /
  "both bad") is a vote that keeps the champion answer in the history and is
  counted separately from win rates, which only use decisive votes. The vote
  is also the action that continues the conversation, so it never feels like
  a survey.
- **Abandonment is data, not a vote.** Comparisons with no vote are stored
  and counted in the abandonment rate and cost totals, never in the win
  rate. The champion's answer is written to the history the moment it
  finishes streaming, so the conversation is never left without an answer
  whatever the user does next. A vote for the challenger swaps that answer.
- **One variable at a time.** Same system prompt, same tools, same retrieved
  context for both models. Only the model changes.
- **Tag, don't exclude.** Turns with web search, attachments or project
  context are arena-eligible and tagged with their context kind, so results
  can be sliced by task type.
- **Minimal UI footprint.** Reuse the existing message component, buttons
  from the La Suite ui-kit and Cunningham, and add nothing but a split
  container, two labels, two buttons and a one-line intro.

## 3. Scope

### In scope

1. Arena turn: champion and challenger answers streamed side by side, user
   picks one, winner is committed to the conversation.
2. One-line explanation the first time a user meets an arena.
3. Admin-configured experiment: challengers picked from a dropdown, sampling
   rate, per-user daily cap, prices per million tokens.
4. Comparison log with tokens, latency, co2, trace ids, vote outcome and
   context tags (plain, web_search, attachment, project).
5. Results page in Django admin: win rate of each challenger against the
   champion with confidence interval, cost per answer, latency, abandonment
   rate, win rate by context tag, position check. Win rate and interval
   are always shown; rows under a configurable vote threshold are labeled
   "indicative".
6. Seed command producing synthetic comparisons for the demo dashboard,
   clearly labeled as seed data.

### Deferred (explicitly out of the first iteration)

Revealing model names after the vote, CSV export, import or prior from the
Compar:IA dataset, classification of the user's question into themes, tie
option, multi-turn arena, challenger versus challenger pairs, prompt or
formatting experiments, ui-kit admin page, runtime creation of model
configurations from the Albert model list, user opt-out UI, testing whether
the vote button variant or emphasis nudges the choice.

## 4. User flow

1. User sends a message (first turn included: the creation handoff passes
   the new conversation id to the draw).
2. Frontend asks the backend whether this turn is an arena turn (draw).
3. If yes, frontend opens two streams in parallel, one for the champion and
   one for the challenger, for the same message. The assistant bubble splits
   into "Réponse A" and "Réponse B", in random order. Stop stops both.
4. First time only, one line above the split: "Deux réponses vous sont
   proposées. Choisissez celle que vous préférez, elle sera conservée dans
   la conversation." A "seen" flag is persisted in the chat preferences
   store (local storage).
5. When both streams finish, a button appears under each: "Je préfère cette
   réponse". Copy, thumbs, regenerate and sources are hidden on candidates.
6. User clicks one. A vote for the champion only records the vote (its
   answer is already in the history); a vote for the challenger swaps the
   assistant bubble, the turn's model history and the usage totals. The split
   collapses into a normal message. Nothing is revealed.
7. Leave without voting (reload, navigation, tab close, next session): the
   comparison stays pending. Reopening the conversation puts the two answers
   and the vote bar back on screen, and the champion answer committed as a
   safety net is hidden from the history while the choice is open. The choice
   is only ever closed by the user picking a side: sending another message is
   refused with a "pick an answer to continue" note until they do. A
   comparison with an unfinished candidate remains pending: reading history
   never cancels active work. An explicit cancellation or a new turn closes it,
   keeping any champion answer already committed.
8. The challenger stream fails: comparison marked errored, champion answer
   committed. The champion stream fails: comparison marked errored, the
   normal error path applies (no answer committed), the challenger answer
   is discarded.

The arena turn is a single-turn probe. The conversation stays pinned to the
champion afterwards, whichever side won. Re-pinning to the winner is
deferred: one vote is a weak reason to move a user to an unvalidated model,
and it would complicate health fallback and cost accounting.

## 5. When a turn is eligible, and how it is tagged

All conditions must hold, checked server side in the draw endpoint:

- Feature flag `arena` is enabled and exactly one experiment is active.
- The conversation's pinned model (or the model the first message would pin)
  is the experiment's champion.
- `random() < experiment.sampling_rate`.
- User has fewer than `experiment.daily_cap_per_user` comparisons today.
- The drawn challenger is healthy in `ModelHealth` (status not red), and the
  champion is not being routed around by the health fallback.
- Champion and challengers are configured with the same tool list
  (validated when the experiment is saved).

Hard exclusions, the only ones:

- An attachment on the turn is an image and the challenger does not declare
  `supports_image`.

Context tags, derived from conversation state at draw time and stored on the
comparison as a list (a turn can carry several):

- `plain`: no tool context.
- `web_search`: `force_web_search` is true, or the model chose to search
  during the run (set post hoc from the run's tool calls).
- `attachment`: the conversation has indexed attachments.
- `project`: the conversation belongs to a project (RAG over the project).

Fairness notes:

- Retrieval for attachments and projects is model independent. Both models
  receive the same document chunks.
- Web search runs once per model, so sources may differ. This is wanted: it
  measures how well each model drives the search tool with its own queries.
- Tools with persistent side effects (file generation such as the slide deck
  tool, edit in Docs) are stripped from both models in arena mode so the
  losing answer leaves no trace. The comparison records `tools_stripped`.

Challenger sampling: one challenger uniformly at random among the
experiment's healthy challengers, then randomize which side (left or right)
shows the champion. Record the side.

## 6. Data model (app `chat`)

`ArenaExperiment`
- name, description
- is_active (bool). Validation: at most one active experiment.
- champion_model_hrid (defaults to `LLM_DEFAULT_MODEL_HRID`)
- sampling_rate (float 0..1, default 0.10)
- daily_cap_per_user (int, default 1)
- min_votes_for_conclusion (int, default 100; the demo uses 10)
- champion_input_price_eur_per_mtok, champion_output_price_eur_per_mtok
- created_at, updated_at

`ArenaChallenger` (FK experiment, unique per experiment and model)
- model_hrid, chosen from a dropdown (see admin section)
- input_price_eur_per_mtok, output_price_eur_per_mtok (decimal, manual)

`ArenaComparison`
- experiment FK, conversation FK (SET_NULL), user FK (SET_NULL)
- turn (int, index of the user message in the conversation)
- context_tags (JSON list of plain / web_search / attachment / project)
- tools_stripped (bool)
- champion_model_hrid, challenger_model_hrid, champion_side (left / right)
- status: pending / voted / abandoned / errored
- is_seed (bool, set by the seed command, excluded from results on demand)
- winner: champion / challenger / null
- drawn_at, voted_at, time_to_vote_ms
- per model (flat columns prefixed champion_ / challenger_): prompt_tokens,
  completion_tokens, latency_ms, first_token_ms, co2_impact, trace_id,
  finished_at, error (text)
- champion_committed (bool): the champion answer is in the history.
- champion_payload, challenger_payload (JSON): the ui message and pydantic
  messages produced by each model. The champion's is committed as soon as it
  is recorded; the challenger's is held until a vote.
- experiment FK is PROTECT: deleting an experiment that has comparisons is
  refused; deactivate it instead.
- input_snapshot (JSON): the history, summary, current user message and forced
  search flag shared by both candidates. Message attachments are included in
  the frozen user input. Tool searches still run independently.
- conversation_version and per-candidate started_at: guarded turn ownership and
  a single atomic inference claim per candidate.
- price_snapshot (JSON): per-candidate EUR prices per million tokens and capture
  timestamp, recorded at inference claim. Historical results use these values;
  legacy calls without recorded prices are shown as unknown, not repriced.
- closed_reason: distinguishes candidate failures, cancellation, superseded
  turns, deletion and retention expiry from user abandonment.

Snapshots and candidate payloads contain copies of conversation content. Their
retention period is 90 days from the draw. Run
`python manage.py purge_arena_content` daily. The command erases both payloads, the input snapshot,
trace identifiers, free-text fields and the conversation/user associations,
while preserving anonymous outcomes, token counts, timings and recorded prices.
Conversation or user deletion applies the same redaction immediately, including
admin, bulk and cascading deletions. Late workers cannot repopulate erased data.

Cost per answer is computed from recorded token counts and the price snapshot,
with prices divided by one million. The dashboard labels partial subtotals and
shows the number of answers without recorded prices.

## 7. Backend API

Base: existing `ChatViewSet` under `/api/v1.0/chats/{id}/`.

`POST /chats/{id}/arena/draw/`
- Body: `{"force_web_search": bool}`. Eligibility and tags are computed
  server side from conversation state (attachments, project) plus the
  client's web search flag.
- 200 `{"arena": false}` or `{"arena": true, "comparison_id": "..."}`.
  No model name leaves the server: the streaming endpoint derives the model
  from the comparison and the side, so the client never needs a hrid.
- Side effect: creates the `ArenaComparison` in status pending. Before
  creating, auto-resolves any pending comparison on this conversation (see
  auto-resolution).

`POST /chats/{id}/` (existing streaming endpoint), new query params
- `arena_comparison=<id>&arena_side=left|right`
- Behaviour change when present: the agent runs with that side's model, the
  stream is identical, but the persistence step in the pydantic-ai client
  (the block that appends to `messages`, `pydantic_messages` and
  `agent_usage`) is skipped. Instead the ui message, pydantic messages,
  usage, latency, first token time and trace id are stored on the
  comparison for that model. Title generation is skipped for arena turns.
  Side-effect tools are removed from the agent's tool list; if the model
  called web search, the `web_search` tag is added to the comparison.
- Validation: comparison belongs to this conversation and user (404
  otherwise), is pending and the side has not run yet (409 otherwise); both
  parameters must come together (400). `model_hrid` is ignored in arena mode.
- Stop: the server closes the pending comparison immediately with reason
  `cancelled`; the client drops the split and keeps the question on screen.
  Late candidate results cannot commit content after this transition.
- The existing stop-streaming poison pill is per conversation, so one stop
  cancels both streams.

`POST /chats/{id}/arena/{comparison_id}/vote/`
- Body: `{"side": "left" | "right" | "tie" | "both_bad" | null}`.
  `null` explicitly closes without a vote. Initialization never submits it
  merely because a candidate is still running.
- Atomically: append the winner payload to the conversation (messages,
  pydantic_messages, agent_usage merge), set status voted, winner, voted_at,
  time_to_vote_ms. Returns the updated conversation serializer. No model
  names in the response.
- 409 if comparison is not pending or a side has not finished.
- Picking a side that failed keeps the champion answer and closes the
  comparison without a vote; a vote when the other side failed is stored as
  errored, never as a win.

Auto-resolution (no extra endpoint): whenever `post_conversation` or `draw`
runs on a conversation with a pending comparison, the backend commits the
champion payload if it finished (status abandoned), or nothing if the
champion errored (status errored), then proceeds. This covers tab close and
reload.

`ChatConversationSerializer` exposes `pending_arena_comparison`: `null` or
`{"id", "sides_finished": {"left": bool, "right": bool}, "restorable",
"answers"}`. `restorable` is true when both sides succeeded; `answers` then
carries the two ui messages (`{"left", "right"}`, no model name) and the
client rebuilds the split from them. While a restorable comparison is
pending, the serializer drops the trailing assistant message from `messages`:
the committed champion answer is one of the two candidates on screen, not the
conversation's answer yet. A comparison that is not restorable is abandoned
by the client through the vote endpoint.

## 8. Frontend

Location: `src/frontend/apps/conversations/src/features/chat/`. Components
come from `@gouvfr-lasuite/ui-kit` and `@gouvfr-lasuite/cunningham-react`,
already in the app. No new visual language.

- `api/useArenaDraw.ts`: mutation calling draw before send.
- `Chat.tsx`: on send, await draw. If arena, render `ArenaTurn` instead of
  the normal pending assistant message.
- `components/arena/ArenaTurn.tsx`: two `useChat` instances built with the
  existing transport, each with its query params and model hrid. Render the
  last assistant message of each with the existing `MessageItem` in a two
  column grid (stacked under 768px). Disable conversation refetch callbacks
  in arena mode until vote. Labels "Réponse A" / "Réponse B" use the
  existing small caption style. One Cunningham `Button` per column, same
  secondary variant on both sides, enabled when both streams are ready.
- `components/arena/ArenaIntro.tsx`: one sentence above the split, shown
  while `hasSeenArenaIntro` is false in `useChatPreferencesStore`; set to
  true on first render.
- Vote: call vote endpoint, invalidate conversation query, render the
  committed message normally.
- i18n fr/en: "Réponse A", "Réponse B", "Je préfère cette réponse", and the
  intro sentence.
- Reuse existing stop mutation. Hide score, copy, regenerate and sources on
  candidates.

## 9. Admin panel and access

Django admin, same style as the existing singleton and ModelHealth admins.
No ui-kit here; a React admin page is deferred.

Access: the existing `is_staff` flag on the user model already gates the
Django admin site (staff log in with their `admin_email`). Add one Django
permission, `chat.view_arena_results`, granted per user or group by a
superuser, so DINUM devs can see results without being superusers. Editing
experiments uses the standard model permissions.

- `ArenaExperimentAdmin` with `ArenaChallenger` inline. Fields: is_active,
  champion (dropdown of configured active models, default the production
  model), sampling_rate, daily_cap_per_user, champion prices. Link
  "Results" in the changelist.
- Challenger dropdown: lists configured active models from the LLM
  configuration. Also calls the Albert provider's OpenAI-compatible model
  list; Albert models that are not in the configuration appear greyed with
  the hint "add to LLM configuration to use". Runtime creation of model
  configurations from that list is deferred, because the configuration is
  loaded once at startup from JSON and each entry carries prompt, tools and
  provider.
- Validation on save: champion and challengers share the same tool list;
  at most one active experiment.
- `ArenaComparisonAdmin` read-only list with filters on experiment, status,
  challenger, tag, date.

Results view (server-rendered template, one page):
- Header: votes, abandoned, errored, pending, votes / all comparisons,
  votes / closed comparisons (including errors), recorded cost subtotal, and a
  visible "seed data" banner when the experiment contains seeded rows.
- Challenger table: challenger, n votes, win rate against the champion,
  95 percent Wilson interval, always shown; labeled "indicative" when
  n < min_votes_for_conclusion or the interval includes 50%. Sample sufficiency
  and evidence of a preference are separate. Then mean cost per answer for
  challenger and champion on those turns, cost ratio, mean completion tokens, mean latency, mean co2.
- By-context table per challenger: tag, n votes, win rate, interval, same
  "indicative" rule. Tag filter applies to the challenger table too.
- Position check: left win rate across all votes (expect near 50 percent).
- With several challengers the challenger table sorted by win rate is the
  scoreboard. No Bradley-Terry needed since every vote is against the same
  champion.

## 10. Configuration and metrics

- Feature flag `arena` added to the existing `FeatureFlags` model
  (`core/feature_flags/flags.py`), default disabled, driven by the
  `FEATURE_FLAG_ARENA` env var like the other flags, and exposed to the
  frontend through the existing config endpoint so it never calls draw when
  disabled. Every PR below merges to main with the flag off.
- Challengers must be `is_active` models in the LLM configuration, with the
  same tool list as the champion. Add the challenger to `default.json` (or
  the deployment config), reusing the default provider if it is served by
  Albert.
- Prices are entered manually in admin.
- Per model: prompt and completion tokens (existing usage), co2_impact
  (existing Albert co2 handling), latency_ms and first_token_ms (measured in
  the view around the stream), trace_id (from the `trace-<id>` message id),
  error.

## 11. Demo strategy

The feature cannot reach production before the hackathon ends, so the demo
combines two sources, both honest about what they are:

1. **Live flow on the real app.** Rate 100 percent, cap raised, the team and
   a few DINUM devs use the assistant for a day. Expect 30 to 60 real votes:
   enough to show the arena end to end and a first real row, not enough to
   conclude anything.
2. **Seeded dashboard.** A management command
   (`seed_arena_comparisons --experiment <id> --count 300 --challenger-win-rate 0.58`)
   creates synthetic comparisons with realistic token counts, latencies,
   tags and a left/right balance, flagged `is_seed=True`. The results page
   shows a "seed data" banner and lets the reader exclude seeded rows.

Why the threshold defaults to 100 votes: with a 60/40 observed preference,
the 95 percent Wilson interval is about 42 to 76 percent at 30 votes, 50 to
69 percent at 100 votes, 54 to 65 percent at 300 votes. At 100 the result
just separates from a coin flip. The threshold is a label, not a gate, and
is set per experiment.

## 12. Build order (two days, two to three people)

Day 1 morning, backend A: models, migration, admin registration with the
challenger dropdown, draw endpoint with eligibility and tags.
Day 1 morning, backend B: arena params on the streaming endpoint, skip
persistence, strip side-effect tools, store payload and metrics.
Day 1 afternoon, backend: vote endpoint, auto-resolution, serializer field,
results permission.
Day 1 afternoon, frontend: draw mutation, `ArenaTurn` split view with
ui-kit components, intro line, vote.
Day 2 morning, backend: results view, Wilson interval, cost ratio, tag
slices, Albert model list in the dropdown.
Day 2 morning, frontend: error and abandonment paths, mobile stacking, i18n.
Day 2 afternoon: seed command, end to end run with the production model
and one challenger, dogfood at rate 100 percent, demo script.

## 13. Delivery process (contributing guide and handbook)

Sources: `CONTRIBUTING.md`, the La Suite handbook (git and code reviews
chapters), the CI workflow, and the repo's observed habits.

### Issues

- One epic issue labeled `EPIC` and `enhancement`, linking this spec and the
  child issues below. Purpose, proposal, label; no assignee without prior
  agreement (handbook rule).
- One child issue per PR, labeled `backend` or `frontend`. The frontend
  issue also gets `✏️ Needs design`: the arena is a user-facing UX change and
  the repo runs a PO design workflow (Needs design, Design approved, Design
  check). Not blocking for the hackathon, required before production.

### Branches and merging

- Branch from `main`, named `<issue-number>-short-slug` (observed
  convention). Rebase on `main` and force-push; never merge `main` into the
  branch. Merge strategy is rebase and merge, no merge commits.
- No long-lived integration branch. Each PR lands on `main` behind the
  `arena` flag, which is off by default, so partial features are inert.

### Commits

- Format `<gitmoji>(scope) title`, blank line, mandatory body explaining
  why. No capital, no trailing period, imperative. Scopes in use: `back`,
  `front`, `doc`, `ci`. Enforced by gitlint in CI, which also rejects fixup
  commits and any "wip" in a title.
- `git commit --signoff -S`: DCO sign-off and GPG or SSH signature are both
  required.
- Since `main` is rebase-merged, every commit becomes a `main` commit and
  must be green. Aim for one meaningful commit per PR, squash locally before
  requesting review.

### Pull requests

- Under about 500 lines each. Use the PR template: Purpose, Proposal
  checklist, `Closes #<issue>`. Screenshots or a short video for the
  frontend PR.
- Open as a GitHub Draft early (the handbook's WIP prefix would trip
  gitlint). Mark ready when CI is green.
- One changelog line under `## [Unreleased]`, under 80 characters, ending
  with the PR number. CI fails without it unless the PR carries
  `noChangeLog`.
- Before requesting review: `make lint`, `make frontend-lint`, `make test`.
  CI also checks for stray `print` statements and typos (codespell).

### Reviews

- A teammate reviews first, within the day, aiming for under 30 minutes per
  PR. Then request one core team approval, which is required to merge.
- Review the design choices and algorithm, suggest alternatives, stay
  pragmatic. Author fixes with a new commit during review, then squashes
  and force-pushes before merge.

### Tests and docs

- Backend: pytest under `src/backend/chat/tests/`, next to the existing
  view and admin tests, reusing `chat/tests/factories`. Every test has a
  docstring stating the scenario and expected behaviour. Coverage must not
  decrease.
- Frontend: vitest in the `__tests__` folder next to each component.
- Docstring on every module, class and function (pylint).
- Migrations via `make makemigrations`, one per PR at most.
- This spec lives in `docs/`; a short `docs/arena.md` user and admin guide
  ships with the results page PR.

### i18n and accessibility

- UI strings as English source keys through `useTranslation`, like the
  existing components. French arrives through the automated Crowdin PRs.
  If the demo needs French before Crowdin syncs, adding the strings to the
  local translations file is a deviation to clear with maintainers first.
- Handbook accessibility guide applies: unique accessible names on the two
  vote buttons ("Je préfère la réponse A" / "B"), focus moved to the
  committed message when the split collapses, label contrast of at least
  3:1, no information carried by colour alone.

### PR plan

| # | Scope | Title | Depends on | Must test |
|---|---|---|---|---|
| 0 | doc | 📝(doc) add the arena MVP spec | – | – |
| 1 | back | ✨(back) add arena experiment models, admin and feature flag | 0 | one-active validation, same-tool-list validation, flag default off |
| 2 | back | ✨(back) add the arena draw endpoint with eligibility and tags | 1 | each eligibility rule, tag derivation, side randomisation, cap |
| 3 | back | ✨(back) run candidate answers without persisting them | 1 | conversation untouched after stream, payload and metrics stored, side-effect tools stripped |
| 4 | back | ✨(back) add the arena vote endpoint and auto-resolution | 3 | commit of winner, abandoned keeps champion, errored paths, 409s, ownership |
| 5 | front | ✨(front) show two answers on arena turns and record the vote | 2, 4 | draw skipped when flag off, split renders both streams, buttons enable when both ready, vote collapses, intro shown once, stacked layout |
| 6 | back | ✨(back) add the arena results page and its permission | 1 | Wilson interval, indicative label, cost ratio, tag slices, 403 without permission, seed banner |
| 7 | back | 🔧(back) add a command seeding arena comparisons for demos | 1 | count, win rate, left/right balance, is_seed flag |

PRs 2 and 3 run in parallel, then 4; 5 starts on hooks while 4 is in
review; 6 and 7 are independent of 2 to 5.

## 14. Acceptance criteria

- With rate 1.0 and cap 100, every eligible turn shows two streamed answers,
  no model name visible before or after the vote, in either response body
  or UI.
- The intro line appears exactly once per browser.
- Voting commits exactly one assistant message; the next turn works normally
  and uses the champion.
- Reload after both answers arrived, without voting: the split and its vote
  bar come back, the comparison is still pending and the history shows the
  question alone. A new message is refused until a side is picked.
- Reload with only one answer stored: the comparison stays pending and the
  champion's answer remains in history; no automatic abandonment request.
- Killing the challenger provider: errored comparison, champion answer
  committed, no visible error to the user beyond the split collapsing.
- Attachment, project and forced web search turns trigger arena with the
  right context tags; an image attachment with a text-only challenger never
  does.
- A candidate run never creates a generated file or a Docs edit.
- Results page matches a seeded set of comparisons, labels challengers
  under the threshold as indicative, and shows the seed banner.
- Abandoned and errored comparisons never change a win rate.
- Left win rate on seeded random votes lands near 50 percent.
- A staff user without `view_arena_results` gets a 403 on the results page.

## 15. Risks and open points

- **Provider rate limits.** Production runs uvicorn (ASGI) with async
  streaming, so a second stream per user is cheap on the app side. The
  limit that matters is the Albert API rate limit, since arena doubles
  in-flight calls. Sampling rate and daily cap are the knobs. Under the WSGI
  path (gunicorn, used by tests) each stream blocks one worker for the whole
  answer, so keep arena off there.
- **History mismatch.** When the challenger wins, one answer in the history
  was written by a model that does not continue the conversation. Accepted
  for a single-turn probe; it only happens on turns the user preferred.
- **Sample size.** About 100 votes per challenger before any conclusion. At
  a 30 percent vote rate that is roughly 350 arena turns. Each context slice
  needs the same, so slices stay greyed for a while; the global win rate is
  the headline, slices are the follow-up.
- **Search noise.** Two independent web searches per turn may return
  different sources. Wanted: it measures how well each model drives search.
- **Privacy.** Input snapshots and both candidate payloads are retained for at
  most 90 days, with immediate redaction on conversation/user deletion. Anonymous
  metrics remain independent of content and personal associations.
- **Champion drift.** If the health fallback routes new conversations to a
  fallback model, those conversations are not eligible (their pinned model
  is not the champion). The results page should show how many draws were
  refused for that reason.

## 16. Demo script (5 minutes)

1. Admin: the experiment, champion and one challenger from the dropdown,
   rate set to 100 percent for the demo.
2. User: first arena, the intro line, a typical task ("Reformule ce mail
   pour un directeur"), two answers stream, pick one.
3. The conversation continues normally, no names shown.
4. Admin results: challenger win rate, interval, cost ratio, left/right
   check, tag slices.
5. Close: "this is the tool that turns 'we use Mistral because we must'
   into 'we use Mistral because agents prefer it', or not".

## 17. Reliability rollout

Stop or drain existing application workers, run `python manage.py migrate`, and
restart with the updated backend and frontend. Migration `0018_arena_reliability`
closes legacy pending comparisons that lack a frozen input, corrects abandoned
rows with challenger failures, and removes content and user links from legacy
orphan records. Existing conversation answers remain available. Historical prices
cannot be reconstructed and remain unknown. Configure a daily invocation of
`python manage.py purge_arena_content`; it is safe to retry.

Candidate claims, result commits, votes and deletion follow the same lock order:
conversation first, then comparison. A candidate can be claimed only once, and a
result can be recorded only once while the comparison is pending and its turn
version still matches. Champion content and its committed marker are saved in
one transaction. Initialization is read-only; it cannot infer abandonment from
an unfinished stream. Arena self-documentation returns the same neutral payload
for both candidates; legacy tool outputs are anonymized on restore and before
being reused as model history.

Validation on 2026-09-16 used Python 3.14.7, PostgreSQL 16.2 and Node 22.23.2
with the project lockfiles in an isolated temporary environment:

- 78 Arena, self-documentation and migration tests passed, including real
  PostgreSQL concurrent claims/commits, rollback, cancellation, deletion,
  snapshot consistency, tool-stream anonymity, pricing and metric semantics.
- All 444 frontend tests passed. The new handoff regression exercises delayed
  initialization, both candidate streams and a successful explicit vote.
- Production frontend build, affected-file ESLint, Python Ruff checks/formatting
  and `makemigrations --check --dry-run` passed.
- The expanded backend selection passed 165 of 169 tests. Four existing history
  assertions also failed on an unmodified HEAD checkout: the two variants of
  `test_post_conversation_data_protocol_with_history`,
  `test_post_conversation_with_existing_image_history`, and
  `test_post_conversation_with_existing_tool_history`. They expect older CO2 or
  provider metadata and were left outside this repair's scope.

The focused backend selection is reproducible from `src/backend` in the normal
test environment:

```sh
pytest chat/tests/test_arena.py \
  chat/tests/test_arena_admin.py \
  chat/tests/test_arena_migrations.py \
  chat/tests/views/chat/conversations/test_arena.py \
  chat/tests/tools/test_self_documentation.py
```
