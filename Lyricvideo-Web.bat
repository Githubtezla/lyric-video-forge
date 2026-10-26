@echo off
rem Starts the web UI of lyricvideo.py in the browser.
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" "%~dp0lyricvideo.py" web
pause
