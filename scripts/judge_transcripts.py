"""Estimate transcription accuracy per Whisper model without ground-truth audio.

There is no reference text for past dictations, so an LLM reads each RAW Whisper
transcript and flags words that are probably misheard (wrong names, words that do
not fit, garbled phrases, truncation, hallucinated filler). This measures
plausibility, not truth: a mishearing that still reads naturally goes uncounted.

Usage:
  venv/bin/python scripts/judge_transcripts.py \
      --era medium=2026-10-01..2026-11-11T10:50 --era distil=2026-11-11T11:00.. \
      [--limit 200] [--out /tmp/judged.jsonl]
"""
import argparse
import json
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import boto3  # noqa: E402
from config import Config  # noqa: E402

JUDGE_PROMPT = """You audit speech-to-text output. The text below is a RAW Whisper transcript of \
one person's casual spoken journal (day recaps with friends, food, NYC/Seattle places). Slang \
and casual grammar are NOT errors (lowkey, hella, gonna, like, yapping, grinded, bro, -ish).

Known spellings of people/places/words the speaker uses: {names}.

Flag only words or phrases that were probably MISHEARD by the speech recognizer: a word that \
does not make sense in context but sounds like one that would, a misspelled known name, a \
garbled phrase, text that stops mid-thought or is unrelated to the rest, or invented filler \
like "Thank you." Do not flag style, grammar, or punctuation.

Reply with ONLY JSON: {{"errors": [{{"heard": "...", "likely": "...", "severity": "minor|major"}}], \
"verdict": "clean|minor|major"}}. "major" means the meaning of a sentence is lost or wrong.

Transcript:
<t>{text}</t>"""


def parse_era(spec):
    name, _, span = spec.partition("=")
    start, _, end = span.partition("..")
    return name, start, end or "9999"


def judge(client, model_id, names, text):
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 600,
        "temperature": 0,
        "messages": [
            {"role": "user", "content": JUDGE_PROMPT.format(names=names, text=text)},
            {"role": "assistant", "content": "{"},
        ],
    }
    resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
    raw = "{" + json.loads(resp["body"].read())["content"][0]["text"]
    match = re.search(r"\{.*\}", raw, re.S)
    if match is None:
        raise ValueError(f"judge returned no JSON: {raw[:120]}")
    return json.loads(match.group(0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--era", action="append", required=True, help="name=start..end (ISO prefixes, end optional)")
    ap.add_argument("--limit", type=int, default=0, help="random sample per era (0 = all)")
    ap.add_argument("--out", default="/tmp/judged.jsonl")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    cfg = Config()
    names = ", ".join(
        [w["correct"] for w in cfg.get_known_words()] + list(cfg.get("prompt_names") or [])
    )
    client = boto3.Session(
        profile_name=cfg.get("aws_profile"), region_name=cfg.get("aws_region")
    ).client("bedrock-runtime")
    model_id = cfg.get("bedrock_model_id")

    rows = [json.loads(l) for l in open(os.path.join(ROOT, "logs", "transcripts.jsonl")) if l.strip()]
    dictations = [r for r in rows if r.get("event") == "dictation" and r.get("raw", "").strip()]

    random.seed(0)
    results = []
    for spec in args.era:
        name, start, end = parse_era(spec)
        sample = [r for r in dictations if start <= r["ts"] < end]
        if args.limit and len(sample) > args.limit:
            sample = random.sample(sample, args.limit)

        def run(r):
            try:
                v = judge(client, model_id, names, r["raw"])
            except Exception as e:  # noqa: BLE001 - recorded, not fatal
                v = {"verdict": "judge_error", "errors": [], "error": str(e)[:200]}
            return {"era": name, "ts": r["ts"], "words": len(r["raw"].split()),
                    "captured_s": r.get("captured_s"), "raw": r["raw"], **v}

        with ThreadPoolExecutor(args.workers) as pool:
            results.extend(pool.map(run, sample))

    with open(args.out, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    for spec in args.era:
        name = parse_era(spec)[0]
        R = [r for r in results if r["era"] == name and r["verdict"] != "judge_error"]
        if not R:
            continue
        words = sum(r["words"] for r in R)
        errs = sum(len(r["errors"]) for r in R)
        major = sum(1 for r in R for e in r["errors"] if e.get("severity") == "major")
        pct = lambda v: 100 * sum(r["verdict"] == v for r in R) / len(R)
        print(f"{name}: n={len(R)} clean {pct('clean'):.0f}% minor {pct('minor'):.0f}% "
              f"major {pct('major'):.0f}% | flagged errors per 1000 words {1000 * errs / words:.1f} "
              f"(major {1000 * major / words:.1f})")
    failed = sum(r["verdict"] == "judge_error" for r in results)
    if failed:
        print(f"judge errors: {failed}")
    print(f"details: {args.out}")


if __name__ == "__main__":
    main()
