# yt-transcribe

Speaker-labelled transcripts (and, through a Hermes Agent skill, summaries) of
videos and local audio/video files. Three parts:

- `api/` — FastAPI service (port 8765), runs in an always-on Proxmox LXC without a GPU.
  Downloads (yt-dlp), converts to 16 kHz mono FLAC (ffmpeg), job queue, uploads, result
  cache, output files. Transcribes in one of two modes:
  - **local**: sends the FLAC to the GPU worker through llama-swap (`local.py`).
  - **cloud**: AssemblyAI pre-recorded API (`universal-2`, `universal-3-5-pro`); the GPU server
    may be off.
- `gpu-worker/` — FastAPI container on the Unraid GPU server (port 8766): WhisperX
  (faster-whisper `large-v3` / `large-v3-turbo`) → wav2vec2 alignment → pyannote
  `speaker-diarization-community-1`. Returns raw segments with diarizer speaker ids.
- `hermes-skill/yt-transcribe/` — Hermes Agent skill (`SKILL.md`) plus a stdlib-only
  client (`scripts/yt_transcribe.py`) that runs on the user's Windows PC.

The repo also documents and ships the whole home AI setup the service lives in:

- `docs/` (Hungarian, user-facing): `architektura.md` (+ `architektura.svg`/`.png`, the diagram the
  owner likes — keep its layout; a Mermaid version was tried and lays out differently),
  `telepites.md` (step-by-step install with checks), `frissites.md` (per-component updates and rollback).
- `deploy/llama-swap/`: `config.yaml`, `unraid-container.sh` (starts/stops Unraid containers via the
  Unraid GraphQL API; `cmd` must stay alive while the container runs), `unraid.env.example`, systemd unit.
- `deploy/litellm/`: `config.yaml` (local preset names, `cloud/...` OpenRouter names, `auto` with
  fallback), env example, systemd unit (LiteLLM pinned to 1.104.2).
- `deploy/yt-transcribe/`: the API's systemd unit and env example. `deploy/clients/opencode.json`.

When a change affects installation or operation, update `docs/telepites.md`, `docs/frissites.md`
and the matching `deploy/` file in the same change. Keep the diagram in sync (edit the SVG, re-render the PNG).

## How the GPU is shared

llama-swap (on the same Proxmox LXC) owns the GPU queue. It starts/stops Unraid containers
through the Unraid GraphQL API (`deploy/llama-swap/unraid-container.sh`), one at a time:
the llama.cpp router container and the GPU worker. A local job is ONE streamed
HTTP request `POST WORKER_URL/transcribe` (WORKER_URL = `http://127.0.0.1:8080/upstream/yt-transcribe-gpu`).
While it is open, llama-swap keeps the GPU for the worker and queues LLM requests; the next
LLM request stops the worker and restarts llama-server. Verified with the real llama-swap
v262 binary and a simulated Unraid API (not on the real servers yet).

Consequences — keep them:
- The API never unloads/reloads LLMs itself and never calls the worker outside a job
  (`/health` does not contact it: through llama-swap that would take the GPU).
- The worker streams NDJSON lines (`{"stage"...}` + 10 s heartbeats, then `{"result"...}` or
  `{"error"...}`); polling-style job APIs would let llama-swap swap the worker out mid-job.

## Code map

| File | Role |
| --- | --- |
| `api/app/server.py` | HTTP API, job queue (one job at a time), request validation, result cache, uploads |
| `api/app/runner.py` | Child process per job (`python -m app.runner <job_dir>`); isolates crashes |
| `api/app/pipeline.py` | Settings (env vars), `Options`, audio sources (URL / upload), `to_flac` |
| `api/app/local.py` | Local mode: stream the FLAC to the worker, follow progress, name speakers |
| `api/app/cloud.py` | AssemblyAI client, model resolution (`auto`), cost estimate, delete-after-use |
| `api/app/usage.py` | Monthly cloud-hours budget (`DATA_DIR/cloud_usage.json`) |
| `api/app/formatting.py` | Pure functions: speaker relabelling, paragraphs, TXT/SRT, word → segment grouping |
| `gpu-worker/gpuworker/server.py` | Worker HTTP API: receives audio, runs one job at a time, streams progress |
| `gpu-worker/gpuworker/job.py` | Child process per job; CUDA failure → retry on CPU with a warning |
| `gpu-worker/gpuworker/asr.py` | WhisperX pipeline; ASR checkpoint keyed by audio SHA-256 + model + language |

