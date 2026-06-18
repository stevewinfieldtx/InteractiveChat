"""
patch_practice.py — Add /practice voice training page with live AI coaching.
- New route /practice: voice call with practice customer + coaching panel
- New endpoint /practice/coach: scores the call and gives real-time tips
- New front page button
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
# 1. Add PRACTICE_AGENT_ID constant near the other config
# ============================================================

if "PRACTICE_AGENT_ID" not in code:
    anchor = 'ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID"'
    insert = 'PRACTICE_AGENT_ID  = os.getenv("PRACTICE_AGENT_ID", "agent_6801kvdhttspey5r9svxt9w9e8rt")\n'
    # Find the full line and insert after it
    idx = code.find(anchor)
    if idx != -1:
        end_of_line = code.index("\n", idx)
        code = code[:end_of_line+1] + insert + code[end_of_line+1:]
        changes += 1
        print("  [1/4] Added PRACTICE_AGENT_ID constant")
    else:
        print("  [1/4] WARN: Could not find ELEVENLABS_AGENT_ID anchor")
else:
    print("  [1/4] PRACTICE_AGENT_ID already exists")
    changes += 1

# ============================================================
# 2. Add /practice/coach endpoint + coaching generator
# ============================================================

if '"/practice/coach"' not in code:
    # Insert before the demo state API section
    anchor = "# ─── Demo state API"

    practice_code = '''

# ─── Practice mode: voice training with AI coaching ──────────────────────────

_practice_transcript: list[dict] = []
_practice_coach: dict = {}

class PracticeCoachRequest(BaseModel):
    turns: list[dict] = []

async def _generate_practice_coach(turns: list) -> dict:
    """Score Steve's live practice call. Health, intent, close %, tips."""
    if not OPENROUTER_API_KEY or not turns:
        return {}
    import json as _j
    import httpx
    convo = "\\n".join(
        f"{'CUSTOMER' if t.get('role') == 'user' else 'STEVE (rep)'}: {t.get('message','')}"
        for t in turns[-25:]
    )

    prompt = (
        f"{CPP_STYLE_LIGHT}\\n\\n"
        "You are a live sales coach watching Steve Winfield (Rain Networks) on a practice call "
        "with a simulated IT reseller customer. The customer is exploring whether to resell Guardz "
        "to their SMB clients. Your job is to coach Steve in REAL TIME. Be blunt. Be specific.\\n\\n"
        "Return ONLY this JSON, no prose:\\n"
        '{"health":"green|yellow|red","close_pct":<integer 0-100>,'
        '"tone":"<3-7 word read of the customer\'s mood right now>",'
        '"tips":["<tip>","<tip>","<tip>"]}\\n\\n'
        "TIPS RULES:\\n"
        "- 2-4 tips. Terse fragments, MAX 8 WORDS each.\\n"
        "- Good examples: 'Ask about the phishing incident', 'Mention free tier now', "
        "'He is warming up... push for email', 'Stop talking... let him respond', "
        "'Name a specific CPA pain', 'Tie it to cyber insurance'.\\n"
        "- Bad examples: anything generic, anything over 8 words, anything that sounds like a textbook.\\n"
        "- MATCH THE STAGE:\\n"
        "  * Opening: discovery tips. Learn about them first.\\n"
        "  * Middle: tie Guardz to THEIR clients' pain. Handle objections.\\n"
        "  * Late: close. Get email. Get phone. Offer callback with Steve.\\n"
        "- Call out mistakes: 'You talked over him', 'That was too scripted', "
        "'You dodged his pricing question'.\\n"
        "- Praise good moves: 'Good discovery question', 'Nice objection handle'.\\n\\n"
        "health: green = deal moving forward. yellow = stalling or neutral. red = losing them.\\n"
        "close_pct: your honest read on likelihood this becomes a real deal. 0-100.\\n\\n"
        f"CALL TRANSCRIPT:\\n{convo}"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "HTTP-Referer": "https://interactivechat.up.railway.app",
                         "X-Title": "Rain Networks Practice Coach"},
                json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 350, "temperature": 0.3},
            )
        if resp.status_code == 200:
            txt = resp.json()["choices"][0]["message"]["content"]
            i, j = txt.find("{"), txt.rfind("}")
            if i != -1 and j != -1:
                result = _j.loads(txt[i:j + 1])
                if "tips" in result:
                    result["tips"] = [_clean_voice(t) for t in result.get("tips", [])]
                if "tone" in result:
                    result["tone"] = _clean_voice(result["tone"])
                return result
        else:
            print(f"[practice-coach] OpenRouter {resp.status_code}: {resp.text[:160]}")
    except Exception as e:
        print(f"[practice-coach] error: {e}")
    return {}


