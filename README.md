# Vera Bot — Submission README

## Approach

The composer is a **deterministic, rule-based message engine** — no LLM call in
the hot path. For each `trigger.kind` there's a dedicated handler (`composer.py`,
`DISPATCH` table) that pulls real facts out of the four context layers
(category, merchant, trigger, customer) and slots them into a category-voiced
template. A generic fallback handler covers any trigger kind without a
dedicated handler, still grounded only in real merchant/category/customer data.

**Why no LLM in the hot path:**
- The brief requires strict determinism ("same output for same input") and a
  30s budget — a rule-based engine is instant and never flaky.
- The hardest constraint in the brief is "don't fabricate." An LLM without
  retrieval/validation risks inventing numbers or citations; hand-written
  templates that only interpolate real fields structurally can't fabricate.
- ~75 of the 100 dataset triggers are auto-expanded "placeholder" triggers
  with no rich payload — the interesting design problem was building strong
  fallbacks that still ground in real merchant/customer/category data (e.g.
  `customer.relationship.last_visit`, `merchant.performance`,
  `category.peer_stats`) rather than writing something generic.

## What's implemented

- All 5 endpoints (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`,
  `/v1/metadata`), plus an optional `/v1/teardown`.
- `/v1/context` is idempotent by `(scope, context_id, version)` per spec.
- 26 trigger-kind-specific composers + a grounded generic fallback.
- Category-voice awareness: taboo-word scrubbing per category, category-
  appropriate customer noun (patients/clients/members/customers), Hindi-
  English code-mix triggered off `merchant.identity.languages` /
  `customer.identity.language_pref`.
- Anti-repetition: per-conversation (never resend the exact same body) and
  per-merchant across triggers within a tick (skip if an identical message
  already went out to that merchant this run).
- Restraint: `/v1/tick` returns `{"actions": []}` for triggers already covered
  by `suppression_key`, and doesn't spam a merchant with duplicate facts.
- Multi-turn handling (`conversation.py`):
  - **Auto-reply detection** — tries once for a human on the first canned
    reply (matches Pattern B in the brief), ends the conversation on the
    second repeat.
  - **Intent transition** — explicit commitment language ("let's do it",
    "go ahead") routes straight to action mode, never back to qualifying
    questions (avoids the Pattern D failure called out in the brief).
  - **Hostile/opt-out handling** — one graceful apology, then exits for good
    if the person persists.
  - **Soft backoff** — "not now" / "call me later" returns `wait` with a
    30-minute delay instead of pushing again immediately.

## Tradeoffs

- **No LLM** trades away some naturalness/variety (a frontier model would
  write punchier, more varied prose) for guaranteed determinism, zero
  hallucination risk, and near-zero latency. If I had more time, I'd add an
  LLM as a *rewriting* pass over the deterministic draft — same facts, more
  natural phrasing — with a validator that rejects any rewrite introducing a
  fact not present in the draft.
- **Generic fallback quality** depends entirely on what's actually populated
  on the merchant/customer records. For the ~75 placeholder triggers, the bot
  is only as specific as the underlying merchant/customer JSON allows — it
  will never invent a number to fill the gap.
- **Multi-turn state is in-memory**, per the spec ("storing in memory is
  fine"). It does not persist across a real restart; `/v1/teardown` wipes it
  intentionally at test end.

## What additional context would have helped most

- A merchant-level "preferred send hour" would let the bot avoid buzzing
  merchants at bad times.
- More granular customer `preferences` (e.g. actual next-available slot per
  merchant, not just a day-part preference) would improve the
  `appointment_tomorrow` / `recall_due` fallback paths, which currently can't
  offer a specific time when the trigger payload is a placeholder.

## Running locally

```bash
pip install fastapi "uvicorn[standard]"
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then point `judge_simulator.py`'s `BOT_URL` at `http://localhost:8080` (or
your deployed public URL) and run it.

## Files

- `composer.py` — the deterministic message engine (`compose()`)
- `conversation.py` — multi-turn reply logic (`next_move()`)
- `bot.py` — FastAPI server wiring the 5 endpoints together
- `submission.jsonl` — the 30 canonical test-pair outputs
- `local_test.py` — a local smoke test that pushes the full expanded
  dataset, runs every trigger through `/v1/tick`, and exercises the
  auto-reply / intent / hostile scenarios from `judge_simulator.py`
