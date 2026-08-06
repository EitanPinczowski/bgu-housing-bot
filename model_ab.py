"""Does a DIFFERENT Gemini model change what we extract?

The free daily quota is per project per MODEL, so a second model carries its own
allowance and spilling over to it when the first is exhausted roughly doubles capacity
(measured 2026-08-06: `gemini-3.5-flash-lite` RPD 500, `gemini-3.1-flash-lite` RPD 500).
That is only worth having if the second model reads Hebrew housing posts as well as the
first — a model that loses prices costs more than a model that runs out early, which is
exactly how the batching question was decided.

GATE: no post may flip is_apartment_ad, and no MATCH-eligible post may lose its price,
rooms, or address — beyond what the control model loses against ITSELF.

RUN IT IN TWO PHASES, one per model. The quota is per model and the two windows are
rarely both full at once, so asking each model on its own schedule is what makes a large
sample affordable. The posts and the prompt are pinned by the sample file, so the phases
are still comparing the same thing:

    python model_ab.py sample  [n_per_bucket]      # pick posts once, write the sample
    python model_ab.py ask     gemini-3.1-flash-lite
    python model_ab.py ask     gemini-3.5-flash-lite
    python model_ab.py report  gemini-3.5-flash-lite gemini-3.1-flash-lite

Each `ask` runs the model TWICE over the sample: once for its answer and once for its
own noise floor. A candidate's disagreement only means something measured against how
much a model disagrees with itself.

Two lessons carried over from batch_ab.py, both learned the hard way:

  * THE CONTROL IS A LIVE CALL, NOT THE ARCHIVE. `posts.parsed_json` was written over
    days by two different models (186 posts on 2026-08-03 came from the Ollama fallback)
    and by older prompts, so comparing against it measures drift, not the model. Measured
    that way the address field "disagreed" 80% of the time and said nothing.
  * A RUN THAT FALLS BACK MEASURES OLLAMA. `llm.extract` would quietly serve a post from
    the local model on a quota error, so `ask` calls `_extract_gemini` directly and dies
    loudly instead — a silent fallback here would look like the candidate disagreeing.

DO NOT RUN THIS WHILE A SCRAPE IS RUNNING. `GEMINI_MIN_INTERVAL_SEC` paces requests per
PROCESS, not per project, and the RPM limit is per project — so two processes each pacing
at 4.5 s issue ~27/min against a limit of 15 and manufacture the very 429s this is meant
to avoid. Same applies to `replay.py --llm`.
"""
import collections
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, r"C:\Users\eitan\OneDrive\Desktop\bgu_housing_bot")
from dotenv import load_dotenv  # noqa: E402

# .env is loaded PER ENTRY POINT in this project; without it the GEMINI_API_KEY lookup
# raises and the harness silently measures nothing useful.
load_dotenv(r"C:\Users\eitan\OneDrive\Desktop\bgu_housing_bot\.env")

import config  # noqa: E402
import llm  # noqa: E402

OUT = pathlib.Path(config.DATA_DIR) / "model_ab"
BUCKETS = ("MATCH", "NEEDS_DATA", "DROP", "NOT_AD")
FIELDS = ("is_apartment_ad", "price_per_room_ils", "available_rooms_count",
          "total_roommates_in_apt", "street_address_or_neighborhood",
          "contact_phone_or_link", "floor", "furnished", "balcony_or_garden",
          "has_elevator", "price_from_comment")
# The four the hard filters actually run on — a wrong answer here changes a verdict.
CRITICAL = ("is_apartment_ad", "price_per_room_ils", "available_rooms_count",
            "street_address_or_neighborhood")


def _sample_path():
    return OUT / "sample.json"


def _answers_path(model):
    return OUT / f"answers-{model.replace('/', '_')}.json"


def cmd_sample(per_bucket: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    rows = []
    for b in BUCKETS:
        rows += con.execute(
            "SELECT sig, raw_text, comments, verdict FROM posts "
            "WHERE verdict=? AND parsed_json IS NOT NULL AND length(raw_text) > 60 "
            "ORDER BY RANDOM() LIMIT ?", (b, per_bucket)).fetchall()
    sample = [{"sig": r["sig"], "verdict": r["verdict"],
               "text": llm.with_comments(r["raw_text"], r["comments"] or "")}
              for r in rows]
    _sample_path().write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(sample)} posts to {_sample_path()}  "
          f"({collections.Counter(s['verdict'] for s in sample)})")
    print(f"each `ask` will cost {2 * len(sample)} calls on that model")