@app.post("/practice/coach")
async def practice_coach(req: PracticeCoachRequest):
    global _practice_transcript, _practice_coach
    if req.turns:
        _practice_transcript = req.turns[-50:]
    if len(_practice_transcript) >= 2:
        coach = await _generate_practice_coach(_practice_transcript)
        if coach:
            _practice_coach = coach
    return _practice_coach


@app.get("/practice/reset")
async def practice_reset():
    global _practice_transcript, _practice_coach
    _practice_transcript = []
    _practice_coach = {}
    return {"ok": True}


'''
    if anchor in code:
        code = code.replace(anchor, practice_code + anchor, 1)
        changes += 1
        print("  [2/4] Added /practice/coach endpoint + coaching generator")
    else:
        print("  [2/4] WARN: Could not find demo state anchor")
else:
    print("  [2/4] /practice/coach already exists")
    changes += 1

# ============================================================
# 3. Add /practice HTML page
# ============================================================

if '"/practice"' not in code and "@app.get(\"/practice\"" not in code:
    # Insert before the final comment or at the end of routes
    # Find the demo_live route and insert the practice page after the voice SDK JS

    practice_page = '''

PRACTICE_PAGE_JS = """
<script type="module">
import { Conversation } from "https://esm.sh/@elevenlabs/client";

const AGENT_ID = "__PRACTICE_AGENT_ID__";
let convSession = null;
let liveTurns = [];
let coachInterval = null;

function setCallUI(active) {
  const btn = document.getElementById('call-btn');
  const st  = document.getElementById('call-state');
  if (btn) btn.textContent = active ? '\\\\u23F9 End Call' : '\\\\uD83C\\\\uDFA4 Start Practice Call';
  if (st)  st.style.display = active ? 'inline-block' : 'none';
}

function escapeHtml(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderTranscript() {
  const body = document.getElementById('transcript-body');
  if (!body || !liveTurns.length) return;
  body.innerHTML = liveTurns.map(t => {
    const isAgent = t.role === 'agent';
    const label = isAgent ? '\\\\uD83E\\\\uDD16 Customer' : '\\\\uD83C\\\\uDFA4 You (Steve)';
    const cls = isAgent ? 'agent' : 'user';
    return '<div class=\"tx-turn\"><div class=\"tx-label tx-label-' + cls + '\">' + label + '</div>' +
           '<div class=\"tx-bubble tx-bubble-' + cls + '\">' + escapeHtml(t.message) + '</div></div>';
  }).join('');
  body.scrollTop = body.scrollHeight;
}

async function fetchCoaching() {
  if (liveTurns.length < 2) return;
  try {
    const r = await fetch('/practice/coach', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ turns: liveTurns })
    });
    const c = await r.json();
    if (c && c.health) {
      const dot = document.getElementById('dot');
      dot.className = 'dot ' + (c.health || '');
      document.getElementById('health').textContent = c.health || '—';
      document.getElementById('close-pct').textContent = (c.close_pct != null ? c.close_pct + '%' : '—');
      document.getElementById('closebar').style.width = (c.close_pct || 0) + '%';
      document.getElementById('tone').textContent = c.tone || 'Waiting...';
      const tips = c.tips || [];
      document.getElementById('tips').innerHTML = tips.length
        ? '<ul style=\"margin:0;padding-left:18px\">' + tips.map(t => '<li style=\"margin:6px 0\">' + escapeHtml(t) + '</li>').join('') + '</ul>'
        : '—';
    }
  } catch(e) {}
}

async function toggleCall() {
  if (convSession) {
    try { await convSession.endSession(); } catch(e) {}
    convSession = null;
    setCallUI(false);
    if (coachInterval) { clearInterval(coachInterval); coachInterval = null; }
    fetchCoaching();
    return;
  }
  try {
    await fetch('/practice/reset');
    liveTurns = [];
    document.getElementById('transcript-body').innerHTML = '';
    convSession = await Conversation.startSession({
      agentId: AGENT_ID,
      connectionType: 'webrtc',
      onConnect: () => { setCallUI(true); coachInterval = setInterval(fetchCoaching, 4000); },
      onDisconnect: () => { convSession = null; setCallUI(false); if (coachInterval) clearInterval(coachInterval); fetchCoaching(); },
      onError: (e) => { console.error('[practice]', e); },
      onModeChange: (m) => {
        const cs = document.getElementById('call-status');
        if (cs && m) cs.textContent = (m.mode === 'speaking') ? 'Customer speaking...' : 'Your turn... go';
      },
      onMessage: (m) => {
        const text = (typeof m === 'string') ? m : (m && (m.message || m.text)) || '';
        const src  = (m && (m.source || m.role)) || '';
        if (!text) return;
        const role = (src === 'ai' || src === 'agent') ? 'agent' : 'user';
        const PHANTOMS = ['okay','ok','uh-huh','uh huh','um','uh','hmm','mm','mhm','yeah','yep','right','sure'];
        if (role === 'user' && PHANTOMS.includes(text.toLowerCase().replace(/[.!?,]/g,''))) return;
        if (role === 'agent' && liveTurns.length > 0 && liveTurns[liveTurns.length - 1].role === 'agent') return;
        liveTurns.push({ role, message: text });
        renderTranscript();
      }
    });
  } catch(e) {
    console.error('[practice] start failed', e);
    setCallUI(false);
  }
}
window.toggleCall = toggleCall;
</script>
""".replace("__PRACTICE_AGENT_ID__", PRACTICE_AGENT_ID)


