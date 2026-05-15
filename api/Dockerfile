FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Default index is Aliyun (fast inside mainland CN). For builds outside CN —
# local Mac/Linux dev, GitHub Actions — pass `--build-arg PIP_INDEX_URL=https://pypi.org/simple/`
# (or set PIP_INDEX_URL in the build env) to avoid the slow cross-firewall fetch.
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir \
    --index-url ${PIP_INDEX_URL} \
    --trusted-host mirrors.aliyun.com \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    -r requirements.txt

COPY . .

# Generate the BAML client at build time so the image is self-contained.
RUN cd v1/resume_matching && baml-cli generate

# Listen on $PORT if injected (Tencent CloudBase, Cloud Run, Aliyun FC all do this);
# fall back to 8000 for local docker-compose / direct runs.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
