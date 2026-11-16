@echo off
set "URL=%~1"
if "%URL%"=="" set "URL=https://www.reddit.com"
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=18801 --user-data-dir="%USERPROFILE%\.openclaw\browser\shoro-test\user-data" --no-first-run --no-default-browser-check --start-maximized "%URL%"

