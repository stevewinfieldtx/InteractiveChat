"""
patch_emdash.py — Kill em dashes from all LLM output.
1. Strengthens the NEVER rule in cpp_voice.py
2. Adds a _clean_voice() post-processor in api.py
3. Wraps all 5 generation function outputs through it
Run from your InteractiveChat project root.
"""
import sys, os

if not os.path.exists("api.py"):
    print("ERROR: api.py not found.")
    sys.exit(1)

# ============================================================
# STEP 1: Rewrite cpp_voice.py with stronger em dash rule
# ============================================================

CPP_VOICE_FILE = r'''"""
cpp_voice.py - Steve Winfield Communication Personality Profile (TW-0 baseline)
Source: TrueWriting CPP v3.0 (2,841 messages, 646,592 words analyzed)
"""

CPP_VOICE = """
VOICE RULES - STEVE WINFIELD (TW-0 BASELINE)
Write as Steve Winfield. Follow these rules exactly:

ABSOLUTE RULE - NO EM DASHES: Never use the em dash character. Not once. Not ever. Zero in 646,000 words of real writing. Use ellipsis (...) for pauses, or break into two sentences. If you catch yourself about to write one, stop and rewrite the sentence.

SENTENCES: Default 8-14 words. Use fragments deliberately (38% of sentences should be 5 words or fewer). Break complex ideas into multiple short sentences. Paragraphs max 3-4 sentences.

VOCABULARY: Action verbs (build, ship, deploy, test, prove, fix). Anglo-Saxon over Latinate ('use' not 'utilize'). No filler. No hedge language. Specific numbers and concrete examples.

PUNCTUATION: Ellipsis (...) for pauses/transitions. Minimal commas. No semicolons. Exclamation points rare. Capitalize for emphasis. NO EM DASHES EVER.

REGIONAL VOICE: Always 'y'all' never 'you guys'. 'Hey...' for casual greetings. 'shoot' and 'dang' for mild expletives. 'no worries' and 'you bet' for acknowledgment.

FRAMING: Problem first, then solution. Personal experience as proof. Close with action, not sentiment.

CORRECTIONS: Immediate. No softening preamble. Say what is wrong, say what is right, move on.

TONE: Direct, confident, warm through teaching not politeness. Skip greetings in working contexts. Start with the point, not a hello.

NEVER: Em dashes (---NEVER---). 'Leverage/utilize/facilitate/endeavor.' 'You guys.' 'I think maybe.' Paragraphs over 4 sentences. Passive voice. 'Best regards' or 'Sincerely' (always ---Steve).
""".strip()


CPP_STYLE_LIGHT = """
STYLE: Short sentences (8-14 words default). Fragments OK. Action verbs. No filler. No hedge words. NEVER use em dashes (use ellipsis or two sentences instead). Concrete specifics over abstractions. Problem first, then solution.
""".strip()
'''

with open("cpp_voice.py", "w", encoding="utf-8") as f:
    f.write(CPP_VOICE_FILE)
print("  cpp_voice.py rewritten with stronger em dash rule")

# ============================================================
# STEP 2: Add _clean_voice() to api.py and wrap all outputs
# ============================================================

with open("api.py", "r", encoding="utf-8") as f:
    code = f.read()

changes = 0

# 2a. Add the cleanup function after the imports (find a safe anchor)
if "_clean_voice" not in code:
    anchor = "app = FastAPI("
    clean_func = '''

# ─── CPP post-processor: kill em dashes from all LLM output ──────────────────
def _clean_voice(text: str) -> str:
    """Strip em dashes from LLM output. Steve never uses them. Zero in 646K words."""
    if not text:
        return text
    # Replace em dash (U+2014) and en dash (U+2013) with ellipsis or space
    text = text.replace("\\u2014", "...")   # em dash -> ellipsis
    text = text.replace("\\u2013", "...")   # en dash -> ellipsis
    text = text.replace(" ... ", "... ")    # clean up double spaces around ellipsis
    return text


'''
    if anchor in code:
        code = code.replace(anchor, clean_func + anchor, 1)
        changes += 1
        print("  [1] Added _clean_voice() function")
    else:
        print("  [1] WARN: could not find anchor for _clean_voice()")
