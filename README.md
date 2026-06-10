# Domo

**Domo** (short for *majordomo*) is a personal, Discord-fronted assistant built on Claude Code. You talk to an **operator agent** in Discord — DMs and a dedicated channel — and it coordinates a small **council of specialist agents**, each with its own Discord bot identity, persona, and tool allowlist. The operator can spawn isolated per-thread sub-agents for long-running work and delegate to the council. A local **Mission Control dashboard** gives a web view of agents, the run ledger, scheduled jobs, and the vault. All sessions run with Claude Code's full tool access against a working directory you choose (typically a personal notes vault).

This is the public, standalone extraction of a personal bot. The generic council + infrastructure are here; the owner's personal integrations are intentionally left out (see [Not included](#not-included)).

> **Security writeup:** for the full agent-security threat model behind Domo — these controls mapped onto the [OWASP LLM Top 10 (2025)](https://owasp.org/www-project-top-10-for-large-language-model-applications/) — see [*A threat model for a personal multi-agent system*](https://dylan-palumbo.com/writing/discord-ops-security-model/).

## Features

- **Council of agents** — an operator (`main`) that can delegate, plus `research` (deep web research) and `comms` (correspondence) specialists. Each agent is a separate Discord bot account with its own channel, persona file, model, and tool allowlist (`agents.yaml`).
- **Per-thread sub-agents** — `spawn_thread_agent` (operator-only MCP tool) opens a Discord thread and runs a sub-agent there in the background, isolated from the main session.
- **Mission Control dashboard** — a localhost FastAPI/SPA app: agent tiles, the task ledger, usage/credit summaries, the cron scheduler, and a read-only vault browser. PIN-gated.
- **Cron scheduler** — schedule recurring or one-shot agent runs (`mcp__cron__*` tools + a dashboard panel); results post to a channel.
- **Calendar** — Apple iCloud CalDAV read for the whole council, write for `main`/`comms` (`mcp__calendar_read__*` / `mcp__calendar_write__*`). Skipped unless iCloud creds are set.
- **Video / watch** — `watch_video` for the research agent (wraps a `/watch` skill if installed).
- **Voice notes (TTS)** — `src/voice/`: ElevenLabs text-to-speech to a temp mp3, budget-gated by a per-day character ceiling, played live in a Discord voice channel with an mp3 attachment fallback. See [Voice notes](#voice-notes-elevenlabs-tts).
- **GPT-5 specialist** — an `mcp__specialist__query_gpt5` tool that shells out to a local Codex CLI, so the council can consult a non-Claude model. Skipped unless the Codex CLI is found.
- **Admin elevation** — an operator-only `request_admin_elevation` tool with a Discord Approve/Deny flow, executed by a **separate elevated broker service**. See [Security](#security) before enabling.
- **Credit governor** — tracks Anthropic Agent SDK spend against a monthly ceiling and can refuse new runs when exhausted (observe-only by default).
- **Vault write-guard** — a `PreToolUse` hook that blocks agent writes to protected paths and reads of secret files, even under `bypassPermissions`.

## Not included

This extraction deliberately omits the original owner's personal subsystems. The wiring for them has been removed:

- **Email** (Gmail read/draft, inbox categorization).
- **Email cull** (bulk promotional-mail review/trash).
- **Reverse recruiter** (job-hunt scanning + pipeline tracking).
- **Nightly mission** (the long-running autonomous nightly research runner).

The dashboard, council, cron, calendar, video, specialist, and elevation features above are the supported surface.

## Architecture

- **Python 3.12**, `discord.py`, `claude-agent-sdk`, FastAPI.
- `src/bot.py` defines `BotApp`, which owns a shared `RunnerRegistry` and one `discord.Client` per agent. Each feature wires in via a `_wire_<name>_tools()` method that builds a context and registers an in-process MCP server scoped to a set of agents.
- One `ClaudeSDKClient` per Discord key (an agent, or `thread:<id>`), held warm across messages, torn down after idle, resumed from session ID on restart.
- **Authentication** runs through Claude Code itself — either a logged-in Claude subscription (`claude login` → `~/.claude/.credentials.json`) or an `ANTHROPIC_API_KEY`. Domo requires neither specifically and sets up neither for you; whichever Claude Code is configured with is used (when both are present, the SDK uses `ANTHROPIC_API_KEY`).

## Companion vault

Domo runs **on top of a [second-brain vault](https://github.com/Aquinas-Protocol/second-brain)** — a Claude-Code-powered, Obsidian-style knowledge base it uses as shared memory: it reads the vault's `CLAUDE.md` conventions, loads agent personas from `<vault>/agents/`, and reads & writes `wiki/` pages. The vault is a separate, MIT-licensed starter project; Domo is the multi-agent council layer its setup guide refers to as *"going further."*

**Set up a vault first** (~30–45 min) by following that repo's [`GUIDE.md`](https://github.com/Aquinas-Protocol/second-brain/blob/main/GUIDE.md). Then point Domo at it — the wizard asks for the path (`VAULT_ROOT`, default `~/Documents/second-brain`) and installs the council personas into `<vault>/agents/`.

## Setup

```
py -3.12 -m venv .venv
.\.venv\Scripts\activate
pip install -e .
py wizard.py
```

The wizard checks your Claude auth (subscription login or API key), points Domo at your [companion vault](#companion-vault) (and installs the council personas into it), validates each agent's Discord bot token + channel, optionally wires Groq voice transcription / Apple Calendar / the GPT-5 specialist / a dashboard PIN, writes `.env` (ACL'd to your user), creates the bot's working-directory junction, and points you at the service-install scripts.

Copy `.env.example` to `.env` to see every variable the included features read. The wizard fills in the core ones; the rest are optional per-feature.

## Run

```
py -m src.bot
```

Or install as a Windows service via NSSM for always-on operation (see `install_service.ps1` and the wizard output).

## Security

> Beyond the elevation-broker specifics below, the complete agent-security threat model — every control here mapped onto the OWASP LLM Top 10 — is written up in [*A threat model for a personal multi-agent system*](https://dylan-palumbo.com/writing/discord-ops-security-model/).

**Read this before enabling the elevation broker.**

Domo ships an *optional* second service — the **elevation broker** (`broker/elevation_broker.py`, installed by `broker/install_broker.ps1` as the Windows service `domo-elevation-broker`). It runs as **`LocalSystem`**, i.e. with **full Administrator privileges and no UAC prompt**. Its job is to execute commands that the operator agent requested and that you explicitly approved with a Discord button click.

What this means concretely:

- When the broker is installed, an approved `request_admin_elevation` call runs a shell command **as SYSTEM**. A mistaken or manipulated approval is a full-privilege command on your machine.
- The bot process itself stays at your normal user trust level. Only the broker is elevated, and it only ever runs the canonical command stored for an *approved* request — the bot passes the broker only a request UUID over a named pipe, never the command text, so the bot can't bypass the approval gate.
- The broker has a small, non-overridable deny list (e.g. `format X:`, `Remove-Item C:\Windows`, `reg delete HKLM\SYSTEM`), but that is a backstop, **not** a sandbox. **You are the security boundary** — every Approve click is you authorizing admin code.

Only install the broker if you understand and accept this. The main bot runs fine without it: `request_admin_elevation` simply returns "broker not reachable" until the broker is installed and started.

Other security notes:

- The `PreToolUse` hook (`hooks/pretooluse_guard.py`) blocks agent writes to protected directories and blocks reads of secret files (`.env`, OAuth tokens, the credential dir under `~/.config/domo`). It is a mechanical floor, not a complete sandbox.
- `.env` holds Discord bot tokens and other secrets. The wizard ACLs it to your user; never commit it. `.env.example` is the committed template and contains only placeholders.
- Agents run with `permission_mode="bypassPermissions"` — they do not prompt before tool use. Scope each agent's `enabled_tools` in `agents.yaml` accordingly.

## Two-service layout (admin elevation)

```
domo main service (your user)              domo-elevation-broker (LocalSystem)
  ─── operator agent                         ─── named-pipe server
       ├── request_admin_elevation                ├── reads elevation_requests row
       ├── posts Discord Approve/Deny embed        ├── re-validates: status, sha256, deny list
       ├── awaits the Approve button               ├── spawns elevated subprocess
       └── pipe.write({"request_id": uuid}) ───>   └── pipe.write({exit_code, stdout, stderr})
```

The two services share `data/sessions.sqlite`. On bot startup, every `pending` elevation row is mass-flipped to `expired` and persistent views are re-bound, so stale embeds resolve to "request expired" rather than Discord's generic "interaction failed".

Install both from an elevated PowerShell:

```
.\install_service.ps1            # main bot, runs as your user
.\broker\install_broker.ps1      # broker, runs as LocalSystem (see Security)
```

## Voice notes (ElevenLabs TTS)

The voice subsystem (`src/voice/`) turns an agent's end-of-run summary into a spoken brief: synthesize ~60 seconds of audio via ElevenLabs, play it in a Discord voice channel, and attach the mp3 to a text channel so the brief survives if nobody is listening live. In the owner's deployment it runs as the final step of the (omitted) reverse-recruiter morning cron; the mirrored package is the complete mechanics, callable from any agent-scoped MCP tool.

- `src/voice/elevenlabs_client.py` — thin httpx client against the streaming TTS endpoint (`/v1/text-to-speech/{voice_id}/stream`), no SDK. Model pinned to `eleven_turbo_v2_5` at `mp3_22050_32`: a once-a-day cron summary is latency-tolerant, so the cheap/fast tier beats higher-fidelity formats on every axis that matters here.
- `src/voice/voice_channel_player.py` — discord.py playback: join → play → await the `after` callback → always disconnect (`try/finally`), so no orphan voice client outlives a failed playback. `VoiceClient.play()` is non-blocking; completion is bridged from the player thread with `call_soon_threadsafe`.
- `src/voice/budget.py` — per-day character ceiling (default 2,000) enforced as a single conditional `UPDATE` against `voice_tts_usage` in `sessions.sqlite`. It consumes *before* the API call: over-counting is the safe direction for a spend ceiling. Defense in depth: restrict the API key provider-side too (TTS-only scope, per-cycle credit cap).
- The calling tool treats budget exhaustion as a data answer (`ok=false reason=budget`), not a tool error — so the agent reports it and moves on instead of retrying. Playback failure degrades to attachment-only; only a double failure errors.

The summary text is composed by the agent per its cron directive (~150 words), not in code — the tool owns mechanics, the persona owns words.

**Multi-tenant translation** — what this control looks like deployed for customers rather than one user: the budget gate becomes a `(tenant_id, date)`-keyed allowance with per-tenant voice IDs and output formats; the usage row gains tenant scope for audit; and the provider-side key restriction becomes per-tenant scoped keys, so one tenant's runaway loop can't drain a shared credit pool. The single-tenant version here is deliberately the smallest correct shape of that design.

**Failure modes hit while shipping:** (1) the voice step was first wired into the cron *prompt* — dead code on the live path, because the owner's scan pipeline runs server-side and never dispatches an agent with that prompt; the fix is a scheduler-side dispatch after a successful scan, pinned by tests. The lesson: once parts of a cron move server-side, "the cron prompt" is no longer "the cron behavior". (2) `discord.opus.is_loaded()` is `False` at import on Windows — the bundled DLL lazy-loads on first voice connect; call `discord.opus._load_default()` if you need to verify it earlier. (3) ElevenLabs streaming mp3s carry no Xing header, so ffmpeg logs a benign "estimating duration from bitrate" warning on every playback. (4) Schema lineage: this table ships as migration v19 here but v22 in the owner's private lineage — migration numbers are deployment-local, which is exactly why the migration runner keys on `PRAGMA user_version` per database rather than a global registry.

## Layout

- `src/config.py` — constants, env loading, the agent registry loader.
- `src/agent_runner.py` — `AgentRunner` + `RunnerRegistry` (lifecycle, lock, resume, backpressure, credit governor).
- `src/bot.py` — Discord clients, routing, streaming, chunking, feature wiring, OAuth sanity loop.
- `src/spawner.py` — `spawn_thread_agent` + delegation MCP tools + multimodal attachment intake.
- `src/cron_scheduler.py` / `src/cron_store.py` / `src/cron_tools.py` — scheduler loop, store, MCP tools.
- `src/calendar_*.py` — CalDAV client + read/write MCP tools.
- `src/specialist_*.py` — GPT-5-via-Codex specialist tool + store.
- `src/elevation_*.py` / `broker/` — Discord-approved admin elevation (operator tool + elevated broker service).
- `src/voice/` — ElevenLabs TTS client, daily character budget, voice-channel player.
- `src/web/` — FastAPI dashboard (routes, auth, vault browser, static SPA).
- `hooks/pretooluse_guard.py` — write/read guard hook.
- `agents.yaml` — agent roster. `agents/*.md` — persona files.
- `wizard.py` — interactive first-run setup.

## License

MIT — see [LICENSE](LICENSE). Builds on the [second-brain](https://github.com/Aquinas-Protocol/second-brain) vault (also MIT).
