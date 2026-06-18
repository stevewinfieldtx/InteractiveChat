@echo off
echo ============================================
echo   CPP Voice Patch - One Click Deploy
echo ============================================
echo.

if not exist api.py (
    echo ERROR: api.py not found. Run this from your InteractiveChat project root.
    pause
    exit /b 1
)

echo [1/3] Patching...
py patch_cpp.py
if %errorlevel% neq 0 (
    echo.
    echo PATCH FAILED. Check output above.
    pause
    exit /b 1
)

echo.
echo [2/3] Git commit...
git add -A
git commit -m "CPP voice v3 injected into all LLM prompts"

echo.
echo [3/3] Pushing to Railway...
git push

echo.
echo ============================================
echo   DONE. Railway will auto-deploy.
echo   Test at: /copilot then click Respond for me
echo ============================================
pause
