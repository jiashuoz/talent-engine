// Resume × Job fit scorer.
//
// Rubric the LLM follows (from user-agreed criteria):
//   HARD fails (auto-exclude from recommendation):
//     - education below JD minimum
//     - missing required certifications (护士证, 驾照, etc.)
//   SOFT penalties (scored, not excluded):
//     - age out of range
//     - major mismatch
//     - experience years short
//   BONUSES:
//     - location match
//     - image/height match when stated
//     - gender preference align
//
// The scorer emits structured `hard_fails` so the Python side can decide
// whether to still include the pair in top-3 (for demo) or drop it. It
// also emits `gaps` — the plain-language list of what the candidate is
// missing, which is the hook for the client's resume-coaching service.

function ScoreMatch(resume: Resume, job: Job) -> MatchScore {
  client Default
  prompt #"
    You are evaluating how well a Chinese job candidate fits a Chinese
    job posting. Output a single MatchScore.

    Scoring rubric — apply it consistently:

    HARD requirements (if failed, list in `hard_fails` and cap score at 40):
    - Education: candidate's highest degree must meet `job.education_min`.
      Map Chinese degrees: 高中 < 专科 < 本科 < 研究生 < 博士. If
      `education_min` is null, no education hard-fail.
    - Certifications: every item in `job.certifications_required` must
      appear (semantically — "护士执业资格证" satisfies "护士证") in
      `resume.certifications`.

    SOFT signals (penalise but do not hard-fail):
    - Age: outside `age_min`/`age_max` → strong penalty (5-15pt) but keep
      scoring. Close to the boundary (±2 years) → mild penalty.
    - Major: if `majors_preferred` is non-empty, judge semantic fit
      (e.g. "软件工程技术" satisfies "计算机相关专业"; "汉语言文学" does
      not). Mismatch → 10-20pt penalty.
    - Experience years: short by N years → proportional penalty. Fresh
      graduates explicitly welcomed by the JD → no penalty for zero years.
    - Relevant experience content: look at `resume.experience` vs
      `job.duties`. Matching domain experience (e.g. 酒店接待 for
      会务助理) is a strong strength.

    BONUSES (small positive signal only):
    - Location matches city/district → note in strengths.
    - Height/image: if JD mentions and resume shows (photos aren't in
      text, but height sometimes is), note in strengths. Do not penalise
      for absence — we do not want to judge appearance from text.
    - Gender: if JD has a preference and candidate matches → strength.
      Never penalise for mismatch unless JD says explicitly "仅限X".

    Verdict mapping from final score:
      90-100 → "强烈推荐"
      75-89  → "可推荐"
      60-74  → "勉强"
      0-59   → "不推荐"

    Output guidance:
    - `hard_fails`: short bullet strings, one per failed hard requirement.
      Empty list if none failed.
    - `strengths`: concrete, quoting specifics from the resume (e.g.
      "专业前10% + 华为ICT大赛三等奖").
    - `gaps`: what's missing, framed so it could be fed into a resume
      coaching prompt (e.g. "缺少高端酒店/会务服务经验，建议补充实习或
      培训证书"). This is the most important field for the business use
      case — be specific, not generic.
    - `reasoning`: one or two sentences tying it together.
    - Write all output in Simplified Chinese. The candidate and employer
      read Chinese.

    {{ ctx.output_format }}

    === 简历 ===
    姓名: {{ resume.name }}
    性别: {{ resume.gender }}
    出生年份: {{ resume.birth_year }}
    年龄: {{ resume.age }}
    籍贯: {{ resume.hometown }}
    教育背景:
    {% for edu in resume.education %}
      - {{ edu.school }} / {{ edu.degree }} / {{ edu.major }} ({{ edu.start }}–{{ edu.end }}) {{ edu.gpa_or_rank }}
    {% endfor %}
    经历:
    {% for exp in resume.experience %}
      - {{ exp.organization }} / {{ exp.title }} ({{ exp.start }}–{{ exp.end }})
        {{ exp.description }}
    {% endfor %}
    证书: {{ resume.certifications }}
    技能: {{ resume.skills }}
    语言: {{ resume.languages }}
    自我评价: {{ resume.self_evaluation }}

    完整简历原文（供补充信息参考）:
    ```
    {{ resume.raw_text }}
    ```

    === 岗位 ===
    招聘单位: {{ job.company }}
    招聘岗位: {{ job.position }}
    学历要求: {{ job.education_min }}
    年龄: {{ job.age_min }} - {{ job.age_max }}
    专业倾向: {{ job.majors_preferred }}
    经验年限: {{ job.experience_years_min }}
    性别: {{ job.gender_preference }}
    身高: {{ job.height_min_cm }}
    必需证书: {{ job.certifications_required }}
    形象要求: {{ job.image_requirements }}
    工作内容:
    {% for d in job.duties %}
      - {{ d }}
    {% endfor %}
    薪资: {{ job.salary_min }} - {{ job.salary_max }}
    工作时间: {{ job.work_schedule }}
    工作地点: {{ job.location }}
    福利: {{ job.benefits }}

    岗位原文:
    ```
    {{ job.raw_text }}
    ```
  "#
}
