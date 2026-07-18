"""
LLM extraction of a Hebrew housing post -> ListingExtract.

Default provider is Gemini's free tier because it is genuinely free and the
most reliable on colloquial Hebrew. It is wrapped so you can swap to any
OpenAI-compatible endpoint (a local Ollama model for full privacy, or Groq)
by changing LLM_PROVIDER in config.py — pipeline code never changes.
"""
from __future__ import annotations
import json
import os

import config
from models import ListingExtract

# Hebrew instruction prompt. The null-not-guess rule is the single most
# important line here: a hallucinated "1800" would sail through the price gate.
_SYSTEM_HE = """אתה מנתח מודעות שכירות של דירות שותפים בבאר שבע, מקבוצות פייסבוק בעברית מדוברת.
חלץ את השדות לפי הסכימה. כללים מחייבים:
- אם שדה כלשהו אינו מופיע במפורש במודעה — החזר null. אסור לנחש או להמציא מספרים.
- price_per_room_ils = העלות החודשית לשותף אחד (חדר אחד), ללא חשבונות (ארנונה/ועד/מים).
  אם מצוין רק שכר הדירה הכולל, חלק במספר הדיירים הכולל בדירה. אם אין מספיק מידע — null.
- available_rooms_count = מספר החדרים הפנויים כרגע להשכרה. אם הפוסט מחפש שותפים לדירה,
  זהו מספר השותפים המבוקשים (מחפשים "שותף/ה" ביחיד = 1; "שני שותפים" = 2).
- total_roommates_in_apt = מספר הדיירים הכולל בדירה כשהיא מלאה.
- missing_critical_data = true אם חסר מספר חדרים או רחוב/שכונה. מחיר חסר אינו קריטי
  (הרבה מודעות לא כותבות מחיר — זה בסדר, אל תסמן חוסר בגללו).
- summary_hebrew = משפט תקציר אחד.
- is_apartment_ad = true רק אם הפוסט *מציע* דירה/חדר/מקום בדירה קיימת להשכרה (כולל חיפוש
  שותף/ה לדירה קיימת שמושכרת). אחרת false. במפורש החזר false עבור:
  * דורש דיור — מי שמחפש/ת דירה או חדר לעצמו/ה להיכנס אליו ("מחפש/ת דירה", "רוצה להצטרף").
  * דירה או נכס *למכירה* (ולא להשכרה) — אנחנו רוצים השכרה בלבד.
  * מכירת רהיטים, שירותים, חיה אבודה, או כל פוסט שאינו השכרת דירה.
החזר JSON בלבד."""


def _extract_gemini(post_text: str) -> ListingExtract:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[_SYSTEM_HE, "\n\nהמודעה:\n" + post_text],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ListingExtract,      # guarantees a valid, parseable object
            temperature=0.0,
        ),
    )
    # google-genai returns a parsed pydantic instance on .parsed
    if getattr(resp, "parsed", None) is not None:
        return resp.parsed
    return ListingExtract.model_validate_json(resp.text)


# Local/OpenAI-compatible models don't get the guaranteed-schema treatment
# Gemini does, so spell the exact JSON keys out for them.
_SCHEMA_HINT = (
    "החזר אך ורק אובייקט JSON יחיד, ללא טקסט לפניו או אחריו, עם המפתחות האלה:\n"
    '{"is_apartment_ad": true/false, "price_per_room_ils": מספר או null, '
    '"available_rooms_count": מספר או null, "total_roommates_in_apt": מספר או null, '
    '"street_address_or_neighborhood": מחרוזת או null, "lease_start_date": מחרוזת או null, '
    '"contact_phone_or_link": מחרוזת או null, "missing_critical_data": true/false, '
    '"summary_hebrew": מחרוזת או null}'
)


def _extract_openai_compatible(post_text: str) -> ListingExtract:
    """For Ollama (http://localhost:11434/v1) or Groq — set LLM_BASE_URL,
    LLM_MODEL, and (if needed) LLM_API_KEY in your .env."""
    from openai import OpenAI

    client = OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("LLM_API_KEY", "ollama"),
    )
    kwargs = dict(
        model=os.environ.get("LLM_MODEL", "gemma2:9b"),
        messages=[
            {"role": "system", "content": _SYSTEM_HE + "\n\n" + _SCHEMA_HINT},
            {"role": "user", "content": "המודעה:\n" + post_text},
        ],
        temperature=0.0,
    )
    # Prefer SCHEMA-CONSTRAINED output: the runtime is forced to emit valid JSON
    # matching ListingExtract, which fixes the classic break where Hebrew
    # gershayim (e.g. מגדלי ח"ן, 1500 ש"ח) puts an unescaped " inside a string.
    # Fall back to plain json_object if the provider doesn't support schemas.
    try:
        resp = client.chat.completions.create(
            response_format={"type": "json_schema", "json_schema":
                             {"name": "ListingExtract",
                              "schema": ListingExtract.model_json_schema()}},
            **kwargs)
    except Exception:
        resp = client.chat.completions.create(
            response_format={"type": "json_object"}, **kwargs)
    raw = (resp.choices[0].message.content or "").strip()
    # Some local models wrap JSON in ``` fences or add a preamble — pull out the
    # object between the first "{" and the last "}".
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1]
    return ListingExtract.model_validate_json(raw)


def _run(provider: str, post_text: str) -> ListingExtract:
    if provider == "gemini":
        return _extract_gemini(post_text)
    return _extract_openai_compatible(post_text)


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc)
    return "RESOURCE_EXHAUSTED" in s or "429" in s or "quota" in s.lower()


# Set for the rest of the process once the primary provider hits its quota, so
# we don't re-hit (and pay the retry-backoff on) an exhausted primary each post.
# Fresh per run (each scheduled run is a new process, so it retries the primary).
_primary_exhausted = False
# How many extractions this run were served by the fallback — so the run summary
# can tell you whether (and how hard) you're leaning on the local model.
fallback_used = 0


def extract(post_text: str) -> ListingExtract:
    global _primary_exhausted, fallback_used
    primary = config.LLM_PROVIDER
    fallback = getattr(config, "LLM_FALLBACK_PROVIDER", None)

    if _primary_exhausted and fallback:
        fallback_used += 1
        return _run(fallback, post_text)
    try:
        return _run(primary, post_text)
    except Exception as exc:
        if fallback and fallback != primary and _is_quota_error(exc):
            _primary_exhausted = True
            fallback_used += 1
            print(f"[llm] {primary} quota reached — using {fallback} "
                  "for the rest of this run.")
            return _run(fallback, post_text)
        raise
