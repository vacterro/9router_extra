@echo off
rem Review an external-agent worktree: REVIEW_AGENT_WORKTREE.cmd <name>
if "%~1"=="" (
    echo Usage: REVIEW_AGENT_WORKTREE.cmd ^<agent-name^>
    exit /b 2
)
python "%~dp0tools\agent_worktree.py" review "%~1"
exit /b %errorlevel%
