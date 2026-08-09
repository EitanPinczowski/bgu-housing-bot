---
name: llm-notes
description: >
  Reference notes on the Gemini/Ollama extraction layer: the pinned model ladder, per-
  model quota accounting, the 10:00 Israel reset, transient vs daily 429s, retries and
  budget. Load before editing llm.py, models.py, the extraction schema, or anything
  touching GEMINI_MODELS / LLM_DAILY_BUDGET.
---

# Llm Notes

How the LLM layer behaves, and why each part is the way it is. For the procedure for CHANGING the prompt or comparing models, use the `prompt-tuning` skill instead.

> Moved verbatim from `CLAUDE.md`. Do not reword in place — see the
> `write-a-note` skill.

- **LLM = Google Gemini free tier, as a PINNED MODEL LADDER** (`config.GEMINI_MODELS` =
  `gemini-3.1-flash-lite` then `gemini-3.5-flash-lite`), behind a small interface in `llm.py` so
  it can swap to an OpenAI-compatible endpoint (Ollama/Groq). Guaranteed
  structured output + a Hebrew prompt whose core rule is *return null, never
  guess*. On quota (429) or repeated errors it falls back to a local Ollama model
  for the rest of the run; a client-side min-interval paces Gemini under the RPM cap.
  - **THE QUOTA IS PER PROJECT PER MODEL, SO A SECOND MODEL DOUBLES THE DAY** (2026-08-06).
    Each rung has its own RPD 500, taking the ceiling to ~1,000 against ~700–870 demand.
    Only a **PerDay** exhaustion advances a rung; a per-minute 429 or a 503 is retried in
    place, because advancing on a blip would burn the reserve on a problem that clears
    itself in seconds. `llm.active_model()` is the rung in use; `_model_rung` is per
    process, so every run starts back at the best model.
  - **3.5 LEADS. A PROMOTION OF 3.1 ON n=48 WAS REVERTED AT n=100** (2026-08-07) — the
    clearest lesson of the whole exercise: **a 48-post A/B is not enough to reorder the
    models.** At n=48, 3.1 won every one of 5 price disagreements by dividing a total
    rent by the residents as the prompt asks. At n=100 they disagree 15 times (13 with
    3.5 self-consistent, so not noise) and **3.1 twice returns the WHOLE FLAT'S rent as
    the per-room price** — 2,800 where 3.5 said 1,400 for 2 roommates, 3,000 where 3.5
    said 1,000 for 3. n=48 had simply caught 3.1's good cases. Both gates failed.
    - **The failure modes cost differently.** 3.1's error INFLATES the price and the
      ≤2000 filter then drops the flat silently; 3.5's usual miss is null, which lands in
      NEEDS_DATA where a person still sees it. Prefer the visible failure.
    - 3.1 is not simply worse — 11 of the 15 are `3.5=null` against a real 3.1 number, so
      it finds prices 3.5 misses. **The honest fix is the PROMPT's division rule**, which
      neither model applies reliably. Until that is tightened, neither ordering is clearly
      right and the safer failure mode wins.
    - The two `is_apartment_ad` flips at n=100 are NOT evidence against 3.1: one is a
      flatmate-wanted post that the prompt says IS an ad (3.1 right, 3.5 wrong), the other
      is one of the 346 un-reparsed MERGED posts, so the input itself is polluted.
    - The address column (79% self vs 65% cross) still means little: `רחוב X` vs `X`,
      `שכונה ב` vs `שכונה ב׳`. Same place. Same artefact the batching work hit.
    - 3.1 is ~100% self-consistent on every field; 3.5 is 91% on rooms and 79% on address
      *against itself*. That noise floor is what retired an early scare from a 4-post
      smoke test where 3.5 appeared to "lose" room counts.
    - **The ladder is unaffected** — 3.1 stays the reserve rung, which is where the
      doubled daily capacity comes from. Only the ORDER changed back.
  - **PIN THE MODEL, NEVER `-latest`.** The alias moves, and a silent swap changes what is
    extracted from thousands of posts. (It was not the cause of the 08-06 outage — the
    per-model usage chart shows one model all week — but it is a standing hazard.)
  - **The budget is counted PER MODEL** (`llm.budget_state(model)`), or the first rung
    running out would stop the whole ladder. A legacy flat count is attributed to NO
    model: charging it to "the first rung" billed 429 calls to a model that had made
    ~100 and would have stopped it 375 calls early.
  - **A TRANSIENT 429/503 IS RETRIED, NOT LATCHED** (2026-08-05). There was no retry at
    all: any error matching `_is_quota_error` set `_primary_exhausted` for the whole
    process, so a per-minute blip and a daily exhaustion were the same thing and the
    blip cost an entire run — the 18:00 run that day ground at **~2 min/post** on Ollama,
    reaching group 1 of 15 in 90 minutes, while the allowance was intact (the counter
    went on past refusals at 252 and 389 to 501).
  - **The AI Studio usage dashboard is the sizing, and it is not close**: ~500–750
    requests/day at **~100% success**, with only **2–7 errors a day**, split between
    `429 TooManyRequests` and `503 ServiceUnavailable`. Under 1% of requests fail and
    each one was forfeiting a run. (Its axis reads UTC-8 — independent confirmation of
    the 10:00-Israel reset.)
  - **503 counts too.** `_is_quota_error` never matched it, so it took the
    consecutive-error path and spent a post on the local model at first sight, despite
    being about as common as 429.
  - **RETRYING IS THE DISCRIMINATOR.** Google often names no quota metric — both 08-05
    refusals came back `unknown` — so parsing PerDay/PerMinute cannot be relied on. A
    retry that succeeds proves the refusal was transient. The string test is used only
    to SKIP retries when the error explicitly says per-day, where waiting cannot help.
  - Google's own `retryDelay` is honoured when present, else 5/15/45 s backoff, every
    sleep capped by `GEMINI_RETRY_MAX_SLEEP_SEC` so one poisoned post can't park a run.
  - **`GEMINI_MIN_INTERVAL_SEC` IS 4.5** (13.3/min against a measured RPM limit of 15).
    It was 4.0 — exactly 15/min, zero headroom — and the Rate Limit page shows a peak of
    **17**, so we were over. Raising it was proposed, then dropped on the reasoning that
    429s were under 1% of requests so the cap "is not being saturated"; that was wrong,
    because the error rate was low only in that the DAILY ceiling bit first. **Measure a
    cap, don't infer it from how often it complains.**
  - The run summary prints retries and how many kept a post on Gemini — the old
    behaviour was only detectable by noticing the counter frozen while posts advanced.

