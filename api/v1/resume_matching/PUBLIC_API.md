# talent-engine — Public API

Three surfaces, all authenticated with the same `X-API-Key`:

- **`POST /v1/resume/parse`** — turn resume PDFs into structured `Resume` JSON.
- **`POST /v1/job/parse`** — turn JD `.txt` files (single or `招聘单位`-bundled) into structured `Job` JSON.
- **`POST /v1/resume-matching/match`** + async variants — score parsed resumes against parsed job postings.

Typical flow from a WeChat mini program:

1. Send the user's PDF(s) to `/v1/resume/parse` → get back `Resume` JSON.
2. Send the JD text(s) to `/v1/job/parse` → get back `Job` JSON.
3. Pass both straight into `/v1/resume-matching/match` (or `/match/async`).

No transformation between calls — the parse outputs and the match inputs share the same `Resume` / `Job` shapes.

- **Base URL**: `https://api.mnexa.ai`
- **Auth**: `X-API-Key` header on every request
- **Content type**: `application/json` (UTF-8 — Chinese text preserved verbatim) for the match API; `multipart/form-data` for the parse API

Interactive schema browser: <https://api.mnexa.ai/docs> (search for `resume-matching-api`).

---

## Authentication

Every request must include an `X-API-Key` header. Keys are issued by Mnexa
operators on request — there is no self-serve signup.

```
X-API-Key: mnk_<43 url-safe chars>
```

Missing or revoked keys return `401`. Keep the key server-side; do not
embed it in mini-program client code.

---

## Endpoints

### `POST /v1/resume/parse` — PDF → JSON

Parse one or more resume PDFs into the structured `Resume` shape consumed by `/match`.

```bash
curl -X POST https://api.mnexa.ai/v1/resume/parse \
  -H "X-API-Key: $KEY" \
  -F "resumes=@张三.pdf" \
  -F "resumes=@李四.pdf" \
  -F "resume_ids=r_001" \
  -F "resume_ids=r_002"
```

| Form field | Type | Notes |
|---|---|---|
| `resumes` | file (one or more) | Resume PDFs. Up to 20 per request, 5 MB each. |
| `resume_ids` | string (one per file, optional) | Client-supplied ids in file order. If omitted, `resume_id` defaults to the upload filename. |

**Response (`200 OK`)**:

```jsonc
{
  "status": "completed",
  "parsed": [
    {
      "resume_id": "r_001",
      "filename": "张三.pdf",
      "resume": { /* full Resume — same shape as the /match input */ }
    }
  ],
  "errors": [
    { "resume_id": "r_002", "filename": "broken.pdf", "error": "ParseResume failed: ..." }
  ],
  "stats": {
    "files_received": 2,
    "parsed_ok": 1,
    "parsed_failed": 1,
    "elapsed_ms": 8420,
    "input_tokens": 4180,
    "output_tokens": 920
  }
}
```

Per-file failures (corrupt PDF, BAML extraction error) appear in `errors[]` with status `200`. Only request-level failures (auth, validation, size cap) return 4xx.

**Status codes**: `200` (with possible `errors[]`); `400` on validation; `401` on auth; `413` on size/count; `500` on full-pipeline failure.

Sync only. A typical batch (≤ 20 PDFs) parses in 10–30 s and fits comfortably under WeChat's default 60 s timeout.

---

### `POST /v1/job/parse` — JD text → JSON

Parse JD `.txt` files into the structured `Job` shape consumed by `/match`. Each file may contain a single JD or a bundle separated by `招聘单位` headers; the server splits before extraction so each chunk gets a focused LLM call.

```bash
curl -X POST https://api.mnexa.ai/v1/job/parse \
  -H "X-API-Key: $KEY" \
  -F "jds=@招聘公告-A.txt" \
  -F "jds=@招聘公告-B.txt"
```

| Form field | Type | Notes |
|---|---|---|
| `jds` | file (one or more) | UTF-8 `.txt` files. Up to 10 per request, 200 KB each. Combined chunks capped at 100 per request. |

**Response (`200 OK`)**:

