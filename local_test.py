import json, glob, requests, sys, subprocess
from pathlib import Path

BASE = "https://vera-bot-1-48g7.onrender.com"

HERE = Path(__file__).resolve().parent
DATA_PATH = HERE / "dataset" / "expanded"
SEED_DIR = HERE / "dataset"
GENERATOR = HERE / "dataset" / "generate_dataset.py"

if not DATA_PATH.exists():
    print(f"Expanded dataset not found at {DATA_PATH}, generating it now...")
    subprocess.run(
        [sys.executable, str(GENERATOR), "--seed-dir", str(SEED_DIR), "--out", str(DATA_PATH)],
        check=True,
    )

DATA = str(DATA_PATH)

def push_all():
    for f in glob.glob(f"{DATA}/categories/*.json"):
        d = json.load(open(f))
        r = requests.post(f"{BASE}/v1/context", json={
            "scope": "category", "context_id": d["slug"], "version": 1,
            "payload": d, "delivered_at": "2026-07-02T00:00:00Z"})
        assert r.json()["accepted"], r.text

    for f in glob.glob(f"{DATA}/merchants/*.json"):
        d = json.load(open(f))
        r = requests.post(f"{BASE}/v1/context", json={
            "scope": "merchant", "context_id": d["merchant_id"], "version": 1,
            "payload": d, "delivered_at": "2026-07-02T00:00:00Z"})
        assert r.json()["accepted"], r.text

    for f in glob.glob(f"{DATA}/customers/*.json"):
        d = json.load(open(f))
        r = requests.post(f"{BASE}/v1/context", json={
            "scope": "customer", "context_id": d["customer_id"], "version": 1,
            "payload": d, "delivered_at": "2026-07-02T00:00:00Z"})
        assert r.json()["accepted"], r.text

    trig_ids = []
    for f in glob.glob(f"{DATA}/triggers/*.json"):
        d = json.load(open(f))
        trig_ids.append(d["id"])
        r = requests.post(f"{BASE}/v1/context", json={
            "scope": "trigger", "context_id": d["id"], "version": 1,
            "payload": d, "delivered_at": "2026-07-02T00:00:00Z"})
        assert r.json()["accepted"], r.text
    return trig_ids

def run_all_ticks(trig_ids):
    all_actions = []
    for i in range(0, len(trig_ids), 5):
        batch = trig_ids[i:i+5]
        r = requests.post(f"{BASE}/v1/tick", json={"now": "2026-07-02T10:00:00Z", "available_triggers": batch})
        r.raise_for_status()
        actions = r.json()["actions"]
        all_actions.extend(actions)
    return all_actions

def sanity_checks(actions):
    problems = []
    seen_bodies = set()
    for a in actions:
        body = a["body"]
        if not body or len(body) < 10:
            problems.append(f"EMPTY/short body: {a}")
        if body in seen_bodies:
            problems.append(f"DUPLICATE body across actions: {body[:60]}")
        seen_bodies.add(body)
        if a["cta"] not in ("binary", "open_ended", "none"):
            problems.append(f"BAD cta value: {a['cta']}")
        if a["send_as"] not in ("vera", "merchant_on_behalf"):
            problems.append(f"BAD send_as: {a['send_as']}")
        if not a.get("suppression_key"):
            problems.append(f"MISSING suppression_key: {a}")
        if not a.get("rationale"):
            problems.append(f"MISSING rationale: {a}")
    return problems

def test_conversation_scenarios():
    print("\n--- auto-reply detection ---")
    auto_msg = "Thank you for contacting us! Our team will respond shortly."
    mid = "m_001_drmeera_dentist_delhi"
    ended = False
    for i in range(1, 5):
        r = requests.post(f"{BASE}/v1/reply", json={
            "conversation_id": "conv_auto_test", "merchant_id": mid, "customer_id": None,
            "from_role": "merchant", "message": auto_msg, "received_at": "2026-07-02T10:00:00Z",
            "turn_number": i + 1})
        d = r.json()
        print(f"turn {i}: action={d['action']} body={d.get('body','')[:60]!r}")
        if d["action"] == "end":
            ended = True
            break
    assert ended, "Bot never ended on repeated auto-reply!"
    print("PASS: bot ended after repeated auto-reply")

    print("\n--- intent transition ---")
    r = requests.post(f"{BASE}/v1/reply", json={
        "conversation_id": "conv_intent_test", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Ok lets do it. Whats next?",
        "received_at": "2026-07-02T10:00:00Z", "turn_number": 2})
    d = r.json()
    print(f"action={d['action']} body={d.get('body','')!r}")
    body_lower = d.get("body", "").lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    assert not any(w in body_lower for w in qualifying), "Bot re-qualified after commitment!"
    print("PASS: bot switched to action mode, did not re-qualify")

    print("\n--- hostile handling ---")
    r = requests.post(f"{BASE}/v1/reply", json={
        "conversation_id": "conv_hostile_test", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-07-02T10:00:00Z", "turn_number": 2})
    d = r.json()
    print(f"action={d['action']} body={d.get('body','')!r}")
    assert d["action"] == "end" or "sorry" in d.get("body", "").lower()
    print("PASS: bot handled hostility gracefully")

if __name__ == "__main__":
    trig_ids = push_all()
    print(f"Pushed dataset. {len(trig_ids)} triggers available.")
    actions = run_all_ticks(trig_ids)
    print(f"\n{len(actions)} actions generated from {len(trig_ids)} triggers.")
    problems = sanity_checks(actions)
    if problems:
        print(f"\n{len(problems)} PROBLEMS FOUND:")
        for p in problems[:30]:
            print(" -", p)
    else:
        print("\nNo structural problems found.")

    print("\n=== Sample outputs ===")
    for a in actions[:6]:
        print(f"\n[{a['trigger_id']}] send_as={a['send_as']} cta={a['cta']}")
        print(" ", a["body"])
        print("  rationale:", a["rationale"])

    test_conversation_scenarios()

    if problems:
        sys.exit(1)
    print("\nALL CHECKS PASSED")
