#!/usr/bin/env python3
"""Генерирует CRM-выгрузку и финтаблицу ИЗ ground-truth.yaml.

Принцип: ни одна цифра не пишется руками и не рождается в LLM.
Правка числа = правка ground-truth.yaml + перезапуск этого скрипта.
"""
import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GT = ROOT / "ground-truth.yaml"
OUT = ROOT / "data"


def load():
    with open(GT, encoding="utf-8") as f:
        return yaml.safe_load(f)


def client_name(gt, cid):
    for c in gt["clients"]:
        if c["id"] == cid:
            return c["name"]
    return cid  # фоновые клиенты записаны строкой прямо в сделке


def gen_crm(gt):
    OUT.mkdir(exist_ok=True)
    path = OUT / "crm_deals.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "client", "title", "amount_rub", "stage", "closed_at"])
        for d in gt["deals"]:
            w.writerow([
                d["id"], client_name(gt, d["client"]), d["title"],
                d["amount"], d["stage"], d["closed"] or "",
            ])
    return path, len(gt["deals"])


def gen_finance(gt):
    path = OUT / "finance_monthly.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["month", "revenue_rub", "costs_rub", "margin_rub"])
        for m in gt["finance_monthly"]:
            w.writerow([m["month"], m["revenue"], m["costs"],
                        m["revenue"] - m["costs"]])
    return path, len(gt["finance_monthly"])


def check_derived(gt):
    """Проверяем, что производные величины в ground truth сходятся с рядом.

    Это защита от расхождения между тем, что говорит персонаж, и тем,
    что лежит в данных: арка №2 держится ровно на этой разнице.
    """
    q1 = sum(m["revenue"] for m in gt["finance_monthly"]
             if m["month"].startswith("2026-0") and m["month"][-1] in "123")
    declared = gt["derived"]["q1_2026_revenue"]
    claim = gt["derived"]["guryev_claim_q1"]
    ok = q1 == declared
    gap = (claim - q1) / q1 * 100
    print(f"  Q1-2026 по ряду:        {q1:,} ₽".replace(",", " "))
    print(f"  Q1-2026 в derived:      {declared:,} ₽".replace(",", " "))
    print(f"  {'OK' if ok else 'РАСХОЖДЕНИЕ!'}")
    print(f"  Гурьев говорит:         {claim:,} ₽".replace(",", " "))
    print(f"  Разрыв арки №2:         {gap:.1f} %")
    return ok


def main():
    gt = load()
    print("Генерация производных данных из ground-truth.yaml\n")

    p, n = gen_crm(gt)
    print(f"  {p.name}: {n} сделок")
    p, n = gen_finance(gt)
    print(f"  {p.name}: {n} месяцев\n")

    print("Сверка производных величин:")
    ok = check_derived(gt)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
