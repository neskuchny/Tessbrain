#!/usr/bin/env python3
"""Corpus validator for "Helion".

Three classes of check — exactly the ones without which a single question
breaks the demo:

  A. Golden facts        — every fact is present in the NAMED meeting
                           and spoken by the NAMED person.
  B. Negative controls   — strings that must appear NOWHERE
                           (arc 12 "the honest I don't know", the red
                           fields of the proposal in arc 13).
  C. Forbidden co-occurrence — pairs that must not meet inside one
                           transcript (arc 1: the misalignment).

Run BEFORE ingest. Red means the corpus gets fixed, not the report.
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
    """Returns {meeting_id: (text, {speaker_name: [lines]})}."""
    by_id = {}
    # YAML parses dates into date objects — cast to string, otherwise the
    # match against the file name silently fails.
    id_by_date = {str(m["date"]): m["id"] for m in gt["meetings"]}
    for p in sorted(TR.glob("*.txt")):
        date = p.name[:10]
        mid = id_by_date.get(date)
        if mid is None:
            print(f"{YELLOW}  ? file {p.name} not matched to a meeting{RESET}")
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
    print("A. Golden facts")
    fails = 0
    for gf in gt["golden_facts"]:
        mid = gf["meeting"]
        if mid not in corpus:
            print(f"  {YELLOW}○ {gf['id']}: meeting {mid} not generated yet{RESET}")
            continue
        _, lines = corpus[mid]
        who = speaker_name(gt, gf["speaker"])
        said = " ".join(lines.get(who, []))
        if re.search(gf["anchor"], said, re.I):
            print(f"  {GREEN}✓{RESET} {gf['id']}  {mid:9} {who}")
        else:
            print(f"  {RED}✗ {gf['id']}  {mid:9} {who} — "
                  f"«{gf['anchor']}» not found in their lines{RESET}")
            fails += 1
    return fails


def check_negative(gt, corpus):
    print("\nB. Negative controls (must appear nowhere)")
    fails = 0
    for nc in gt["negative_controls"]:
        hits = [mid for mid, (text, _) in corpus.items()
                if re.search(nc["pattern"], text, re.I)]
        if hits:
            print(f"  {RED}✗ {nc['id']}: «{nc['pattern']}» found in {hits}{RESET}")
            fails += 1
        else:
            print(f"  {GREEN}✓{RESET} {nc['id']}  «{nc['pattern']}» absent")
    return fails


def check_cooccurrence(gt, corpus):
    print("\nC. Forbidden co-occurrence (not inside one transcript)")
    fails = 0
    for fc in gt["forbidden_cooccurrence"]:
        bad = [mid for mid, (text, _) in corpus.items()
               if re.search(fc["a"], text, re.I) and re.search(fc["b"], text, re.I)]
        if bad:
            print(f"  {RED}✗ {fc['id']}: «{fc['a']}» and «{fc['b']}» together in {bad}{RESET}")
            fails += 1
        else:
            print(f"  {GREEN}✓{RESET} {fc['id']}  «{fc['a']}» × «{fc['b']}» do not overlap")
    # plus the explicit bans declared on the meeting itself
    for m in gt["meetings"]:
        for banned in m.get("forbidden_in_text", []):
            if m["id"] not in corpus:
                continue
            text, _ = corpus[m["id"]]
            if re.search(banned, text, re.I):
                print(f"  {RED}✗ {m['id']}: forbidden string «{banned}» is present{RESET}")
                fails += 1
            else:
                print(f"  {GREEN}✓{RESET} {m['id']:9} clean of «{banned}»")
    return fails


def check_lonely(gt, corpus):
    """A fact must be said in EXACTLY one named meeting and nowhere else in
    the loaded corpus. Shows "decided and forgotten" as a fact of the corpus
    rather than as a claim in the text of the spec."""
    print("\nE. Lonely facts (exactly one meeting, nowhere else)")
    fails = 0
    for lf in gt.get("lonely_facts", []):
        mid = lf["meeting"]
        if mid not in corpus:
            print(f"  {YELLOW}○ {lf['id']}: meeting {mid} not generated yet{RESET}")
            continue
        hits = [m for m, (text, _) in corpus.items()
                if re.search(lf["pattern"], text, re.I)]
        if hits == [mid]:
            print(f"  {GREEN}✓{RESET} {lf['id']}  «{lf['pattern']}» — only in {mid}")
        elif mid not in hits:
            print(f"  {RED}✗ {lf['id']}: not found even in {mid}{RESET}")
            fails += 1
        else:
            extra = [m for m in hits if m != mid]
            print(f"  {RED}✗ {lf['id']}: also surfaces in {extra} — "
                  f"the fact is no longer lonely{RESET}")
            fails += 1
    return fails


def check_graph_answers(gt):
    """graph_answers are not checked by regex — they are an answer of the
    graph channel, not a text anchor. Printed as a reminder for ingest and
    manual verification."""
    ga = gt.get("graph_answers", [])
    if not ga:
        return
    print("\nF. Graph answers (verified by hand after ingest, not by regex)")
    for g in ga:
        names = ", ".join(p["person"] for p in g["answer"])
        print(f"  {YELLOW}i{RESET} {g['id']}  «{g['question']}» → {names}")


def check_access_declared(gt):
    """Checks only that board-01 is DECLARED with restricted access in the
    ground truth. It does NOT and cannot check real access enforcement —
    that is a property of the live system, not of the corpus text. The real
    test of arc 10: ask the same question as the intern role and as the CEO
    role against a deployed system and compare the answers by hand."""
    print("\nG. Declared access level (not an enforcement test — see note)")
    board = [m for m in gt["meetings"] if m.get("access") == "board"]
    other_board_access = [m for m in gt["meetings"]
                           if m["id"] != "board-01" and m.get("access") == "board"]
    if any(m["id"] == "board-01" for m in board) and not other_board_access:
        print(f"  {GREEN}✓{RESET} board-01 — the only meeting with access=board")
    else:
        print(f"  {RED}✗ access declared incorrectly: board={[m['id'] for m in board]}{RESET}")
        return 1
    print(f"  {YELLOW}i{RESET} Real enforcement is NOT tested by this script. "
          f"Run by hand: the same question from the intern role and from the CEO role "
          f"against a deployed system.")
    return 0


def check_client_boundary(gt, corpus):
    """Client calls must not contain internal company facts. A direct check
    of the same access boundary the product sells as an enterprise feature
    (ch. 14) — but only at the text level of the corpus, not at the level of
    real simulation behaviour."""
    print("\nH. Client-call boundary (internal facts do not leak outward)")
    total_fails = 0
    for cb in gt.get("client_boundary", []):
        cb_fails = 0
        checked = 0
        for m in cb["meetings"]:
            if m not in corpus:
                print(f"  {YELLOW}○ {cb['id']}: meeting {m} not generated yet{RESET}")
                continue
            checked += 1
            text, _ = corpus[m]
            for f in cb["forbidden"]:
                if re.search(f["pattern"], text, re.I):
                    print(f"  {RED}✗ {cb['id']}: «{f['pattern']}» (internal {f['from']}) "
                          f"leaked into {m}{RESET}")
                    cb_fails += 1
        if cb_fails == 0 and checked:
            print(f"  {GREEN}✓{RESET} {cb['id']}  {len(cb['forbidden'])} internal patterns "
                  f"clean across {checked} client meetings")
        total_fails += cb_fails
    return total_fails


def check_noise(corpus):
    """A rough heuristic: share of lines outside the golden storylines.

    A sterile corpus gives itself away in a minute. Target noise is 15-20 %.
    Here we only count volume as a signal, not as an acceptance criterion.
    """
    print("\nD. Volume and density (informational, not acceptance)")
    for mid, (text, lines) in sorted(corpus.items()):
        n = sum(len(v) for v in lines.values())
        words = len(text.split())
        print(f"     {mid:9} lines: {n:3}   words: {words:5}   speakers: {len(lines)}")


def main():
    gt = load_gt()
    corpus = load_transcripts(gt)
    print(f"Validating the «{gt['meta']['name']}» corpus — slice {gt['meta']['slice']}: "
          f"{len(corpus)} of {len(gt['meetings'])} described meetings\n")

    # Control experiment: the validator has no right to report green having
    # checked nothing. An empty corpus or unmatched files is a failure of the
    # validator, not a success of the corpus.
    expected = {m["id"] for m in gt["meetings"]}
    missing = expected - set(corpus)
    if not corpus:
        print(f"{RED}VALIDATOR FAILURE: not a single transcript was loaded.{RESET}")
        return 2
    if missing:
        print(f"{RED}VALIDATOR FAILURE: meetings with no transcript: "
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
        print(f"{RED}FAILED: {fails} violations. The corpus is not ready for ingest.{RESET}")
        return 1
    print(f"{GREEN}GREEN. The corpus can be ingested and the acceptance questions run.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
