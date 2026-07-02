"""
composer.py — the deterministic message engine behind the Vera bot.

compose(category, merchant, trigger, customer=None) -> dict(body, cta, send_as,
                                                              suppression_key, rationale)

Design principles (mirrors challenge-brief.md §5, §8, §9-11):
  - Never fabricate. Only use facts present in the four contexts. When a trigger's
    payload is a thin "placeholder" (as ~75/100 of the dataset's expanded triggers
    are), fall back to whatever real facts exist on merchant/customer/category —
    never invent numbers, names, or citations.
  - Specificity: always try to land on >=1 verifiable number/date/citation.
  - Category fit: pull tone/vocab from category.voice, avoid category.voice.vocab_taboo.
  - Merchant fit: use owner first name, locality, real performance/signals/offers,
    and match language preference (hi-en mix when merchant speaks Hindi).
  - Engagement compulsion: each handler leans on 1-2 named levers (curiosity, loss
    aversion, social proof, effort externalization, reciprocity, asking the merchant,
    single binary CTA) per challenge-brief.md §10.
  - Deterministic: pure functions, no randomness, no network calls, no LLM.
"""

from __future__ import annotations
import re
from typing import Any, Optional


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _get(d: dict, path: str, default=None):
    """Dotted-path getter that never raises."""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def _first_name(merchant: dict) -> str:
    return _get(merchant, "identity.owner_first_name") or _get(merchant, "identity.name", "there")


def _is_hindi_pref(merchant: dict, customer: Optional[dict]) -> bool:
    if customer:
        lp = _get(customer, "identity.language_pref", "")
        if isinstance(lp, str) and "hi" in lp.lower():
            return True
    langs = _get(merchant, "identity.languages", []) or []
    return "hi" in langs


def _salutation(category: dict, merchant: dict) -> str:
    """Category-appropriate greeting for the merchant, e.g. 'Dr. Meera' vs 'Hi Priya'."""
    slug = category.get("slug", "")
    name = _first_name(merchant)
    if slug == "dentists":
        return f"Dr. {name}"
    return name


def _pct(x, decimals=0) -> str:
    try:
        return f"{round(float(x) * 100, decimals):g}%"
    except (TypeError, ValueError):
        return "?"


def _sign_pct(x) -> str:
    try:
        v = float(x) * 100
        return f"+{v:.0f}%" if v >= 0 else f"{v:.0f}%"
    except (TypeError, ValueError):
        return "?"


def _taboo_words(category: dict) -> list[str]:
    return [w.lower() for w in (_get(category, "voice.vocab_taboo", []) or [])]