- **THE GEMINI DAILY QUOTA RESETS AT 10:00 ISRAEL TIME**, not local midnight — it is
  midnight US Pacific. Measured 2026-08-03: the 08:00 run was `RESOURCE_EXHAUSTED`
  while the 11:09 run did 233 fresh posts on Gemini. **The 08:00 run therefore always
  spends the PREVIOUS day's leftovers**, which the previous evening's runs drained.
  - Anything counting calls must key on `dates.quota_window`, never `date.today()`: a
    midnight-reset counter hands the 08:00 run a budget it does not have and reports
    healthy right up to the failure it exists to prevent.
  - **The damage is lost runs, not slowness.** That morning the run fell through to
    Ollama at ~63 s/post, ground 186 posts, took **5h12m**, held the scraper lock, and
    the 10:00 and 12:00 runs both logged `SKIP another scraper session is running`.
    Three scheduled runs, one completion — and the locked-out 10:00 run is the one that
    would have had fresh quota. `LOCAL_FALLBACK_MAX_POSTS_PER_RUN` (40) ends a run
    before it can do that again; unread posts are never marked seen, so the next run
    takes them.
  - `LLM_DAILY_BUDGET` (900) stops us *before* Google does, taking the same code path
    as a real 429 so the run-cap fires next. `doctor`'s `llm budget` row shows it.
  - **Why calls grew**: 302 fresh posts/day on 07-30 → **1,184** on 08-02, mostly real
    post volume (August is peak season; per-run fresh went 51–93 → 233–347). The four
    pre-LLM gates already absorb ~27%, so the worst day was ~865 actual calls.
  - **A CHEAP PRE-LLM TEXT GATE IS A MEASURED DEAD END — do not retry** (2026-08-06,
    over all 6,939 archived posts). The idea is to save quota by skipping posts before
    they reach the LLM. Every candidate costs real listings:
    | rule | would skip | MATCH/NEEDS_DATA LOST |
    |---|---|---|
    | text < 40 chars | 192 (2.8%) | **35** |
    | text < 120 chars | 863 (12.4%) | **92** |
    | says `מחפש/ת דירה` (a wanted ad) | 761 (11.0%) | **19** |
    | no housing word at all | 267 (3.8%) | **47** |
    - **The cause is the OCR path, and it is structural.** 57–68% of the listings each
      gate would lose are IMAGE posts — the ad text is in the picture, so `raw_text` is
      short and keyword-free by definition, and the LLM reads the image. Any gate that
      judges a post by its TEXT throws those away. The rest are ordinary short posts that
      still resolve.
    - Not needed anyway: the model ladder took capacity to ~1,000/day against ~700–870
      demand, so the shortfall this was meant to close is gone.
  - **A local Ollama "is this an ad" triage is a MEASURED DEAD END — do not retry.**
    Timed on 12 real archived posts: `gemma2:9b` is 11/12 correct but **25.4 s median
    per post** (≈106 min added per run, to save the ~20% of calls that are NOT_AD);
    `gemma2:2b` is 6.6 s but **7/12 correct**, i.e. it discards real listings. Both
    trades are worse than the problem.
  - **Batching (`llm.extract_many`) is built and MEASURED TO HARM — it stays OFF**
    (`LLM_BATCH_SIZE = 1`). The free tier meters REQUESTS and posts are tiny (p50 316
    chars, p90 602, max 1,784), so 5 per request would have cut ~865 calls/day to ~175.
    It does not survive its accuracy gate (`python batch_ab.py 5 --batch 5`):
    | field | single vs single (noise floor) | batched 5 |
    |---|---|---|
    | `is_apartment_ad` | 100% | 100% |
    | `price_per_room_ils` | 100% | **80%** |
    | `available_rooms_count` | 100% | **70%** |
    | `street_address_or_neighborhood` | 85% | **70%** |
    Price and rooms agree PERFECTLY call-to-call, so their drop is batching, not model
    variance — and they are the fields the hard filters run on. 3 MATCH-eligible posts
    lost a price or a room count outright (one lost `price_per_room_ils=2800`). n=20, so
    the percentages are wide, but the losses are concrete.
    - **The control must be a single Gemini call in the SAME SESSION — NOT the archived
      `parsed_json`**, which two models (the Ollama fallback) and older prompts wrote;
      measured that way the address field "disagrees" 80% of the time and says nothing.
    - **Run the harness at `--batch N`, never at `config.LLM_BATCH_SIZE`.** It used to
      chunk by the config knob, which is 1 while batching is disabled — so it compared a
      single call against a single call and printed PASS on both gates without batching
      anything. A test that reads the switch it is gating can only agree with itself.
    - The accident was still useful: it is where the noise floor above comes from.
    - Retrying at 2 or 3 would trade a smaller saving for the same class of loss, and the
      quota pressure that motivated this is already handled by `LLM_DAILY_BUDGET` and
      `LOCAL_FALLBACK_MAX_POSTS_PER_RUN`. Don't re-enable without new evidence.

- `llm.py` — Gemini extraction + Ollama fallback (provider-abstracted); rate-limit;
  optional bounded OCR of image-only posts (one image, Gemini-only, capped per run).
