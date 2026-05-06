FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Aliyun's PyPI mirror is fast from mainland China and acts as a transparent
# pypi.org proxy elsewhere, so this is safe to keep as the default.
RUN pip install --no-cache-dir \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt

COPY . .

# Generate the BAML client at build time so the image is self-contained.
RUN cd v1/resume_matching && baml-cli generate

# Listen on $PORT if injected (Tencent CloudBase, Cloud Run, Aliyun FC all do this);
# fall back to 8000 for local docker-compose / direct runs.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
