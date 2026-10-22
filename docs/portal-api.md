# Portal and Drive API used by the editor

The editor talks to two services:
- **The Portal** queues jobs and holds show data. The editor only ever holds the Portal's editor token.
- **Drive**, the NAS file server. The editor reaches it only through short-lived, path-scoped file tokens that the Portal mints for each job.

## Portal service routes

- **Base:** `<portal>/api/service/show-editor/`.
- **Method:** every route is `POST` with a JSON body.
- **Auth:** `Authorization: Bearer <SHOW_EDITOR_TOKEN>`. A wrong or missing token returns 401.
- **Responses:** success is `{ "data": <payload> }`. An error is `{ "error": { "message": string } }` with a 4xx/5xx status.

| Route | Body | `data` |
|---|---|---|
| `heartbeat` | `{ version: string≤40, busy: boolean, health: { ok: boolean, problems: string[] (≤20, each ≤300) }, publicKey: string (base64 X25519), backends: [{ name: "claude"\|"codex"\|"grok", connected: boolean, detail: string≤200 }], credentialAcks: [{ id, ok: boolean, error?: string≤300 }] (≤10) }` | `{ cancelJobIds: string[], targetVersion: string \| null, credentials: [{ id, backend, ciphertext }] }` |
| `claim` | `{}` | `{ job: null }`, or a claim (below) |
| `jobs/<id>/progress` | `{ stage: Stage, message: string≤300 }` | `{ cancel: boolean }` |
| `jobs/<id>/upload-targets` | `{}` | `{ driveBaseUrl, video: { path, token }, poster: { path, token }, log: { path, token } }` |
| `jobs/<id>/complete` | `{ outputNasPath, decisions, checks, needsReview, costUsd, agentSessionId, reply?, debugLogPath? }` | `{ ok: true }` |
| `jobs/<id>/fail` | `{ error: string≤1000, cancelled: boolean, debugLogPath? }` | `{ ok: true }` |

`Stage` is one of `download | analyze | graphics | audio | build | render | check | upload | fix`.

### Claim

```jsonc
{
  "kind": "edit",                 // or "fix"
  "job": { "id": "…", "showDate": "2026-10-21", "version": 1, "attempt": 0, "status": "RUNNING" },
  "brief": {
    "jobId": "…",
    "showDate": "2026-10-21",     // airing date
    "filmedOn": "2026-10-20",     // or null
    "version": 1,                 // timeline version this run must produce
    "sources": [{ "path": "Shows/<file>.mov", "role": "auto", "order": 1, "modifiedAt": "2026-10-21T22:29:46Z" }],
                                                 // 1–12; role auto|program|M|L|R|ignore (see multicam.md);
                                                 // modifiedAt is the export time, used to order takes
    "sourcePaths": ["Shows/<file>.mov"],         // legacy: the non-ignored paths, in order
    "anchors": ["First Last", "First Last"],     // display names from the Portal
    "announcements": ["<announcement text as written in the script>"],
    "pastCards": [{ "showDate": "2026-10-21", "title": "…", "points": ["…"], "script": "…" }],
                                                 // cards from recent other shows' edits (their checks.cards),
                                                 // newest first, up to 16; the style for written cards
    "roster": ["First Last"],                    // the class's full names (no staff), for name bars and for
                                                 // working out who's on air when it isn't the scheduled anchors
    "packages": [{ "title": "…", "nasPath": "Package Storage/…/x.mp4", "toss": "…" }],  // queue order; toss may be null
    "note": null,                 // the producer's Redo note, or null
    "outputNasPath": "Package Storage/Shows/2026-10-21/InFocus Show 2026-10-21-<stamp>.mp4",
    "posterNasPath": "Package Storage/Shows/2026-10-21/InFocus Show 2026-10-21-<stamp>.poster.jpg"
  },
  // kind "edit" only: one entry per non-ignored source, then one per package, in brief order
  "downloads": [{ "path": "Shows/<file>.mov", "url": "https://<drive>/api/service/file?path=…&token=…&inline=false", "size": 345000000 }],
  // kind "fix" only:
  "fix": { "text": "the producer's request", "remember": false, "agentSessionId": "…", "version": 2 }
}
```

