# Project: talent-engine

FastAPI service for resume parsing + JD-to-resume matching, backed by BAML-driven LLM calls. Deployed to mainland China cloud infrastructure (UCloud) for Chinese-market users.

## China-cloud deployment gotchas

When deploying to mainland China cloud (UCloud, Aliyun, Tencent Cloud), several upstream resources are blocked or unreliable from mainland VMs. **Always configure these workarounds at the start of any new deployment.**

### Docker Hub is blocked (`registry-1.docker.io`)

Symptom: `docker pull` (or `docker compose up`) fails with `dial tcp ...:443: i/o timeout`.

Fix: configure registry mirrors in `/etc/docker/daemon.json` *before* the first pull:

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1panel.live",
    "https://hub-mirror.c.163.com",
    "https://mirror.baidubce.com"
  ]
}
```

Then `sudo systemctl restart docker`. Verify with `sudo docker info | grep -A 5 "Registry Mirrors"`.

These four mirrors are public and have been reliable as of 2026; if all four fail, search for "Docker Hub mirror" — Chinese cloud providers and community projects rotate the available list periodically.

### Docker install script blocked (`get.docker.com`)

Symptom: `curl -fsSL https://get.docker.com | sh` returns `Connection reset by peer`.

Fix: install via apt instead — `sudo apt-get install -y docker.io docker-compose-v2`. UCloud images already have `mirrors.ucloud.cn` configured, so apt is fast and reliable.

### GitHub clone can be slow or unreliable

Symptom: `git clone https://github.com/...` hangs or runs at <100 KB/s.

Fix: use a mainland mirror like `https://gitclone.com/github.com/<user>/<repo>.git` as fallback, or mirror the repo to Gitee and clone from there. For development of this project, pushing to Gitee as a secondary remote is a reasonable workflow if GitHub becomes a bottleneck.

### Other notes

- DashScope (Qwen), DeepSeek, and Tencent Hunyuan API endpoints work fine from mainland VMs (they're hosted in China).
- Google AI / Vertex (Gemini) endpoints are blocked — don't try `LLM_PROVIDER=Gemini` from a mainland VM.
- The `apt-get update` repos on UCloud images already use UCloud mirrors; no extra config needed.
