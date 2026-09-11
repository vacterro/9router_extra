@echo off
rem 9Router AutoVault - manual backup run (thin launcher)
setlocal
set "HERE=%~dp0"
python "%HERE%tools\autovault.py" run
