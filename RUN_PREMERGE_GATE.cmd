@echo off
rem FULL PRE-MERGE pipeline: safety scan -> tests -> compile -> git sanity -> re-scan
python "%~dp0tools\run_premerge_gate.py" %*
exit /b %errorlevel%
