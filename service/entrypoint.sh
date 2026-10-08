#!/bin/sh
# YouTube changes often; an outdated yt-dlp is the usual reason downloads break.
if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
  pip install --quiet --upgrade "yt-dlp[default]" || echo "yt-dlp update failed, using the installed version"
fi
exec uvicorn app.server:app --host 0.0.0.0 --port "${PORT:-8765}"