## Decisions the owner made (keep them unless asked)

- Default mode is **local**; cloud only when the request asks for it (`mode=cloud`).
- **No silent fallback** between modes: a cloud failure is an error; local never goes to cloud,
  not even when the GPU server is off. (Inside local mode a failed GPU run may finish on CPU —
  that is not a mode switch.)
- Cloud model `auto`: Universal-3.5 Pro when the *stated* language is in `cloud.PRO_LANGUAGES`,
  otherwise Universal-2; auto language detection uses Universal-2. Explicit 3.5 Pro + Hungarian is refused.
- Monthly cloud budget (`CLOUD_MONTHLY_HOURS`, default 20); over budget = refuse before upload.
- Sources: URL (anything yt-dlp reads) **and** local files uploaded through `PUT /uploads/{sha256}`.
- Speaker labels are anonymous: `Szereplő N` (Hungarian) / `Speaker N`, numbered by first appearance.
- Architecture: control plane on Proxmox (LiteLLM, llama-swap, this API), GPU containers on
  Unraid, started on demand by llama-swap via the Unraid API (no Docker socket).
- llama.cpp runs as ONE router-mode container with the owner's `models.ini` (6 presets); llama-swap
  has one entry for it with every preset name as an alias (the request's model name passes through).
- Strata (Qwen3.8-Flash-Next) was tried and dropped: ~4 min load per start (every swap restarts it)
  and it scored no better than Qwen3.6-35B-A3B in the owner's benchmark. Not in the diagram or configs.
- LiteLLM stays because of error-based fallback (`auto` → cloud when Unraid is off); llama-swap's
  selectors choose before sending and never retry (checked in v262 source). Without `auto` it could go.

## Deployment context

- Proxmox LXC `ai-router`: llama-swap (:8080), LiteLLM (:4000), this API (:8765, systemd + venv,
  `deploy/yt-transcribe/yt-transcribe-api.service`). 2.5 Gbit LAN to Unraid.
- Unraid (192.168.1.20; Ryzen 9 5950X, 64 GB RAM, one RTX 3090 24 GB): llama.cpp router container
  (:8001, `models.ini` presets), GPU worker container `yt-transcribe-gpu` (:8766), autostart off.
- The Hermes client blocks in one call (≤ 570 s); Hermes caps a foreground terminal call at 600 s.
- Hermes Agent runs on Windows; the skill must stay Windows-safe (`python`, quoted paths, UTF-8 stdout).
- `whisperx==3.8.6` is pinned (signatures verified against it). yt-dlp is unpinned and upgraded
  when the API starts; YouTube needs deno on the LXC.

## Verified vs. not verified

- Verified on the real server (before the split): the WhisperX GPU path (one 14-min video ≈ 23 s).
- Verified on the real servers (2026-10-09, owner): llama-swap + Unraid API script, LiteLLM incl.
  cloud fallback, Hermes through LiteLLM, the split API/worker in local mode.
- Not yet verified for real: the whole cloud transcription mode (real AssemblyAI not yet called),
  uploads, budget; OpenCode/VS Code through LiteLLM. Treat behaviour of the real
  AssemblyAI API (Hungarian accuracy, diarization for Hungarian) as unknown until tested.

## Tests

```bash
pip install -r tests/requirements.txt     # plus ffmpeg/ffprobe on PATH
python -m pytest -q tests
```

The tests run the real API and the real worker as subprocesses (the API talks to the worker
directly; llama-swap only forwards in production). `tests/fakes` stands in for torch, whisperx,
yt_dlp and AssemblyAI; see `tests/conftest.py` for the flag files that switch fake behaviour
(crash, GPU failure, provider error…). Add a test there for every behaviour change.

## Conventions

- The owner writes in Hungarian. User-facing docs (`README.md`) are Hungarian; code, comments,
  `SKILL.md` and this file are English.
- Keep the client (`yt_transcribe.py`) standard-library only.
- Never commit tokens or keys (HF token, AssemblyAI key are env vars on the hosts).
