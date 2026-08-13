"""
LLM extraction of a Hebrew housing post -> ListingExtract.

Default provider is Gemini's free tier because it is genuinely free and the
most reliable on colloquial Hebrew. It is wrapped so you can swap to any
OpenAI-compatible endpoint (a local Ollama model for full privacy, or Groq)
by changing LLM_PROVIDER in config.py — pipeline code never changes.
"""
from __future__ import annotations
import os
import re
import time

from pydantic import BaseModel

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
- street_address_or_neighborhood = הכתובת המדויקת ביותר של הדירה כפי שמופיעה במודעה.
  כלול תמיד את מספר הבית אם צוין (למשל "אברהם אבינו 38", לא רק "אברהם אבינו"). חלץ גם
  שמות אזורים מדוברים בבאר שבע, לא רק רחובות — למשל: "הבלוק", "וינגייט", "שכונה ב'/ג'/ד'".
  אם מוזכר אזור או שכונה כזה (גם בצורה מוטה כמו "בבלוק", "בשכונה ג'") — החזר את שם המקום,
  אל תחזיר null. אם מופיעים גם רחוב (עם מספר בית) וגם שם שכונה — החזר את שניהם יחד
  ("רחוב אברהם אבינו 38, שכונה ד'"), כדי לשמור גם על הדיוק וגם על ההקשר של השכונה.
  החזר null רק אם אין במודעה שום אזכור של רחוב/שכונה/אזור.
- missing_critical_data = true אם חסר מספר חדרים או רחוב/שכונה. מחיר חסר אינו קריטי
  (הרבה מודעות לא כותבות מחיר — זה בסדר, אל תסמן חוסר בגללו).
- floor = הקומה כפי שכתובה במודעה ("קרקע"/"3"/"3 מתוך 5"). אם לא מצוין — null.
- furnished = true אם הדירה מרוהטת — לכל חדר שינה יש לפחות מיטה, שולחן וארון (למשל
  "בכל חדר מיטה, ארון ושולחן"). false אם כתוב "לא מרוהט"/"ריקה"/"מרוהט חלקית". אם לא
  מצוין ריהוט כלל — null.
- balcony_or_garden = "מרפסת" אם מוזכרת מרפסת (כולל מרפסת שמש); אחרת "גינה" אם מוזכרת
  גינה או חצר; אם מוזכרות גם מרפסת וגם גינה — החזר "מרפסת". אם לא מוזכר כלל — null.
  החזר בדיוק אחת מהמילים "מרפסת" או "גינה" (לא שתיהן).
- has_elevator = true אם מוזכרת מעלית. false אם כתוב "אין מעלית"/"ללא מעלית"/"בלי
  מעלית". אם לא מוזכר כלל — null.
- summary_hebrew = משפט תקציר אחד.
- is_apartment_ad = true רק אם הפוסט *מציע* דירה/חדר/מקום בדירה קיימת להשכרה (כולל חיפוש
  שותף/ה לדירה קיימת שמושכרת). כל *סוג* של נכס מגורים להשכרה נחשב — גם "יחידת דיור",
  "דופלקס", "סטודיו", "דירת גן", "מרתף" או "סאבלט". מה שקובע הוא להשכרה מול מכירה/חיפוש,
  לא סוג הנכס. אחרת false. במפורש החזר false עבור:
  * דורש דיור — מי שמחפש/ת דירה או חדר לעצמו/ה להיכנס אליו ("מחפש/ת דירה", "רוצה להצטרף").
  * דירה או נכס *למכירה* (ולא להשכרה) — אנחנו רוצים השכרה בלבד.
  * מכירת רהיטים, שירותים, חיה אבודה, או כל פוסט שאינו השכרת דירה.
