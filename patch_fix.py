"""
patch_fix.py — Fix the apostrophe syntax error in _generate_practice_coach.
Run from your InteractiveChat project root.
"""
import sys

with open("api.py", "r", encoding="utf-8") as f:
    code = f.read()

# The problem: customer's has an unescaped apostrophe inside a single-quoted string
old = """'"tone":"<3-7 word read of the customer's mood right now>",'"""
new = """'"tone":"<3-7 word read of how the customer feels right now>",'"""

if old in code:
    code = code.replace(old, new, 1)
    with open("api.py", "w", encoding="utf-8") as f:
        f.write(code)
    print("Fixed apostrophe on line 1611.")
else:
    print("Anchor not found. Checking for the error another way...")
    # Try alternate form in case escaping differs
    for bad in ["customer's mood", "customer\\'s mood"]:
        if bad in code:
            code = code.replace(bad, "how the customer feels", 1)
            with open("api.py", "w", encoding="utf-8") as f:
                f.write(code)
            print(f"Fixed: replaced '{bad}' variant.")
            break
    else:
        print("Could not find the broken string. Check api.py line 1611 manually.")
        sys.exit(1)

import ast
try:
    ast.parse(code)
    print("Syntax OK.")
except SyntaxError as e:
    print(f"STILL BROKEN: {e}")
    sys.exit(1)
