#Requires -Version 5.1
<#
.SYNOPSIS
    Generate a developer-specific otel-local-config.yaml from the template.

.DESCRIPTION
    Identity fields (user.email, user.id) are derived from the active AWS SSO
    session. Org attributes can be auto-populated from the IAM Identity Center
    identity store (--AutoLookup) or supplied manually. Explicit parameters
    always override auto-looked-up values.

.PARAMETER Region
    AWS region (required). e.g. us-west-2

.PARAMETER Profile
    AWS named profile (default: codex-bedrock)

.PARAMETER UserEmail
    Override derived email

.PARAMETER UserId
    Override derived user ID

.PARAMETER AutoLookup
    Fetch org attrs from the IdC identity store. Requires the calling role to
    have sso-admin:ListInstances and identitystore:DescribeUser.

.PARAMETER Department
    e.g. Engineering

.PARAMETER Team
    e.g. platform

.PARAMETER CostCenter
    e.g. CC-9001

.PARAMETER Organization
    e.g. ACME

.PARAMETER Location
    e.g. Seattle

.PARAMETER Role
    e.g. developer

.PARAMETER Manager
    e.g. manager@example.com

.PARAMETER Output
    Path to write the rendered config (default: otel-local-config-<user>.yaml
    next to the template)

.EXAMPLE
    # Minimal — identity only
    .\generate-sidecar-config.ps1 -Region us-west-2

.EXAMPLE
    # Auto-populate from IdC (recommended for MDM fleet scripts)
    .\generate-sidecar-config.ps1 -Region us-west-2 -AutoLookup

.EXAMPLE
    # Fully manual
    .\generate-sidecar-config.ps1 -Region us-west-2 `
        -Department Engineering -Team platform -CostCenter CC-9001 `
        -Output C:\codex\otel-local-config.yaml
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Region,
    [string]$Profile    = "codex-bedrock",
    [string]$UserEmail  = "",
    [string]$UserId     = "",
    [switch]$AutoLookup,
    [string]$Department  = "",
    [string]$Team        = "",
    [string]$CostCenter  = "",
    [string]$Organization = "",
    [string]$Location    = "",
    [string]$Role        = "",
    [string]$Manager     = "",
    [string]$Output      = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Template   = Join-Path $ScriptDir "..\templates\otel-local-config.yaml"

function Log  { param($msg) Write-Host "[$([datetime]::Now.ToString('HH:mm:ss'))] $msg" -ForegroundColor Cyan }
function Ok   { param($msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function Err  { param($msg) Write-Error "[ERROR] $msg" }

if (-not (Test-Path $Template)) { Err "Template not found: $Template" }

if (-not ($Region -match '^[a-z]{2}-[a-z]+-[0-9]+$')) {
    Err "Invalid -Region '$Region' (expected format like 'us-west-2')"
}

# ---------------------------------------------------------------------------
# Derive identity from SSO session (skipped when both are already supplied)
# ---------------------------------------------------------------------------
$needsAws = ($UserEmail -eq "") -or ($UserId -eq "") -or $AutoLookup

if ($needsAws) {
    if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
        Err "AWS CLI v2 is required but was not found in PATH."
    }

    Log "Deriving identity from AWS profile '$Profile'..."
    try {
        $callerArn = aws sts get-caller-identity --profile $Profile --query Arn --output text 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($callerArn)) { throw }
    } catch {
        Err "Could not get caller identity for profile '$Profile'. Run: aws sso login --profile $Profile"
    }

    # SSO ARN: arn:aws:sts::<account>:assumed-role/AWSReservedSSO_<permset>/<username>
    $ssoUsername = $callerArn.Split("/")[-1]

    if ($UserEmail -eq "") { $UserEmail = $ssoUsername; Log "Derived user.email: $UserEmail" }
    if ($UserId   -eq "") {
        $rolePath = ($callerArn -replace ".*assumed-role/", "")
        $UserId = $rolePath
        Log "Derived user.id: $UserId"
    }
}

