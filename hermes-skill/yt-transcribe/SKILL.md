---
name: yt-transcribe
description: Speaker-labelled video transcripts and summaries
version: 2.0.0
author: Zoltán
license: MIT
metadata:
  hermes:
    tags: [YouTube, Video, Transcripts, Diarization, Summary, Media]
    related_skills: [youtube-content]
    requires_toolsets: [terminal]
    config:
      - key: yt_transcribe.url
        description: Base URL of the yt-transcribe API (on the Proxmox LXC)
        default: "http://localhost:8765"
        prompt: yt-transcribe API URL (e.g. http://192.168.1.10:8765)
      - key: yt_transcribe.out_dir
        description: Folder where transcript files are saved
        default: "~/yt-transcripts"
        prompt: Folder for saved transcripts
---

# yt-transcribe

Makes a transcript from the audio of a video with a speech recognition
service, labels who said what (Szereplő 1, Szereplő 2 / Speaker 1, Speaker 2),
then summarises it. The source is a video URL (YouTube and other sites) or a
local audio/video file. Works for Hungarian and English and for videos that
have no captions.

## When to Use

- The user gives a video URL or a local audio/video file and asks for a
  transcript, subtitles (felirat) or a summary (összefoglaló) and wants to
  know who said what, or several people talk in it (interview, podcast,
  panel, meeting).
- The video has no captions, or the `youtube-content` skill returned nothing.
- The user asks for an `.srt` file.

For a quick summary of a single-speaker video that already has captions,
`youtube-content` is faster; use this skill when speakers or accuracy matter.

## Quick Reference

`SERVER` is the `yt_transcribe.url` value and `OUT` is the
`yt_transcribe.out_dir` value from the skill config shown when this skill
loads.

**Always call the `terminal` tool with `timeout=600` for `run` and `wait`.**
The command blocks until the transcript is ready and returns at once when it
is.

| Task | Command |
| --- | --- |
| Transcribe with speakers | `python "${HERMES_SKILL_DIR}/scripts/yt_transcribe.py" run "SOURCE" --server SERVER --out-dir OUT` |
| Keep waiting | `python "${HERMES_SKILL_DIR}/scripts/yt_transcribe.py" wait JOB_ID --server SERVER --out-dir OUT` |
| Check the service | `python "${HERMES_SKILL_DIR}/scripts/yt_transcribe.py" health --server SERVER` |

If `python` is not found, use `python3` (Linux/macOS) or `py` (Windows)
instead. The script needs no extra packages.

`SOURCE` is the video URL or the full path of a local file, always in double
quotes. A local file is uploaded to the service by the script; nothing else is
needed.

Options for `run`: `--language hu` or `--language en` when the user states the
language (otherwise it is detected); `--speakers N` when the user says how
many people talk; `--min-speakers A --max-speakers B` for a range;
`--no-diarize` when speaker labels are not wanted (faster); `--force` to redo
a video instead of using the saved result.

## Mode and model

The service transcribes either on the user's own server (`local`) or with a
paid cloud provider (`cloud`). Cloud mode sends the audio to a third party and
uses a monthly budget, so it is the user's decision, never yours.

- Pass `--mode cloud` only when the user asks for it in this request (for
  example "felhőben", "in the cloud", "AssemblyAI"). Pass `--mode local` when
  they ask for local. Otherwise pass no `--mode`; the service default applies.
- Pass `--model` only when the user names a model. Local: `large-v3` (most
  accurate) or `large-v3-turbo` (faster). Cloud: `universal-2`,
  `universal-3.5-pro`, or `auto` (the default, which picks the better model
  the language allows).
- If a job fails in one mode, do not retry it in the other mode on your own.
  Report the error and let the user choose.
- The output shows which `mode` and `model` produced the transcript. For cloud
  jobs it also has `cloud.estimated_cost_usd`; mention the mode, and the cost
  when it is a cloud job, in one short line of your answer.

## Why one long call

In local mode the transcription runs on the same GPU as the language model
you are running on. While a job runs, your own requests to the language model
wait in a queue until the job ends, and afterwards the model is loaded again.
Every turn you take while a job is running therefore stalls until the job is
done and makes the model reload. So:

- Start the job and wait for it in a single `terminal` call with
  `timeout=600`. Do not poll with short timeouts, do not run it in the
  background, and do not send progress messages while it runs.
- Do not call any other tool between starting the job and getting its result.

## Procedure

1. Run the `run` command with the `terminal` tool and `timeout=600`. Always
   quote the URL or file path.
2. Read the JSON it prints.
   - `"status": "done"`: continue with step 3.
   - `"status": "running"` or `"queued"`: the job needs more than one call
     (a very long video). Immediately run the `wait JOB_ID` command, again
     with `timeout=600`, and repeat until the status changes.
   - `"status": "error"`: follow Pitfalls below.
3. Read the whole file at `files.txt` with `read_file`. If it is returned in
   pages, read every page before writing anything. Do not split the transcript
   into chunks and summarise the chunks separately; summarise from the full
   text.
4. Answer in the language the user wrote in, whatever the language of the
   video. Unless the user asked for something else, give:
   - Title, channel, length, language, number of speakers.
   - **Summary**: 5-10 sentences covering the whole video.
   - **Main points**: bullets with `[hh:mm:ss]` timestamps taken from the
     transcript.
   - **Speakers** (only when `diarized` is true): one line per speaker with
     what they argued or contributed.
   - **Files**: the paths in `files.txt` (readable transcript) and `files.srt`
     (subtitle file).
5. If the user asked only for the transcript or subtitles, skip the summary
   and give the file paths plus the first few lines as a preview.

## Speaker labels

- Keep the labels exactly as they are in the transcript (`Szereplő 1`,
  `Speaker 2`). They are anonymous voices, numbered by first appearance.
- Add a real name or role in brackets only when the transcript itself makes it
  unambiguous (someone is addressed by name, or introduces themselves), for
  example `Szereplő 1 (műsorvezető)`. Never guess a name from the video title
  alone.
- Speaker separation is imperfect when people talk over each other, have
  similar voices, or only say a word or two. If `speakers` contains a label
  with a share of about 2% or less, mention that it may be a mis-split of
  another speaker rather than a real person.
- If the count looks wrong and the user knows the right number, rerun with
  `--speakers N --force`.

## Pitfalls

- `cannot reach the yt-transcribe service`: the API is stopped or the URL is
  wrong. Run the `health` command, report the result, and ask the user to
  start the service or fix `yt_transcribe.url`
  (`hermes config set skills.config.yt_transcribe.url http://HOST:8765`).
- `The GPU worker could not be reached`: the GPU server is off or llama-swap
  is not running, so local mode is unavailable. Tell the user; offer cloud
  mode, but do not switch to it without their answer.
- `Speaker separation needs a Hugging Face token`: the GPU worker has no
  `HF_TOKEN`. Tell the user, and offer to rerun with `--no-diarize`.
- `yt-dlp could not ...`: the video is private, age-restricted,
  region-blocked, or yt-dlp is out of date. Relay the message; restarting the
  API service updates yt-dlp.
- `process was killed` or `The connection to the GPU worker broke` or
  `The GPU worker stopped`: the job was interrupted. Run the same `run`
  command once more; it resumes from the saved transcription. If it fails
  again, report it to the user.
- `warnings` mentions `ran on CPU` or `finished on CPU`: the GPU could not be
  used, so the job was slow. Pass the warning on to the user as it is.
- `universal-3.5-pro does not support the language`: that cloud model cannot
  do this language (Hungarian among others). Rerun with `--model auto`.
- `The monthly cloud budget would be exceeded`: tell the user. Offer local
  mode; do not switch to it without their answer.
- `cloud mode is not set up` or `AssemblyAI could not be reached` or
  `AssemblyAI answered HTTP ...`: cloud mode is unavailable right now. Relay
  the message and offer local mode; do not switch on your own.
- `The file has no audio track` / `could not be read as audio or video`: the
  local file is not usable media. Tell the user which file.
- `is neither a URL nor an existing file`: the path is wrong. Check the path
  with the user; on Windows pass it exactly as given, in double quotes.
- `Unknown job id`: the service restarted. Run the `run` command again;
  finished work is cached and returns at once.
- Wrong language detected (the transcript is gibberish): rerun with
  `--language hu` or `--language en` and `--force`.
- Never paste the full transcript into the chat unless the user asks for it;
  point to the saved file.

## Verification

- The `run`/`wait` output has `"status": "done"` and both files in `files`
  exist.
- `language` matches the language actually spoken in the video.
- Every timestamp in the answer appears in the transcript.
