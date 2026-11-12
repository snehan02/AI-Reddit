@echo off
title Reddit Login Helper for Shoro AI
echo ========================================================
echo   Opening Google Chrome for Reddit Login (Shoro-AI-02)
echo ========================================================
echo.
echo 1. Ensuring profile lock is released...
python "C:\Users\sneha\.gemini\antigravity\brain\ee628f13-5ecc-4120-b360-bf8f061bfb3a\scratch\clean_shoro_profile.py" 2>nul
echo 2. Launching Google Chrome...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%USERPROFILE%\.openclaw\browser\shoro-test\user-data" --no-first-run --no-default-browser-check "https://www.reddit.com/login"
echo.
echo ========================================================
echo Chrome window has opened on your screen!
echo Please log in to Reddit using your account (Shoro-AI-02).
echo Once logged in, simply close that Chrome window.
echo Your session will be saved automatically for the bot!
echo ========================================================
echo.
pause