```jsonc
{
  "status": "completed",
  "parsed": [
    {
      "job_id": "招聘公告-A.txt#0",     // synthesized as `{filename}#{chunk_index}`
      "filename": "招聘公告-A.txt",
      "chunk_index": 0,
      "job": { /* full Job — same shape as the /match input */ }
    },
    {
      "job_id": "招聘公告-A.txt#1",
      "filename": "招聘公告-A.txt",
      "chunk_index": 1,
      "job": { ... }
    }
  ],
  "errors": [
    { "filename": "招聘公告-B.txt", "chunk_index": 2, "error": "ParseSingleJob failed: ..." }
  ],
  "stats": {
    "files_received": 2,
    "chunks_detected": 5,
    "jobs_parsed": 4,
    "jobs_failed": 1,
    "elapsed_ms": 9100,
    "input_tokens": 6240,
    "output_tokens": 1480
  }
}
```

**Splitting behavior:**

- Files containing `招聘单位` headers are split on each header and parsed in parallel via `ParseSingleJob`. Per-chunk failures land in `errors[]` without sinking the rest.
- Files with no `招聘单位` header fall back to a single `ParseJobDescriptions` list call. Per-chunk error granularity isn't available on this path — failures show up as one file-level error.

**Status codes**: `200` (with possible `errors[]`); `400` on validation or non-UTF-8; `401` on auth; `413` on size, file count, or chunk count caps; `500` on full-pipeline failure.

Sync only.

---

### `POST /v1/resume-matching/match` — synchronous match

Score every (resume × job) pair and return results inline.

Use for small batches that fit in WeChat's request timeout (~60 s default).
For larger batches, use the async variant below.

```bash
curl -X POST https://api.mnexa.ai/v1/resume-matching/match \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d @request.json
```

**Status codes**: `200` on success (with possible per-pair errors in the
body); `400`/`413` on validation/size errors; `401` on auth failure;
`500` on pipeline failure.

---

### `POST /v1/resume-matching/match/async` — accept and queue

Returns immediately with a `job_id` to poll. The match runs as a
background task on the server.

```bash
curl -X POST https://api.mnexa.ai/v1/resume-matching/match/async \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d @request.json
```

**Response (`202 Accepted`)**:
```json
{ "job_id": "rmj_AbCdEfGh...", "status": "queued" }
```

Use this for any batch with more than ~50 pairs (`resumes × jobs`), or
any case where you can tolerate polling.

---

### `GET /v1/resume-matching/match/{job_id}` — poll

Returns the current state of an async job. Poll at 1–2 second intervals.

```bash
curl https://api.mnexa.ai/v1/resume-matching/match/$JOB_ID \
  -H "X-API-Key: $KEY"