@app.get("/practice", response_class=HTMLResponse)
async def practice_page():
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rain Networks — Practice Mode</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  html,body {{ height:100%; background:#080818; color:#fff; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; overflow:hidden }}
  header {{ height:52px; background:#0d0d24; border-bottom:1px solid rgba(255,255,255,.07); display:flex; align-items:center; justify-content:space-between; padding:0 20px; flex-shrink:0 }}
  .hlogo {{ font-size:14px; font-weight:700; color:#a78bfa; letter-spacing:.1em; text-transform:uppercase }}
  .hright {{ display:flex; align-items:center; gap:16px }}
  .badge {{ font-size:11px; padding:3px 10px; border-radius:20px; font-weight:600 }}
  .badge.live {{ background:rgba(16,185,129,.15); color:#10b981; border:1px solid rgba(16,185,129,.3) }}
  .panels {{ display:grid; grid-template-columns:1fr 380px; height:calc(100vh - 52px); gap:1px; background:rgba(255,255,255,.05) }}
  .panel {{ background:#0d0d24; display:flex; flex-direction:column; overflow:hidden }}
  .panel-head {{ padding:16px 20px 12px; border-bottom:1px solid rgba(255,255,255,.06); flex-shrink:0 }}
  .panel-title {{ font-size:11px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:#6b7280; margin-bottom:4px }}
  .panel-status {{ font-size:13px; font-weight:600; color:#e2e8f0 }}
  .panel-body {{ flex:1; overflow-y:auto; padding:20px }}
  .panel-body::-webkit-scrollbar {{ width:4px }}
  .panel-body::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,.1); border-radius:2px }}
  .tx-turn {{ margin-bottom:12px }}
  .tx-label {{ font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:4px }}
  .tx-label-agent {{ color:#0ea5e9 }}
  .tx-label-user {{ color:#7c3aed }}
  .tx-bubble {{ font-size:13px; line-height:1.6; padding:10px 14px; border-radius:10px; word-break:break-word }}
  .tx-bubble-agent {{ background:rgba(14,165,233,.08); color:#bae6fd; border:1px solid rgba(14,165,233,.15) }}
  .tx-bubble-user {{ background:rgba(124,58,237,.1); color:#ddd6fe; border:1px solid rgba(124,58,237,.2) }}
  .cop {{ padding:20px; overflow-y:auto }}
  .light {{ display:flex; align-items:center; gap:14px; margin-bottom:24px }}
  .dot {{ width:56px; height:56px; border-radius:50%; background:#374151; transition:all .3s }}
  .dot.green {{ background:#10b981; box-shadow:0 0 30px rgba(16,185,129,.6) }}
  .dot.yellow {{ background:#f59e0b; box-shadow:0 0 30px rgba(245,158,11,.6) }}
  .dot.red {{ background:#ef4444; box-shadow:0 0 30px rgba(239,68,68,.6) }}
  .lt {{ font-size:13px; color:#94a3b8 }}
  .lt b {{ display:block; font-size:20px; color:#e2e8f0; text-transform:capitalize }}
  .sec {{ margin-top:20px }}
  .sec-l {{ font-size:10px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:#4b5563; margin-bottom:8px }}
  .tone {{ font-size:15px; color:#c4b5fd; font-weight:600 }}
  .bar {{ height:8px; background:rgba(255,255,255,.08); border-radius:4px; overflow:hidden; margin-top:8px }}
  .bar > i {{ display:block; height:100%; border-radius:4px; background:linear-gradient(90deg,#ef4444,#f59e0b,#10b981); width:0%; transition:width .5s }}
  .tips {{ font-size:14px; color:#f1f5f9; line-height:1.6 }}
  .tips li {{ margin:8px 0; padding:8px 12px; background:rgba(124,58,237,.08); border:1px solid rgba(124,58,237,.2); border-radius:8px }}
  .call-controls {{ padding:16px 20px; border-top:1px solid rgba(255,255,255,.06); display:flex; align-items:center; gap:14px }}
</style>
</head><body>
<header>
  <span class="hlogo">Rain Networks &middot; Practice Mode</span>
  <div class="hright">
    <button id="call-btn" onclick="toggleCall()" style="font-size:13px;font-weight:700;color:#fff;background:linear-gradient(135deg,#7c3aed,#4f46e5);border:none;border-radius:8px;padding:8px 18px;cursor:pointer">&#127908; Start Practice Call</button>
    <span id="call-state" class="badge live" style="display:none">&#9679; LIVE</span>
    <span id="call-status" style="font-size:12px;color:#6b7280">Ready to practice</span>
    <a href="/" style="font-size:11px;color:#6b7280;text-decoration:none">&larr; Home</a>
  </div>
</header>
<div class="panels">
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Live Transcript</div>
      <div class="panel-status">&#128221; You are the rep. The AI is the customer calling in.</div>
    </div>
    <div class="panel-body" id="transcript-body">
      <div style="text-align:center;color:#4b5563;padding:40px 0">
        <div style="font-size:28px;margin-bottom:12px">&#127908;</div>
        <div style="font-size:13px">Click <b>Start Practice Call</b> to begin.<br>You're Steve. The AI plays a skeptical MSP owner calling about Guardz.</div>
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">AI Coach</div>
      <div class="panel-status">&#127919; Live guidance while you sell</div>
    </div>
    <div class="cop">
      <div class="light">
        <div class="dot" id="dot"></div>
        <div class="lt">Deal health<b id="health">&mdash;</b></div>
      </div>
      <div class="sec">
        <div class="sec-l">Close probability</div>
        <div id="close-pct" style="font-size:22px;font-weight:800;color:#e2e8f0">&mdash;</div>
        <div class="bar"><i id="closebar"></i></div>
      </div>
      <div class="sec">
        <div class="sec-l">Customer mood</div>
        <div class="tone" id="tone">Waiting for the call to start...</div>
      </div>
      <div class="sec">
        <div class="sec-l">Coaching tips</div>
        <div class="tips" id="tips">&mdash;</div>
      </div>
    </div>
  </div>
</div>
""" + PRACTICE_PAGE_JS + "</body></html>")

'''
    # Insert before the demo_live route
    insert_anchor = "@app.get(\"/demo/live\""
    if insert_anchor in code:
        code = code.replace(insert_anchor, practice_page + insert_anchor, 1)
        changes += 1
        print("  [3/4] Added /practice page + PRACTICE_PAGE_JS")
    else:
        print("  [3/4] WARN: Could not find /demo/live anchor for insertion")
else:
    print("  [3/4] /practice page already exists")
    changes += 1

# ============================================================
# 4. Add Practice button to front page
# ============================================================

old_buttons = """      <button class="launch-btn" onclick="window.location='/copilot'" style="width:340px;background:linear-gradient(135deg,#4f46e5,#0ea5e9)">"""

new_buttons = """      <button class="launch-btn" onclick="window.location='/practice'" style="width:340px;background:linear-gradient(135deg,#059669,#10b981)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:20px;height:20px"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/></svg>
      Practice Mode (You Sell, AI Buys)
    </button>
    <button class="launch-btn" onclick="window.location='/copilot'" style="width:340px;background:linear-gradient(135deg,#4f46e5,#0ea5e9)">"""

if old_buttons in code:
    code = code.replace(old_buttons, new_buttons, 1)
    changes += 1
    print("  [4/4] Added Practice Mode button to front page")
else:
    if "/practice" in code and "Practice Mode" in code:
        print("  [4/4] Practice button may already exist")
        changes += 1
    else:
        print("  [4/4] WARN: Could not find front page button anchor")

# Write
with open("api.py", "w", encoding="utf-8") as f:
    f.write(code)

import ast
try:
    ast.parse(code)
    print("  Syntax OK")
except SyntaxError as e:
    print(f"  SYNTAX ERROR: {e}")
    sys.exit(1)

print(f"\n  {changes}/4 applied.")
if changes == 4:
    print("  All done. Push it.")
