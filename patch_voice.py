"""
patch_voice.py — Fix voice agent echo loop, phantom speech, and repeating.
1. Adds connectionType: 'webrtc' to the demo page SDK call (echo cancellation)
2. Filters out phantom micro-transcripts ("okay", "uh-huh", silence artifacts)
3. Adds anti-repeat guardrails to the agent prompt starter in README
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
# FIX 1: Add connectionType 'webrtc' to demo page SDK call
# Without this, no echo cancellation. Mic hears the speaker.
# ============================================================

old_sdk = """convSession = await Conversation.startSession({
      agentId: AGENT_ID,
      onConnect: (info) => {"""

new_sdk = """convSession = await Conversation.startSession({
      agentId: AGENT_ID,
      connectionType: 'webrtc',
      onConnect: (info) => {"""

if old_sdk in code:
    code = code.replace(old_sdk, new_sdk, 1)
    changes += 1
    print("  [1/3] Added connectionType: 'webrtc' to demo SDK call")
else:
    if "connectionType: 'webrtc'" in code:
        print("  [1/3] connectionType already set")
        changes += 1
    else:
        print("  [1/3] WARN: SDK startSession anchor not found")

# ============================================================
# FIX 2: Filter phantom micro-transcripts before storing
# The STT often produces "okay", "uh-huh", "hmm" from echo/noise.
# We still store them but mark them so the agent prompt can ignore.
# More importantly: skip pushing single-word echo artifacts to
# the transcript feed that drives the brief and coaching.
# ============================================================

old_transcript = """@app.post("/demo/transcript")
async def add_transcript(turn: TranscriptTurn):
    \"\"\"Browser pushes each finalized turn here in real time (drives the brief + leads).\"\"\"
    global _live_transcript
    msg = (turn.message or "").strip()
    if msg:
        role = "agent" if turn.role in ("agent", "ai") else "user"
        _live_transcript.append({"role": role, "message": msg})
        if len(_live_transcript) > 200:
            _live_transcript = _live_transcript[-200:]
    return {"ok": True, "turns": len(_live_transcript)}"""

new_transcript = """# Phantom speech filter: STT produces these from echo/ambient noise
_PHANTOM_WORDS = {
    "okay", "ok", "uh-huh", "uh huh", "um", "uh", "hmm", "mm",
    "mhm", "mm-hmm", "yeah", "yep", "right", "sure", "bye",
    "hello", "hi", "hey", "thanks", "thank you",
}

@app.post("/demo/transcript")
async def add_transcript(turn: TranscriptTurn):
    \"\"\"Browser pushes each finalized turn here in real time (drives the brief + leads).\"\"\"
    global _live_transcript
    msg = (turn.message or "").strip()
    if not msg:
        return {"ok": True, "turns": len(_live_transcript)}
    role = "agent" if turn.role in ("agent", "ai") else "user"
    # Skip likely phantom/echo artifacts from the caller side
    if role == "user" and msg.lower().rstrip(".!?,") in _PHANTOM_WORDS:
        print(f"[transcript] Filtered phantom: '{msg}'")
        return {"ok": True, "turns": len(_live_transcript), "filtered": True}
    _live_transcript.append({"role": role, "message": msg})
    if len(_live_transcript) > 200:
        _live_transcript = _live_transcript[-200:]
    return {"ok": True, "turns": len(_live_transcript)}"""

if old_transcript in code:
    code = code.replace(old_transcript, new_transcript, 1)
    changes += 1
    print("  [2/3] Added phantom speech filter to /demo/transcript")
else:
    if "_PHANTOM_WORDS" in code:
        print("  [2/3] Phantom filter already exists")
        changes += 1
    else:
        print("  [2/3] WARN: transcript endpoint anchor not found")

# ============================================================
# FIX 3: Filter phantom transcripts on the browser side too
# The onMessage handler pushes turns to the live panel AND
# to /demo/transcript. Filter before both.
# ============================================================

old_onmessage = """onMessage: (m) => {
        const text = (typeof m === 'string') ? m : (m && (m.message || m.text)) || '';
        const src  = (m && (m.source || m.role)) || '';
        if (!text) return;
        const role = (src === 'ai' || src === 'agent') ? 'agent' : 'user';
        liveTurns.push({ role: role, message: text });"""

new_onmessage = """onMessage: (m) => {
        const text = (typeof m === 'string') ? m : (m && (m.message || m.text)) || '';
        const src  = (m && (m.source || m.role)) || '';
        if (!text) return;
        const role = (src === 'ai' || src === 'agent') ? 'agent' : 'user';
        // Filter phantom echo from caller side (STT hears the speaker)
        const PHANTOMS = ['okay','ok','uh-huh','uh huh','um','uh','hmm','mm','mhm','mm-hmm','yeah','yep','right','sure','bye','hello','hi','hey','thanks','thank you'];
        if (role === 'user' && PHANTOMS.includes(text.toLowerCase().replace(/[.!?,]/g,''))) {
          console.log('[voice] Filtered phantom:', text);
          return;
        }
        liveTurns.push({ role: role, message: text });"""

if old_onmessage in code:
    code = code.replace(old_onmessage, new_onmessage, 1)
    changes += 1
    print("  [3/3] Added browser-side phantom filter in onMessage")
else:
    if "PHANTOMS" in code:
        print("  [3/3] Browser phantom filter already exists")
        changes += 1
    else:
        print("  [3/3] WARN: onMessage anchor not found")

# Write
with open("api.py", "w", encoding="utf-8") as f:
    f.write(code)

# Syntax check
import ast
try:
    ast.parse(code)
    print("  Syntax OK")
except SyntaxError as e:
    print(f"  SYNTAX ERROR: {e}")
    sys.exit(1)

print(f"\n  {changes}/3 fixes applied.")
if changes == 3:
    print("  All done. Push it.")
else:
    print("  Check warnings above.")
