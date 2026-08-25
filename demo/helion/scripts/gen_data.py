#!/usr/bin/env python3
"""Generates the CRM export and the finance sheet FROM ground-truth.yaml.

The rule: no number is ever typed by hand and no number is ever born inside
an LLM. Changing a figure means editing ground-truth.yaml and re-running this
script — never editing the CSV.

English localization of demo/gelion/scripts/gen_data.py. Two differences,
both deliberate:
  * the currency columns are USD (`amount_usd`, `revenue_usd`, ...), and the
    figures are rebased to a plausible US scale for an ~85-person SaaS company
    (2025 revenue 17,175,000) — they are NOT a fixed conversion of the rouble
    originals;
  * the CSVs are comma-delimited, the US convention, where the Russian
    originals are semicolon-delimited. Column order, row order and the empty
    `closed_at` for open deals are unchanged.
"""
import csv
import datetime
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GT = ROOT / "ground-truth.yaml"
OUT = ROOT / "data"

Q1_2026 = ("2026-01", "2026-02", "2026-03")
PIPELINE_AS_OF = datetime.date(2026, 4, 7)


def load():
    with open(GT, encoding="utf-8") as f:
        return yaml.safe_load(f)


def client_name(gt, cid):
    for c in gt["clients"]:
        if c["id"] == cid:
            return c["name"]
    return cid  # background clients are written into the deal as a plain string


def gen_crm(gt):
    OUT.mkdir(exist_ok=True)
    path = OUT / "crm_deals.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=",")
        w.writerow(["id", "client", "title", "amount_usd", "stage", "closed_at"])
        for d in gt["deals"]:
            w.writerow([
                d["id"], client_name(gt, d["client"]), d["title"],
                d["amount"], d["stage"], d["closed"] or "",
            ])
    return path, len(gt["deals"])


def gen_finance(gt):
    path = OUT / "finance_monthly.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=",")
        w.writerow(["month", "revenue_usd", "costs_usd", "margin_usd"])
        for m in gt["finance_monthly"]:
            w.writerow([m["month"], m["revenue"], m["costs"],
                        m["revenue"] - m["costs"]])
    return path, len(gt["finance_monthly"])


def check_derived(gt):
    """Checks that the derived figures in the ground truth agree with the series.

    This is the guard against drift between what a person says out loud and
    what the data holds: arc 2 rests on exactly that difference, so the gap
    has to be arithmetic, not a coincidence of two hand-typed numbers.
    """
    q1 = sum(m["revenue"] for m in gt["finance_monthly"]
             if m["month"] in Q1_2026)
    declared = gt["derived"]["q1_2026_revenue"]
    claim = gt["derived"]["guryev_claim_q1"]
    ok = q1 == declared
    gap = (claim - q1) / q1 * 100
    print(f"  Q1-2026 from the series:  ${q1:,}")
    print(f"  Q1-2026 in derived:       ${declared:,}")
    print(f"  {'OK' if ok else 'MISMATCH!'}")
    print(f"  Guryev says:              ${claim:,}")
    print(f"  Arc 2 gap:                {gap:.1f} %")
    return ok


def check_pipeline(gt):
    """Ties background_figures.open_pipeline_2026_04_07 to the CRM.

    The figure is spoken out loud in the corpus, so it may not be a number
    somebody remembered: it is the sum of every deal that had not closed yet
    on that date — the still-open ones plus the ones that closed later.
    """
    total = sum(d["amount"] for d in gt["deals"]
                if d["closed"] is None or d["closed"] > PIPELINE_AS_OF)
    declared = gt["background_figures"]["open_pipeline_2026_04_07"]["value"]
    ok = total == declared
    print(f"  Open pipeline {PIPELINE_AS_OF}:  ${total:,}")
    print(f"  In background_figures:    ${declared:,}")
    print(f"  {'OK' if ok else 'MISMATCH!'}")
    return ok


def main():
    gt = load()
    print("Generating derived data from ground-truth.yaml\n")

    p, n = gen_crm(gt)
    print(f"  {p.name}: {n} deals")
    p, n = gen_finance(gt)
    print(f"  {p.name}: {n} months\n")

    print("Reconciling derived figures:")
    ok = check_derived(gt)
    ok = check_pipeline(gt) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
