#!/usr/bin/env python3
"""Валидатор корпуса «Гелион».

Три класса проверок — ровно те, без которых демо ломается вопросом:

  A. Золотые факты      — каждый факт присутствует в НАЗВАННОЙ встрече
                          и произнесён НАЗВАННЫМ человеком.
  B. Негативные контроли — строки, которых не должно быть НИГДЕ
                          (арка №12 «честное не знаю», красные поля КП).
  C. Запрещённые совмещения — пары, которые не должны встретиться
                          в одном транскрипте (арка №1: рассинхрон).

Прогоняется ПЕРЕД инжестом. Красное — правится корпус, а не отчёт.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GT = ROOT / "ground-truth.yaml"
TR = ROOT / "transcripts"

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def load_gt():
    with open(GT, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_transcripts(gt):
    """Возвращает {meeting_id: (текст, {speaker_name: [реплики]})}."""
    by_id = {}
    # YAML парсит даты в объекты date — приводим к строке, иначе
    # сопоставление с именем файла молча не срабатывает.
    id_by_date = {str(m["date"]): m["id"] for m in gt["meetings"]}
    for p in sorted(TR.glob("*.txt")):
        date = p.name[:10]
        mid = id_by_date.get(date)
        if mid is None:
            print(f"{YELLOW}  ? файл {p.name} не сопоставлен встрече{RESET}")
            continue
        text = p.read_text(encoding="utf-8")
        lines = {}
        for m in re.finditer(r"^\[\d+:\d+\]\s+([^:]+):\s*(.+)$", text, re.M):
            lines.setdefault(m.group(1).strip(), []).append(m.group(2))
        by_id[mid] = (text, lines)
    return by_id


def speaker_name(gt, pid):
    for p in gt["people"]:
        if p["id"] == pid:
            return p["name"]
    return None


def check_golden(gt, corpus):
    print("A. Золотые факты")
    fails = 0
    for gf in gt["golden_facts"]:
        mid = gf["meeting"]
        if mid not in corpus:
            print(f"  {YELLOW}○ {gf['id']}: встреча {mid} ещё не сгенерирована{RESET}")
            continue
        _, lines = corpus[mid]
        who = speaker_name(gt, gf["speaker"])
        said = " ".join(lines.get(who, []))
        if re.search(gf["anchor"], said, re.I):
            print(f"  {GREEN}✓{RESET} {gf['id']}  {mid:9} {who}")
        else:
            print(f"  {RED}✗ {gf['id']}  {mid:9} {who} — "
                  f"не найдено «{gf['anchor']}» в его репликах{RESET}")
            fails += 1
    return fails


def check_negative(gt, corpus):
    print("\nB. Негативные контроли (не должно быть нигде)")
    fails = 0
    for nc in gt["negative_controls"]:
        hits = [mid for mid, (text, _) in corpus.items()
                if re.search(nc["pattern"], text, re.I)]
        if hits:
            print(f"  {RED}✗ {nc['id']}: «{nc['pattern']}» найдено в {hits}{RESET}")
            fails += 1
        else:
            print(f"  {GREEN}✓{RESET} {nc['id']}  «{nc['pattern']}» отсутствует")
    return fails


def check_cooccurrence(gt, corpus):
    print("\nC. Запрещённые совмещения (не в одном транскрипте)")
    fails = 0
    for fc in gt["forbidden_cooccurrence"]:
        bad = [mid for mid, (text, _) in corpus.items()
               if re.search(fc["a"], text, re.I) and re.search(fc["b"], text, re.I)]
        if bad:
            print(f"  {RED}✗ {fc['id']}: «{fc['a']}» и «{fc['b']}» вместе в {bad}{RESET}")
            fails += 1
        else:
            print(f"  {GREEN}✓{RESET} {fc['id']}  «{fc['a']}» × «{fc['b']}» не пересекаются")
    # плюс явные запреты, объявленные у самой встречи
    for m in gt["meetings"]:
        for banned in m.get("forbidden_in_text", []):
            if m["id"] not in corpus:
                continue
            text, _ = corpus[m["id"]]
            if re.search(banned, text, re.I):
                print(f"  {RED}✗ {m['id']}: запрещённая строка «{banned}» присутствует{RESET}")
                fails += 1
            else:
                print(f"  {GREEN}✓{RESET} {m['id']:9} без «{banned}»")
    return fails


def check_lonely(gt, corpus):
    """Факт обязан прозвучать РОВНО в одной названной встрече и нигде
    больше в загруженном корпусе. Показывает «решение забыто» как факт
    корпуса, а не как утверждение в тексте спецификации."""
    print("\nE. Одинокие факты (ровно одна встреча, нигде больше)")
    fails = 0
    for lf in gt.get("lonely_facts", []):
        mid = lf["meeting"]
        if mid not in corpus:
            print(f"  {YELLOW}○ {lf['id']}: встреча {mid} ещё не сгенерирована{RESET}")
            continue
        hits = [m for m, (text, _) in corpus.items()
                if re.search(lf["pattern"], text, re.I)]
        if hits == [mid]:
            print(f"  {GREEN}✓{RESET} {lf['id']}  «{lf['pattern']}» — только в {mid}")
        elif mid not in hits:
            print(f"  {RED}✗ {lf['id']}: не найдено даже в {mid}{RESET}")
            fails += 1
        else:
            extra = [m for m in hits if m != mid]
            print(f"  {RED}✗ {lf['id']}: всплывает ещё и в {extra} — "
                  f"факт больше не одинокий{RESET}")
            fails += 1
    return fails


def check_graph_answers(gt):
    """graph_answers не проверяется regex'ом — это ответ графового канала,
    не текстовый якорь. Печатаем как памятку для инжеста и ручной сверки."""
    ga = gt.get("graph_answers", [])
    if not ga:
        return
    print("\nF. Графовые ответы (сверяются вручную после инжеста, не regex'ом)")
    for g in ga:
        names = ", ".join(p["person"] for p in g["answer"])
        print(f"  {YELLOW}i{RESET} {g['id']}  «{g['question']}» → {names}")


def check_access_declared(gt):
    """Проверяет только, что sovet-01 ЗАЯВЛЕН с ограниченным грифом в
    ground truth. НЕ проверяет и не может проверить реальное разграничение
    доступа — это свойство живой системы, не текста корпуса. Настоящий
    тест арки №10: задать один и тот же вопрос от роли стажёра и от роли
    CEO против развёрнутой системы и сравнить ответы вручную."""
    print("\nG. Заявленный гриф (не тест enforcement — см. примечание)")
    board = [m for m in gt["meetings"] if m.get("access") == "board"]
    other_board_access = [m for m in gt["meetings"]
                           if m["id"] != "sovet-01" and m.get("access") == "board"]
    if any(m["id"] == "sovet-01" for m in board) and not other_board_access:
        print(f"  {GREEN}✓{RESET} sovet-01 — единственная встреча с access=board")
    else:
        print(f"  {RED}✗ гриф заявлен неверно: board={[m['id'] for m in board]}{RESET}")
        return 1
    print(f"  {YELLOW}i{RESET} Реальный enforcement НЕ тестируется этим скриптом. "
          f"Прогнать вручную: тот же вопрос от роли стажёра и от роли CEO "
          f"на развёрнутой системе.")
    return 0


def check_client_boundary(gt, corpus):
    """Клиентские звонки не должны содержать внутренние факты компании.
    Прямая проверка того же разграничения доступа, которое продукт
    продаёт как enterprise-фичу (гл. 14) — только на текстовом уровне
    корпуса, не на уровне реального поведения симуляции."""
    print("\nH. Граница клиентских звонков (внутреннее не протекает наружу)")
    total_fails = 0
    for cb in gt.get("client_boundary", []):
        cb_fails = 0
        checked = 0
        for m in cb["meetings"]:
            if m not in corpus:
                print(f"  {YELLOW}○ {cb['id']}: встреча {m} ещё не сгенерирована{RESET}")
                continue
            checked += 1
            text, _ = corpus[m]
            for f in cb["forbidden"]:
                if re.search(f["pattern"], text, re.I):
                    print(f"  {RED}✗ {cb['id']}: «{f['pattern']}» (внутр. {f['from']}) "
                          f"протекло в {m}{RESET}")
                    cb_fails += 1
        if cb_fails == 0 and checked:
            print(f"  {GREEN}✓{RESET} {cb['id']}  {len(cb['forbidden'])} внутренних паттернов "
                  f"чисты в {checked} клиентских встречах")
        total_fails += cb_fails
    return total_fails


def check_noise(corpus):
    """Грубая эвристика: доля реплик вне golden-сюжетов.

    Стерильный корпус палится за минуту. Целевой шум — 15–20 %.
    Здесь считаем только объём как сигнал, не как приёмку.
    """
    print("\nD. Объём и плотность (справочно, не приёмка)")
    for mid, (text, lines) in sorted(corpus.items()):
        n = sum(len(v) for v in lines.values())
        words = len(text.split())
        print(f"     {mid:9} реплик: {n:3}   слов: {words:5}   спикеров: {len(lines)}")


def main():
    gt = load_gt()
    corpus = load_transcripts(gt)
    print(f"Валидация корпуса «{gt['meta']['name']}» — срез {gt['meta']['slice']}: "
          f"{len(corpus)} из {len(gt['meetings'])} описанных встреч\n")

    # Контрольный опыт: валидатор не имеет права выдать зелёный,
    # ничего не проверив. Пустой корпус или несопоставленные файлы —
    # это провал валидатора, а не успех корпуса.
    expected = {m["id"] for m in gt["meetings"]}
    missing = expected - set(corpus)
    if not corpus:
        print(f"{RED}ПРОВАЛ ВАЛИДАТОРА: не загружено ни одного транскрипта.{RESET}")
        return 2
    if missing:
        print(f"{RED}ПРОВАЛ ВАЛИДАТОРА: встречи без транскрипта: "
              f"{sorted(missing)}{RESET}\n")

    fails = check_golden(gt, corpus)
    fails += check_negative(gt, corpus)
    fails += check_cooccurrence(gt, corpus)
    fails += check_lonely(gt, corpus)
    fails += check_access_declared(gt)
    fails += check_client_boundary(gt, corpus)
    check_graph_answers(gt)
    check_noise(corpus)

    fails += len(missing)
    print()
    if fails:
        print(f"{RED}ПРОВАЛ: {fails} нарушений. Корпус к инжесту не готов.{RESET}")
        return 1
    print(f"{GREEN}ЗЕЛЁНО. Корпус можно инжестить и прогонять приёмочные вопросы.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