# ---------------------------------------------------------------------------
# Auto-lookup org attributes from IdC identity store
# ---------------------------------------------------------------------------
if ($AutoLookup) {
    Log "Looking up org attributes for '$($callerArn.Split("/")[-1])' in IdC identity store..."

    try {
        $idcJson = aws sso-admin list-instances --region us-east-1 --profile $Profile --output json 2>$null | ConvertFrom-Json
        $identityStoreId = $idcJson.Instances[0].IdentityStoreId
    } catch { $identityStoreId = $null }

    if ([string]::IsNullOrEmpty($identityStoreId)) {
        Write-Warning "Could not discover IdC instance. Continuing without org attributes."
    } else {
        Log "Identity store: $identityStoreId"
        $ssoUser = $callerArn.Split("/")[-1]

        try {
            $listJson = aws identitystore list-users `
                --identity-store-id $identityStoreId `
                --filters "AttributePath=UserName,AttributeValue=$ssoUser" `
                --region us-east-1 --profile $Profile --output json 2>$null | ConvertFrom-Json
            $idcUserId = $listJson.Users[0].UserId
        } catch { $idcUserId = $null }

        if ([string]::IsNullOrEmpty($idcUserId)) {
            Write-Warning "User '$ssoUser' not found in identity store. Continuing without org attributes."
        } else {
            Log "IdC UserId: $idcUserId"
            try {
                $userJson = aws identitystore describe-user `
                    --identity-store-id $identityStoreId `
                    --user-id $idcUserId `
                    --extensions aws:identitystore:enterprise `
                    --region us-east-1 --profile $Profile --output json 2>$null | ConvertFrom-Json

                $ext = $userJson.Extensions."aws:identitystore:enterprise"

                if ($Department   -eq "" -and $ext.Department)   { $Department   = $ext.Department }
                if ($Organization -eq "" -and $ext.Organization)  { $Organization = $ext.Organization }
                if ($CostCenter   -eq "" -and $ext.CostCenter)    { $CostCenter   = $ext.CostCenter }
                if ($Location     -eq "" -and $userJson.Locale)   { $Location     = $userJson.Locale }
                if ($Role         -eq "" -and $userJson.Title)    { $Role         = $userJson.Title }
                $mgrObj = $ext.Manager
                if ($Manager -eq "" -and $mgrObj) {
                    $Manager = if ($mgrObj -is [string]) { $mgrObj } `
                               elseif ($mgrObj.value)       { $mgrObj.value } `
                               else                         { $mgrObj.displayName }
                }
                if ($UserEmail    -eq "" -and $userJson.Emails[0].Value) {
                    $UserEmail = $userJson.Emails[0].Value
                }

                Log "Org attributes populated from IdC identity store."
            } catch {
                Write-Warning "Failed to fetch user record from IdC. Continuing without org attributes."
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Default output path
# ---------------------------------------------------------------------------
if ($Output -eq "") {
    $safeName = $UserEmail -replace "@", "-" -replace "\.", "-"
    $Output = Join-Path (Split-Path $Template) "otel-local-config-$safeName.yaml"
}

# ---------------------------------------------------------------------------
# Render template
# ---------------------------------------------------------------------------
Log "Rendering template → $Output"

$content = Get-Content $Template -Raw
$content = $content `
    -replace "__AWS_REGION__",  $Region `
    -replace "__USER_EMAIL__",  $UserEmail `
    -replace "__USER_ID__",     $UserId `
    -replace "__DEPARTMENT__",  $Department `
    -replace "__TEAM_ID__",     $Team `
    -replace "__COST_CENTER__", $CostCenter `
    -replace "__ORGANIZATION__",$Organization `
    -replace "__LOCATION__",    $Location `
    -replace "__ROLE__",        $Role `
    -replace "__MANAGER__",     $Manager

# Write UTF-8 without BOM so the OTEL collector can parse it
[IO.File]::WriteAllText($Output, $content, [Text.UTF8Encoding]::new($false))

Ok "Generated: $Output"

Write-Host ""
Write-Host "  user.email:   $UserEmail"
Write-Host "  user.id:      $UserId"
Write-Host "  department:   $(if ($Department)   { $Department }   else { '<empty>' })"
Write-Host "  team.id:      $(if ($Team)          { $Team }         else { '<empty>' })"
Write-Host "  cost_center:  $(if ($CostCenter)   { $CostCenter }   else { '<empty>' })"
Write-Host "  organization: $(if ($Organization) { $Organization } else { '<empty>' })"
Write-Host "  location:     $(if ($Location)     { $Location }     else { '<empty>' })"
Write-Host "  role:         $(if ($Role)          { $Role }         else { '<empty>' })"
Write-Host "  manager:      $(if ($Manager)      { $Manager }      else { '<empty>' })"
Write-Host ""
Write-Host "Start the sidecar with:"
Write-Host "  .\otelcol-local-windows-amd64.exe --config `"$Output`""
