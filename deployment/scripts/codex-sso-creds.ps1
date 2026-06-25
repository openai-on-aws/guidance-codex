# credential_process helper for Codex-on-Bedrock SSO profiles (Windows).
#
# The AWS SDK invokes this from a profile's credential_process. If the cached
# SSO token is still valid, we return credentials immediately. If it's missing
# or expired, we trigger `aws sso login` (opens a browser) and then return
# credentials. Codex stays unaware of the auth state — users just see a
# browser pop once per working day.
#
# Args: -AwsProfile <aws profile name> -SsoSession <sso-session name>
#       Positional fallback: codex-sso-creds.ps1 <profile> <sso-session>
#
# CONTRACT: Only the credential JSON from `aws configure export-credentials
# --format process` is allowed on stdout. All login chatter, errors, and
# diagnostics go to stderr — otherwise the AWS SDK fails to parse the JSON.

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string]$AwsProfile,

  [Parameter(Mandatory = $true, Position = 1)]
  [string]$SsoSession,

  # Use device-code flow instead of the default browser flow. Useful for
  # headless / RDP-less sessions. Devs can flip this in the .cmd shim.
  [switch]$UseDeviceCode
)

$ErrorActionPreference = 'Stop'
# PS 7+ defaults can turn native-command stderr into terminating errors that
# look like script bugs. We want raw aws.exe behaviour: stdout/stderr/exit
# code passed through verbatim.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Script -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}

# Resolve the aws binary. `Get-Command` honours PATH; fall back to the standard
# AWS CLI v2 install location if PATH is stripped (some launchers do this).
$awsCmd = Get-Command aws.exe -ErrorAction SilentlyContinue
if ($awsCmd) {
  $AwsBin = $awsCmd.Source
} else {
  $fallbacks = @(
    "$env:ProgramFiles\Amazon\AWSCLIV2\aws.exe",
    "${env:ProgramFiles(x86)}\Amazon\AWSCLIV2\aws.exe"
  )
  $AwsBin = $fallbacks | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $AwsBin) {
  [Console]::Error.WriteLine('aws CLI not found on PATH')
  exit 1
}

function Invoke-Aws {
  # Run aws.exe with raw stdio piped through this process. PowerShell's `&`
  # operator wraps native stderr in ErrorRecord objects, which corrupts the
  # output stream; Start-Process with file redirection keeps the bytes intact.
  param(
    [string[]]$Arguments,
    [switch]$SuppressStderr,   # fast path: swallow stderr so a missing-token
                               # message doesn't reach the caller's terminal
    [switch]$StderrToOurStderr # login path: forward aws.exe stderr verbatim
  )
  $outFile = [IO.Path]::GetTempFileName()
  $errFile = [IO.Path]::GetTempFileName()
  try {
    $p = Start-Process -FilePath $AwsBin -ArgumentList $Arguments `
      -NoNewWindow -Wait -PassThru `
      -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    # Emit stdout to our stdout (this is the credential JSON on the happy path).
    $stdout = Get-Content $outFile -Raw
    if (-not [string]::IsNullOrEmpty($stdout)) { [Console]::Out.Write($stdout) }
    if ($StderrToOurStderr) {
      $stderr = Get-Content $errFile -Raw
      if (-not [string]::IsNullOrEmpty($stderr)) { [Console]::Error.Write($stderr) }
    }
    return $p.ExitCode
  } finally {
    Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
  }
}

$exportArgs = @('configure', 'export-credentials', '--profile', $AwsProfile, '--format', 'process')

if ((Invoke-Aws -Arguments $exportArgs -SuppressStderr) -eq 0) {
  exit 0
}

# Token missing or expired — launch interactive login, then retry.
$loginArgs = @('sso', 'login', '--sso-session', $SsoSession)
if ($UseDeviceCode) { $loginArgs += '--use-device-code' }

# `aws sso login` writes its "Attempting to open ..." messages and the
# device-code URL+code to stdout. We must keep our own stdout reserved for
# the credential JSON, so route everything from the login step to stderr.
$loginOut = [IO.Path]::GetTempFileName()
$loginErr = [IO.Path]::GetTempFileName()
try {
  $lp = Start-Process -FilePath $AwsBin -ArgumentList $loginArgs `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $loginOut -RedirectStandardError $loginErr
  $loginStdout = Get-Content $loginOut -Raw
  $loginStderr = Get-Content $loginErr -Raw
  if (-not [string]::IsNullOrEmpty($loginStdout)) { [Console]::Error.Write($loginStdout) }
  if (-not [string]::IsNullOrEmpty($loginStderr)) { [Console]::Error.Write($loginStderr) }
  if ($lp.ExitCode -ne 0) {
    [Console]::Error.WriteLine("aws sso login failed (exit $($lp.ExitCode))")
    exit $lp.ExitCode
  }
} finally {
  Remove-Item $loginOut, $loginErr -ErrorAction SilentlyContinue
}

# Final emit — surface stderr too, so a real export failure is visible.
exit (Invoke-Aws -Arguments $exportArgs -StderrToOurStderr)
