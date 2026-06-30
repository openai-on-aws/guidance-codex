#Requires -Version 5.1
<#
.SYNOPSIS
    Generate a developer-specific otel-local-config.yaml from the template (Windows).

.DESCRIPTION
    PowerShell counterpart to generate-sidecar-config.sh. Renders the sidecar
    template, which sets identity/org as OTEL RESOURCE attributes (copied onto
    datapoints by a transform processor). Emitting them as resource attributes is
    what lets one "@resource."-prefixed dashboard work for BOTH this sidecar path
    and the no-collector bearer-token path (where Codex sets the same keys via
    OTEL_RESOURCE_ATTRIBUTES).

    Identity fields (user.email, user.id) are derived from the active AWS SSO
    session. Org attributes can be supplied three ways:

      1. -AutoLookup   Pull Department, Organization, CostCenter, Title, Locale,
                       Manager, and display name from the IAM Identity Center
                       identity store (requires sso-admin:ListInstances +
                       identitystore:DescribeUser on the calling role). Ideal for
                       MDM / fleet scripts run by an admin role with IdC read access.

      2. Explicit flags  -Department, -Team, -CostCenter, etc. Explicit flags win
                         over any -AutoLookup value for the fields they supply.

      3. Omit            The field's resource block is removed from the rendered
                         config, so no attribute (and no empty CloudWatch
                         dimension) is emitted. The transform statements are
                         guarded on the attribute existing, so a removed block is
                         automatically skipped downstream too.

    MDM / fleet usage (Intune, Group Policy, login script): run once per device
    under an admin profile and write the output to a known path; the management
    policy then starts the sidecar against it. See
    docs/deploy-identity-center.md "MDM distribution".

.PARAMETER Region
    AWS region (required). e.g. us-west-2

.PARAMETER Profile
    AWS named profile (default: codex-bedrock)

.PARAMETER UserEmail
    Override derived email

.PARAMETER UserId
    Override derived user ID

.PARAMETER UserName
    Display name (optional)

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
    # Auto-populate from IdC (recommended for MDM / fleet scripts)
    .\generate-sidecar-config.ps1 -Region us-west-2 -AutoLookup

.EXAMPLE
    # Auto-lookup with one field overridden
    .\generate-sidecar-config.ps1 -Region us-west-2 -AutoLookup -Team infra

.EXAMPLE
    # Fully manual
    .\generate-sidecar-config.ps1 -Region us-west-2 `
        -Department Engineering -Team platform -CostCenter CC-9001 `
        -Output C:\codex\otel-local-config.yaml
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Region,
    [string]$Profile      = "codex-bedrock",
    [string]$UserEmail    = "",
    [string]$UserId       = "",
    [string]$UserName     = "",
    [switch]$AutoLookup,
    [string]$Department   = "",
    [string]$Team         = "",
    [string]$CostCenter   = "",
    [string]$Organization = "",
    [string]$Location     = "",
    [string]$Role         = "",
    [string]$Manager      = "",
    [string]$Output       = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Template  = Join-Path $ScriptDir "..\templates\otel-local-config.yaml"

