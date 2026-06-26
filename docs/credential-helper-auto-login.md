# Seamless Auto-Login (optional credential helper)

This optional helper makes IdC sign-in automatic. It wires the profile's
`credential_process` to a small script that returns cached credentials while the
token is valid and **triggers `aws sso login` on demand** when a fresh token is
needed. Codex resolves credentials through the standard AWS SDK chain, so the
developer's daily loop becomes simply launching Codex — a browser pops (or a
device-code prompt appears on headless hosts) once per working day, and every
call after that is silent until the token expires.

The helper uses the AWS SDK's own `credential_process` mechanism, configured in
`~/.aws/config`, so it works with the built-in `amazon-bedrock` provider exactly
as it works for any AWS SDK consumer.

**This covers the CLI, the IDE extension, and the desktop app alike.** All Codex
surfaces read the same user-level `~/.codex/config.toml` and resolve Bedrock
credentials through the same AWS SDK chain, so the helper fires no matter how
Codex is launched — there is nothing IDE- or app-specific to configure. (In a
remote IDE session — Remote-SSH, a devcontainer, or Codespaces — credentials
resolve on the *remote* host, which is browser-less; follow the
[headless](#headless--ssh--ci-hosts) pre-warm path there.)

## Before you start

**Install the tooling** (skip any you already have):

```bash
# AWS CLI v2 — macOS: brew install awscli | Linux: see https://aws.amazon.com/cli/
#                Windows: winget install Amazon.AWSCLI
# Codex CLI (needs Node.js >= 16):
npm install -g @openai/codex
# or follow https://developers.openai.com/codex/cli
```

**Get these three values from your admin** (the same values distributed in
QUICKSTART "Step 4: Distribute Configuration"):

| Value | Looks like | Where it goes |
|---|---|---|
| IdC start URL | `https://d-xxxxxxxxxx.awsapps.com/start` | `sso_start_url` |
| AWS account ID | `123456789012` | `sso_account_id` |
| Permission set name | `CodexBedrockUser` | `sso_role_name` |

**Clone the repo** (the helper scripts live in it) — or download the script(s)
for your platform from [`deployment/scripts/`](../deployment/scripts/):

```bash
git clone https://github.com/openai-on-aws/guidance-codex.git
cd guidance-codex
```

This helper replaces only the manual `aws sso login` step; the IdC permission set
and the Codex `amazon-bedrock` provider come from
[QUICKSTART_NATIVE_AWS_ACCESS.md](QUICKSTART_NATIVE_AWS_ACCESS.md).

## How it works

```
codex → AWS SDK credential chain → profile [codex-bedrock]
        → credential_process → codex-sso-creds helper
            1. aws configure export-credentials  (fast path: cached token → JSON)
            2. on demand → aws sso login         (browser / device-code)
            3. retry export-credentials → JSON
```

The helper writes **only the credential JSON to stdout** and routes all login
chatter, prompts, and errors to stderr — exactly the format the AWS SDK expects
from a `credential_process`.

The profile layout uses **two profiles** alongside the `codex-bedrock-sso`
sso-session from the quickstart:

- `codex-bedrock-base` — the *base* SSO profile (`sso_session`, account, role) the
  helper resolves credentials from.
- `codex-bedrock` — the *Codex-facing* profile whose `credential_process` calls
  the helper, passing the base profile and sso-session names as arguments
  (profile first, sso-session second — the order the helper expects).

> **Already followed [QUICKSTART_NATIVE_AWS_ACCESS.md](QUICKSTART_NATIVE_AWS_ACCESS.md)?**
> You have a `[profile codex-bedrock]` that holds the SSO settings directly.
> To switch to the helper: **rename** that block's header to
> `[profile codex-bedrock-base]` (keep its `sso_session`, `sso_account_id`,
> `sso_role_name`, `region` lines as-is), then **add** the new
> `[profile codex-bedrock]` shown below — the one with the `credential_process`
> line. Your `~/.codex/config.toml` already points at `codex-bedrock`, so it needs
> no change. End state: one `[sso-session codex-bedrock-sso]` + two profiles
> (`codex-bedrock-base`, `codex-bedrock`).

The helper scripts live in [`deployment/scripts/`](../deployment/scripts/):

| Platform | Script | Notes |
|---|---|---|
| macOS / Linux | `codex-sso-creds` | bash; auto-detects headless and uses device-code |
| Windows | `codex-sso-creds.ps1` + `codex-sso-creds.cmd` | `.cmd` shim launches the `.ps1` |

## macOS / Linux

Install the helper to a fixed location (e.g. `~/.local/bin`) and make it
executable. The `credential_process` line below references it by absolute path,
so it does not need to be on your `PATH`. Run this from the root of your cloned
`guidance-codex` repo:

```bash
install -m 0755 deployment/scripts/codex-sso-creds ~/.local/bin/codex-sso-creds
```

Append to `~/.aws/config` (replace the start URL, account ID, and `sso_role_name`
with your values):

```ini
[sso-session codex-bedrock-sso]
sso_start_url = https://d-xxxxxxxxxx.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

# Base SSO profile — the helper resolves credentials from this.
[profile codex-bedrock-base]
sso_session = codex-bedrock-sso
sso_account_id = 123456789012
sso_role_name = CodexBedrockUser     # your IdC permission set name (see deploy-identity-center.md step 4)
region = us-east-1

# Codex-facing profile — delegates to the helper, which triggers
# `aws sso login` automatically when a fresh token is needed.
[profile codex-bedrock]
region = us-east-1
credential_process = sh -c 'exec "$HOME/.local/bin/codex-sso-creds" codex-bedrock-base codex-bedrock-sso'
```

The Codex `~/.codex/config.toml` points at the **Codex-facing** profile. Set
`aws.region` to a region that serves your model — `openai.gpt-5.4` on the Codex
Bedrock (Mantle) path is verified in `us-east-1`:

```toml
model_provider = "amazon-bedrock"
model = "openai.gpt-5.4"

[model_providers.amazon-bedrock.aws]
region = "us-east-1"
profile = "codex-bedrock"
```

> The built-in `amazon-bedrock` provider reads `aws.profile` and `aws.region`
> from this block — keep it to those two keys so Codex starts cleanly.

### Headless / SSH / CI hosts

On a headless host, **pre-warm the token before launching Codex**:

```bash
aws sso login --sso-session codex-bedrock-sso --use-device-code
```

This prints a verification URL + one-time code; complete it on any device with a
browser. **When the command returns to your prompt, the token is cached** —
verify with `aws sts get-caller-identity --profile codex-bedrock`. With the token
cached, Codex hits the helper's fast path and runs without interruption.

The benefit the helper still gives you on a headless host: it detects the
browser-less session (SSH connection; no `DISPLAY`/`WAYLAND_DISPLAY` on Linux; on
macOS, when `launchctl` reports a non-Aqua session manager) and selects
`--use-device-code` for you on the logins it does run, so the same profile works
unchanged on desktops and headless hosts. (You still pre-warm once per token
lifetime, just as with the manual flow — on-demand mid-run login needs a local
browser, covered in the note below.)

> On-demand login (letting the helper trigger the device-code flow mid-run)
> works only when there is a local browser: from inside `credential_process` the
> verification URL+code is captured by the AWS SDK rather than shown in your
> terminal, so Codex would wait on a prompt you can't see. Pre-warming as above
> is the reliable headless path. The helper still auto-detects headless sessions
> and selects `--use-device-code` for the cases where on-demand login does run.

## Windows

Two files work together: `credential_process` points at the `.cmd` shim, which
launches the `.ps1` under `powershell.exe -NoProfile -ExecutionPolicy Bypass`.
(The AWS SDK invokes `credential_process` as a command line, so the `.cmd` shim
provides that entry point and forwards the arguments to the PowerShell helper.)

From your cloned `guidance-codex` repo, copy both scripts from
`deployment\scripts\` to a known location, e.g. `C:\Users\<you>\codex\`:

```
deployment\scripts\codex-sso-creds.cmd
deployment\scripts\codex-sso-creds.ps1
```

Append to `%USERPROFILE%\.aws\config` (replace the start URL, account ID, and
`sso_role_name` with your values). For the `credential_process` path, use **your
real profile directory** — run `echo %USERPROFILE%` in `cmd` to find it (e.g.
`C:\Users\jsmith`) and substitute it for `C:\Users\<you>` below. Use single
backslashes (INI files do not process `\\` escapes), and wrap the `.cmd` path in
double quotes if it contains spaces, e.g.
`credential_process = "C:\Users\Jane Smith\codex\codex-sso-creds.cmd" codex-bedrock-base codex-bedrock-sso`.

```ini
[sso-session codex-bedrock-sso]
sso_start_url = https://d-xxxxxxxxxx.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile codex-bedrock-base]
sso_session = codex-bedrock-sso
sso_account_id = 123456789012
sso_role_name = CodexBedrockUser
region = us-east-1
output = json

[profile codex-bedrock]
region = us-east-1
output = json
credential_process = C:\Users\<you>\codex\codex-sso-creds.cmd codex-bedrock-base codex-bedrock-sso
```

The `~/.codex/config.toml` block is identical to the macOS/Linux one above.

The `.ps1` runs `aws.exe` through `Start-Process` with file-redirected stdio and
sets `$PSNativeCommandUseErrorActionPreference = $false`, so the CLI's stdout and
stderr pass through verbatim and the stdout stays pure credential JSON. If PATH
is stripped by a GUI launcher, it falls back to
`%ProgramFiles%\Amazon\AWSCLIV2\aws.exe`. (The bash helper applies the same
PATH fallback to `/opt/homebrew/bin`, `/usr/local/bin`, and `/usr/bin`.)

### No browser on Windows (RDP-less / Server Core)

Pre-warm the token before launching Codex (same reasoning as the macOS/Linux
headless section above — on-demand login needs a local browser):

```bat
aws sso login --sso-session codex-bedrock-sso --use-device-code
```

Complete the printed URL + code on another device; when the command returns, the
token is cached. To also have the helper select the device-code flow on the
occasions it does run a login, add `-UseDeviceCode` to the `powershell.exe` line
in **your copy** of `codex-sso-creds.cmd` (the one in `C:\Users\<you>\codex\`, not
the repo checkout — a `git pull` would overwrite an edit made in the checkout):

```bat
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%codex-sso-creds.ps1" -AwsProfile "%~1" -SsoSession "%~2" -UseDeviceCode
```

## Verify

First, sanity-check your `~/.aws/config` has the expected three blocks — one
sso-session and two profiles:

```bash
grep -E '^\[(sso-session|profile)' ~/.aws/config | grep codex-bedrock
# Expect exactly:
#   [sso-session codex-bedrock-sso]
#   [profile codex-bedrock-base]
#   [profile codex-bedrock]
```

Then run the helper's exact path. On first use it opens a browser and returns
credential JSON; the next run returns JSON **instantly from the cached token**:

```bash
# macOS / Linux and Windows (cmd / PowerShell)
aws configure export-credentials --profile codex-bedrock --format process
```

**Success looks like** a JSON object containing `Version`, `AccessKeyId` (starts
with `ASIA`), `SecretAccessKey`, `SessionToken`, and `Expiration` — printed to
stdout with nothing else mixed in. Confirm the resolved identity, then launch
Codex:

```bash
aws sts get-caller-identity --profile codex-bedrock
# → Arn: arn:aws:sts::<account>:assumed-role/AWSReservedSSO_CodexBedrockUser_.../<user>
codex
```

Pre-warming with this `export-credentials` call once before launching Codex gives
the smoothest first run: Codex then starts straight from the cached token.

## Operational notes

- **Confirm the signed-in identity is assigned the permission set.** The browser
  login completes against your IdP, and Bedrock access flows from the
  `CodexBedrockUser` permission set assigned to that user on the target account.
  Assign it with `aws sso-admin create-account-assignment` for each developer (or
  a group). To switch which user the helper authenticates as, run `aws sso logout`
  and clear the SSO cache (`~/.aws/sso/cache/*`, or
  `del /q %USERPROFILE%\.aws\sso\cache\*` on Windows) so the next login prompts
  fresh — the cached SSO token is keyed by `sso_start_url`, shared across users of
  that portal. Signing in through a private/incognito window keeps the intended
  IdP session distinct.
- **Match `aws.region` to a region that serves the model.** `openai.gpt-5.4` on
  the Codex Bedrock (Mantle) path is verified in `us-east-1`; point `aws.region`
  there (or at another region offering your model) so calls resolve promptly.
- **Keep `[model_providers.amazon-bedrock.aws]` to `region` and `profile`.** Those
  are the keys the built-in provider reads; limiting the block to them keeps Codex
  startup clean (verified on Codex 0.142.2).
