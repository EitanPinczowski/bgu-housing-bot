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
import time

import config
from models import ListingExtract

_last_gemini_call = 0.0   # monotonic time of the last Gemini call (rate limiting)

# Hebrew instruction prompt. The null-not-guess rule is the single most
# important line here: a hallucinated "1800" would sail through the price gate.
_SYSTEM_HE = """אתה מנתח מודעות שכירות של דירות שותפים בבאר שבע, מקבוצות פייסבוק בעברית מדוברת.
חלץ את השדות לפי הסכימה. כללים מחייבים:
- אם שדה כלשהו אינו מופיע במפורש במודעה — החזר null. אסור לנחש או להמציא מספרים.
- price_per_room_ils = העלות החודשית לשותף אחד (חדר אחד), ללא חשבונות (ארנונה/ועד/מים).
  אם מצוין רק שכר הדירה הכולל, חלק במספר הדיירים הכולל בדירה. אם אין מספיק מידע — null.
- ייתכן שבסוף יופיע חלק "[תגובות למודעה]". אם המחיר אינו בגוף המודעה אך מופיע בתגובות —
  קח אותו משם וסמן price_from_comment=true. אחרת price_from_comment=false. אל תמציא מחיר.
- available_rooms_count = מספר החדרים הפנויים כרגע להשכרה. אם הפוסט מחפש שותפים לדירה,
  זהו מספר השותפים המבוקשים (מחפשים "שותף/ה" ביחיד = 1; "שני שותפים" = 2).
- total_roommates_in_apt = מספר הדיירים הכולל בדירה כשהיא מלאה.
- street_address_or_neighborhood = הרחוב, השכונה או האזור שבו נמצאת הדירה, כפי שמופיע
  במודעה. חלץ גם שמות אזורים מקומיים ומדוברים בבאר שבע, לא רק רחובות — למשל: "הבלוק",
  "הרובע", "העיר העתיקה", "רסקו", "וינגייט", "נאות לון", "שכונה ג'/ד'/ה'/ו'/ט'". אם מוזכר
  אזור או שכונה כזה (גם בצורה מוטה כמו "בבלוק", "בשכונה ג'") — החזר את שם המקום, אל תחזיר
  null. החזר null רק אם אין במודעה שום אזכור של רחוב/שכונה/אזור.
- missing_critical_data = true אם חסר מספר חדרים או רחוב/שכונה. מחיר חסר אינו קריטי
  (הרבה מודעות לא כותבות מחיר — זה בסדר, אל תסמן חוסר בגללו).
- floor = הקומה כפי שכתובה במודעה ("קרקע"/"3"/"3 מתוך 5"). אם לא מצוין — null.
- furnished = true אם הדירה מרוהטת — לכל חדר שינה יש לפחות מיטה, שולחן וארון (למשל
  "בכל חדר מיטה, ארון ושולחן"). false אם כתוב "לא מרוהט"/"ריקה"/"מרוהט חלקית". אם לא
  מצוין ריהוט כלל — null.
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

    global _last_gemini_call
    gap = config.GEMINI_MIN_INTERVAL_SEC - (time.monotonic() - _last_gemini_call)
    if gap > 0:                      # stay under the free-tier requests-per-minute
        time.sleep(gap)
    _last_gemini_call = time.monotonic()

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
    '"floor": מחרוזת או null, "furnished": true/false/null, '
    '"contact_phone_or_link": מחרוזת או null, "missing_critical_data": true/false, '
    '"price_from_comment": true/false, "summary_hebrew": מחרוזת או null}'
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
# Consecutive non-quota primary errors; after LLM_MAX_CONSECUTIVE_ERRORS we give
# up on the primary for the rest of the run (like quota). Reset on any success.
_consecutive_errors = 0


def extract(post_text: str, comments: str | None = None) -> ListingExtract:
    global _primary_exhausted, fallback_used, _consecutive_errors
    if comments:
        post_text = post_text + "\n\n[תגובות למודעה]:\n" + comments
    primary = config.LLM_PROVIDER
    fallback = getattr(config, "LLM_FALLBACK_PROVIDER", None)

    if _primary_exhausted and fallback:
        fallback_used += 1
        return _run(fallback, post_text)
    try:
        result = _run(primary, post_text)
        _consecutive_errors = 0
        return result
    except Exception as exc:
        if not (fallback and fallback != primary):
            raise                               # nothing to fall back to
        fallback_used += 1
        if _is_quota_error(exc):
            _primary_exhausted = True
            print(f"[llm] {primary} quota reached — using {fallback} "
                  "for the rest of this run.")
        else:
            # Transient error: serve THIS post from the fallback so it isn't lost,
            # and only abandon the primary after enough consecutive failures.
            _consecutive_errors += 1
            if _consecutive_errors >= config.LLM_MAX_CONSECUTIVE_ERRORS:
                _primary_exhausted = True
                print(f"[llm] {primary} failed {_consecutive_errors}x — using "
                      f"{fallback} for the rest of this run.")
            else:
                print(f"[llm] {primary} error, using {fallback} for this post: {exc}")
        return _run(fallback, post_text)