החזר JSON בלבד."""


def _image_part(url: str):
    """Fetch a post image and wrap it as a Gemini image Part (for OCR of a post
    that is a photo of its text). Raises on fetch failure so the caller can skip."""
    import requests
    from google.genai import types
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return types.Part.from_bytes(data=r.content, mime_type=mime)


def with_comments(post_text: str, comments: str | None) -> str:
    """Compose the text the model sees. Shared by `extract` and `extract_many` so a
    batched read can never see a different string from a single one — the prompt has
    a rule about the `[תגובות למודעה]` section, and two copies of this would drift."""
    if comments:
        return post_text + "\n\n[תגובות למודעה]:\n" + comments
    return post_text


def _pace_gemini() -> None:
    """Client-side min-interval so we stay under the free-tier requests-per-minute —
    and the ONE place a Gemini request is counted against the daily budget.

    COUNT WHERE THE REQUEST IS ISSUED, NOT IN THE WRAPPER. `extract()` used to do the
    counting, so anything calling `_extract_gemini` directly spent real quota invisibly:
    `batch_ab.py` does exactly that for its control, and burned ~44 uncounted requests on
    2026-08-04 — `doctor` read 286/900 while Google was already returning 429.

    It counts the ATTEMPT, before the call, not the success. A request that comes back
    500 still consumed one, and a counter that only tallies successes drifts low in
    exactly the situation where you need it to be right."""
    global _last_gemini_call
    gap = config.GEMINI_MIN_INTERVAL_SEC - (time.monotonic() - _last_gemini_call)
    if gap > 0:
        time.sleep(gap)
    _last_gemini_call = time.monotonic()
    _spend_budget()


def _extract_gemini(post_text: str, images=None) -> ListingExtract:
    from google import genai
    from google.genai import types

    _pace_gemini()

    contents = [_SYSTEM_HE, "\n\nהמודעה:\n" + post_text]
    if images:                       # OCR path — the ad text is in the picture
        contents.append("\n\nטקסט המודעה נמצא בתמונה המצורפת — קרא אותו ממנה:")
        for url in images[:1]:       # one image only, to bound tokens
            try:
                contents.append(_image_part(url))
            except Exception as exc:
                print(f"[llm] could not fetch OCR image: {exc}")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=active_model(),
        contents=contents,
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
    '"balcony_or_garden": "מרפסת"/"גינה"/null, "has_elevator": true/false/null, '
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


def _run(provider: str, post_text: str, images=None) -> ListingExtract:
    if provider == "gemini":
        return _extract_gemini(post_text, images)
    return _extract_openai_compatible(post_text)   # local fallback is text-only


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc)
    return "RESOURCE_EXHAUSTED" in s or "429" in s or "quota" in s.lower()


def _is_overloaded_error(exc: Exception) -> bool:
    """503 — Google's side, not ours, and it is NOT a quota error.

    It matters because it is about as common as 429 on the usage dashboard, and
    `_is_quota_error` does not match it — so it fell to the consecutive-error path and
    sent that post to the local model on the very first occurrence."""
    s = str(exc)
    return "503" in s or "UNAVAILABLE" in s or "overloaded" in s.lower()


def _retryable(exc: Exception) -> bool:
    """Worth asking Gemini again before touching the fallback.

    The AI Studio dashboard (2026-08-05) shows these two codes ARE the whole error
    population: ~500-750 requests/day at ~100% success with 2-7 errors, split between
    `429 TooManyRequests` and `503 ServiceUnavailable`. Both are transient by
    definition. A genuine per-day exhaustion is excluded — see `_quota_kind`."""
    if _quota_kind(str(exc)) == "day":
        return False                      # the allowance is gone; waiting cannot help
    return _is_quota_error(exc) or _is_overloaded_error(exc)


def _quota_kind(text: str) -> str:
    """'day' | 'minute' | 'unknown' from the provider's own error text.

    Shared by `_retryable` (skip pointless retries) and `quota_refusal_kind` (advise on
    the budget). Google does not always name the metric — both refusals on 2026-08-05
    came back 'unknown' — which is exactly why RETRYING is the real discriminator and
    this is only used to skip work when the answer is unambiguous."""
    s = text.lower().replace("_", "").replace(" ", "")
    if "perday" in s or "requestsperday" in s:
        return "day"
    if "perminute" in s:
        return "minute"
    return "unknown"


_RETRY_DELAY_RE = re.compile(r"retry(?:delay|\sin)[\"':\s]*([0-9]+(?:\.[0-9]+)?)\s*s",
                             re.IGNORECASE)


def _retry_sleep_for(exc: Exception, attempt: int) -> float:
    """How long to wait before retrying: Google's own number when it gives one.

    RESOURCE_EXHAUSTED bodies usually carry a `retryDelay` ("retry in 27.9s"), and
    honouring it is both politer and more likely to succeed than a guess. Falls back to
    exponential backoff, and is capped either way so one poisoned post cannot park the
    run."""
    cap = getattr(config, "GEMINI_RETRY_MAX_SLEEP_SEC", 30.0)
    m = _RETRY_DELAY_RE.search(str(exc))
    if m:
        try:
            return min(float(m.group(1)), cap)
        except ValueError:
            pass
    return min(5.0 * (3 ** attempt), cap)          # 5s, 15s, 45s -> capped


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
# How many image (OCR) extractions this run has spent, to cap token cost. Fresh
# per run (new process), like fallback_used.
ocr_used = 0
# Retries of a transient 429/503 this run, and how many of them worked. These are the
# evidence that the retry loop is doing its job: before it existed, the only way to
# tell a run had been exiled to the local model was noticing the Gemini counter frozen
# while posts kept advancing. `succeeded` is the number of posts that stayed on Gemini
# instead of costing ~2 min each on Ollama.
retries_attempted = 0
retries_succeeded = 0
# Which rung of the model ladder is in use. Fresh per run (each scheduled run is a new
# process), so every run starts back at the best model rather than inheriting an
# exhaustion that may have expired.
_model_rung = 0


def _ladder() -> list:
    """The models to try, best first. Falls back to the single-model setting."""
    return list(getattr(config, "GEMINI_MODELS", None) or [config.GEMINI_MODEL])


def active_model() -> str:
    """The model requests are being sent to right now."""
    lad = _ladder()
    return lad[min(_model_rung, len(lad) - 1)]


def _advance_model(why: str) -> bool:
    """Move to the next rung. True if there was one.

    ONLY a per-day exhaustion should get here. A per-minute 429 or a 503 is retried in
    place (see `extract`) — advancing on a blip would burn the reserve model on a
    problem that clears itself in seconds."""
    global _model_rung
    lad = _ladder()
    if _model_rung + 1 >= len(lad):
        return False
    _model_rung += 1
    print(f"[llm] {lad[_model_rung - 1]} {why} — switching to {lad[_model_rung]} "
          f"(its daily quota is separate)", flush=True)
    return True


def _short_err(exc: Exception) -> str:
    """A one-line label for a provider error, for logs that stay readable."""
    if _quota_kind(str(exc)) == "day":
        return "daily quota exhausted"
    if _is_overloaded_error(exc):
        return "503 overloaded"
    if _is_quota_error(exc):
        return "429 rate-limited"
    return str(exc)[:80]


# --- the daily budget, counted against the 10:00 quota window ----------------------
# Each scheduled run is a NEW PROCESS, so the count has to live on disk or every run
# would start from zero and the ceiling would never bind.
_BUDGET_PATH = config.DATA_DIR / "llm_budget.json"


def budget_state(model: str | None = None) -> tuple[str, int]:
    """(window, calls used) for the CURRENT quota window. A window that has rolled
    over reads as 0 — no cleanup job, the stale entry is simply not this window.

    COUNTED PER MODEL, because the quota is. One shared number would stop the whole
    ladder the moment its first rung ran out, which is the opposite of the point.
    `model=None` means the ladder's ACTIVE rung; the flat `calls` key is still summed
    so a budget file written before the ladder existed does not read as zero."""
    import dates
    window = dates.quota_window()
    model = model or active_model()
    try:
        import json
        d = json.loads(_BUDGET_PATH.read_text(encoding="utf-8"))
        if d.get("window") != window:
            return window, 0
        per = d.get("models") or {}
        # A LEGACY FLAT COUNT IS NOT ATTRIBUTED TO ANY MODEL. It was written before the
        # split, so which model spent it is genuinely unknown — and guessing "the first
        # rung" is actively wrong the moment the ladder is reordered: it charged 429
        # calls to the model that had made ~100 of them and would have stopped it 375
        # calls early. Under-protecting for the one window that straddles the upgrade is
        # the safe direction, because Google's own 429 is the real gate and the ladder
        # now handles it.
        return window, int(per.get(model, 0))
    except Exception:
        return window, 0


def _spend_budget(n: int = 1) -> None:
    """Record n primary-provider requests against the current window.

    CARRIES THE WHOLE REFUSAL RECORD THROUGH, not a named subset. Writing a bare
    {window, calls} erased the measured refusal point the moment the NEXT run in the
    same window made a call. Preserving only `refused_at` fixed that and then repeated
    it one field later: `refused_kind`, `refused_detail` and `stated_limit` were still
    dropped, so the diagnosis vanished and a later refusal "upgraded" over a record
    that had merely been blanked. Keep everything and overwrite only the count."""
    import json
    model = active_model()
    window, used = budget_state(model)
    try:
        rec = json.loads(_BUDGET_PATH.read_text(encoding="utf-8"))
        if rec.get("window") != window:
            # A ROLLED-OVER WINDOW IS ARCHIVED, NOT DISCARDED. It used to be dropped on
            # the floor, so the file could only ever answer "today" — and the measured
            # refusal point, the one piece of evidence for where Google's real cap sits,
            # went with it. The WHOLE record is kept for the same reason the docstring
            # above gives: a named subset loses the diagnosis.
            prev, rec = rec, {}
            hist = prev.pop("history", []) if isinstance(prev, dict) else []
            if prev.get("window"):
                hist.append(prev)
            keep = getattr(config, "LLM_BUDGET_HISTORY_WINDOWS", 14)
            rec["history"] = hist[-keep:] if keep else []
    except Exception:
        rec = {}
    rec["window"] = window
    rec.setdefault("models", {})[model] = used + n
    # `calls` stays as the window TOTAL across models — it is what `doctor` and the run
    # summary have always shown, and it is still the right headline number.
    rec["calls"] = sum(int(v) for v in rec["models"].values())
    try:
        _BUDGET_PATH.write_text(json.dumps(rec), encoding="utf-8")
    except Exception as exc:                      # a counter must never break a run
        print(f"[llm] could not record budget: {exc}")


def budget_history() -> list:
    """Past quota windows, oldest first — [{window, calls, models, …}, …].

    Empty until a window has actually rolled over; retention starts from the first
    roll-over after this landed, so it fills in over days rather than retroactively."""
    import json
    try:
        rec = json.loads(_BUDGET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    hist = list(rec.get("history") or [])
    if rec.get("window"):                      # the live window belongs at the end
        hist.append({k: v for k, v in rec.items() if k != "history"})
    return hist


def budget_spent() -> bool:
    """Have we used this window's self-imposed allowance?"""
    cap = getattr(config, "LLM_DAILY_BUDGET", 0)
    return bool(cap) and budget_state()[1] >= cap


