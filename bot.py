"""
bot.py — Vera message-engine bot server.

Implements the 5 endpoints required by challenge-testing-brief.md:
  POST /v1/context   — idempotent context push (category/merchant/customer/trigger)
  POST /v1/tick      — periodic wake-up; bot may proactively send messages
  POST /v1/reply     — respond to a merchant/customer reply, synchronously
  GET  /v1/healthz   — liveness probe
  GET  /v1/metadata  — bot identity

Run:
    uvicorn bot:app --host 0.0.0.0 --port 8080

State is in-memory (per brief §2.1: "Storing in memory is fine; just don't
restart between calls"). A POST /v1/teardown is also implemented (optional
per testing brief §11) to wipe state at the end of a test.
"""

from __future__ import annotations
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from composer import compose
from conversation import next_move

app = FastAPI(title="Vera Bot")
START = time.time()

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> conversation state
conversations: dict[str, dict] = {}

# suppression_key -> True, once sent this test run (dedup across ticks)
sent_suppression_keys: set[str] = set()

# (merchant_id, trigger_id) -> conversation_id, to avoid double-starting the same trigger
trigger_conversations: dict[tuple[str, Optional[str]], str] = {}

# merchant_id -> set of message bodies already sent this test run (anti-repetition
# across different triggers that happen to compose to the same text, e.g. two
# milestone triggers on the same merchant with the same underlying fact)
merchant_sent_bodies: dict[str, set[str]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_ctx(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


# ---------------------------------------------------------------------------
# GET /v1/healthz, /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vanshika",
        "team_members": ["Vanshika"],
        "model": "rule-based-deterministic-v1 (no external LLM call; see README)",
        "approach": (
            "Deterministic per-trigger-kind composer grounded in the four context "
            "layers (category/merchant/trigger/customer). No LLM in the hot path, "
            "which guarantees determinism and sub-second latency and avoids "
            "hallucination risk on the 'don't fabricate' constraint. See README.md."
        ),
        "contact_email": "vanshikagarg.20744@gmail.com",
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)

    if cur and cur["version"] == body.version:
        # Idempotent replay: same (scope, context_id, version) seen before — succeed without re-processing.
        return {
            "accepted": True,
            "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": _now_iso(),
            "idempotent_replay": True,
        }

    if cur and cur["version"] > body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


@app.post("/v1/teardown")
async def teardown():
    """Optional per testing-brief §11: wipe state at end of test."""
    contexts.clear()
    conversations.clear()
    sent_suppression_keys.clear()
    trigger_conversations.clear()
    merchant_sent_bodies.clear()
    return {"accepted": True}


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    for trg_id in body.available_triggers:
        trigger = _get_ctx("trigger", trg_id)
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = _get_ctx("merchant", merchant_id)
        if not merchant:
            continue

        category_slug = merchant.get("category_slug")
        category = _get_ctx("category", category_slug)
        if not category:
            continue

        customer_id = trigger.get("customer_id")
        customer = _get_ctx("customer", customer_id) if customer_id else None

        # Restraint: don't resend something already sent for this suppression key
        composed = compose(category, merchant, trigger, customer)
        supp_key = composed["suppression_key"]
        if supp_key in sent_suppression_keys:
            continue

        # One action per (merchant_id, conversation_id) per tick; don't restart
        # an existing conversation for the same trigger, start a fresh one.
        conv_key = (merchant_id, trg_id)
        if conv_key in trigger_conversations:
            continue

        # Anti-repetition: don't send the same message body twice to the same
        # merchant even if two different triggers happened to compose identically
        # (e.g. two generic-fallback triggers grounded in the same underlying fact).
        if composed["body"] in merchant_sent_bodies.get(merchant_id, set()):
            continue

        conversation_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:6]}"
        trigger_conversations[conv_key] = conversation_id
        sent_suppression_keys.add(supp_key)
        merchant_sent_bodies.setdefault(merchant_id, set()).add(composed["body"])

        conversations[conversation_id] = {
            "history": [{"from": "vera", "body": composed["body"]}],
            "auto_reply_streak": 0,
            "sent_bodies": {composed["body"]},
            "merchant_name": merchant.get("identity", {}).get("name", "there"),
            "last_topic": trigger.get("kind", "this"),
            "merchant_id": merchant_id,
            "customer_id": customer_id,
        }

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind','generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", ""), trigger.get("kind", "")],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": supp_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.get(body.conversation_id)
    if state is None:
        # Judge may start a conversation directly via /v1/reply without a prior
        # tick (e.g. auto-reply / hostile / intent test scenarios). Build minimal
        # state from whatever context we have.
        merchant = _get_ctx("merchant", body.merchant_id) if body.merchant_id else None
        state = {
            "history": [],
            "auto_reply_streak": 0,
            "sent_bodies": set(),
            "merchant_name": (merchant or {}).get("identity", {}).get("name", "there"),
            "last_topic": "this",
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
        }
        conversations[body.conversation_id] = state

    state["history"].append({"from": body.from_role, "body": body.message})

    from conversation import is_auto_reply
    if is_auto_reply(body.message):
        state["auto_reply_streak"] = state.get("auto_reply_streak", 0) + 1
    else:
        state["auto_reply_streak"] = 0

    result = next_move(state, body.message)

    if result["action"] == "send":
        out_body = result["body"]
        # Anti-repetition: never resend the exact same body verbatim
        if out_body in state.get("sent_bodies", set()):
            out_body = out_body.rstrip(".") + " — following up on this."
        state.setdefault("sent_bodies", set()).add(out_body)
        state["history"].append({"from": "vera", "body": out_body})
        return {"action": "send", "body": out_body, "cta": result.get("cta", "open_ended"),
                "rationale": result["rationale"]}

    if result["action"] == "wait":
        return {"action": "wait", "wait_seconds": result.get("wait_seconds", 1800),
                "rationale": result["rationale"]}

    return {"action": "end", "rationale": result["rationale"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