function Log { param($msg) Write-Host "[$([datetime]::Now.ToString('HH:mm:ss'))] $msg" -ForegroundColor Cyan }
function Ok  { param($msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function Err { param($msg) Write-Error "[ERROR] $msg" }

# Safe property accessor: returns $null for a missing property instead of
# throwing under Set-StrictMode (ConvertFrom-Json objects vary by IdP).
function Get-Prop {
    param($obj, [string]$name)
    if ($null -eq $obj) { return $null }
    $p = $obj.PSObject.Properties[$name]
    if ($p) { return $p.Value }
    return $null
}

if (-not (Test-Path $Template)) { Err "Template not found: $Template"; exit 1 }

if (-not ($Region -match '^[a-z]{2}-[a-z]+-[0-9]+$')) {
    Err "Invalid -Region '$Region' (expected format like 'us-west-2')"
    exit 1
}

$needsAws = ($UserEmail -eq "") -or ($UserId -eq "") -or $AutoLookup
if ($needsAws -and -not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Err "AWS CLI v2 is required but was not found in PATH."
    exit 1
}

# ---------------------------------------------------------------------------
# Derive identity from the SSO session (skipped when both are already supplied)
# ---------------------------------------------------------------------------
$ssoUsername = ""
if ($needsAws) {
    Log "Deriving identity from AWS profile '$Profile'..."
    try {
        $callerArn = aws sts get-caller-identity --profile $Profile --query Arn --output text 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($callerArn)) { throw }
    } catch {
        Err "Could not get caller identity for profile '$Profile'. Run: aws sso login --profile $Profile"
        exit 1
    }

    # SSO ARN: arn:aws:sts::<account>:assumed-role/AWSReservedSSO_<permset>_<hex>/<username>
    $ssoUsername = $callerArn.Split("/")[-1]

    if ($UserEmail -eq "") { $UserEmail = $ssoUsername; Log "Derived user.email: $UserEmail" }
    if ($UserId   -eq "") {
        # Stable unique ID: role/username portion of the ARN, without account number.
        $UserId = ($callerArn -replace ".*assumed-role/", "")
        Log "Derived user.id: $UserId"
    }
}

# ---------------------------------------------------------------------------
# Auto-lookup org attributes from the IdC identity store
# ---------------------------------------------------------------------------
if ($AutoLookup) {
    Log "Looking up org attributes for '$ssoUsername' in IdC identity store..."

    $identityStoreId = $null
    try {
        $idcJson = aws sso-admin list-instances --region us-east-1 --profile $Profile --output json 2>$null | ConvertFrom-Json
        $identityStoreId = (Get-Prop $idcJson "Instances")[0].IdentityStoreId
    } catch { $identityStoreId = $null }

    if ([string]::IsNullOrEmpty($identityStoreId)) {
        Write-Warning "Could not discover IdC instance (needs sso-admin:ListInstances). Continuing without org attributes."
    } else {
        Log "Identity store: $identityStoreId"

        $idcUserId = $null
        try {
            $listJson = aws identitystore list-users `
                --identity-store-id $identityStoreId `
                --filters "AttributePath=UserName,AttributeValue=$ssoUsername" `
                --region us-east-1 --profile $Profile --output json 2>$null | ConvertFrom-Json
            $users = Get-Prop $listJson "Users"
            if ($users -and $users.Count -gt 0) { $idcUserId = $users[0].UserId }
        } catch { $idcUserId = $null }

        if ([string]::IsNullOrEmpty($idcUserId)) {
            Write-Warning "User '$ssoUsername' not found in identity store. Continuing without org attributes."
        } else {
            Log "IdC UserId: $idcUserId"
            try {
                $u = aws identitystore describe-user `
                    --identity-store-id $identityStoreId `
                    --user-id $idcUserId `
                    --extensions aws:identitystore:enterprise `
                    --region us-east-1 --profile $Profile --output json 2>$null | ConvertFrom-Json

                # Standard SCIM fields
                $emails = Get-Prop $u "Emails"
                $lkEmail = if ($emails -and $emails.Count -gt 0) { $emails[0].Value } else { "" }
                $lkName  = Get-Prop $u "DisplayName"
                if ([string]::IsNullOrEmpty($lkName)) { $lkName = Get-Prop (Get-Prop $u "Name") "Formatted" }
                $lkLoc   = Get-Prop $u "Locale"
                if ([string]::IsNullOrEmpty($lkLoc)) {
                    $addrs = Get-Prop $u "Addresses"
                    if ($addrs -and $addrs.Count -gt 0) { $lkLoc = Get-Prop $addrs[0] "Locality" }
                }
                $lkRole  = Get-Prop $u "Title"

                # Enterprise extension (aws:identitystore:enterprise), synced via SCIM
                $ext     = Get-Prop (Get-Prop $u "Extensions") "aws:identitystore:enterprise"
                $lkDept  = Get-Prop $ext "Department"
                $lkOrg   = Get-Prop $ext "Organization"
                $lkCc    = Get-Prop $ext "CostCenter"
                $mgrObj  = Get-Prop $ext "Manager"
                $lkMgr   = ""
                if ($mgrObj) {
                    if ($mgrObj -is [string]) { $lkMgr = $mgrObj }
                    elseif (Get-Prop $mgrObj "value")       { $lkMgr = Get-Prop $mgrObj "value" }
                    elseif (Get-Prop $mgrObj "displayName") { $lkMgr = Get-Prop $mgrObj "displayName" }
                }

                # Explicit flags win over looked-up values.
                if ($UserEmail    -eq "" -and $lkEmail) { $UserEmail    = $lkEmail }
                if ($UserName     -eq "" -and $lkName)  { $UserName     = $lkName }
                if ($Department   -eq "" -and $lkDept)  { $Department   = $lkDept }
                if ($Organization -eq "" -and $lkOrg)   { $Organization = $lkOrg }
                if ($CostCenter   -eq "" -and $lkCc)    { $CostCenter   = $lkCc }
                if ($Location     -eq "" -and $lkLoc)   { $Location     = $lkLoc }
                if ($Role         -eq "" -and $lkRole)  { $Role         = $lkRole }
                if ($Manager      -eq "" -and $lkMgr)   { $Manager      = $lkMgr }

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
# Strategy mirrors generate-sidecar-config.sh: substitute placeholders only for
# fields that HAVE a value (required region/email/id always). Then drop any
# 3-line resource block whose value: line still holds an unfilled
# "__PLACEHOLDER__" — so we never emit an empty-string attribute. Literal
# String.Replace() is used (not -replace) so values containing regex/`$`
# metacharacters render verbatim.
Log "Rendering template -> $Output"

$subs = [ordered]@{
    "__AWS_REGION__" = $Region
    "__USER_EMAIL__" = $UserEmail
    "__USER_ID__"    = $UserId
}
if ($UserName)     { $subs["__USER_NAME__"]    = $UserName }
if ($Department)   { $subs["__DEPARTMENT__"]   = $Department }
if ($Team)         { $subs["__TEAM_ID__"]      = $Team }
if ($CostCenter)   { $subs["__COST_CENTER__"]  = $CostCenter }
if ($Organization) { $subs["__ORGANIZATION__"] = $Organization }
if ($Location)     { $subs["__LOCATION__"]     = $Location }
if ($Role)         { $subs["__ROLE__"]         = $Role }
if ($Manager)      { $subs["__MANAGER__"]      = $Manager }

# Pass 1: substitute filled placeholders, line by line.
$rendered = [System.Collections.Generic.List[string]]::new()
foreach ($line in (Get-Content -LiteralPath $Template)) {
    $l = [string]$line
    foreach ($k in $subs.Keys) { $l = $l.Replace($k, [string]$subs[$k]) }
    [void]$rendered.Add($l)
}

# Pass 2: drop any 3-line block (- key: / value: "__X__" / action:) whose value
# line still holds an unfilled placeholder (key line above + action line below).
$drop = [System.Collections.Generic.HashSet[int]]::new()
for ($i = 0; $i -lt $rendered.Count; $i++) {
    if ($rendered[$i] -match 'value:\s*"__[A-Z_]+__"') {
        [void]$drop.Add($i - 1)
        [void]$drop.Add($i)
        [void]$drop.Add($i + 1)
    }
}
$final = [System.Collections.Generic.List[string]]::new()
for ($i = 0; $i -lt $rendered.Count; $i++) {
    if (-not $drop.Contains($i)) { [void]$final.Add($rendered[$i]) }
}

# Write UTF-8 without BOM so the OTEL collector can parse it.
[IO.File]::WriteAllText($Output, ($final -join "`n") + "`n", [Text.UTF8Encoding]::new($false))

Ok "Generated: $Output"

function Show($label, $val) {
    if ([string]::IsNullOrEmpty($val)) { Write-Host ("  {0}{1}" -f $label, "<omitted>") }
    else { Write-Host ("  {0}{1}" -f $label, $val) }
}
Write-Host ""
Show "user.email:   " $UserEmail
Show "user.id:      " $UserId
Show "user.name:    " $UserName
Show "department:   " $Department
Show "team.id:      " $Team
Show "cost_center:  " $CostCenter
Show "organization: " $Organization
Show "location:     " $Location
Show "role:         " $Role
Show "manager:      " $Manager
Write-Host ""
Write-Host "Start the sidecar with:"
Write-Host "  .\otelcol-local-windows-amd64.exe --config `"$Output`""
