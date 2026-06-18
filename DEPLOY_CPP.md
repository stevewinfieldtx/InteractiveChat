# CPP Voice Patch — Deployment Guide

## What changed

Your Communication Personality Profile (TW-0 v3.0) is now injected into every LLM prompt in the app.

### New file: `cpp_voice.py`
Two constants extracted from your CPP:
- **CPP_VOICE** — Full voice rules (sentences, vocabulary, punctuation, regional voice, framing, tone, NEVER list). Used when the AI IS you.
- **CPP_STYLE_LIGHT** — Condensed style rules (short sentences, action verbs, no filler). Used for analytical outputs you READ.

### Modified: `api.py`
Five generation functions patched:

| Function | CPP Applied | Why |
|----------|------------|-----|
| `_generate_rep_reply` | **CPP_VOICE (full)** | This IS Steve talking. "Respond for me" drafts. |
| `_generate_guardz_reply` | **CPP_VOICE (full)** | Represents Rain Networks / Steve to visitors. |
| `_generate_sales_brief` | CPP_STYLE_LIGHT | Brief is FOR Steve to read. Match his preferred reading style. |
| `_generate_call_summary` | CPP_STYLE_LIGHT | Summary is FOR Steve to read. |
| `_generate_copilot` | CPP_STYLE_LIGHT | Coaching tips match Steve's terse, direct preference. |
| `_generate_customer_reply` | **UNCHANGED** | Simulates a DIFFERENT person (the prospect). Not Steve's voice. |

### Modified: `Dockerfile`
Added `cpp_voice.py` to the COPY line.

## Deploy steps

1. Copy `cpp_voice.py` to your project root (same directory as `api.py`)
2. Merge the patched functions into your `api.py` (the HTML page endpoints at the bottom are omitted from the patch file — keep your originals)
3. Update `Dockerfile` to include `cpp_voice.py` in the COPY line
4. Run: `node --check api.py` — wait, this is Python. Run: `python3 -c "import ast; ast.parse(open('api.py').read()); print('OK')"`
5. Git workflow: `git add -A && git commit -m "CPP voice v3 injected into all LLM prompts" && git push`
6. Railway auto-deploys from git push

## Testing

Hit `/copilot` and click "Respond for me" — the suggested reply should now sound like YOU:
- Short punchy sentences (8-14 words)
- Fragments ("No security expertise needed.")
- "y'all" not "you guys"
- No em dashes anywhere
- Ellipsis for pauses
- Problem first, then solution
- Direct, warm through teaching not politeness
