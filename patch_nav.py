"""
patch_nav.py — Adds demo environment buttons to the front page.
Run from your InteractiveChat project root.
"""
import sys, os

if not os.path.exists("api.py"):
    print("ERROR: api.py not found.")
    sys.exit(1)

with open("api.py", "r", encoding="utf-8") as f:
    code = f.read()

old = """  <button class="launch-btn" onclick="window.location='/demo/live'">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
    Launch Demo
  </button>
  <div class="pills">
    <span class="pill">Live customer research</span>
    <span class="pill">Real-time signals</span>
    <span class="pill">Instant human callback</span>
  </div>"""

new = """  <div style="display:flex;flex-direction:column;gap:14px;align-items:center">
    <button class="launch-btn" onclick="window.location='/demo/live'" style="width:340px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:20px;height:20px"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      Voice Demo (Live Call)
    </button>
    <button class="launch-btn" onclick="window.location='/copilot'" style="width:340px;background:linear-gradient(135deg,#4f46e5,#0ea5e9)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width:20px;height:20px"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
      Chat Copilot (AI Customer)
    </button>
    <div style="display:flex;gap:12px">
      <button class="launch-btn" onclick="window.location='/visitor'" style="padding:12px 24px;font-size:14px;background:linear-gradient(135deg,#059669,#10b981)">
        Visitor Chat
      </button>
      <button class="launch-btn" onclick="window.location='/agent'" style="padding:12px 24px;font-size:14px;background:linear-gradient(135deg,#d97706,#f59e0b)">
        Agent Console
      </button>
    </div>
  </div>
  <div class="pills" style="margin-top:32px">
    <span class="pill">Voice Demo &mdash; live call + research + handoff</span>
    <span class="pill">Chat Copilot &mdash; AI plays customer, you practice</span>
    <span class="pill">Visitor + Agent &mdash; two-tab live chat with coaching</span>
  </div>"""

if old in code:
    code = code.replace(old, new, 1)
    with open("api.py", "w", encoding="utf-8") as f:
        f.write(code)
    print("Front page patched with demo buttons.")
else:
    print("WARN: Could not find the button anchor. Page may already be patched or changed.")
    sys.exit(1)

import ast
try:
    ast.parse(code)
    print("Syntax OK.")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    sys.exit(1)