- **Fixes:** a fix gets a fresh `outputNasPath` and `posterNasPath` (new stamp) and `version + 1`. A fix never re-downloads anything.
- **Download URLs:** read-only tokens that last 6 hours.
- **`upload-targets`:** mints read/write tokens (6 h) for the job's *current* output and poster paths. Call it right before uploading.

### Debug logs

- **Every run uploads a debug bundle,** whether it completes, fails or is cancelled. Failures upload too, by calling `upload-targets` before `fail`.
- **Where it goes:** `log.path` is `Package Storage/Shows/<date>/editor-logs/<jobId>-v<version>-<stamp>.tar.gz`, minted fresh by `upload-targets`.
- **What's in it (no media):**
  - the job log and the agent transcripts (secrets scrubbed)
  - `brief.json`, `result.json` and `status.json`
  - `show/edl.json`, `show/recordings.json`, `show/layout.json` and `show/check/verify.json`
  - the analysis reports
- **Recording it:** `debugLogPath` on `complete`/`fail` must equal that path. The Portal stores it on the job (as `checks.debugLog`).
- **In the panel:** each version gets a **Download debug log** link (a 6-hour read token).
- **The `logs` remote command** also returns the tail of the latest job's log.

### Complete

- `outputNasPath` must equal the claim's `brief.outputNasPath`, or the Portal returns 409.
- `decisions`: `[{ kind: string≤40, detail: string≤300, flagged: boolean }]`, at most 50.
- `checks`: the `checks` object from `result.json` (see `job-contract.md`). The worker adds `cards`: `[{ title, points, script }]` from `edl.json`, where `script` is the announcement text the card was made from, or null.
- `costUsd ≥ 0` is the cost of this run only. The Portal keeps the running total.
- `reply` (≤1000) is required for `kind: "fix"`.

### Portal behaviour the editor relies on

- **Stale jobs:** a job whose last progress or heartbeat is more than 15 minutes old goes back to QUEUED (`attempt + 1`). It becomes FAILED once `attempt ≥ 2`. While a job runs, the editor must heartbeat every 60 s and report progress when the stage changes.
- **Cancel:** shows up in `progress → { cancel: true }` and in `heartbeat → cancelJobIds`.
- **Remote update:** `targetVersion` is the Portal setting `SHOW_EDITOR_TARGET_VERSION`, a release tag such as `v0.3.1`, or `null` when it isn't set.
  - When it's set, the editor moves to exactly that tag (pin or roll back) the next time it's idle.
  - When it's `null`, the editor follows the newest GitHub release.
- **Online:** the editor counts as online when its last heartbeat is under 3 minutes old. The panel shows `health.problems`. Send `health.ok: false` whenever the editor can't take jobs, and don't call `claim` then.

## Connecting an AI subscription (no API keys)

The editor runs on a Claude, ChatGPT/Codex or SuperGrok subscription. Someone signs in on their own computer, and the credential reaches the Mac end-to-end encrypted. The Portal only ever stores ciphertext.

1. **Key:** the Mac keeps an X25519 key pair (private key in Keychain) and sends `publicKey` with every heartbeat.
2. **Pairing code:** a producer clicks **Connect AI account → Claude / Codex / Grok** in the panel. The producer route `POST /api/show-roles/edits { action: "pair", backend }` returns `{ code: "XXXX-XXXX", expiresAt, command }`.
   - The code lasts 10 minutes and can be used once. It's stored hashed and bound to the current `publicKey`.
   - `command` is the line to run on the producer's computer: `uvx --from git+<SHOW_EDITOR_REPO_URL> infocus-editor connect <backend> --code <code> --portal <APP_BASE_URL>`.
