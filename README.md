# infocus-editor

Unattended editing for a student TV broadcast. A producer picks the filmed show in the team Portal and clicks **Start automatic editing**. This program, running on the studio Mac, then does the rest:
1. Downloads the switcher recording and the show's queued packages.
2. Runs an AI agent session: Claude, Codex or Grok, signed in with a subscription, not an API key. The agent follows the `infocus-show` skill (`.claude/skills/infocus-show/`) to pick takes, replace every burned-in graphic with a fresh render and level the audio. ffmpeg assembles and encodes the show.
3. Checks the render with code.
4. Uploads the finished show for a producer to review and schedule.

Producers can also type a fix ("the Math Club card should say …"). The same agent session applies it and produces the next version.

Every edit also leaves `<job>/show/timeline.fcpxml`. Open it in DaVinci Resolve (the free version works) to fix an edit by hand.

The design is in `docs/design.md`. The two contracts are:
- `docs/portal-api.md`: the Portal routes, Drive file transfer and AI account pairing.
- `docs/job-contract.md`: the job folder shared by this worker and the skill.

## How it works

```
Portal  ── claim / heartbeat / progress / complete ──►  this worker (launchd, on the studio Mac)
        ◄─ brief + short-lived Drive links + sealed AI sign-ins ─┤
                                                          ├─ download recording + packages (Drive)
                                                          ├─ agent session: Claude │ Codex │ Grok
                                                          │    "Read …/SKILL.md first and follow it"
                                                          ├─ verify_render.py (hard checks, up to 2 resumes)
                                                          └─ upload show + poster (Drive) → Portal draft
```