def record_quota_refusal(detail: str = "") -> None:
    """Remember the counted call number at which GOOGLE ACTUALLY REFUSED, once per window.

    `LLM_DAILY_BUDGET` (900) is a guess, and the only thing that can replace it with a
    measurement is the count standing when the first real 429 arrives. That number was
    previously observable for a few seconds inside one run's stdout — you had to be
    watching `doctor`'s budget row at the moment it happened, on a machine that scrapes
    unattended. So it records itself, and `doctor` reports it afterwards.

    ONLY a real refusal from the provider. Our own ceiling tripping is not evidence about
    where theirs is — recording that would just play back the guess as if it were a
    measurement. First writer per window wins: the interesting number is where the
    refusals START, not where the last one landed.

    `detail` is the provider's own error text, kept because A DAILY REFUSAL AND A
    PER-MINUTE ONE ARE THE SAME 429 HERE: `_is_quota_error` matches any of
    RESOURCE_EXHAUSTED / 429 / "quota", collapses it to a boolean, and the text was
    thrown away — so the first refusal this recorded (252, 2026-08-05) cannot be
    diagnosed at all. Google names the metric in the body (…PerDay vs …PerMinute), which
    is the whole difference between "the real ceiling is 250" and "one burst was too
    fast". Only a PerDay refusal may be used to set `LLM_DAILY_BUDGET`."""
    import json
    window, used = budget_state()
    try:
        d = json.loads(_BUDGET_PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    # First writer per window wins — the interesting number is WHERE THE REFUSALS
    # STARTED, and a later one is noise. The single exception is an upgrade: if the
    # stored refusal named no quota and this one does, it is strictly better evidence.
    # Holding the first `unknown` is how "limit: 500" was thrown away and had to be
    # recovered by hand.
    have = d.get("window") == window and d.get("refused_at") is not None
    # The kind is computed from the FULL error and STORED. Re-deriving it from the
    # truncated copy silently loses it: `quotaId: …PerDay…` sits near the end of
    # Google's body, past the 300-character cut, while `limit: 500` sits early — so a
    # genuine PerDay refusal read back as `unknown` and a later one "upgraded" over it.
    kind = _quota_kind(str(detail)) if detail else "unknown"
    upgrade = have and _stored_kind(d) == "unknown" and kind != "unknown"
    if have and not upgrade:
        return
    # An upgrade keeps the ORIGINAL count — only the diagnosis improves, not the point
    # at which Google started refusing.
    at = d.get("refused_at") if upgrade else used
    # MUTATE THE EXISTING RECORD; DO NOT REBUILD IT. Constructing a fresh dict here is
    # the third time this file has silently dropped a sibling field — first `refused_at`,
    # then the diagnosis, and now the per-model `models` counts, which reset the whole
    # ladder's budget to zero the moment a refusal was recorded.
    rec = d if d.get("window") == window else {}
    rec["window"] = window
    rec["refused_at"] = at
    rec["refused_kind"] = kind
    rec.setdefault("calls", used)
    if detail:
        rec["refused_detail"] = str(detail)[:500]
        lim = _stated_limit(detail)
        if lim:
            rec["stated_limit"] = lim
    try:
        _BUDGET_PATH.write_text(json.dumps(rec), encoding="utf-8")
        print(f"[llm] provider refused at {used} counted calls this window — "
              f"`doctor` will report it; set LLM_DAILY_BUDGET just under it")
    except Exception as exc:                      # a counter must never break a run
        print(f"[llm] could not record the refusal point: {exc}")


def quota_refusal() -> int | None:
    """The counted call number at which the provider refused in THIS window, if it has."""
    return (_refusal_record() or {}).get("refused_at")


def _refusal_record() -> dict | None:
    import json
    import dates
    try:
        d = json.loads(_BUDGET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return d if d.get("window") == dates.quota_window() and d.get("refused_at") is not None else None


_LIMIT_RE = re.compile(r"(?:\blimit:\s*|\"?quotaValue\"?:\s*['\"])(\d{2,7})")


def _stated_limit(text: str):
    """The daily cap Google NAMES in its own refusal, or None.

    `LLM_DAILY_BUDGET` was 900 against a real limit of 500, because it had been set from
    a usage chart — which shows where you have been, never where the cap is. The refusal
    says it plainly (`limit: 500`, `quotaValue: '500'`), so read it and stop guessing."""
    m = _LIMIT_RE.search(str(text))
    return int(m.group(1)) if m else None


def stated_quota_limit():
    """Google's own stated daily limit from this window's refusal, if it gave one."""
    return (_refusal_record() or {}).get("stated_limit")


def quota_refusal_kind() -> str:
    """'day' | 'minute' | 'unknown' — which ceiling the provider said we hit.

    Only 'day' may be used to set `LLM_DAILY_BUDGET`: a per-minute refusal says the burst
    was too fast, not that the allowance is gone, and treating it as the daily ceiling
    would cut the budget to a fraction of the real one."""
    rec = _refusal_record()
    return _stored_kind(rec) if rec else "unknown"


def _stored_kind(rec: dict) -> str:
    """The kind as RECORDED. Falls back to re-parsing the stored detail for records
    written before the kind was stored explicitly."""
    return rec.get("refused_kind") or _quota_kind(str(rec.get("refused_detail", "")))


def _set_primary_exhausted(why: str) -> None:
    """Latch the primary off for the rest of this run. Shared by the single-post and
    batched paths so a quota error found by a batch behaves exactly like one found by
    a single call — otherwise the next batch pays Gemini's slow retry-backoff again."""
    global _primary_exhausted
    _primary_exhausted = True
    fb = getattr(config, "LLM_FALLBACK_PROVIDER", None)
    print(f"[llm] {config.LLM_PROVIDER} {why} — using {fb} for the rest of this run.")


def fallback_budget_spent() -> bool:
    """Has this run served as many posts locally as it is allowed to?

    A QUESTION, NOT AN EXCEPTION. `extract` must keep answering for whoever asks —
    `manual.py` and `replay.py --use-llm` are interactive, have no scraper lock to
    hold and no next run to protect, so raising from in here would break them to
    solve a problem they don't have. The scraper loop is the only caller that has a
    reason to stop, so it is the one that asks. See LOCAL_FALLBACK_MAX_POSTS_PER_RUN
    for what a run that ignored this cost on 2026-08-03."""
    cap = getattr(config, "LOCAL_FALLBACK_MAX_POSTS_PER_RUN", 0)
    return bool(cap) and fallback_used >= cap


def extract(post_text: str, comments: str | None = None, images=None) -> ListingExtract:
    global _primary_exhausted, fallback_used, _consecutive_errors, ocr_used
    global retries_attempted, retries_succeeded
    post_text = with_comments(post_text, comments)
    primary = config.LLM_PROVIDER
    fallback = getattr(config, "LLM_FALLBACK_PROVIDER", None)

    # OCR only on the PRIMARY (Gemini) path, one image, hard-capped per run so the
    # free-tier quota can't be blown. The local fallback stays text-only.
    use_img = None
    if images and ocr_used < getattr(config, "SCRAPER_MAX_OCR_PER_RUN", 0):
        use_img = images[:1]
        ocr_used += 1

    # Our own ceiling, checked BEFORE Google's. Stopping a little early is what keeps
    # the local fallback available for genuine surprises instead of spending it on a
    # quota we could see coming.
    #
    # IT MUST CLIMB THE LADDER, NOT JUMP STRAIGHT TO OLLAMA. The budget is per model, so
    # the first rung filling up says nothing about the second — and this latched anyway:
    # caught live on 2026-08-06, `daily budget of 480 spent — using openai_compatible`
    # while the reserve model sat at 0 of its own 500. Our own gate was defeating the
    # ladder it was supposed to feed.
    while not _primary_exhausted and budget_spent():
        if not _advance_model(f"client-side budget of {config.LLM_DAILY_BUDGET} spent"):
            _set_primary_exhausted(
                f"daily budget of {config.LLM_DAILY_BUDGET} spent on every model")
            break

    if _primary_exhausted and fallback:
        fallback_used += 1
        return _run(fallback, post_text)          # text-only
    try:
        result = _run(primary, post_text, use_img)   # counted inside _pace_gemini
        _consecutive_errors = 0
        return result
    except Exception as exc:
        if not (fallback and fallback != primary):
            raise                               # nothing to fall back to
        # ASK AGAIN BEFORE GIVING UP ON THE PRIMARY. A 429 or 503 used to latch Gemini
        # off for the whole process, so one blip cost an entire run to a ~2 min/post
        # local model — while the daily allowance was demonstrably intact (the counter
        # sailed past both of 2026-08-05's refusals). Retrying is also the only
        # DISCRIMINATOR that cannot be fooled: Google often names no quota metric, so a
        # retry that succeeds proves the refusal was transient where string-matching
        # could not.
        if _retryable(exc):
            for attempt in range(getattr(config, "GEMINI_RETRIES", 3)):
                delay = _retry_sleep_for(exc, attempt)
                retries_attempted += 1
                print(f"[llm] {primary} {_short_err(exc)} — retrying in {delay:.0f}s "
                      f"({attempt + 1}/{config.GEMINI_RETRIES})")
                time.sleep(delay)
                try:
                    result = _run(primary, post_text, use_img)
                    _consecutive_errors = 0
                    retries_succeeded += 1
                    return result
                except Exception as exc2:       # noqa: PERF203 - a retry loop
                    exc = exc2
                    if not _retryable(exc):
                        break
        # A PER-DAY EXHAUSTION IS THE ONE THING THE LADDER IS FOR. The quota is per
        # model, so the next rung has its own untouched allowance — trying it beats
        # spending the rest of the run on a ~2 min/post local model. Only reached after
        # the retries above, so a per-minute blip can never burn the reserve.
        if _quota_kind(str(exc)) == "day" and _advance_model("daily quota exhausted"):
            record_quota_refusal(str(exc))
            try:
                result = _run(primary, post_text, use_img)
                _consecutive_errors = 0
                return result
            except Exception as exc2:
                exc = exc2               # the next rung failed too — fall through
        fallback_used += 1
        if _is_quota_error(exc):
            _primary_exhausted = True
            record_quota_refusal(str(exc))   # their ceiling, and WHICH ceiling
            # SAY WHICH CEILING AND WHAT GOOGLE CALLED IT. "quota reached" alone left
            # today's run indistinguishable from a rate-limit blip, and answering "why
            # is it on Ollama when the dashboard says we had quota?" took a hand-made
            # API call to find `limit: 500`.
            lim = _stated_limit(str(exc))
            print(f"[llm] {primary} quota reached "
                  f"({_short_err(exc)}"
                  + (f", Google states limit {lim}/day" if lim else "")
                  + f") — using {fallback} for the rest of this run.")
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


# --- batched extraction ----------------------------------------------------------
# The free tier meters REQUESTS PER DAY, not tokens, and these posts are tiny —
# measured over the 4,935-post archive: p50 316 chars, p90 602, max 1,784. Five in
# one request is ~3 KB, nowhere near a context limit, and costs ONE request instead
# of five. That is the whole reason this exists: on 2026-08-02 the bot made ~865
# calls against a ~1,000/day ceiling, and batching turns that into ~175.

class _IndexedExtract(BaseModel):
    """One post's extract, tagged with WHICH post it belongs to.

    The index is not decoration. Without it a model that returns four objects for
    five posts, or reorders them, would silently shift every listing onto the wrong
    post — wrong phone, wrong address, wrong flat — and nothing downstream could
    detect it. With it, any mismatch is caught and the batch is redone one by one."""
    index: int
    listing: ListingExtract


def _validate_batch(items, n: int) -> list[ListingExtract]:
    """Every post answered EXACTLY ONCE, or we trust none of it.

    Raises rather than repairing. A batch that answered 4 objects for 5 posts, or
    repeated an index, has told us it lost track of which post is which — and a
    silently mis-attributed listing (right flat, wrong phone and address) is the one
    failure nothing downstream can detect. Redoing the batch one by one costs a few
    requests; getting it wrong costs a wrong alert, permanently."""
    by_index = {it.index: it.listing for it in items}
    if len(items) != n or set(by_index) != set(range(n)):
        raise ValueError(f"batch answered indices {sorted(by_index)} for {n} posts")
    return [by_index[i] for i in range(n)]


def _extract_gemini_many(texts: list[str]) -> list[ListingExtract]:
    """One Gemini request for N posts. Raises if the answer doesn't line up."""
    from google import genai
    from google.genai import types

    _pace_gemini()
    numbered = "\n\n".join(f"### פוסט {i}\n{t}" for i, t in enumerate(texts))
    instruction = (
        f"\n\nלפניך {len(texts)} פוסטים נפרדים, כל אחד מסומן ב'### פוסט N'.\n"
        "נתח כל פוסט בנפרד לפי הכללים למעלה והחזר מערך JSON עם בדיוק "
        f"{len(texts)} איברים, אחד לכל פוסט.\n"
        "בכל איבר: index = מספר הפוסט כפי שמסומן, listing = תוצאת הניתוח שלו.\n"
        "אל תערבב מידע בין פוסטים — כל פוסט עומד בפני עצמו."
    )
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=active_model(),
        contents=[_SYSTEM_HE, instruction, "\n\nהפוסטים:\n" + numbered],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[_IndexedExtract],
            temperature=0.0,
        ),
    )
    items = getattr(resp, "parsed", None)
    if items is None:
        import json
        items = [_IndexedExtract.model_validate(o) for o in json.loads(resp.text)]

    return _validate_batch(items, len(texts))


