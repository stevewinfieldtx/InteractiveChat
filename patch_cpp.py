"""
patch_cpp.py — Run from your InteractiveChat project root.
Creates cpp_voice.py, patches api.py in-place, updates Dockerfile.
"""
import sys
import os

if not os.path.exists("api.py"):
    print("ERROR: api.py not found. Run this from your project root.")
    sys.exit(1)

# Check if already patched
with open("api.py", "r", encoding="utf-8") as f:
    check = f.read()
if "from cpp_voice import" in check:
    print("Already patched. cpp_voice import found in api.py.")
    sys.exit(0)

# ============================================================
# STEP 1: Create cpp_voice.py
# ============================================================

CPP_VOICE_FILE = r'''"""
cpp_voice.py - Steve Winfield Communication Personality Profile (TW-0 baseline)
Source: TrueWriting CPP v3.0 (2,841 messages, 646,592 words analyzed)
"""

CPP_VOICE = """
VOICE RULES - STEVE WINFIELD (TW-0 BASELINE)
Write as Steve Winfield. Follow these rules exactly:

SENTENCES: Default 8-14 words. Use fragments deliberately (38% of sentences should be 5 words or fewer). Break complex ideas into multiple short sentences. Paragraphs max 3-4 sentences.

VOCABULARY: Action verbs (build, ship, deploy, test, prove, fix). Anglo-Saxon over Latinate ('use' not 'utilize'). No filler. No hedge language. Specific numbers and concrete examples.

PUNCTUATION: NEVER em dashes. Ellipsis (...) for pauses/transitions. Minimal commas. No semicolons. Exclamation points rare. Capitalize for emphasis.

REGIONAL VOICE: Always 'y'all' never 'you guys'. 'Hey...' for casual greetings. 'shoot' and 'dang' for mild expletives. 'no worries' and 'you bet' for acknowledgment.

FRAMING: Problem first, then solution. Personal experience as proof. Close with action, not sentiment.

CORRECTIONS: Immediate. No softening preamble. Say what is wrong, say what is right, move on.

TONE: Direct, confident, warm through teaching not politeness. Skip greetings in working contexts. Start with the point, not a hello.

NEVER: Em dashes. 'Leverage/utilize/facilitate/endeavor.' 'You guys.' 'I think maybe.' Paragraphs over 4 sentences. Passive voice. 'Best regards' or 'Sincerely' (always ---Steve).
""".strip()


CPP_STYLE_LIGHT = """
STYLE: Short sentences (8-14 words default). Fragments OK. Action verbs. No filler. No hedge words. No em dashes. Concrete specifics over abstractions. Problem first, then solution.
""".strip()
'''

with open("cpp_voice.py", "w", encoding="utf-8") as f:
    f.write(CPP_VOICE_FILE)
print("  Created cpp_voice.py")

# ============================================================
# STEP 2: Patch api.py (6 targeted replacements)
# ============================================================

with open("api.py", "r", encoding="utf-8") as f:
    code = f.read()

changes = 0

# 2a. Add import line
old = (
    "from company_research import (\n"
    "    ResearchResult, domain_from_email, domain_from_url,\n"
    "    research_company, research_industry,\n"
    ")"
)
new = old + "\nfrom cpp_voice import CPP_VOICE, CPP_STYLE_LIGHT"
if old in code:
    code = code.replace(old, new, 1)
    changes += 1
    print("  [1/6] Added import")
else:
    print("  [1/6] WARN: import anchor not found")

# 2b. Patch _generate_sales_brief (inside an f-string)
old = "Be specific to THIS conversation. No generic advice. Use ONLY what the PARTNER"
new = "Be specific to THIS conversation. No generic advice.\n\n{CPP_STYLE_LIGHT}\n\nUse ONLY what the PARTNER"
if old in code:
    code = code.replace(old, new, 1)
    changes += 1
    print("  [2/6] Patched _generate_sales_brief")
else:
    print("  [2/6] WARN: sales_brief anchor not found")

# 2c. Patch _generate_call_summary (inside an f-string)
old = "Read the transcript (lines marked PARTNER are the human prospect; AGENT is our rep). Use ONLY what was actually said"
new = "{CPP_STYLE_LIGHT}\n\nRead the transcript (lines marked PARTNER are the human prospect; AGENT is our rep). Use ONLY what was actually said"
if old in code:
    code = code.replace(old, new, 1)
    changes += 1
    print("  [3/6] Patched _generate_call_summary")
else:
    print("  [3/6] WARN: call_summary anchor not found")

# 2d. Patch _generate_copilot (concatenated string)
old = '    prompt = (\n        "You are a live sales coach sitting beside a Rain Networks rep'
new = '    prompt = (\n        f"{CPP_STYLE_LIGHT}\\n\\n"\n        "You are a live sales coach sitting beside a Rain Networks rep'
if old in code:
    code = code.replace(old, new, 1)
    changes += 1
    print("  [4/6] Patched _generate_copilot")
else:
    print("  [4/6] WARN: copilot anchor not found")

# 2e. Patch _generate_rep_reply (concatenated string)
old = '    prompt = (\n        "You are a top Rain Networks rep in a live chat'
new = '    prompt = (\n        f"{CPP_VOICE}\\n\\n"\n        "You are a top Rain Networks rep in a live chat'
if old in code:
    code = code.replace(old, new, 1)
    changes += 1
    print("  [5/6] Patched _generate_rep_reply")
else:
    print("  [5/6] WARN: rep_reply anchor not found")

# 2f. Patch _generate_guardz_reply (concatenated string)
old = '    prompt = (\n        "You are a friendly, sharp Guardz expert at Rain Networks'
new = '    prompt = (\n        f"{CPP_VOICE}\\n\\n"\n        "You are a friendly, sharp Guardz expert at Rain Networks'
if old in code:
    code = code.replace(old, new, 1)
    changes += 1
    print("  [6/6] Patched _generate_guardz_reply")
else:
    print("  [6/6] WARN: guardz_reply anchor not found")

with open("api.py", "w", encoding="utf-8") as f:
    f.write(code)
print(f"  api.py: {changes}/6 patches applied")

# ============================================================
# STEP 3: Update Dockerfile
# ============================================================

if os.path.exists("Dockerfile"):
    with open("Dockerfile", "r", encoding="utf-8") as f:
        df = f.read()
    old_copy = "COPY api.py company_research.py ./"
    new_copy = "COPY api.py company_research.py cpp_voice.py ./"
    if old_copy in df and new_copy not in df:
        df = df.replace(old_copy, new_copy, 1)
        with open("Dockerfile", "w", encoding="utf-8") as f:
            f.write(df)
        print("  Dockerfile updated")
    else:
        print("  Dockerfile already has cpp_voice.py or anchor not found")
else:
    print("  No Dockerfile found (nixpacks picks up files from repo root)")

# ============================================================
# STEP 4: Syntax check
# ============================================================

import ast
try:
    ast.parse(open("api.py", encoding="utf-8").read())
    print("  api.py syntax OK")
except SyntaxError as e:
    print(f"  SYNTAX ERROR in api.py: {e}")
    sys.exit(1)

try:
    ast.parse(open("cpp_voice.py", encoding="utf-8").read())
    print("  cpp_voice.py syntax OK")
except SyntaxError as e:
    print(f"  SYNTAX ERROR in cpp_voice.py: {e}")
    sys.exit(1)

if changes < 6:
    print(f"\nWARNING: Only {changes}/6 patches applied. Check the warnings above.")
    sys.exit(1)

print("\nAll done. Run git add/commit/push or use deploy_cpp.bat.")