else:
    print("  [1] _clean_voice() already exists")
    changes += 1

# 2b. Wrap _generate_sales_brief return
old_brief = '''            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            else:
                print(f"[brief] OpenRouter {resp.status_code}: {resp.text[:200]}")'''
new_brief = '''            if resp.status_code == 200:
                return _clean_voice(resp.json()["choices"][0]["message"]["content"].strip())
            else:
                print(f"[brief] OpenRouter {resp.status_code}: {resp.text[:200]}")'''
if old_brief in code:
    code = code.replace(old_brief, new_brief, 1)
    changes += 1
    print("  [2] Wrapped _generate_sales_brief")
else:
    if "_clean_voice" in code and "[brief] OpenRouter" in code:
        print("  [2] _generate_sales_brief already wrapped")
        changes += 1
    else:
        print("  [2] WARN: sales_brief return anchor not found")

# 2c. Wrap _generate_call_summary return
old_summary = '''        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[summary] OpenRouter {resp.status_code}: {resp.text[:160]}")'''
new_summary = '''        if resp.status_code == 200:
            return _clean_voice(resp.json()["choices"][0]["message"]["content"].strip())
        print(f"[summary] OpenRouter {resp.status_code}: {resp.text[:160]}")'''
if old_summary in code:
    code = code.replace(old_summary, new_summary, 1)
    changes += 1
    print("  [3] Wrapped _generate_call_summary")
else:
    print("  [3] WARN: call_summary return anchor not found (may already be wrapped)")
    changes += 1

# 2d. Wrap _generate_rep_reply return
old_rep = '''        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip().strip('"')
        print(f"[rep-suggest] OpenRouter {resp.status_code}: {resp.text[:160]}")'''
new_rep = '''        if resp.status_code == 200:
            return _clean_voice(resp.json()["choices"][0]["message"]["content"].strip().strip('"'))
        print(f"[rep-suggest] OpenRouter {resp.status_code}: {resp.text[:160]}")'''
if old_rep in code:
    code = code.replace(old_rep, new_rep, 1)
    changes += 1
    print("  [4] Wrapped _generate_rep_reply")
else:
    print("  [4] WARN: rep_reply return anchor not found (may already be wrapped)")
    changes += 1

# 2e. Wrap _generate_guardz_reply return
old_guardz = '''        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip().strip('"')
        print(f"[guardz-chat] OpenRouter {resp.status_code}: {resp.text[:160]}")'''
new_guardz = '''        if resp.status_code == 200:
            return _clean_voice(resp.json()["choices"][0]["message"]["content"].strip().strip('"'))
        print(f"[guardz-chat] OpenRouter {resp.status_code}: {resp.text[:160]}")'''
if old_guardz in code:
    code = code.replace(old_guardz, new_guardz, 1)
    changes += 1
    print("  [5] Wrapped _generate_guardz_reply")
else:
    print("  [5] WARN: guardz_reply return anchor not found (may already be wrapped)")
    changes += 1

# 2f. Wrap _generate_copilot - this returns JSON, but the tips contain text
# We clean the tips inside the parsed JSON
old_copilot_parse = '''            if i != -1 and j != -1:
                return _j.loads(txt[i:j + 1])'''
new_copilot_parse = '''            if i != -1 and j != -1:
                result = _j.loads(txt[i:j + 1])
                if "tips" in result:
                    result["tips"] = [_clean_voice(t) for t in result.get("tips", [])]
                if "tone" in result:
                    result["tone"] = _clean_voice(result["tone"])
                return result'''
if old_copilot_parse in code:
    code = code.replace(old_copilot_parse, new_copilot_parse, 1)
    changes += 1
    print("  [6] Wrapped _generate_copilot JSON output")
else:
    print("  [6] WARN: copilot parse anchor not found (may already be wrapped)")
    changes += 1

with open("api.py", "w", encoding="utf-8") as f:
    f.write(code)

# Syntax check
import ast
try:
    ast.parse(open("api.py", encoding="utf-8").read())
    print("  Syntax OK")
except SyntaxError as e:
    print(f"  SYNTAX ERROR: {e}")
    sys.exit(1)

print(f"\nDone. Em dashes will be stripped from all LLM output.")
