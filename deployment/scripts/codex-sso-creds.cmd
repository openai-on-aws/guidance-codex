@echo off
REM credential_process shim for Codex-on-Bedrock SSO profiles (Windows).
REM
REM The AWS SDK invokes credential_process as a command line. PowerShell
REM scripts can't be referenced directly, so point credential_process at
REM this .cmd file. It forwards %1 (profile) and %2 (sso-session) to the
REM PowerShell helper sitting next to it.
REM
REM Add -UseDeviceCode to the powershell line below for headless/RDP-less
REM hosts where no browser is available.

setlocal
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%codex-sso-creds.ps1" -AwsProfile "%~1" -SsoSession "%~2"
exit /b %ERRORLEVEL%