| Module | Job |
|---|---|
| `editor/main.py`, `editor/worker.py` | CLI (`run`, `doctor`, `link`, `connect`) and the loop: a heartbeat every 60 s (also during jobs, carrying health, machine, AI accounts, pairing key and acks), Portal settings and commands, a health check every 10 min, one job at a time, updates between jobs. |
| `editor/link.py`, `editor/commands.py`, `editor/service.py` | Linking by device code; Portal commands (health check, update, restart, repair, log); restarting and reloading the launchd agent. |
| `editor/power.py`, `editor/logs.py` | Job-length sleep assertion on personal computers; scrubbed log tail. |
| `editor/joblog.py`, `editor/scrub.py`, `editor/debugbundle.py` | The per-job log and agent transcripts; secret scrubbing for every log; the debug bundle uploaded after each run. |
| `editor/runner.py`, `editor/sessions.py` | One job: prepare → agent sessions → verify → check the output → upload → complete. Also handles cancel, the 75-minute limit and falling back to the next AI account. |
| `editor/backends/` | One interface over `claude.py` (Agent SDK + OAuth token), `codex.py` (`codex exec --json`) and `grok.py` (`grok -p`). `guard.py` holds the rules that keep agents away from root; `proc.py` runs a CLI in its own process group. |
| `editor/pairing.py`, `editor/connect.py` | Connecting an AI account: the Mac's key pair and credential install; `connect` signs in on a producer's computer. |
| `editor/prepare.py`, `editor/recordings.py` | Downloads, then works out what the files are from their content (names and times can't be trusted): SD files, duplicates and Ignore'd files are skipped; files whose audio correlates are one recording; a program feed vs M/L/R cameras comes from the skill's frame classifier (`editor/frame_profile.py`); a role set in the Portal overrides. Writes `show/recordings.json`; recordings are never joined. |
| `editor/sync.py` | 1 kHz audio envelopes and FFT cross-correlation with a peak-ratio confidence. |
| `editor/portal.py`, `editor/drive.py` | Portal client with retries; Drive download with resume and chunked upload. |
| `editor/doctor.py` | Health check in plain sentences. |
| `editor/updater.py` | Remote self-update to the latest release or the Portal's pin, with rollback. |
| `editor/housekeeping.py` | Job folders kept 14 days, leftover job processes stopped. |
| `install/` | `install.sh` (entry point, run by the setup app), `bootstrap-root.sh` (the one admin step), `infocus-root` (the limited root helper), `user-setup.sh` (no admin; also used by updates and Repair), `uninstall.sh`, `lib.sh` (progress protocol). |
| `scripts/release.sh` | Publishing an update. |

**What makes it unattended:**
- The agent can't ask questions. It decides with the house rules and flags what they don't cover.
- Code decides when the edit is done (`verify_render.py`), not the agent.
- When one AI account hits its usage limit, the next one takes over.
- A stuck or crashed run still ends with a clear reason in the Portal. Cancel stops the agent's whole process group, so a running ffmpeg dies too.
- Bad inputs fail early with a plain message: an SD capture, a 40-second clip, or files the Portal didn't send.

## Set up a Mac: open InFocus Editor Setup

Setup is one signed app. Everything after that is managed from the Portal (**Automatic editing**).

1. **Open the app:** on the Mac that will edit, log in to the account the editor should run under and open **InFocus Editor Setup** (signed and notarized; built from `setup-app/`).
2. **What is this computer?**
   - **The editing Mac (dedicated):** a standard (non-admin) account, kept plugged in with the lid open.
   - **My own computer, for now (personal):** for running the editor on a producer's laptop until the team Mac is ready. Personal mode:
     - keeps the normal sleep settings and holds a sleep assertion only while a job runs
     - reuses a Homebrew, Codex or Grok that this account already has (never upgrades them)
     - stops with a plain message if `/opt/homebrew` belongs to another account

     The editor runs with that account's access, so a separate standard account is still the safer choice.
3. **Set up:** an admin types their name and password once into the standard macOS dialog. The app then shows each step (admin approval, command-line tools, Homebrew, video tools, AI tools, speech model, editor program, graphics package, background service, health check).
4. **Link:** the app shows a code like `K7Q2-M9XD` and an **Open Portal** button. A producer approves the Mac there; nobody copies a token.
5. **Done:**
   - In the Portal, connect an AI account (below) and check the machine's health.
   - On a dedicated Mac, turn on automatic login for the editor account (Users & Groups; the app has a button).

Opening the app again later offers **Repair** (re-runs setup; safe) and **Link again**.

Under the hood the app runs `install/install.sh --unattended --progress --repo <bundled git bundle> --package-src <bundled package> --portal <url> [--personal]` and reads its `@@` progress lines (`docs/setup-app.md`). From a terminal, `install/install.sh` still works interactively (it asks for the Portal address and the package folder).

**Managed (MDM) Macs:** if the district manages this Mac, IT may need to approve or run the admin step themselves. Give them `install/bootstrap-root.sh <editor user> <folder>` and list what it changes (the table below). An MDM profile can also block the sudoers rule or the power settings.

## Managed from the Portal

Dedicated editing Macs run **InFocus Editor Guard** alongside the service. It cancels normal macOS logout,
restart and shutdown requests, including between jobs. Setup and updates build it locally with Swift; no new
admin permission is needed. If it cannot start, the worker reports a blocking health problem and stays online
for remote Repair or updates. The update health check launches the helper to catch failures before accepting
an update. Personal computers
keep their normal logout and shutdown behavior. Sleep settings still use the existing power controls.

For intentional maintenance, stop the service in Terminal on the editing Mac with
`launchctl bootout gui/$(id -u)/com.infocus.editor`, then wait two seconds before logging out or shutting down.
Run `launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.infocus.editor.plist"` to start it again
without logging out. The guard exits when the worker stops or crashes. It cannot block forced shutdowns,
power loss, kernel crashes or forced administrator/MDM restarts. The next root
setup removes a weekday shutdown from the power schedule (the studio Mac's
6:00 PM shutdown) and leaves the 7:00 AM power-on. `install/prevent-shutdown.sh`
then runs every 30 seconds so a shutdown that comes back is cleared again. After an interruption, the editor needs a
logged-in user session again.
Protection pauses briefly while the editor service restarts for an update or a Portal restart command.

The guard uses AppKit's [`applicationShouldTerminate`](https://developer.apple.com/documentation/appkit/nsapplicationdelegate/applicationshouldterminate(_:))
callback to cancel termination. `caffeinate` only prevents sleep.

- **Settings:**
  - **AI accounts:** connect, and set their order (`agentOrder`, which overrides `agents` in config.toml).
  - **House rules:** sent with every job and written into the job folder. A remembered fix sends the updated text back to the Portal, which is the record.
  - **Update pin.**
- **Editing machines:** several Macs can be linked; one is **active** and gets new edits. Others stay on standby and only finish fixes for edits they made. **Make active** switches.
- **Commands per machine:**
  - **Health check** and **Log**: run at once, even mid-job.
  - **Update**, **Restart** and **Repair**: wait until the current job ends. Repair re-runs `user-setup.sh --update --unattended`, then restarts the editor.

  Each result comes back in the next heartbeat. **Log** sends the last 100 lines of the editor's log plus the last 150 lines of the latest job's log, with tokens scrubbed.
- **Unlink:** the machine shows "This Mac isn't linked to the Portal — open InFocus Editor Setup and choose Link again". It stops taking jobs and checks again every 5 minutes.

`~/InFocusEditor/config.toml` needs only `portal_url` (setup writes it, along with `machine_kind`); everything else has defaults.

## Connect an AI subscription

No API keys. The editor runs on a Claude, ChatGPT (Codex) or SuperGrok subscription that someone signs in to from their own computer.

1. **Pairing code:** in the Portal, open **Automatic editing → Connect AI account → Claude / Codex / Grok**. It shows a one-time code (good for 10 minutes) and a command.
2. **Run the command on your own computer:**
   ```sh
   uvx --from git+https://github.com/neelsatyavolu/infocus-editor infocus-editor connect codex --code ABCD-1234 --portal https://portal.example.org
   ```
   Add `--device-auth` to sign in with a device code instead of a browser (Codex, Grok).
3. **Sign in:**
   - **Claude:** runs `claude setup-token` and captures the token without showing it.
   - **Codex, Grok:** sign in fresh in a throwaway profile, so the editor never shares a refresh token with your own login (that would sign one of them out).
4. **Delivery:** the credential is sealed to the editing Mac's public key before it leaves your computer. The Portal only relays ciphertext, and the temporary profile is wiped.
5. **On the Mac:** within a minute the Mac installs it (Claude in the Keychain, Codex and Grok in `~/InFocusEditor/codex-home` / `grok-home`, mode 600), checks that the sign-in works and reports it. The panel shows each account as connected or not.

**Limits and order:** subscription usage limits apply. The Portal's agent order (or `agents` in `config.toml` until one is set) decides which account goes first. The first connected account edits; if it hits its limit or its sign-in stops working, the job restarts on the next one, and the edit's decisions say so. Connect more than one account to cover limits.

**Permissions:** agents run with approvals off. The boundary is the dedicated standard account, plus deny rules for root and system commands:
- Claude: a permission callback
- Grok: `--deny` rules
- Codex: execpolicy rules in its `CODEX_HOME`

Agents get only shell and file tools; no MCP servers are configured.

### What gets root, and why

`install/bootstrap-root.sh` is the only script that runs as root. It runs once:

| Change | Why |
|---|---|
| Installs the Xcode Command Line Tools (headless `softwareupdate`) | git and the Swift compiler for the anchor matte |
| Creates `/opt/homebrew` owned by the editor user and clones Homebrew into it (a personal computer's own Homebrew is reused) | Tools install and update without admin |
| `pmset -c sleep 0 disksleep 0 autorestart 1 womp 1` (dedicated Macs only) | Never sleep on the power adapter; restart after a power cut |
| Installs `/Library/InFocusEditor/bin/infocus-root` (root:wheel 755, in a folder only root can write) | The few root actions that come up later |
| Adds `/etc/sudoers.d/infocus-editor` (validated with `visudo -cf`, mode 440) | Lets **only** the editor user run **only** that helper, with no password |
| Writes `/Library/InFocusEditor/bootstrap.done` | So re-runs know the step is done and at which helper version |

The helper (`install/infocus-root`) has a fixed allowlist and strict argument checks. Anything else exits 2.

| Command | What it does |
|---|---|
| `sudo infocus-root status` | Helper version, Command Line Tools version, power settings |
| `sudo infocus-root apply-power` | Always on (a dedicated Mac's setup): the same as `always-on on` |
| `sudo infocus-root always-on on\|off` | On: never sleeps (lid closed or open, adapter or battery), restarts after a power cut, no auto-logout, no macOS update restarts. Off: macOS's own settings. The Portal's Always on / Normal power commands run it |
| `sudo infocus-root update-clt` | Installs a Command Line Tools update, and nothing else |

There is no blanket admin and no general passwordless `sudo`. The editing agents are also blocked from:
- `sudo`, `infocus-root`, `osascript … administrator` and `launchctl`
- the Keychain and the AI sign-in files
- the install scripts and the updater
- writes to `/Library` or `/etc`

The helper can't update itself: a release that changes it needs an admin to run `install.sh` again. Until then the doctor reports "an admin needs to approve once", and the update is rolled back.

### Remote commands (super admin only)

The super admin can run a shell command on a **dedicated** editing Mac without the Portal UI: `scripts/editor-shell.mjs` in the Portal repo writes a signed command to the database, the Mac picks it up with its next heartbeat and the output comes back the same way. `editor/shell.py` runs it only if:
- the machine is dedicated (a personal computer refuses every one),
- it's signed with the Ed25519 key whose public half is `editor/admin_shell.pub` (the private key never leaves the super admin's computer),
- the signature names this command and this machine (the sha256 of its device token), and it's under 10 minutes old.

It runs as the editor's own account with a time limit (no root; root stays limited to the helper's subcommands). Anyone running their own fork replaces `admin_shell.pub` with their own key, or deletes it to turn remote commands off.

### Updating remotely (no admin)

The production copy (`~/InFocusEditor/app`) updates itself, the whole checkout including the installer scripts and the skill. It checks at start-up, then every 15 minutes, between jobs only.

**Picking the version:**
- **Normal updates:** each editor moves to the newest GitHub release.
- **Pin or roll back:** set the update pin in the Portal (or `SHOW_EDITOR_TARGET_VERSION`). Editors move to exactly that tag, older or newer. Clear it to follow the newest release again.

**Each update:**
1. `git fetch --tags`, checking that the tag exists on GitHub (and `git verify-tag` if `require_signed_tags = true`).
2. **The admin check, before anything is touched:** if the release changes the root helper (`install/infocus-root`), the editor stays where it is. The panel shows "An update needs admin approval: open InFocus Editor Setup and choose Repair". Repair moves to the newest release first, so the admin step installs its helper.
3. `git checkout --detach <tag>`, then `install/user-setup.sh --update --unattended`. That installs new Homebrew packages and Python and node dependencies, with no prompts. It also rewrites the launchd definition if the release changed it.
4. `infocus-editor doctor --json`. If setup failed, or the doctor reports a problem that wasn't there before, the editor checks out the previous tag, re-runs setup and reports "Update to vX failed: …". It won't retry that tag until the target changes.
5. **On success:** the editor exits with code 10 and launchd starts it on the new code. `ThrottleInterval` (15 s) only delays that restart when the process ran less than 15 s. A changed launchd definition is reloaded by a small detached script a few seconds after the exit.

A dev checkout (anything other than `<data dir>/app`) never updates itself, unless `auto_update = true` is set.

### Publishing an update

- **`scripts/release.sh 0.3.0`** (from a clean `main`):
  1. bumps `pyproject.toml` and runs `uv lock`
  2. runs the tests that need no private fixtures or AI account
  3. commits "release v0.3.0" and creates an annotated tag (signed if `git config user.signingkey` is set)
  4. pushes the commit and tag, then runs `gh release create v0.3.0 --generate-notes`
- **Rollout:** every editor picks the release up within about 15 minutes. The Portal pin and the automatic rollback still apply.
- **CI:** `.github/workflows/checks.yml` runs those same tests and shellcheck on every push and pull request (macOS, uv).

### Uninstall

Run `install/uninstall.sh` as the editor user. It removes the launchd agent, then asks an admin once to remove `/etc/sudoers.d/infocus-editor` and `/Library/InFocusEditor`. It leaves `~/InFocusEditor` (config, job folders, AI sign-ins) and `/opt/homebrew`; delete those by hand if you want.

**Exit codes** (launchd restarts it after any exit):
- `10`: updated to a new release.
- `11`: restart or repair asked for in the Portal.
- `2`: `config.toml` is missing or invalid; open InFocus Editor Setup and choose Repair.

A Portal that rejects the token (401) doesn't stop the editor: it waits to be linked again.

## Logs and debugging

When an automatic edit goes wrong, these show what happened:

| What | Where (on the editing Mac) |
|---|---|
| The editor's log: start-up, heartbeat problems, one line when each job starts and ends | `~/Library/Logs/infocus-editor.log` |
| The job log: every step of every run of that job (claim, downloads, detection, each agent session, `verify_render` results, uploads, complete or fail, tracebacks) | `<job folder>/logs/editor.log` |
| The agent transcripts: each session's raw event stream, plus a CLI agent's stderr | `<job folder>/logs/agent-<NN>-<backend>.jsonl`, `agent-<NN>-<backend>.stderr.log` |
| The debug bundle: the job log, the transcripts and the job's small JSON files (brief, result, EDL, recordings, verify, analysis reports), no media | `<job folder>/logs/debug-<version>.tar.gz` |

Job folders are `~/InFocusEditor/jobs/<showDate>-<jobId>/` and are kept 14 days.

**From the Portal, without touching the Mac:**
- **Download debug log:** after every run (finished, failed or cancelled), the editor uploads the debug bundle, and the job's version gets this link.
- **Show log:** the machine's **Log** command returns the editor's recent log and the tail of the latest job's log.

**Secrets are scrubbed** from all of these before they're written: Claude tokens, editor tokens, Bearer headers, `token=` links and JSON fields such as `token`, `refresh_token` and `password`.

## Running and testing

```sh
uv run infocus-editor doctor     # health check (also says whether this Mac is linked and active)
uv run infocus-editor link       # link this Mac to the Portal (the setup app does this)
uv run infocus-editor run        # the worker loop (normally started by launchd)
uv run pytest                    # unit tests: fakes only, no network or real AI sessions
uv run pytest -m golden -s       # on the studio Mac only: a real agent on a fixture show (uses subscription usage)
```

Tests marked `fixtures` need private show recordings in `~/InFocusEditor/fixtures/`. Without them, those tests are skipped.

## Open source, private data

This repo is MIT licensed and public. It holds no names, recordings, rosters, NAS paths, school URLs or secrets.
- **Show data:** names, scripts and package files arrive from the Portal per job.
- **Local files:** fixtures, job folders and settings stay in `~/InFocusEditor/`. House rules live in the Portal.
- **Secrets:** the editor token (from linking) and the Claude token stay in the Keychain. Codex and Grok sign-ins stay in owner-only files in the data folder.

Keep it that way in contributions: tests and docs use placeholders (`portal.example.org`, "Abby Example").
