"""
patch_selfloop.py — Stop the agent from answering its own questions.
Blocks consecutive agent messages with no real user input between them.
Applies to both browser-side (live panel) and server-side (transcript store).
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
# FIX 1: Browser-side — block consecutive agent messages
# in the onMessage handler before they hit the live panel
# ============================================================

old_push = """liveTurns.push({ role: role, message: text });
        if (window.renderPanel3) window.renderPanel3(liveTurns);
        fetch('/demo/transcript', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: role, message: text })
        });"""

new_push = """// Block agent talking to itself: skip if last turn was also agent
        if (role === 'agent' && liveTurns.length > 0 && liveTurns[liveTurns.length - 1].role === 'agent') {
          console.log('[voice] Blocked consecutive agent message:', text.slice(0, 60));
          return;
        }
        liveTurns.push({ role: role, message: text });
        if (window.renderPanel3) window.renderPanel3(liveTurns);
        fetch('/demo/transcript', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: role, message: text })
        });"""

if old_push in code:
    code = code.replace(old_push, new_push, 1)
    changes += 1
    print("  [1/2] Added browser-side consecutive agent block")
else:
    if "Blocked consecutive agent message" in code:
        print("  [1/2] Browser block already exists")
        changes += 1
    else:
        print("  [1/2] WARN: onMessage push anchor not found")

# ============================================================
# FIX 2: Server-side — block consecutive agent turns in
# the /demo/transcript endpoint
# ============================================================

# Find the line that appends to _live_transcript
old_append = """    _live_transcript.append({"role": role, "message": msg})
    if len(_live_transcript) > 200:
        _live_transcript = _live_transcript[-200:]
    return {"ok": True, "turns": len(_live_transcript)}"""

new_append = """    # Block agent self-loop: skip if last stored turn was also agent
    if role == "agent" and _live_transcript and _live_transcript[-1].get("role") == "agent":
        print(f"[transcript] Blocked consecutive agent message: {msg[:60]}")
        return {"ok": True, "turns": len(_live_transcript), "blocked": "consecutive_agent"}
    _live_transcript.append({"role": role, "message": msg})
    if len(_live_transcript) > 200:
        _live_transcript = _live_transcript[-200:]
    return {"ok": True, "turns": len(_live_transcript)}"""

if old_append in code:
    code = code.replace(old_append, new_append, 1)
    changes += 1
    print("  [2/2] Added server-side consecutive agent block")
else:
    if "Blocked consecutive agent message" in code:
        print("  [2/2] Server block already exists")
        changes += 1
    else:
        print("  [2/2] WARN: transcript append anchor not found")

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
print("  CODE FIX handles the symptom (blocks duplicate agent turns).")
print()
print("  ROOT CAUSE is in your ElevenLabs dashboard. Go to your agent and:")
print("    1. Turn detection > increase silence threshold (try 1500-2000ms)")
print("    2. Add to your agent prompt:")
print('       "NEVER answer your own question. If the caller is silent, WAIT.')
print('        Do not rephrase. Do not repeat. Silence is fine. One question,')
print('        then stop talking until they respond."')
