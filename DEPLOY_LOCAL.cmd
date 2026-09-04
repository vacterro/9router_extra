@echo off
rem Trusted local deploy: validate -> test -> deploy CODE only (private state untouched)
python "%~dp0tools\deploy_local.py" %*
exit /b %errorlevel%
