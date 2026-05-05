"""Usage-log email for the public resume-matching endpoint.

Every pipeline run — success or failure — fires a small email to
`USAGE_NOTIFY_TO` so we have visibility into who is using the public tool
and at what scale. The email carries summary stats only (counts, elapsed
time, error state, IP), never the resume/job content itself.

Fire-and-forget from the router: a flaky email API must not delay or break
the streaming response the user sees.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any, Dict, List, Optional

import httpx
import resend

logger = logging.getLogger(__name__)

USAGE_NOTIFY_TO = os.getenv("USAGE_NOTIFY_TO", "josh@mnexa.ai")

# Short timeout on the geo lookup — it's a nice-to-have annotation and must
# never extend the background email task's lifetime noticeably. On failure
# or timeout we just omit the location.
_GEOIP_TIMEOUT_SEC = 3.0


async def _geoip_lookup(ip: str) -> Optional[str]:
    """Return a short location string for the IP, or None.

    Uses ipinfo.io's unauthenticated endpoint (50k requests/month free, no
    token). Falls back silently on loopback IPs, on network failure, or on
    non-200 responses — this is telemetry, not a required feature.
    """
    if not ip or ip in ("unknown", "127.0.0.1", "::1", "localhost"):
        return None
    try:
        async with httpx.AsyncClient(timeout=_GEOIP_TIMEOUT_SEC) as client:
            resp = await client.get(f"https://ipinfo.io/{ip}/json")
        if resp.status_code != 200:
            return None
        data = resp.json()
        parts = [data.get("city"), data.get("region"), data.get("country")]
        org = data.get("org")  # e.g. "AS4134 Chinanet" — useful for China ISPs
        loc = " / ".join(p for p in parts if p)
        if org:
            loc = f"{loc} ({org})" if loc else org
        return loc or None
    except Exception as e:
        logger.debug("geoip lookup failed for %s: %s", ip, e)
        return None


async def send_usage_email(
    *,
    ip: str,
    resume_count: int,
    jd_file_count: int,
    jobs_parsed: int,
    resumes_scored: int,
    elapsed_sec: float,
    error: Optional[str] = None,
    user_agent: Optional[str] = None,
    summaries: Optional[List[Dict[str, Any]]] = None,
    # Pipeline-specific labels. Defaults are tuned for resume-matching; the
    # webinar-followup router overrides these so "Resumes × JDs" doesn't
    # leak into an email that's really about webinar attendees.
    kind: str = "Resume Match",
    primary_unit: str = "resumes",
    secondary_unit: str = "JDs",         # empty string hides the secondary count
    results_label: str = "Results (top match per resume)",
    row_format: str = "{label} → {company} / {position}  ({score}, {verdict})",
) -> None:
    """Send a usage-log email. Never raises — failures log and return.

    `summaries` — one dict per row in the run, shape:
        {
          "filename": str,
          "name": Optional[str],
          "parse_error": Optional[str],
          "top": Optional[{"company","position","score","verdict"}],
        }
    When provided, the email body includes a per-row results table and
    the subject gets a verdict-distribution tag so you can scan the inbox
    and spot good/bad matches without opening each email.
    """
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        logger.info("RESEND_API_KEY not set — skipping usage email")
        return

    from_email = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
    status = "ERROR" if error else "OK"

    # Verdict rollup — quick visual cue in both subject and body. Counter
    # keeps keys in insertion order so the tag is deterministic.
    verdict_tag = ""
    if summaries:
        verdicts = Counter(
            s["top"]["verdict"]
            for s in summaries
            if s.get("top") and s["top"].get("verdict")
        )
        if verdicts:
            verdict_tag = " · " + " / ".join(
                f"{v}×{n}" for v, n in verdicts.most_common()
            )

    count_segment = f"{resume_count} {primary_unit}"
    if secondary_unit:
        count_segment += f" × {jd_file_count} {secondary_unit}"
    subject = f"[{kind}] {status} · {count_segment}{verdict_tag} · {elapsed_sec:.0f}s"

    location = await _geoip_lookup(ip)
    ip_line = f"{ip}  ({location})" if location else ip

    lines = [
        f"Status:         {status}",
        f"IP:             {ip_line}",
        f"User-Agent:     {user_agent or '-'}",
        f"{primary_unit.capitalize():<15} in: {resume_count}",
    ]
    if secondary_unit:
        lines.append(f"{secondary_unit.capitalize():<15} in: {jd_file_count}")
    lines.extend([
        f"Jobs parsed:    {jobs_parsed}",
        f"Rows processed: {resumes_scored}",
        f"Elapsed:        {elapsed_sec:.1f}s",
    ])
    if summaries:
        lines.append("")
        lines.append(f"{results_label}:")
        for s in summaries:
            label = s.get("name") or s.get("filename", "?")
            if s.get("parse_error"):
                lines.append(f"  • {label} — parse error: {s['parse_error']}")
                continue
            top = s.get("top")
            if not top:
                lines.append(f"  • {label} — no match")
                continue
            lines.append("  • " + row_format.format(
                label=label,
                company=top.get("company", "?"),
                position=top.get("position", "?"),
                score=top.get("score", ""),
                verdict=top.get("verdict", ""),
            ))
    if error:
        lines.append("")
        lines.append("Error:")
        lines.append(error)
    text = "\n".join(lines)

    try:
        resend.api_key = api_key
        payload: Dict[str, Any] = {
            "from": from_email,
            "to": USAGE_NOTIFY_TO,
            "subject": subject,
            "text": text,
        }
        resend.Emails.send(payload)
        logger.info("Usage email sent to %s", USAGE_NOTIFY_TO)
    except Exception as e:
        # Email is telemetry, not a user-visible feature — swallow.
        logger.warning("Failed to send usage email: %s", e)
