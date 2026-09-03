@echo off
REM ---------------------------------------------------------------------------
REM  THE DROP - desktop readiness check.
REM
REM  Double-click this after a restart. It syncs dependencies, starts the agent
REM  runner if it is not already running, and reports whether the GPU, the
REM  credentials and the runner are all in order.
REM
REM  Safe to run at any time: it never pulls code, never kills a healthy runner,
REM  and the single-instance lock means it cannot start a second one.
REM
REM  Lives at the repository root rather than in infrastructure\desktop\ so it is
REM  findable by someone who is not looking for it -- which, after a reboot at
REM  seven in the morning, is the point.
REM ---------------------------------------------------------------------------

REM %~dp0 is this file's directory, with a trailing backslash, and survives being
REM run from a shortcut or another working directory.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0infrastructure\desktop\start-desktop.ps1" %*

REM Hold the window open so a double-click shows its output instead of flashing
REM a console and closing. Skipped when run from an existing terminal, where the
REM output is already visible and a prompt would just be in the way.
echo %CMDCMDLINE% | find /i "%~0" >nul
if not errorlevel 1 pause
