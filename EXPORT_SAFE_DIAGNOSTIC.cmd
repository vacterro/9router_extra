@echo off
rem Sanitized, scanner-gated diagnostic bundle -> OUTSIDE the repository
python "%~dp0tools\export_diagnostics.py"
exit /b %errorlevel%
