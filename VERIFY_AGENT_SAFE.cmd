@echo off
rem AUTHORITATIVE external-agent safety gate (whole repository tree)
python "%~dp0tools\verify_agent_safe.py" --root "%~dp0."
exit /b %errorlevel%
