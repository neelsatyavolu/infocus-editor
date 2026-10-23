# Design

## What it does

A producer picks the filmed show in the Portal and clicks **Start automatic editing**. Then:
1. This program, running on the studio Mac, claims the job.
2. It downloads the switcher recording and the queued packages.
3. It runs an AI agent session (Claude, Codex or Grok) that follows the `infocus-show` skill (`.claude/skills/infocus-show/`). The skill's scripts do the editing work, and ffmpeg assembles and encodes the final show.
4. It checks the render with code.
5. It uploads the finished show to the NAS.

The Portal then turns the upload into a draft in its existing YouTube upload flow, where a producer reviews it and schedules it. A producer can also type a fix request ("the Math Club card should say …"), and the same agent session applies it and produces the next version.

```
Portal (Cloudflare Worker)           Studio Mac (this program, launchd)          Drive (NAS)
 ShowEditJob queue  ◄── heartbeat / claim / progress / complete ──┐
 lists Shows/ recordings ────────────────────────────────────────────────────► /api/service/list
 mints file tokens per job ─────────────► download recording + packages ◄──── /api/service/file
                                          agent + skill + ffmpeg         
                                          verify_render.py (hard checks)
                                          upload show + poster ───────────────► /api/service/upload/*
 draft ShowPublication → producer approves → existing Queue → YouTube
```

## Decisions

- **Mac, not Windows or the cloud.** The edit needs Apple Vision (the anchor matte) and VideoToolbox (fast H.264). Hosted agent sandboxes are Linux.
- **No DaVinci Resolve.** Resolve was only stacking layers and encoding, and scripting it needs the paid Studio edition plus a GUI app that can hang or open dialogs. `build_ffmpeg.py` does the same assembly headless. It also writes an FCPXML timeline, so a person can still open the edit in Resolve (even the free version) for a hand fix.
- **A polling worker.** The Portal is a Worker (responses end at 30 s) and the Mac is behind school NAT, so the Mac polls the Portal and needs no open ports.
- **Drive access through Portal-minted tokens, not the `infocus` CLI.** CLI sign-in needs a person's Google login, and its tokens expire after 90 days, which would stop an unattended editor. Instead:
  - The Portal already holds Drive's service token. It mints read tokens for each job's files and read/write tokens for its output paths.
  - The Mac holds one secret for the Portal and one for Anthropic.
  - Drive exposes the switcher's `Shows/` folder to the service API read-only.
- **The agent never asks.** `house-rules.md` holds every decision producers have made. The skill applies it and records each decision in `result.json`, flagging anything no rule covers. Flagged edits stop at review.
- **Code, not the agent, decides "done".** `verify_render.py` checks the render. The runner sends the agent back with the failures up to twice. After that the edit still lands for review, with the failures listed.
- **Open source, private data.** This repo holds no names, recordings, rosters, NAS paths or secrets. Show data comes from the Portal per job. The house rules, test fixtures, job folders and settings live in `~/InFocusEditor/` (the data dir). Secrets live in the macOS Keychain.
- **Names come from the Portal** (`brief.anchors`), so there is no roster file to keep current on the Mac.

## Data dir (`~/InFocusEditor/`, never in git)

```
config.toml        portal_url, package_dir, work_root, max_budget_usd (non-secret settings)
house-rules.md     the live rules (seeded from house-rules.example.md); a local git repo keeps its history
jobs/              one folder per job, kept 14 days
fixtures/          real shows used by the fixture and golden tests
```

Keychain (service `infocus-editor`):
- `editor_token`
- `pairing_key` (the X25519 private key)
- `claude_oauth_token`

Codex and Grok sign-ins live in `<data dir>/codex-home/auth.json` and `<data dir>/grok-home/auth.json`, mode 600.

## AI backends (subscription login, no API keys)

- **Backends:** the editor can drive Claude (Agent SDK with a `claude setup-token` OAuth token), Codex (`codex exec`) or Grok (`grok -p` headless).
- **Instructions:** every backend gets the same instruction: read `.claude/skills/infocus-show/SKILL.md` and follow it. Only shell and file tools are needed, with no MCP servers.
- **Order and fallback:** The Portal's agent order (or `agents` in config.toml, default `["codex", "claude", "grok"]`) sets the order. The first connected backend runs the job. If it hits a usage limit or its sign-in fails, the job restarts on the next connected one, and the switch is recorded as a decision.
- **Signing in:** see "Connecting an AI subscription" in `portal-api.md`. You sign in on your own computer, and the credential reaches the Mac sealed to the Mac's key.
- **Permissions:** the agents run with approvals off. The safety boundary is the editor's dedicated standard (non-admin) macOS account, and the runner also refuses commands that reach for root.

## Contracts

- `docs/portal-api.md`: Portal service routes and Drive file endpoints.
- `docs/job-contract.md`: `brief.json` / `status.json` / `result.json` and `verify_render.py`.