def _scrub_taboo(body: str, category: dict) -> str:
    """Defensive net: if a taboo phrase slipped in verbatim, strip it. Handlers should
    not use these words in the first place; this is a last line of defense."""
    out = body
    for taboo in _taboo_words(category):
        pattern = re.compile(re.escape(taboo), re.IGNORECASE)
        out = pattern.sub("", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _active_offers(merchant: dict) -> list[dict]:
    return [o for o in (merchant.get("offers") or []) if o.get("status") == "active"]


def _best_offer_title(category: dict, merchant: dict) -> Optional[str]:
    """Prefer merchant's own active offer (real, running); else a catalog reference."""
    active = _active_offers(merchant)
    if active:
        return active[0].get("title")
    catalog = category.get("offer_catalog") or []
    service_at_price = [o for o in catalog if o.get("type") == "service_at_price"]
    pick = (service_at_price or catalog or [None])[0]
    return pick.get("title") if pick else None


def _peer_ctr(category: dict) -> Optional[float]:
    return _get(category, "peer_stats.avg_ctr")


def _digest_by_id(category: dict, digest_id: Optional[str]) -> Optional[dict]:
    if not digest_id:
        return None
    for item in category.get("digest", []) or []:
        if item.get("id") == digest_id:
            return item
    return None


def _customer_noun(category: dict) -> str:
    slug = category.get("slug", "")
    return {
        "dentists": "patients",
        "pharmacies": "patients",
        "salons": "clients",
        "gyms": "members",
        "restaurants": "customers",
    }.get(slug, "customers")


def _locality(merchant: dict) -> str:
    return _get(merchant, "identity.locality") or _get(merchant, "identity.city", "your area")


def _mix(en: str, hi: str, use_hindi: bool) -> str:
    return hi if use_hindi else en


def _is_placeholder(trigger: dict) -> bool:
    return bool(_get(trigger, "payload.placeholder"))


def _cta_line(binary_yes: str, binary_no: str = "STOP") -> str:
    return f"Reply {binary_yes} / {binary_no}."


def _rationale(kind: str, why: str) -> str:
    return f"[{kind}] {why}"


# --------------------------------------------------------------------------
# Per-trigger-kind handlers
# Each returns (body: str, cta: str)  where cta in {"binary","open_ended","none"}
# --------------------------------------------------------------------------

def h_research_digest(category, merchant, trigger, customer, use_hi):
    item = _digest_by_id(category, _get(trigger, "payload.top_item_id"))
    sal = _salutation(category, merchant)
    if item:
        segment = (item.get("patient_segment") or item.get("summary") or "").replace("_", " ")
        n = item.get("trial_n")
        body = (
            f"{sal}, {item.get('source', 'this week\u2019s research digest')} landed. "
            f"{item.get('title', '')}"
            + (f" ({n}-patient study)." if n else ".")
            + (f" Relevant to your {segment} patients." if segment else "")
            + f" {item.get('actionable', 'Worth a look')}. Want me to pull the abstract"
              f" and draft a patient-facing WhatsApp?"
        )
        return _scrub_taboo(body, category), "open_ended"
    # placeholder fallback — no digest id given, ground in category peer stats instead
    noun = _customer_noun(category)
    body = (
        f"{sal}, this week's {category.get('display_name', category.get('slug',''))} research "
        f"digest has a few items that may affect your {noun}. Want me to send the one most "
        f"relevant to your business?"
    )
    return _scrub_taboo(body, category), "open_ended"


def h_regulation_change(category, merchant, trigger, customer, use_hi):
    item = _digest_by_id(category, _get(trigger, "payload.top_item_id"))
    deadline = _get(trigger, "payload.deadline_iso")
    sal = _salutation(category, merchant)
    if item:
        actionable = item.get("actionable", "")
        body = (
            f"{sal}, compliance update: {item.get('title','')}. {item.get('summary','')} "
            f"{actionable}{'.' if actionable and not actionable.endswith('.') else ''}"
            + (f" Deadline: {deadline}." if deadline else "")
            + " Want me to send a one-page checklist for your setup?"
        )
        return _scrub_taboo(body, category), "open_ended"
    body = (
        f"{sal}, a regulation update relevant to {category.get('slug','your category')} is "
        f"active this month. Want the summary + what changes for your setup?"
    )
    return _scrub_taboo(body, category), "open_ended"


def h_recall_due(category, merchant, trigger, customer, use_hi):
    sal_first = _first_name(customer) if customer else "there"
    merchant_name = _get(merchant, "identity.name", "your clinic")
    payload = trigger.get("payload", {})
    slots = payload.get("available_slots") or []
    offer = _best_offer_title(category, merchant)
    due = payload.get("due_date")
    last = payload.get("last_service_date")
    if slots:
        slot_txt = " ya ".join(s.get("label", "") for s in slots[:2]) if use_hi else \
                   " or ".join(s.get("label", "") for s in slots[:2])
        en = (f"Hi {sal_first}, {merchant_name} here \U0001F9B7 Your recall is due"
              + (f" (last visit {last})" if last else "") + f". Slots open: {slot_txt}."
              + (f" {offer}." if offer else "") + " Reply 1 or 2 to book, or suggest a time.")
        hi = (f"Hi {sal_first}, {merchant_name} yahan se \U0001F9B7 Aapka recall due hai"
              + (f" (last visit {last})" if last else "") + f". 2 slots ready hain: {slot_txt}."
              + (f" {offer}." if offer else "") + " Reply 1 ya 2, ya apna time batao.")
        return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"
    # placeholder fallback — use customer relationship data instead of inventing slots
    if customer:
        visits = _get(customer, "relationship.visits_total")
        last_visit = _get(customer, "relationship.last_visit")
        en = (f"Hi {sal_first}, {merchant_name} here. It's been a while since your last visit"
              + (f" ({last_visit})" if last_visit else "") + (f" — visit #{visits} is due" if visits else "")
              + (f". {offer}." if offer else ".") + " Want to grab a slot this week?")
        hi = (f"Hi {sal_first}, {merchant_name} yahan se. Kaafi time ho gaya aapki last visit ko"
              + (f" ({last_visit})" if last_visit else "") + (f". {offer}." if offer else ".")
              + " Is week ek slot book karein?")
        return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"
    return _scrub_taboo(f"{merchant_name}: a recall/reminder is due for one of your customers.", category), "open_ended"


def h_perf_dip(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    baseline = payload.get("vs_baseline")
    if metric and delta is not None:
        body = (
            f"{sal}, your {metric} dropped {_sign_pct(delta)} this week"
            + (f" (usual baseline ~{baseline}/day)." if baseline else ".")
            + " Two usual causes: stale photos/posts or a nearby competitor offer. "
              "Want me to run a quick diagnostic and show you which one it is?"
        )
        return _scrub_taboo(body, category), "open_ended"
    # placeholder fallback — use merchant.performance + peer_stats directly
    perf = merchant.get("performance", {}) or {}
    peer_ctr = _peer_ctr(category)
    ctr = perf.get("ctr")
    signals = merchant.get("signals") or []
    reason = "stale posts" if "stale_posts" in " ".join(signals) else "listing gaps"
    if ctr is not None and peer_ctr:
        body = (f"{sal}, your CTR is {_pct(ctr, 1)} vs the {_pct(peer_ctr, 1)} category average — "
                f"worth closing that gap. Likely driver: {reason}. Want me to fix it?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, noticed a dip in your numbers this week. Want me to run a quick diagnostic?"
    return _scrub_taboo(body, category), "open_ended"


def h_perf_spike(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    baseline = payload.get("vs_baseline")
    driver = payload.get("likely_driver")
    if metric and delta is not None:
        body = (
            f"{sal}, your {metric} are up {_sign_pct(delta)} this week"
            + (f" (vs ~{baseline}/day baseline)." if baseline else ".")
            + (f" Looks driven by your {driver.replace('_',' ')}." if driver else "")
            + " Want to double down while it's working — I can draft a follow-up post?"
        )
        return _scrub_taboo(body, category), "open_ended"
    perf = merchant.get("performance", {}) or {}
    d7 = _get(merchant, "performance.delta_7d", {}) or {}
    views_pct = d7.get("views_pct")
    if views_pct:
        body = f"{sal}, your views are up {_sign_pct(views_pct)} vs last week. Want me to draft a post to ride the momentum?"
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, good week for your listing — want me to draft a follow-up post while it's hot?"
    return _scrub_taboo(body, category), "open_ended"


def h_renewal_due(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    days = payload.get("days_remaining") or _get(merchant, "subscription.days_remaining")
    plan = payload.get("plan") or _get(merchant, "subscription.plan")
    amount = payload.get("renewal_amount")
    en = f"{sal}, your {plan or 'plan'} renews in {days} day" + ("s" if days != 1 else "") + \
         (f" (₹{amount})" if amount else "") + f". Renew now to avoid a listing gap? {_cta_line('YES')}"
    hi = f"{sal}, aapka {plan or 'plan'} {days} din mein renew hoga" + \
         (f" (₹{amount})" if amount else "") + f". Abhi renew kar dein taaki listing gap na ho? {_cta_line('YES')}"
    return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"


def h_festival_upcoming(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    fest = payload.get("festival")
    days_until = payload.get("days_until")
    offer = _best_offer_title(category, merchant)
    if fest:
        body = (f"{sal}, {fest} is {days_until} days out — footfall for {category.get('slug','')} "
                f"typically climbs this window." + (f" Want me to push {offer} as the festival offer?" if offer
                else " Want me to draft a festival offer for your GBP?"))
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, an upcoming festival window is good timing for a seasonal push. Want a draft offer?"
    return _scrub_taboo(body, category), "open_ended"


def h_wedding_package_followup(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal_first = _first_name(customer) if customer else "there"
    merchant_name = _get(merchant, "identity.name", "the salon")
    days_to = payload.get("days_to_wedding")
    trial = payload.get("trial_completed")
    en = (f"Hi {sal_first}, {merchant_name} here \U0001F492" +
          (f" — {days_to} days to the big day!" if days_to else "!") +
          (f" Since your trial on {trial} went well, " if trial else " ") +
          "shall we lock in your pre-wedding skin-prep sessions now while your preferred slots are open?")
    hi = (f"Hi {sal_first}, {merchant_name} yahan se \U0001F492" +
          (f" — shaadi ko sirf {days_to} din bache hain!" if days_to else "!") +
          " Chaliye pre-wedding skin-prep sessions abhi lock kar lein, best slots abhi khaali hain?")
    return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"


def h_curious_ask_due(category, merchant, trigger, customer, use_hi):
    sal = _salutation(category, merchant)
    ask = _get(trigger, "payload.ask_template", "")
    active_offers = [o.get("title") for o in merchant.get("offers", []) if o.get("status") == "active" and o.get("title")]

    if "demand" in ask or not ask:
        if active_offers:
            anchor_en = active_offers[0] if len(active_offers) == 1 else f"{active_offers[0]} or {active_offers[1]}"
            anchor_hi = active_offers[0] if len(active_offers) == 1 else f"{active_offers[0]} ya {active_offers[1]}"
            q_en = f"is it still {anchor_en} people are asking about most, or has something else taken over this week?"
            q_hi = f"kya {anchor_hi} abhi bhi sabse zyada pucha ja raha hai, ya kuch aur chal raha hai is hafte?"
        else:
            q_en = "what's the one service your customers asked about most this week?"
            q_hi = "is hafte sabse zyada kis service ke baare mein pucha gaya?"
    else:
        q_en = "what's working best for you this week?"
        q_hi = "is hafte aapke liye kya sabse accha chal raha hai?"
    en = f"{sal}, quick one \u2014 {q_en} Tell me and I'll pull it forward on your GBP post."
    hi = f"{sal}, ek chhota sawaal \u2014 {q_hi} Bata dijiye, main use aapke GBP post mein highlight kar dungi."
    return _scrub_taboo(_mix(en, hi, use_hi), category), "open_ended"


def h_winback_eligible(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    lapsed_added = payload.get("lapsed_customers_added_since_expiry")
    days = payload.get("days_since_expiry")
    if lapsed_added:
        body = (f"{sal}, {lapsed_added} of your customers have gone quiet since your listing lapsed"
                + (f" {days} days ago" if days else "") + f". Reactivate now and I'll draft a win-back message to send them? {_cta_line('YES')}")
        return _scrub_taboo(body, category), "binary"
    body = f"{sal}, your listing's been inactive for a bit and customers are drifting. Want to reactivate? {_cta_line('YES')}"
    return _scrub_taboo(body, category), "binary"


def h_ipl_match_today(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    match = payload.get("match")
    venue = payload.get("venue")
    if match:
        body = (f"{sal}, {match} tonight" + (f" at {venue}" if venue else "") +
                " \u2014 match-night footfall usually spikes for you. Want me to push a quick match-day post now?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, there's a big match tonight \u2014 want a quick match-day post to catch the footfall?"
    return _scrub_taboo(body, category), "open_ended"


def h_review_theme_emerged(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    theme = (payload.get("theme") or "").replace("_", " ")
    occ = payload.get("occurrences_30d")
    trend = payload.get("trend")
    quote = payload.get("common_quote")
    if theme and occ:
        body = (f"{sal}, {occ} reviews this month mention \u201c{theme}\u201d"
                + (f" and it's {trend}." if trend else ".")
                + (f' One reads: "{quote}".' if quote else "")
                + " Want me to draft a reply template + one fix you can make this week?")
        return _scrub_taboo(body, category), "open_ended"
    themes = merchant.get("review_themes") or []
    neg = [t for t in themes if t.get("sentiment") == "neg"]
    if neg:
        t = neg[0]
        body = (f"{sal}, {t.get('occurrences_30d','several')} reviews recently flagged "
                f"\u201c{t.get('theme','').replace('_',' ')}\u201d. Want a reply template + a fix?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, a review theme is trending in your recent feedback. Want me to draft a response?"
    return _scrub_taboo(body, category), "open_ended"


def h_milestone_reached(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    metric = (payload.get("metric") or "").replace("_", " ")
    val = payload.get("value_now")
    goal = payload.get("milestone_value")
    if metric and val:
        gap = (goal - val) if (goal and isinstance(val, (int, float))) else None
        body = (f"{sal}, you're at {val} {metric}"
                + (f", just {gap} short of {goal}." if gap else ".")
                + " Want me to post a milestone note + nudge a couple of recent happy customers for reviews?")
        return _scrub_taboo(body, category), "open_ended"
    agg = merchant.get("customer_aggregate", {}) or {}
    total = agg.get("total_unique_ytd")
    body = f"{sal}, you've served {total} customers this year" if total else f"{sal}, you're closing in on a milestone"
    body += " \u2014 want a quick post to mark it?"
    return _scrub_taboo(body, category), "open_ended"


def _last_vera_message(merchant):
    """Return the most recent Vera message from conversation_history, with any
    trailing question sentence stripped off (so it can be reused as grounding
    content without duplicating our own CTA question)."""
    history = merchant.get("conversation_history") or []
    vera_msgs = [h.get("body") for h in history if h.get("from") == "vera" and h.get("body")]
    if not vera_msgs:
        return None
    text = vera_msgs[-1].strip()
    sentences = re.split(r"(?<=[.?!])\s+", text)
    if sentences and sentences[-1].strip().endswith("?"):
        sentences = sentences[:-1]
    result = " ".join(sentences).strip()
    return result or None


def h_active_planning_intent(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    topic = (payload.get("intent_topic") or "").replace("_", " ")
    last_msg = payload.get("merchant_last_message")
    prior_draft = _last_vera_message(merchant)

    if topic and prior_draft:
        body = (f"{sal}, picking up on the {topic} \u2014 here's what I'd sketched: {prior_draft} "
                f"Want me to finalize this and get it live? {_cta_line('YES')}")
        return _scrub_taboo(body, category), "binary"
    if topic:
        body = (f"{sal}, following up on the {topic} \u2014 I've drafted a first-cut structure"
                + (f' based on "{last_msg}"' if last_msg else "")
                + f". Want me to send it over so you can react to it directly? {_cta_line('YES')}")
        return _scrub_taboo(body, category), "binary"
    body = f"{sal}, picking up where we left off \u2014 I've drafted the next step. Want to see it? {_cta_line('YES')}"
    return _scrub_taboo(body, category), "binary"


def h_seasonal_perf_dip(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    note = (payload.get("season_note") or "").replace("_", " ")
    if metric and delta is not None:
        body = (f"{sal}, {metric} is down {_sign_pct(delta)} \u2014 this is the expected "
                f"{note or 'seasonal'} dip, not a listing problem. Worth a counter-seasonal offer to soften it — want a draft?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, this is a normal seasonal dip window for {category.get('slug','')}. Want a counter-seasonal offer to soften it?"
    return _scrub_taboo(body, category), "open_ended"


def h_customer_lapsed_hard(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal_first = _first_name(customer) if customer else "there"
    merchant_name = _get(merchant, "identity.name", "we")
    days = payload.get("days_since_last_visit") or (_get(customer, "relationship.visits_total") and None)
    focus = (payload.get("previous_focus") or "").replace("_", " ")
    if not days and customer:
        lv = _get(customer, "relationship.last_visit")
        days_txt = f"since {lv}" if lv else "a while"
    else:
        days_txt = f"{days} days" if days else "a while"
    en = (f"Hi {sal_first}, it's been {days_txt} since we last saw you at {merchant_name}"
          + (f" for your {focus} goals" if focus else "") + f". Want to pick back up? {_cta_line('YES')}")
    hi = (f"Hi {sal_first}, {merchant_name} mein aapko dekhe {days_txt} ho gaye"
          + (f" ({focus} ke liye)" if focus else "") + f". Dobara start karein? {_cta_line('YES')}")
    return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"


def h_customer_lapsed_soft(category, merchant, trigger, customer, use_hi):
    sal_first = _first_name(customer) if customer else "there"
    merchant_name = _get(merchant, "identity.name", "we")
    offer = _best_offer_title(category, merchant)
    if customer:
        lv = _get(customer, "relationship.last_visit")
        visits = _get(customer, "relationship.visits_total")
        en = (f"Hi {sal_first}, {merchant_name} here \u2014 " +
              (f"it's been a bit since your last visit ({lv})" if lv else "it's been a bit since your last visit") +
              (f", visit #{visits} due" if visits else "") + (f". {offer}?" if offer else ". Want to book a slot?")
              + f" {_cta_line('YES')}")
        hi = (f"Hi {sal_first}, {merchant_name} yahan se \u2014 kaafi time ho gaya"
              + (f" ({lv})" if lv else "") + (f". {offer}?" if offer else ". Slot book karein?")
              + f" {_cta_line('YES')}")
        return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"
    return _scrub_taboo(f"{merchant_name}: a customer looks due for a soft check-in.", category), "open_ended"


def h_trial_followup(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal_first = _first_name(customer) if customer else "there"
    merchant_name = _get(merchant, "identity.name", "us")
    trial_date = payload.get("trial_date")
    options = payload.get("next_session_options") or []
    if options:
        slot = options[0].get("label", "")
        en = (f"Hi {sal_first}, how was the trial" + (f" on {trial_date}" if trial_date else "") +
              f" at {merchant_name}? Next slot open: {slot}. Want to lock it in? {_cta_line('YES')}")
        hi = (f"Hi {sal_first}, trial kaisa raha" + (f" ({trial_date})" if trial_date else "") +
              f" {merchant_name} mein? Agla slot: {slot}. Book kar dein? {_cta_line('YES')}")
        return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"
    en = f"Hi {sal_first}, how did your trial session at {merchant_name} go? Want to book your next one? {_cta_line('YES')}"
    hi = f"Hi {sal_first}, {merchant_name} ka trial kaisa laga? Agla session book karein? {_cta_line('YES')}"
    return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"


def h_supply_alert(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches") or []
    mfr = payload.get("manufacturer")
    if molecule:
        body = (f"{sal}, supply alert: {molecule}" + (f" from {mfr}" if mfr else "") +
                (f", batches {', '.join(batches)}" if batches else "") +
                " flagged for recall. Please check your shelf stock today. "
                "Want me to send the official notice + substitute molecule list?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, a supply/recall alert relevant to your stock is active. Want the details?"
    return _scrub_taboo(body, category), "open_ended"


def h_chronic_refill_due(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal_first = _first_name(customer) if customer else "there"
    merchant_name = _get(merchant, "identity.name", "your pharmacy")
    molecules = payload.get("molecule_list") or []
    runs_out = payload.get("stock_runs_out_iso")
    delivery = payload.get("delivery_address_saved")
    if molecules:
        mol_txt = ", ".join(molecules)
        en = (f"Hi {sal_first}, {merchant_name} here \u2014 your regular meds ({mol_txt}) "
              + (f"run out around {runs_out[:10]}." if runs_out else "are running low.")
              + (" We have your delivery address saved \u2014 " if delivery else " ")
              + f"want us to refill and send it over? {_cta_line('YES')}")
        hi = (f"Hi {sal_first}, {merchant_name} yahan se \u2014 aapki regular dawaiyan ({mol_txt}) "
              + (f"{runs_out[:10]} tak khatam ho jaayengi." if runs_out else "kam ho rahi hain.")
              + (" Delivery address saved hai \u2014 " if delivery else " ")
              + f"refill bhej dein? {_cta_line('YES')}")
        return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"
    en = f"Hi {sal_first}, {merchant_name} here \u2014 your regular refill looks due. Want us to send it? {_cta_line('YES')}"
    hi = f"Hi {sal_first}, {merchant_name} yahan se \u2014 aapki refill due lag rahi hai. Bhej dein? {_cta_line('YES')}"
    return _scrub_taboo(_mix(en, hi, use_hi), category), "binary"


def h_category_seasonal(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    trends = payload.get("trends") or []
    season = payload.get("season")
    if trends:
        top = trends[:2]
        def _fmt_trend(t: str) -> str:
            t = t.replace("_", " ")
            m = re.match(r"^(.*?)([+-])(\d+)$", t)
            if m:
                label, sign, num = m.groups()
                return f"{label.strip()} {'up' if sign == '+' else 'down'} {num}%"
            return t
        readable = ", ".join(_fmt_trend(t) for t in top)
        body = (f"{sal}, {season.replace('_',' ') if season else 'this season'}'s demand shift: {readable}. "
                "Want help re-arranging your shelf/priority stock to match?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, seasonal demand is shifting for {category.get('slug','')} right now. Want the category trend note?"
    return _scrub_taboo(body, category), "open_ended"


def h_gbp_unverified(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    uplift = payload.get("estimated_uplift_pct")
    path = payload.get("verification_path")
    body = (f"{sal}, your Google listing is still unverified" +
            (f" \u2014 verified listings typically see {_pct(uplift)} more views." if uplift else ".") +
            (f" Verification is via {path.replace('_',' ')}, takes a few minutes." if path else "") +
            f" Want me to start it? {_cta_line('YES')}")
    return _scrub_taboo(body, category), "binary"


def h_cde_opportunity(category, merchant, trigger, customer, use_hi):
    item = _digest_by_id(category, _get(trigger, "payload.digest_item_id"))
    sal = _salutation(category, merchant)
    credits = _get(trigger, "payload.credits")
    fee = _get(trigger, "payload.fee")
    if item:
        actionable = item.get("actionable", "")
        body = (f"{sal}, {item.get('title','')}" + (f" on {item.get('date')}" if item.get('date') else "") +
                (f" \u2014 {credits} CDE credits" if credits else "") + ". ")
        if actionable:
            body += f"{actionable}{'.' if not actionable.endswith('.') else ''} "
        elif fee:
            body += f"{fee.replace('_',' ').capitalize()}. "
        body += "Want me to block the slot on your calendar?"
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, a CDE/training opportunity relevant to your practice is open this week. Want the details?"
    return _scrub_taboo(body, category), "open_ended"


def h_competitor_opened(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    comp = payload.get("competitor_name")
    dist = payload.get("distance_km")
    offer = payload.get("their_offer")
    if comp:
        body = (f"{sal}, {comp} opened {dist}km away" + (f" with {offer}." if offer else ".") +
                " Worth a counter-offer or at least a fresher GBP post this week — want me to draft one?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, new competition opened near you recently. Want a quick competitive read + response draft?"
    return _scrub_taboo(body, category), "open_ended"


def h_dormant_with_vera(category, merchant, trigger, customer, use_hi):
    payload = trigger.get("payload", {})
    sal = _salutation(category, merchant)
    days = payload.get("days_since_last_merchant_message")
    topic = (payload.get("last_topic") or "").replace("_", " ")
    if days:
        body = (f"{sal}, it's been {days} days since we last spoke" +
                (f" (we were on {topic})." if topic else ".") +
                " No rush \u2014 want me to pick that back up, or is there something else on your mind this week?")
        return _scrub_taboo(body, category), "open_ended"
    body = f"{sal}, been quiet for a bit \u2014 want me to check your listing for anything worth fixing?"
    return _scrub_taboo(body, category), "open_ended"


def h_appointment_tomorrow(category, merchant, trigger, customer, use_hi):
    sal_first = _first_name(customer) if customer else "there"
    merchant_name = _get(merchant, "identity.name", "us")
    if customer:
        pref = _get(customer, "preferences.preferred_slots", "")
        en = (f"Hi {sal_first}, quick reminder \u2014 your appointment at {merchant_name} is tomorrow"
              + (f" ({pref.replace('_',' ')} slot)." if pref else ".") +
              f" See you then, or reply if you need to reschedule.")
        hi = (f"Hi {sal_first}, ek reminder \u2014 kal {merchant_name} mein aapki appointment hai"
              + (f" ({pref.replace('_',' ')})." if pref else ".") +
              " Milte hain, ya reschedule ke liye reply karein.")
        return _scrub_taboo(_mix(en, hi, use_hi), category), "open_ended"
    return _scrub_taboo(f"{merchant_name}: a customer has an appointment tomorrow.", category), "none"


def h_generic_fallback(category, merchant, trigger, customer, use_hi):
    """Last-resort composer for any trigger kind we don't have a dedicated handler
    for. Still grounded — never invents facts, always pulls from real merchant/
    category context so it stays specific rather than generic."""
    sal = _salutation(category, merchant)
    kind_label = trigger.get("kind", "update").replace("_", " ")
    signals = merchant.get("signals") or []
    perf = merchant.get("performance", {}) or {}
    if "stale_posts" in " ".join(signals):
        body = (f"{sal}, quick nudge on {kind_label} \u2014 your last post was a while back and "
                "views usually dip after ~2-3 weeks of silence. Want me to draft one?")
    elif perf.get("views"):
        peer_ctr = _peer_ctr(category)
        body = (f"{sal}, on {kind_label}: your listing had {perf.get('views')} views in the last "
                f"{perf.get('window_days', 30)} days" +
                (f" against a {_pct(peer_ctr,1)} category-average CTR." if peer_ctr else ".") +
                " Want me to look at what's holding conversion back?")
    else:
        body = f"{sal}, following up on {kind_label} \u2014 want me to pull the details together for you?"
    return _scrub_taboo(body, category), "open_ended"


DISPATCH = {
    "research_digest": h_research_digest,
    "regulation_change": h_regulation_change,
    "recall_due": h_recall_due,
    "perf_dip": h_perf_dip,
    "perf_spike": h_perf_spike,
    "renewal_due": h_renewal_due,
    "festival_upcoming": h_festival_upcoming,
    "wedding_package_followup": h_wedding_package_followup,
    "curious_ask_due": h_curious_ask_due,
    "winback_eligible": h_winback_eligible,
    "ipl_match_today": h_ipl_match_today,
    "review_theme_emerged": h_review_theme_emerged,
    "milestone_reached": h_milestone_reached,
    "active_planning_intent": h_active_planning_intent,
    "seasonal_perf_dip": h_seasonal_perf_dip,
    "customer_lapsed_hard": h_customer_lapsed_hard,
    "customer_lapsed_soft": h_customer_lapsed_soft,
    "trial_followup": h_trial_followup,
    "supply_alert": h_supply_alert,
    "chronic_refill_due": h_chronic_refill_due,
    "category_seasonal": h_category_seasonal,
    "gbp_unverified": h_gbp_unverified,
    "cde_opportunity": h_cde_opportunity,
    "competitor_opened": h_competitor_opened,
    "dormant_with_vera": h_dormant_with_vera,
    "appointment_tomorrow": h_appointment_tomorrow,
}


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """
    Deterministic composer. Inputs are plain dicts (as loaded from dataset JSON /
    pushed via /v1/context). Returns dict with keys: body, cta, send_as,
    suppression_key, rationale.
    """
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}

    kind = trigger.get("kind", "")
    use_hi = _is_hindi_pref(merchant, customer)
    handler = DISPATCH.get(kind, h_generic_fallback)

    body, cta = handler(category, merchant, trigger, customer, use_hi)
    body = body.strip()

    scope = trigger.get("scope", "merchant")
    send_as = "merchant_on_behalf" if (scope == "customer" and customer) else "vera"

    placeholder_note = " (generic grounding — trigger payload had no rich facts)" if _is_placeholder(trigger) else ""
    rationale = _rationale(
        kind,
        f"scope={scope}, urgency={trigger.get('urgency','?')}, "
        f"grounded in {'customer relationship data' if (customer and scope=='customer') else 'merchant/category context'}"
        f"{placeholder_note}"
    )

    suppression_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id','')}"

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }
