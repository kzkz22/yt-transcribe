# yt-transcribe

Speaker-labelled transcripts (and, through a Hermes Agent skill, summaries) of
videos and local audio/video files. Two parts:

- `service/` — FastAPI service in Docker (port 8765). Transcribes in one of two modes:
  - **local**: yt-dlp → WhisperX (faster-whisper `large-v3` / `large-v3-turbo`) →
    wav2vec2 alignment → pyannote `speaker-diarization-community-1`.
  - **cloud**: AssemblyAI pre-recorded API (`universal-2`, `universal-3-5-pro`), audio
    converted to 16 kHz mono FLAC and uploaded; never touches the GPU.
- `hermes-skill/yt-transcribe/` — Hermes Agent skill (`SKILL.md`) plus a stdlib-only
  client (`scripts/yt_transcribe.py`) that runs on the user's Windows PC.

## Code map (service/app)

| File | Role |
| --- | --- |
| `server.py` | HTTP API, job queue (one job at a time), request validation, result cache, uploads, LLM reload after a job |
| `runner.py` | Child process per job (`python -m app.runner <job_dir>`); frees all RAM/VRAM on exit, isolates crashes |
| `pipeline.py` | Settings (env vars), `Options`, audio sources (URL / upload), local WhisperX pipeline with ASR checkpoint |
| `gpu.py` | Borrows the single GPU from llama-server (router mode): unload via `/models/unload`, reload via `/models/load` |
| `cloud.py` | AssemblyAI client, model resolution (`auto`), cost estimate, delete-after-use |
| `usage.py` | Monthly cloud-hours budget (`DATA_DIR/cloud_usage.json`) |
| `formatting.py` | Pure functions: speaker relabelling, paragraphs, TXT/SRT, word → segment grouping |

## Decisions the owner made (keep them unless asked)

- Default mode is **local**; cloud only when the request asks for it (`mode=cloud`).
- **No silent fallback** between modes: a cloud failure is an error; local never goes to cloud.
  (Inside local mode a failed GPU run may finish on CPU — that is not a mode switch.)
- Cloud model `auto`: Universal-3.5 Pro when the *stated* language is in `cloud.PRO_LANGUAGES`,
  otherwise Universal-2; auto language detection uses Universal-2. Explicit 3.5 Pro + Hungarian is refused.
- Monthly cloud budget (`CLOUD_MONTHLY_HOURS`, default 20); over budget = refuse before upload.
- Sources: URL (anything yt-dlp reads) **and** local files uploaded through `PUT /uploads/{sha256}`.
- Speaker labels are anonymous: `Szereplő N` (Hungarian) / `Speaker N`, numbered by first appearance.

## Deployment context

- Service runs on an Unraid server (Ryzen 9 5950X, 64 GB RAM, one RTX 3090 24 GB) next to
  llama-server in router mode, which normally fills the GPU with a Qwen model. Local GPU jobs
  therefore unload the LLM and reload it afterwards; the client blocks in one call (≤ 570 s)
  so the agent does not wake the LLM mid-job (Hermes caps a foreground terminal call at 600 s).
- Hermes Agent runs on Windows; the skill must stay Windows-safe (`python`, quoted paths, UTF-8 stdout).
- `whisperx==3.8.6` is pinned (signatures verified against it). yt-dlp is unpinned and
  auto-updated at container start; YouTube needs deno (copied into the image).

## Verified vs. not verified

- Verified on the real server: local GPU path (one 14-min video ≈ 23 s end to end).
- Only tested against fakes: automatic LLM reload, the whole cloud mode (real AssemblyAI not yet
  called), file uploads, budget. Treat behaviour of the real AssemblyAI API (Hungarian accuracy,
  whether diarization works for Hungarian) as unknown until tested.

## Tests

```bash
pip install -r tests/requirements.txt     # plus ffmpeg/ffprobe on PATH
python -m pytest -q tests
```

`tests/fakes` stands in for torch, whisperx, yt_dlp, llama-server and AssemblyAI; see
`tests/conftest.py` for the flag files that switch fake behaviour (crash, GPU OOM, provider error…).
Add a test there for every behaviour change.

## Conventions

- The owner writes in Hungarian. User-facing docs (`README.md`) are Hungarian; code, comments,
  `SKILL.md` and this file are English.
- Keep the client (`yt_transcribe.py`) standard-library only.
- Never commit tokens or keys (HF token, AssemblyAI key are container env vars).