3. **Sign-in and send:** `infocus-editor connect` signs in locally:
   - **Claude:** `claude setup-token`.
   - **Codex:** `codex login`, in a throwaway `CODEX_HOME`.
   - **Grok:** `grok login`, in a throwaway `GROK_HOME`.

   It then seals the credential to the Mac's public key (libsodium sealed box) and sends it:
   - `GET  <portal>/api/service/show-editor/pair/<code>` returns `{ backend, publicKey, expiresAt }`. An unknown, expired or used code gets 404.
   - `POST <portal>/api/service/show-editor/pair/<code>` with `{ ciphertext: base64 ≤ 65536 chars }` returns `{ ok: true }`.

   These two routes are authorised by the code, not the editor token, and are rate-limited.
4. **Install:** the next heartbeat response carries `credentials` (ciphertext for the current key). The Mac decrypts and installs it, then checks the sign-in works and sends `credentialAcks`. The Portal then deletes the ciphertext; any not acked within an hour is deleted anyway.
5. **Status:** the panel shows each backend's `connected` and `detail` from the heartbeat.

`agentSessionId` is stored as `<backend>:<session id>`, so a fix resumes on the same backend. A fix whose `agentSessionId` is `null` starts a fresh session.

## Drive file endpoints (with Portal-minted tokens)

