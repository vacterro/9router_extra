@echo off
rem 9Router AutoVault - one-time setup wizard (thin launcher)
setlocal
set "HERE=%~dp0"
python "%HERE%tools\autovault.py" setup
pause