def extract_many(posts: list[tuple[str, str | None]]) -> list[ListingExtract]:
    """Extract N posts, ideally in one Gemini request. `posts` is (text, comments).

    ALWAYS RETURNS ONE RESULT PER POST, IN ORDER. Any doubt about the batch — a short
    answer, a duplicate or missing index, a validation error, any exception — and the
    whole batch is redone through the ordinary per-post `extract()`, which keeps its
    own fallback ladder. Quota is spent twice only on that failure path, which is the
    right trade: a mis-attributed listing is silent and permanent, a retry is neither.

    Gemini only. The local model keeps the single-post path — array-shaped structured
    output is where small local models are least reliable, and there is no quota
    reason to batch a provider that has no quota.
    """
    global fallback_used

    def one_by_one(why: str | None = None) -> list[ListingExtract]:
        if why:
            print(f"[llm] batch of {len(posts)} fell back to single calls: {why}")
        return [extract(t, comments=c) for t, c in posts]

    if not posts:
        return []
    # Nothing to batch, or nothing to batch WITH: a lone post is already one request,
    # and once Gemini is exhausted `extract` routes to the local model, which does not
    # batch. Both go straight down the ordinary path — no wasted request, no new code
    # path for the case that already worked.
    if len(posts) == 1 or _primary_exhausted or config.LLM_PROVIDER != "gemini":
        return one_by_one()

    texts = [with_comments(t, c) for t, c in posts]
    try:
        return _extract_gemini_many(texts)   # ONE request, counted in _pace_gemini
    except Exception as exc:
        # A quota error must LATCH exactly as it does for a single post, or the next
        # batch pays Gemini's slow retry-backoff all over again — but ONLY once it is
        # not merely transient. `one_by_one` re-runs each post through `extract`, which
        # carries the retry loop, so a 429/503 here still gets its retries; latching
        # first would skip them and hand the whole batch to the local model.
        if _is_quota_error(exc) and not _retryable(exc):
            record_quota_refusal(str(exc))
            _set_primary_exhausted(f"quota reached on a batch of {len(posts)}")
        return one_by_one(str(exc)[:120])
