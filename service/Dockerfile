FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    NLTK_DATA=/models/nltk \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime for YouTube; deno is the one it uses by default.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Lets faster-whisper (CTranslate2) find the cuDNN/cuBLAS libraries that ship with torch
# when the container runs with DEVICE=cuda. Harmless on CPU.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib

COPY app ./app
COPY entrypoint.sh .

VOLUME ["/models", "/data"]
EXPOSE 8765
# Run through sh so it works even if the executable bit was lost while copying the files.
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
