// Extraction functions — resume PDF → Resume, JD text → Job.
//
// JD files often bundle multiple jobs in one file. Rather than ask one LLM
// call to both split AND parse (risk: dropped or merged jobs, silent field
// hallucination), we split in Python on the 招聘单位 header — which every
// real JD the client ships uses exactly once per job — and then call
// ParseSingleJob on each chunk in parallel.
//
// ParseJobDescriptions (list-output) remains as a last-resort fallback
// for inputs where no 招聘单位 header is found (unexpected format).

function ParseResume(text: string) -> Resume {
  client Default
  prompt #"
    You are parsing a Chinese job-seeker resume. The input is plain text
    extracted from a PDF — formatting and layout cues may be lost, so
    rely on field labels and ordering. Extract the fields defined by
    the Resume schema. Rules:

    - Dates: keep the candidate's original format (e.g. "2022.9-2026.6").
    - Age: if the resume states an age directly, fill `age`; otherwise fill
      `birth_year` and leave age null. Do not compute age yourself.
    - education[] should be newest-first. Map 学位/学历 to degree:
      "高中" / "专科" / "本科" / "研究生" / "博士".
    - experience[]: combine 实习经历, 工作经历, and 校园经历 into one list.
      Use bullet points joined by newlines for `description`.
    - certifications: certificates, awards, and licenses. Keep the original
      Chinese phrasing (e.g. "护士执业资格证", "C1驾照", "普通话二级甲等").
    - skills: tools/tech/soft skills that aren't certificates.
    - raw_text: the input text, unchanged. This is our verification that
      the model saw the whole resume.
    - If a field is not stated in the resume, leave it null or empty. Do
      not guess. Do not translate Chinese to English.

    {{ ctx.output_format }}

    Resume:
    ```
    {{ text }}
    ```
  "#
}

function ParseSingleJob(text: string) -> Job {
  client Default
  prompt #"
    You are parsing ONE Chinese job description. The input contains exactly
    one job posting. Extract the fields defined by the Job schema.

    Rules:

    - education_min: map to one of "高中" / "专科" / "本科" / "研究生".
      If the JD says "大专及以上" → "专科"; "本科及以上" → "本科". If not
      stated, leave null.
    - age_min / age_max: parse ranges like "18-28岁" → 18/28, "35岁以下"
      → null/35, "30岁以下" → null/30.
    - majors_preferred: keep the original Chinese phrasing as listed
      ("空乘", "计算机相关专业", "金融/经济"). Do not split "计算机相关"
      into every possible major — keep it as-is.
    - experience_years_min: "1年以上" → 1, "3年以上需求分析经验" → 3. If
      the JD says "可接收应届毕业生", still extract the stated years for
      the normal case and note the applied-fresh exception in raw_text.
    - gender_preference: "男女不限" → "不限"; "女生优先" → "不限"
      (preference not hard); explicit "仅限女" → "女".
    - height_min_cm: "身高170cm以上" → 170, "165cm及以上" → 165. Null if
      not mentioned.
    - certifications_required: only those explicitly required
      (护士职业资格证, C1驾照). Skills like "熟练Office" go in `duties` or
      be omitted — they aren't certificates.
    - image_requirements: copy the 形象气质 / 身高 / 服务意识 sentence
      verbatim if present. If absent leave null.
    - salary_min / salary_max: parse "综合薪资3600-4000元" → 3600/4000,
      "5K-6K" → 5000/6000, "综合薪资5000+" → 5000/null.
    - location: include city + district when given (e.g. "济南市历下区").
    - benefits: extract "五险", "五险一金", "双休", "食宿", "节假日三薪" etc.
    - raw_text: the full input text, unchanged. This is our verification
      that the model saw the whole chunk.

    {{ ctx.output_format }}

    Job posting:
    ```
    {{ text }}
    ```
  "#
}

// Fallback: single-call list parse. Used only when DetectJobBoundaries
// produces obviously bad output (invalid offsets, nothing detected but
// input non-empty, etc). Kept so pipeline has a degraded-but-working path.
function ParseJobDescriptions(text: string) -> Job[] {
  client Default
  prompt #"
    You are parsing a Chinese job-description document that may contain one
    or more jobs. Return one Job entry per job. Follow the same field rules
    as ParseSingleJob (age/education/salary parsing, etc). If the document
    is empty return an empty list.

    {{ ctx.output_format }}

    Document:
    ```
    {{ text }}
    ```
  "#
}
