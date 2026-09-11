@echo off
rem 9Router AutoVault - interactive restore (thin launcher)
setlocal
set "HERE=%~dp0"
python "%HERE%tools\autovault.py" restore %*
pause
