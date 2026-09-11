@echo off
rem 9Router AutoVault - remove task + credential (thin launcher)
setlocal
set "HERE=%~dp0"
python "%HERE%tools\autovault.py" remove
pause
