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
- missing_critical_data = true אם חסר מחיר, מספר חדרים, או רחוב/שכונה.
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


def _extract_openai_compatible(post_text: str) -> ListingExtract:
    """For Ollama (http://localhost:11434/v1) or Groq — set LLM_BASE_URL,
    LLM_MODEL, and (if needed) LLM_API_KEY in your .env."""
    from openai import OpenAI

    client = OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("LLM_API_KEY", "ollama"),
    )
    resp = client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "llama3.1"),
        messages=[
            {"role": "system", "content": _SYSTEM_HE},
            {"role": "user", "content": post_text},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    raw = resp.choices[0].message.content
    return ListingExtract.model_validate(json.loads(raw))


def extract(post_text: str) -> ListingExtract:
    if config.LLM_PROVIDER == "gemini":
        return _extract_gemini(post_text)
    return _extract_openai_compatible(post_text)