def cmd_ask(model: str) -> None:
    sample = json.loads(_sample_path().read_text(encoding="utf-8"))
    # PIN THE LADDER, not `config.GEMINI_MODEL`. `_extract_gemini` sends to
    # `llm.active_model()`, which reads GEMINI_MODELS[_model_rung] — so setting the old
    # single-model name would have left this harness silently measuring whichever model
    # the ladder happened to be on, i.e. reporting one model's answers under another's
    # name. A one-rung ladder also means a quota error cannot quietly hop to the other
    # model mid-run and mix the two into one column.
    real_models, real_single = config.GEMINI_MODELS, config.GEMINI_MODEL
    real_rung = llm._model_rung
    config.GEMINI_MODELS = [model]
    config.GEMINI_MODEL = model
    llm._model_rung = 0
    out = {"model": model, "runs": [[], []]}
    try:
        for pass_no in (0, 1):                    # answer, then the noise floor
            for i, s in enumerate(sample, 1):
                # _extract_gemini directly: llm.extract would serve a quota error from
                # Ollama and the harness would measure the local model instead.
                e = llm._extract_gemini(s["text"])
                out["runs"][pass_no].append({f: getattr(e, f, None) for f in FIELDS})
                if i % 10 == 0:
                    print(f"  {model} pass {pass_no + 1}: {i}/{len(sample)}", flush=True)
    finally:
        config.GEMINI_MODELS, config.GEMINI_MODEL = real_models, real_single
        llm._model_rung = real_rung
        _answers_path(model).write_text(json.dumps(out, ensure_ascii=False),
                                        encoding="utf-8")
    print(f"wrote {_answers_path(model)}")


def _agreement(a_runs, b_runs):
    agree, total = collections.Counter(), collections.Counter()
    for a, b in zip(a_runs, b_runs):
        for f in FIELDS:
            total[f] += 1
            agree[f] += (a.get(f) == b.get(f))
    return agree, total


def cmd_report(control_model: str, cand_model: str) -> None:
    sample = json.loads(_sample_path().read_text(encoding="utf-8"))
    ctl = json.loads(_answers_path(control_model).read_text(encoding="utf-8"))
    cnd = json.loads(_answers_path(cand_model).read_text(encoding="utf-8"))
    ctl_a, ctl_b = ctl["runs"]
    cnd_a, _cnd_b = cnd["runs"]

    noise_a, noise_t = _agreement(ctl_a, ctl_b)          # control vs ITSELF
    cand_a, cand_t = _agreement(ctl_a, cnd_a)            # control vs candidate
    self_a, self_t = _agreement(cnd_a, _cnd_b)           # candidate vs ITSELF

    print(f"CONTROL   {control_model}")
    print(f"CANDIDATE {cand_model}")
    print(f"n={len(sample)} posts  "
          f"({collections.Counter(s['verdict'] for s in sample)})\n")
    print(f"{'field':34}{'ctl vs ctl':>12}{'cand vs cand':>14}{'ctl vs cand':>13}")
    for f in FIELDS:
        nf = 100 * noise_a[f] / noise_t[f] if noise_t[f] else 0
        sf = 100 * self_a[f] / self_t[f] if self_t[f] else 0
        cf = 100 * cand_a[f] / cand_t[f] if cand_t[f] else 0
        # Flag only where the candidate is worse than the control's OWN variance —
        # that is the part attributable to the model rather than to noise.
        flag = "  <--" if cf < nf - 5 and f in CRITICAL else ""
        print(f"  {f:32}{nf:11.0f}%{sf:13.0f}%{cf:12.0f}%{flag}")

    flips, losses = [], []
    for s, o, n in zip(sample, ctl_a, cnd_a):
        if o.get("is_apartment_ad") != n.get("is_apartment_ad"):
            flips.append((s["verdict"], o.get("is_apartment_ad"),
                          n.get("is_apartment_ad"), s["text"][:60]))
        if s["verdict"] in ("MATCH", "NEEDS_DATA"):
            for f in ("price_per_room_ils", "available_rooms_count",
                      "street_address_or_neighborhood"):
                if o.get(f) is not None and n.get(f) is None:
                    losses.append((f, o.get(f), s["text"][:60]))
    # The same two counts for the control against itself — the honest baseline. A
    # candidate that loses two fields is only bad if the control loses none.
    ctl_losses = []
    for s, o, n in zip(sample, ctl_a, ctl_b):
        if s["verdict"] in ("MATCH", "NEEDS_DATA"):
            for f in ("price_per_room_ils", "available_rooms_count",
                      "street_address_or_neighborhood"):
                if o.get(f) is not None and n.get(f) is None:
                    ctl_losses.append(f)

    print(f"\nGATE 1 — is_apartment_ad flips : {len(flips)}  "
          f"{'PASS' if not flips else 'FAIL'}")
    for v, a, b, t in flips[:8]:
        print(f"    [{v}] {a} -> {b}   {t}")
    print(f"GATE 2 — MATCH-eligible losses : {len(losses)} "
          f"(control loses {len(ctl_losses)} against itself)  "
          f"{'PASS' if len(losses) <= len(ctl_losses) else 'FAIL'}")
    for f, was, t in losses[:8]:
        print(f"    lost {f}={was}   {t}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "sample":
        cmd_sample(int(sys.argv[2]) if len(sys.argv) > 2 else 10)
    elif cmd == "ask":
        cmd_ask(sys.argv[2])
    elif cmd == "report":
        cmd_report(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