```

**Status codes**: `200` while the job exists; `404` if it doesn't (never
existed, expired after 1 hour, or lost to a server restart — in all three
cases, retry the request from scratch).

---

## Request schema

Same body for `POST /match` and `POST /match/async`.

```json
{
  "resumes": [
    {
      "resume_id": "r_001",
      "resume": { /* Resume — see below */ }
    }
  ],
  "jobs": [
    {
      "job_id": "j_001",
      "job": { /* Job — see below */ }
    }
  ],
  "options": {
    "concurrency": null
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `resumes[].resume_id` | string | **Required**, **unique**. Client-supplied opaque id; echoed back in the response so you can map results to your records. |
| `resumes[].resume` | Resume | Parsed resume object. |
| `jobs[].job_id` | string | **Required**, **unique**. Same semantics. |
| `jobs[].job` | Job | Parsed job posting. |
| `options.concurrency` | int? | Optional server-side concurrency hint; clamped to a safe range. Leave null to use the default. |

### Resume

All fields except the `education` / `experience` lists are optional.
Send what you have — missing fields hurt match quality but never error.

```jsonc
{
  "name": "张三",
  "gender": "男",                       // "男" / "女" / null
  "birth_year": 2000,                   // for age derivation
  "age": 24,                            // if directly stated
  "phone": "13800000000",
  "email": "zhang@example.com",
  "hometown": "北京",

  "education": [                        // newest first
    {
      "school": "山东大学",
      "degree": "本科",                  // "高中" / "专科" / "本科" / "研究生" / "博士"
      "major": "计算机科学与技术",
      "start": "2018.9",                // any string format; "2018.9" or "2018-09"
      "end": "2022.6",
      "gpa_or_rank": "专业前10%"
    }
  ],

  "experience": [
    {
      "organization": "阿里巴巴",
      "title": "实习生",
      "start": "2021.6",
      "end": "2021.9",
      "description": "merged bullet points as a single string"
    }
  ],

  "certifications": ["护士执业资格证", "C1驾照"],
  "skills": ["Python", "Office", "ERP系统"],
  "languages": ["英语", "日语"],
  "self_evaluation": "勤奋好学，沟通能力强",
  "raw_text": "the full extracted resume text — improves match quality"
}
```

`raw_text` is optional but **strongly recommended**. The matcher uses it
as ground-truth context when scoring; without it the match relies solely
on the structured fields.

### Job

`company`, `position`, and `location` are required (the matcher needs at
least job identity + location to produce useful output). Everything else
is optional.

```jsonc
{
  "company": "北京银行济南分行",         // 招聘单位
  "position": "会务主管",                // 招聘岗位
  "location": "济南市历下区",            // 工作地点 (城市 + 区)

  "education_min": "本科",               // null = 不限
  "age_min": 22,
  "age_max": 35,
  "majors_preferred": ["酒店管理", "工商管理"],
  "experience_years_min": 1,
  "gender_preference": "不限",           // "不限" / "男" / "女"
  "height_min_cm": null,
  "certifications_required": [],
  "image_requirements": "形象气质佳",

  "duties": [
    "负责会议室管理与会前准备",
    "对接会务设备供应商"
  ],
  "salary_min": 6000,                    // 月薪 RMB
  "salary_max": 9000,
  "work_schedule": "长白班 / 双休",
  "benefits": ["五险一金", "餐补"],
  "raw_text": "招聘单位：北京银行济南分行 ..."
}
```

---

## Response schema

`POST /match` returns the body below directly. `GET /match/{job_id}`
returns the same fields when `status == "completed"`, plus a `progress`
field while the job is still running.

```jsonc
{
  "status": "completed",                 // "queued" | "running" | "completed" | "failed"
  "matches": [
    {
      "resume_id": "r_001",
      "job_id": "j_001",
      "score": 87,                       // 0–100 overall fit
      "verdict": "可推荐",                // see verdict mapping below
      "hard_fails": [],                  // hard requirements the candidate missed; empty if none
      "strengths": [                     // concrete matches (quote-able from the resume)
        "学历匹配",
        "在校期间组织过类似活动"
      ],
      "gaps": [                          // what's missing — useful for resume coaching
        "缺少高端酒店会务经验"
      ],
      "reasoning": "1–2 sentence summary tying it together"
    }
  ],
  "errors": [                            // per-pair failures; absent ids in `matches` will appear here
    { "resume_id": "r_002", "job_id": "j_005", "error": "ScoreMatch timeout" }
  ],
  "stats": {
    "pairs_scored": 49,
    "pairs_failed": 1,
    "elapsed_ms": 12340
  }
}
```

### Verdict mapping

| Score range | Verdict |
|---|---|
| 90–100 | `强烈推荐` |
| 75–89 | `可推荐` |
| 60–74 | `勉强` |
| 0–59 | `不推荐` |

Hard-fails (failed required certifications, education below minimum, etc.)
cap the score at 40 by rule.

### Async poll responses

While the job is running:

```jsonc
{
  "job_id": "rmj_AbCdEfGh...",
  "status": "running",                   // or "queued" before the worker starts
  "progress": { "pairs_done": 12, "pairs_total": 50 }
}
```

When complete, the same body as `/match` is returned (with the same
`status: "completed"`, `matches`, `errors`, `stats`), plus `job_id` and
the final `progress` snapshot.

When failed:

```jsonc
{
  "job_id": "rmj_...",
  "status": "failed",
  "progress": { "pairs_done": 0, "pairs_total": 50 },
  "error": "Internal pipeline error: ..."
}
```

---

## Limits

| Limit | Value |
|---|---|
| Max resumes per request | 100 |
| Max jobs per request | 100 |
| Max pairs per request (`resumes × jobs`) | **1,000** |
| Async job result TTL | 1 hour after creation |

Requests exceeding these caps return `413 Payload Too Large`.

Duplicate `resume_id` or `job_id` values within a single request return
`400` — fix client-side rather than relying on silent dedup.

---

## Errors

All errors return a JSON body of the form:

```json
{ "detail": "human-readable message" }
```

| Status | When |
|---|---|
| `400` | Validation error: empty resumes/jobs, duplicate ids, malformed JSON. |
| `401` | Missing or invalid/revoked `X-API-Key`. |
| `404` | `GET /match/{job_id}` — job not found or expired (retry from scratch). |
| `413` | Request exceeds the size or pair-count caps. |
| `500` | Internal pipeline failure (sync `/match` only — async failures show up as `status: "failed"` on the poll response). |

Per-pair scoring failures are **not** errors at the request level: the
HTTP response is `200`, with the failing pairs surfaced in `errors[]`
and the rest of the matches in `matches[]`. This means a single flaky
LLM call doesn't sink the whole batch.

---

## Choosing sync vs async

| You have… | Use |
|---|---|
| ≤ ~50 pairs | `POST /match` (sync). Simpler. |
| > 50 pairs | `POST /match/async` + `GET /match/{job_id}`. Avoids WeChat's request timeout. |
| Need progress updates in the UI | Async + poll. Each poll returns `progress.pairs_done / pairs_total`. |

Polling cadence: 1–2 seconds is plenty. Polling more often gives you no
extra information and wastes bandwidth.

---

## Cost & quota

There is no per-request rate limit today. Each request is logged on the
server with token counts, so usage is auditable. Per-pair cost is
roughly `$0.001–$0.005` depending on resume / job size — assume a few
US cents per typical mini-program search and you'll be in the right
ballpark. Talk to the operator if you need a hard quota.
