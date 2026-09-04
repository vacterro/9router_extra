@echo off
rem Create an external-agent Git worktree: CREATE_AGENT_WORKTREE.cmd <name>
if "%~1"=="" (
    echo Usage: CREATE_AGENT_WORKTREE.cmd ^<agent-name^>
    exit /b 2
)
python "%~dp0tools\agent_worktree.py" create "%~1"
exit /b %errorlevel%
