// Shared data classes used by parse.baml and match.baml.
//
// All candidate/job fields are optional so messy real-world inputs don't
// force the LLM to fabricate. The raw_text field on Job preserves the
// original paragraph so the matcher can quote it back in gap reports.

class Resume {
  name string?
  gender string?                    // "男" / "女" / null
  birth_year int?                   // for age derivation
  age int?                          // if directly stated
  phone string?
  email string?
  hometown string?

  // Education — newest first. Current school + major + degree typically the first entry.
  education Education[]

  // Work / internship / campus experience — any paid or structured experience
  experience Experience[]

  certifications string[]           // 证书: "护士执业资格证", "C1驾照", "普通话二级甲等"
  skills string[]                   // 技能: "Python", "Office", "ERP系统"
  languages string[]                // "英语", "日语"
  self_evaluation string?           // 自我评价原文
  raw_text string                   // full extracted resume text for matcher context
}

class Education {
  school string
  degree string?                    // "专科", "本科", "研究生", "高中"
  major string?
  start string?                     // "2022.9" or "2022-09"
  end string?                       // "2026.6"
  gpa_or_rank string?               // e.g. "专业前10%"
}

class Experience {
  organization string
  title string
  start string?
  end string?
  description string                // merged bullet points
}

// ---------------------------------------------------------------------------

class Job {
  company string                    // 招聘单位
  position string                   // 招聘岗位

  // Hard-ish requirements — mostly optional, LLM extracts only what's stated.
  education_min string?             // "专科", "本科" (null = 不限)
  age_min int?
  age_max int?
  majors_preferred string[]         // 专业倾向 ("空乘", "计算机相关", "金融/经济")
  experience_years_min int?
  gender_preference string?         // "不限", "男", "女"
  height_min_cm int?
  certifications_required string[]  // 护士证, 驾照
  image_requirements string?        // 形象气质原文 (subjective — bonus only)

  duties string[]                   // 工作内容 bullets
  salary_min int?                   // 月薪 RMB
  salary_max int?
  work_schedule string?             // "长白班 / 双休 / 上12休36"
  location string                   // 工作地点 (城市 + 区)
  benefits string[]                 // "五险一金", "双休", "食宿"
  raw_text string                   // full original JD paragraph
}

// ---------------------------------------------------------------------------

// One match between a resume and a job. Score 0-100 — the matcher is told to
// follow the three-tier rubric in match.baml so scoring is consistent.
class MatchScore {
  score int                         // 0-100 overall fit
  verdict string                    // "强烈推荐" / "可推荐" / "勉强" / "不推荐"
  hard_fails string[]               // requirements the candidate fails hard — empty if none
  strengths string[]                // specific matches between resume and JD
  gaps string[]                     // what the candidate is missing (drives resume coaching upsell)
  reasoning string                  // 1-2 sentences summarising the fit
}
