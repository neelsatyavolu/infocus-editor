# Setup app

`InFocus Editor Setup.app` is the only thing a person opens on the editing Mac. It is signed with a Developer ID and notarized, so macOS opens it without a malware warning. It runs the installer and links the Mac to the Portal. After that, everything is managed from the Portal.

## What the person sees

1. **Welcome:** "Set up this Mac as the InFocus editor", with a **Set up** button and a note that an admin will need to approve one password dialog.
2. **Install:** a step list with progress, fed by the installer's progress lines (below):
   1. Admin approval
   2. Apple command-line tools
   3. Homebrew
   4. Video tools (ffmpeg, node, whisper)
   5. AI tools (Codex, Grok)
   6. Speech model
   7. Editor program
   8. Graphics package
   9. Background service
   10. Health check

   A **Show details** disclosure shows the raw log. On an error it shows the message and a **Try again** button; re-running is safe.
3. **Link:** a large code like `K7Q2-M9XD`, the line "Open the Portal and approve this Mac", and an **Open Portal** button that opens `verificationUrl`. It waits for approval and shows "Linked ✓".
4. **Done:**
   - "This Mac is now the InFocus editor. Everything else — AI account, settings, updates, health — is in the Portal under Automatic editing."
   - A reminder to keep it plugged in with the lid open.
   - An **Open Users & Groups** button for turning on automatic login (that needs the account password, so the person does it).

Opening the app again later shows the current state and offers **Repair**, which re-runs the installer, and **Link again**.

## How it runs the installer

The app bundles:
- the editor as a git bundle (`Resources/infocus-editor.bundle`)
- the installer scripts (`Resources/infocus-editor/` source tree)
- the trimmed graphics package (`Resources/package/`)

It runs:

```
/bin/bash Resources/infocus-editor/install/install.sh --unattended --progress \
  --repo Resources/infocus-editor.bundle --package-src Resources/package --portal <InFocusPortalURL>
```

- **Portal address:** `InFocusPortalURL` is set in Info.plist at build time (default `https://infocuspaly.com`).
- **No prompts:** `--unattended` never reads stdin. The admin step is the standard macOS dialog that `install.sh` already opens with `osascript … with administrator privileges`.
- **Package:** `--package-src` copies the package to `~/InFocusEditor/package` if it isn't there yet.
- **Linking:** after setup, the installer runs `infocus-editor link --progress`, which does the device-code flow in `portal-api.md`, before starting the launchd agent.

## Progress protocol (installer stdout)

Lines that start with `@@` are for the app; every other line goes to the details log.

```
@@step <id> <running|done|skipped|failed> <message…>
@@link <userCode> <verificationUrl>
@@linked
@@done
@@error <message…>
```

The step ids are `admin clt homebrew tools ai model app package service health`, with `link` used while linking. The final line is either `@@done` or `@@error`, and the exit code is 0 only after `@@done`.

## Building and signing

`setup-app/build.sh` builds the SwiftUI app with `swiftc`/SwiftPM (macOS 13+, arm64). It copies the resources in and signs with hardened runtime using the Developer ID from `~/Documents/GitHub/APPLE_SIGNING.md` (temp keychain plus the G2 intermediate, using the shared loader). Every nested executable and script is signed. Then it builds a DMG, notarizes it with `notarytool`, staples it and checks it with `spctl`.
- `--sign` signs without notarizing.
- `--release` signs, notarizes and staples.
- No flag gives an unsigned local build.

Secrets come only from 1Password at build time and are never written into the repo.

## Dedicated Mac or personal computer

The welcome screen asks **"What is this computer?"** with two answers:
- **The editing Mac (dedicated):** the defaults above.
- **My own computer, for now (personal):** the installer gets `--personal`.

**What `--personal` changes:**
- **Power:** no never-sleep settings. The background service holds a sleep assertion (`caffeinate -i`) only while a job runs.
- **Existing tools:** a Homebrew, Codex or Grok already installed for this user is reused, never replaced. If `/opt/homebrew` belongs to another account, setup stops with a plain message.
- **Labelling:** the machine reports `kind: "personal"` and shows as Personal in the Portal.
- **Safety note on the welcome screen:** "The AI editor runs with your account's access. For the best isolation, create a separate standard macOS account for the editor and run setup there."
