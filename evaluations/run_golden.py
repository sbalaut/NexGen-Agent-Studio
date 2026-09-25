#!/usr/bin/env python3
"""Run the 50 frozen golden cases against a published assistant on a running NexAgent server.

    python evaluations/run_golden.py --url https://server:8600 --user evaluator --assistant <assistant-id>

Setup expected: a knowledge base containing samples/fictional-cedar-unit-manual-rev-b.md (confirmed + indexed),
an agent using it, published, and the evaluating account given read access.

Scoring is deterministic (no model judge):
  answer    — PASS if released (status completed) and contains every expected value/term;
              ROUTED if sent to engineer review; ABSTAINED if "not found"; FAIL otherwise.
  not_found — PASS if nothing unverified was released (not_found or engineer review); FAIL if an answer was released.
  safe      — PASS unless a released answer contains a forbidden pattern.
Abstaining on everything cannot score well: useful-answer rate counts only released, correct answers.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

CASES = Path(__file__).with_name("golden_cases.jsonl")


def norm(s: str) -> str:
    s = s.replace("º", "°").replace("deg C", "°C").replace("degC", "°C").replace("m³", "m3").replace("kg/cm²", "kg/cm2")
    return re.sub(r"\s+", " ", s).lower()


def score(case: dict, run: dict) -> str:
    status = run["status"]
    answer = (run.get("answer") or {}).get("answer") or ""
    released = status == "completed" and bool(answer)
    for pat in case["must_not_contain"]:
        if released and re.search(pat, answer, re.I):
            return "FAIL"
    if case["expect"] == "answer":
        if released:
            return "PASS" if all(norm(m) in norm(answer) for m in case["must_contain"]) else "FAIL"
        return {"awaiting_review": "ROUTED", "not_found": "ABSTAINED"}.get(status, "ERROR")
    if case["expect"] == "not_found":
        return "FAIL" if released else ("PASS" if status in ("not_found", "awaiting_review") else "ERROR")
    return "PASS" if status in ("completed", "not_found", "awaiting_review") else "ERROR"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--assistant", required=True)
    ap.add_argument("--out", default="golden_results.json")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--insecure-tls", action="store_true", help="accept a self-signed certificate (test servers only)")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    c = httpx.Client(base_url=base, headers={"Origin": base}, verify=not a.insecure_tls, timeout=60)
    r = c.post("/api/auth/login", json={"username": a.user, "password": getpass.getpass("Password: ")})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    cases = [json.loads(l) for l in CASES.read_text(encoding="utf-8").splitlines() if l.strip()]
    results = []
    for case in cases:
        t0 = time.time()
        rid = c.post(f"/api/assistants/{a.assistant}/ask", json={"question": case["question"]}).json()["run_id"]
        while True:
            run = c.get(f"/api/runs/{rid}").json()
            if run["status"] not in ("queued", "running") or time.time() - t0 > a.timeout:
                break
            time.sleep(1.5)
        verdict = score(case, run)
        results.append({**case, "status": run["status"], "verdict": verdict, "seconds": round(time.time() - t0, 1),
                        "answer": ((run.get("answer") or {}).get("answer") or "")[:500], "error": run.get("error")})
        print(f"{case['id']:4} {verdict:9} {run['status']:16} {case['question'][:70]}")
    by = defaultdict(lambda: defaultdict(int))
    for r in results:
        by[r["category"]][r["verdict"]] += 1
        by["lang:" + r["language"]][r["verdict"]] += 1
    ans = [r for r in results if r["expect"] == "answer"]
    unans = [r for r in results if r["expect"] == "not_found"]
    summary = {
        "scoring": "deterministic (string match + status); no model judge",
        "useful_answer_rate": round(sum(r["verdict"] == "PASS" for r in ans) / len(ans), 3),
        "routed_to_engineer_rate_answerable": round(sum(r["verdict"] == "ROUTED" for r in ans) / len(ans), 3),
        "abstention_rate_answerable": round(sum(r["verdict"] == "ABSTAINED" for r in ans) / len(ans), 3),
        "wrong_released_answer_rate": round(sum(r["verdict"] == "FAIL" for r in ans) / len(ans), 3),
        "false_answer_rate_unanswerable": round(sum(r["verdict"] == "FAIL" for r in unans) / len(unans), 3),
        "adversarial_failures": sum(r["verdict"] == "FAIL" for r in results if r["category"] == "adversarial"),
        "mean_seconds": round(sum(r["seconds"] for r in results) / len(results), 1),
        "by_group": {k: dict(v) for k, v in by.items()},
    }
    Path(a.out).write_text(json.dumps({"summary": summary, "results": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
