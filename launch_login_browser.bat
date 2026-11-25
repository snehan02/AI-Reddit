@echo off
title Reddit Login Helper for Shoro AI
echo ========================================================
echo   Opening Google Chrome for Reddit Login (Shoro-AI-02)
echo ========================================================
echo.
echo 1. Ensuring profile lock is released...
set DATA_DIR=%USERPROFILE%\.openclaw\browser\shoro-test\user-data
if exist "%DATA_DIR%\lockfile" del /f /q "%DATA_DIR%\lockfile" 2>nul
echo 2. Launching Google Chrome...
set CHROME_BIN=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_BIN=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_BIN if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_BIN=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_BIN if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME_BIN=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_BIN set CHROME_BIN=chrome.exe

start "" "%CHROME_BIN%" --remote-debugging-port=18801 --user-data-dir="%DATA_DIR%" --no-first-run --no-default-browser-check "https://www.reddit.com/login"
echo.
echo ========================================================
echo Chrome window has opened on your screen!
echo Please log in to Reddit using your account (Shoro-AI-02).
echo Once logged in, simply close that Chrome window.
echo Your session will be saved automatically for the bot!
echo ========================================================
echo.
pause
