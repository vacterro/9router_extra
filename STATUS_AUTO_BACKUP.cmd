@echo off
rem 9Router AutoVault - status report (thin launcher)
setlocal
set "HERE=%~dp0"
python "%HERE%tools\autovault.py" status
pause
