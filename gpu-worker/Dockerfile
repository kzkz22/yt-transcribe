FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    NLTK_DATA=/models/nltk \
    DATA_DIR=/data

# whisperx reads audio through ffmpeg
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Lets faster-whisper (CTranslate2) find the cuDNN/cuBLAS libraries that ship with torch.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib

COPY gpuworker ./gpuworker

VOLUME ["/models", "/data"]
EXPOSE 8766
CMD ["sh", "-c", "exec uvicorn gpuworker.server:app --host 0.0.0.0 --port ${PORT:-8766}"]
