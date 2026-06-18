"""
patch_slack.py — Add Slack availability ping on handoff.
When the agent triggers a handoff, Slack gets a message:
"Hot lead ready for callback. Who can call them back?"
Team reacts or replies to claim it.
Run from your InteractiveChat project root.
"""
import sys, os

if not os.path.exists("api.py"):
    print("ERROR: api.py not found.")
    sys.exit(1)

with open("api.py", "r", encoding="utf-8") as f:
    code = f.read()

changes = 0

# ============================================================
# FIX 1: Add _slack_availability_ping function
# Posts a clean "who's free?" message with prospect details
# ============================================================

if "_slack_availability_ping" not in code:
    # Insert after the SIGNAL_LABELS dict
    anchor = """class NotifyHumanRequest(BaseModel):"""

    slack_func = '''

async def _slack_availability_ping(contact: dict, customer: dict, signal_label: str, last_msg: str):
    """Ping Slack: hot lead, who can call them back? First responder claims it."""
    if not SLACK_WEBHOOK_URL:
        print(f"[slack] No SLACK_WEBHOOK_URL set. Would ping for {contact.get('name', 'Unknown')}")
        return
    import httpx

    name = contact.get("name") or "Unknown"
    company = contact.get("company") or "Unknown"
    phone = contact.get("phone") or "(collecting...)"
    email = contact.get("email") or "(collecting...)"
    vertical = customer.get("name") or customer.get("industry") or ""

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "\\U0001f525 Lead ready for callback", "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Partner:*\\n{name} at {company}"},
            {"type": "mrkdwn", "text": f"*Their customers:*\\n{vertical or '(ask them)'}"},
        ]},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Phone:*\\n{phone}"},
            {"type": "mrkdwn", "text": f"*Email:*\\n{email}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Signal:* {signal_label}\\n*Last thing they said:*\\n> {last_msg or '(on the call now)'}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": "*Who can call them back in the next 5 minutes?*\\nReact with :raised_hand: or reply here to claim it."}},
    ]

    fallback = f"\\U0001f525 {name} @ {company} ready for callback. Phone: {phone}. Who can take it?"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(SLACK_WEBHOOK_URL, json={"text": fallback, "blocks": blocks})
            resp.raise_for_status()
            print(f"[slack] Availability ping sent for {name} @ {company}")
    except Exception as e:
        print(f"[slack] Availability ping error: {e}")


'''
    if anchor in code:
        code = code.replace(anchor, slack_func + anchor, 1)
        changes += 1
        print("  [1/2] Added _slack_availability_ping function")
    else:
        print("  [1/2] WARN: Could not find anchor for function insertion")
else:
    print("  [1/2] _slack_availability_ping already exists")
    changes += 1

# ============================================================
# FIX 2: Wire it into _process_notification on handoff signals
# Fire the Slack ping alongside the email availability ping
# ============================================================

old_avail = """            if SEND_AVAILABILITY_PING and not st["availability_sent"]:
                subj, html = _build_availability_email(contact, customer, req.message, label)
                ok, detail = await send_email(SALES_TEAM_EMAIL, subj, html)
                st["availability_sent"] = True
                print(f"[handoff] availability email -> {SALES_TEAM_EMAIL}: ok={ok} ({detail})")"""

new_avail = """            if SEND_AVAILABILITY_PING and not st["availability_sent"]:
                subj, html = _build_availability_email(contact, customer, req.message, label)
                ok, detail = await send_email(SALES_TEAM_EMAIL, subj, html)
                st["availability_sent"] = True
                print(f"[handoff] availability email -> {SALES_TEAM_EMAIL}: ok={ok} ({detail})")
                # Slack availability ping: "who can call them back?"
                await _slack_availability_ping(contact, customer, label, req.message)"""

if old_avail in code:
    code = code.replace(old_avail, new_avail, 1)
    changes += 1
    print("  [2/2] Wired _slack_availability_ping into handoff flow")
else:
    if "_slack_availability_ping(contact" in code:
        print("  [2/2] Slack ping already wired in")
        changes += 1
    else:
        print("  [2/2] WARN: availability email anchor not found")

with open("api.py", "w", encoding="utf-8") as f:
    f.write(code)

import ast
try:
    ast.parse(code)
    print("  Syntax OK")
except SyntaxError as e:
    print(f"  SYNTAX ERROR: {e}")
    sys.exit(1)

print(f"\n  {changes}/2 fixes applied.")
print()
print("  IMPORTANT: Set SLACK_WEBHOOK_URL in your Railway env vars.")
print("  1. Go to Railway > your service > Variables")
print("  2. Add: SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL")
print("  3. To create a webhook: Slack > your workspace > Apps > Incoming Webhooks")
print("     Pick the channel where your team hangs out.")