- **Download:** `GET <downloads[].url>`. It honours `Range: bytes=<n>-` for resuming.
- **Upload:** Drive's chunked service upload, authorised by the `token` from `upload-targets`:
  1. `POST <driveBaseUrl>/api/service/upload/init` (form: `path`, `token`, `size`, `chunk_size`, plus whatever else Drive's `service_upload_init` reads) returns an upload id.
  2. `PUT <driveBaseUrl>/api/service/upload/chunk?upload_id=…&index=<n>&token=…` with the raw chunk bytes as the body.
  3. `GET <driveBaseUrl>/api/service/upload/status?upload_id=…&token=…` lists the chunks already received, for resuming.
  4. `POST <driveBaseUrl>/api/service/upload/complete` (form: `upload_id`, `token`).

  The exact form fields and response shapes are in infocus-drive `app/main.py` (`service_upload_init`, `service_upload_chunk`, `service_upload_status`, `service_upload_complete`). The Portal's browser client follows the same protocol.
- **Read access:** the `Shows/` folder (where the switcher's recordings land) is readable through these endpoints but never writable. Writes go only to `Package Storage/` and `Package Cycles/`.

## Linking the Mac (no tokens to copy)

The setup app links the Mac to the Portal with a device-code flow, like signing a TV in to a streaming service. Nobody copies a token. `SHOW_EDITOR_TOKEN` (a Worker secret) still works as a fallback, but isn't needed.

1. **Start:** `POST <portal>/api/service/show-editor/link` with `{ deviceName: string≤80 }`.
   - No auth; rate-limited.
   - Returns `{ deviceCode, userCode: "XXXX-XXXX", verificationUrl: "<portal>/show-roles?link=XXXX-XXXX", expiresAt, interval: 5 }`.
   - `deviceCode` is a 32-byte secret only the Mac holds; the Portal stores its sha256. The link expires after 15 minutes.
2. **Approve:** the setup app shows `userCode` and an **Open Portal** button for `verificationUrl`. A producer opens it and sees "Link the editor Mac '<deviceName>'?".
   - Approving calls `POST /api/show-roles/edits { action: "link", userCode }`.
   - It creates a `ShowEditorDevice` with a fresh random token (stored as sha256) and revokes any previously linked device.
3. **Collect the token:** the Mac polls `POST <portal>/api/service/show-editor/link/poll` with `{ deviceCode }` every `interval` seconds. Replies:
   - `{ status: "pending" | "expired" | "denied" }`
   - `{ status: "approved", token }`: returned exactly once. The link row is then deleted and the Mac saves the token in Keychain (`editor_token`).
4. **Auth:** every editor route accepts `Bearer <token>` when its sha256 matches the active device, or the legacy `SHOW_EDITOR_TOKEN`.
5. **Unlink:** `POST /api/show-roles/edits { action: "unlink" }` revokes the device. Its next heartbeat gets 401, and the Mac shows "Not linked" and stops.

## Settings and remote control from the Portal

Everything after setup is managed from the Portal panel by producers. Nothing is changed on the Mac by hand.

- **Settings** live in one `ShowEditorSettings` row: `autoStart`, `autoPublish`, `targetVersion`, `agentOrder` (a subset and order of `["claude","codex","grok"]`), `houseRules` (text ≤ 20000), `updatedById` and `updatedAt`.
  - Change them with `POST /api/show-roles/edits { action: "settings", …fields }`.
  - The env vars `SHOW_EDITOR_AUTO_START`, `SHOW_EDITOR_AUTO_PUBLISH` and `SHOW_EDITOR_TARGET_VERSION` are used only when the matching setting is null.
- **The heartbeat response adds:**
  - `settings: { agentOrder: string[], targetVersion: string | null }`. `targetVersion` also stays top-level.
  - `commands: [{ id, kind: "doctor" | "update" | "restart" | "repair" | "logs" }]`, which producers queue with `POST /api/show-roles/edits { action: "command", kind }`.
- **The heartbeat body adds** `commandAcks: [{ id, ok: boolean, output?: string≤4000 }]` (≤10).
  - For `logs`, `output` is the last 100 log lines, with secrets never included. For `doctor`, it's the health report.
  - The panel shows each command's result.
- **What each command does:**

  | Command | Effect |
  |---|---|
  | `doctor` | Re-run the health check now |
  | `update` | Check for an update now |
  | `restart` | Exit and let launchd restart the editor |
  | `repair` | Re-run `user-setup.sh --update`: reinstall missing tools, reload the agent |
  | `logs` | Send the recent log |

- **House rules live in the Portal:**
  - **In the brief:** the claim's brief gains `houseRules: string | null`. When it's null, the editor uses its built-in `house-rules.example.md`.
  - **Remembered fixes:** after a fix with `remember`, complete sends `houseRules` (the full updated text), and the Portal saves it.
  - **Editing:** the panel shows the rules and lets producers edit them, with the editor's default text as the starting point.

## Several editing machines, one active

More than one Mac can be linked: for example, a producer's own laptop for now and the team MacBook later. Exactly one is **active**. Producers switch which one in the panel.

- **Linking keeps other machines.** The first linked machine becomes active by itself. The approval screen has a "Make it the active editor now" checkbox, on by default.
- **Heartbeat state is per machine** (version, busy, health, backends, publicKey, lastSeenAt, commands). Legacy `SHOW_EDITOR_TOKEN` clients count as a machine named "legacy".
- **The heartbeat body adds** `machine: { name: string≤80, kind: "dedicated" | "personal" }`. **The response adds** `active: boolean`.
- **What a claim returns:**
  - The active machine gets new edits and fixes for its own jobs.
  - An inactive machine gets only fixes for jobs it edited (its job folder is there) and `{ job: null }` otherwise.
  - A fix whose machine is unlinked fails with "The machine that made this edit is no longer linked; use Redo". If that machine is linked but offline, the fix waits in QUEUED and the panel says so.
- **Jobs record their machine** (`deviceId`, set at claim). A job already running when the active machine changes finishes where it is.
- **Producer actions:**
  - `{ action: "activate", deviceId }`
  - `{ action: "unlink", deviceId }`
  - `{ action: "command", deviceId, kind }`
  - `{ action: "pair", backend, deviceId? }`, which defaults to the active machine
- **The panel's "Editing machines" list** shows, for each machine: name, Dedicated or Personal, online and last seen, version, health, AI accounts, an Active badge, and the buttons **Make active**, **Unlink**, and health check, update, restart, repair and log.
