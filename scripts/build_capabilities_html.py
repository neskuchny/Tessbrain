#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Карта возможностей → страница: markdown собирается в тот же HTML.

Зачем скрипт, а не единоразовая вёрстка. Страница с картой продукта уже
делалась руками — и устарела в тот же день, когда документ поехал дальше:
в ней остались «30+ типов объектов» и «29 схем», когда в документе стало
33, 48 и 31, плюс полтора десятка новых разделов. Ручная страница всегда
отстаёт от живого документа, поэтому здесь сборщик: правим документ —
пересобираем страницу одной командой, и расхождение невозможно.

Дизайн-система сохранена от прежней страницы (тёплая книжная палитра,
засечный заголовок, карточки возможностей, врезки «зачем» и «в жизни»,
красная рамка у границ) — менялось только то, что берётся из документа.

Разметка документа, которую понимает сборщик:
  ## N. Заголовок            → раздел с номером в кружке
  > **Зачем.** …             → врезка-обоснование
  > _Как это выглядит…_      → блок примеров (следующие **Заголовок.** …)
  > ⚠️ **Честно о зрелости** → врезка ограничений
  > прочее                   → цитата-примечание
  **Название** — описание    → карточка возможности
  | таблица |                → таблица в рамке
  - **N** — подпись          → плитки чисел (только в разделе 1)
  - **Пункт.** текст         → список границ (раздел 16)

Запуск:  python scripts/build_capabilities_html.py
"""
from __future__ import annotations

import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Двуязычно: страница собирается из документа СВОЕГО языка, а не переводится
# на лету. Английский набор — то, что выкладывается в опенсорс.
LANGS = {
    "ru": {
        "src": os.path.join(ROOT, "docs", "ru", "PRODUCT_CAPABILITIES.md"),
        "dst": os.path.join(ROOT, "docs", "tessent_capabilities.html"),
        "eyebrow": "Карта продукта · август 2026",
        "h1": "Цифровой мозг компании: что он умеет и зачем это бизнесу",
        "ex_label": "Как это выглядит в жизни",
        "footer": (
            "Собрано автоматически из <code>docs/ru/PRODUCT_CAPABILITIES.md</code> — "
            "страница и документ не расходятся по построению. Числа с пометкой "
            "«измерено» получены прогонами, результаты лежат в репозитории вместе "
            "с кодом, которым получены. Схема контура и интерактивный расчёт "
            "стоимости — отдельные страницы."),
    },
    "en": {
        "src": os.path.join(ROOT, "docs", "en", "product-capabilities.md"),
        "dst": os.path.join(ROOT, "docs", "tessent_capabilities_en.html"),
        "eyebrow": "Product map · August 2026",
        "h1": "The company brain: what it does and why a business needs it",
        "ex_label": "What it looks like in practice",
        "footer": (
            "Built automatically from <code>docs/en/product-capabilities.md</code> — "
            "the page and the document cannot drift apart by construction. Figures "
            "marked \u201cmeasured\u201d come from runs whose results live in the "
            "repository next to the code that produced them. The loop diagram and "
            "the interactive cost calculator are separate pages."),
    },
}
LANG = "ru"
SRC = LANGS[LANG]["src"]
DST = LANGS[LANG]["dst"]

# Якоря разделов: подобраны по смыслу, чтобы ссылки в оглавлении читались.
ANCHORS = {
    "1": "how", "1.1": "feeds", "1.2": "sync", "2": "memory",
    "2.1": "cards", "2.2": "unknown", "3": "people", "4": "hr",
    "4.1": "together", "5": "sim", "6": "numbers", "7": "chat",
    "8": "boards", "8.1": "docs", "9": "wisdom", "10": "sima",
    "11": "minitess", "12": "exec", "13": "bus", "14": "enterprise",
    "15": "proof", "16": "honest",
}


def esc(t: str) -> str:
    return html.escape(t, quote=False)


def inline(t: str) -> str:
    """Жирный, курсив, код, ссылки — в HTML. Экранирование до разметки."""
    t = esc(t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    # re.S обязателен: строки врезки склеиваются через \n (см. разбор цитат),
    # поэтому **жирный**, разорванный переносом, без DOTALL не находился и
    # звёздочки уезжали в вёрстку как есть.
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", t)
    t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<em>\1</em>", t)
    return t


class Section:
    def __init__(self, num: str, title: str):
        self.num, self.title = num, title
        self.blocks: list[tuple[str, object]] = []


def parse(md: str) -> tuple[dict, list[Section]]:
    lines = md.split("\n")
    head: dict = {"title": "", "lead": "", "meta": ""}
    sections: list[Section] = []
    cur: Section | None = None
    i, n = 0, len(lines)
    intro: list[str] = []

    while i < n:
        line = lines[i]

        m = re.match(r"^#\s+(.+)$", line)
        if m and not head["title"]:
            head["title"] = m.group(1).strip()
            i += 1
            continue

        m = re.match(r"^##\s+([\d.]+)\.\s+(.+)$", line)
        if m:
            cur = Section(m.group(1), m.group(2).strip())
            sections.append(cur)
            i += 1
            continue

        # таблица
        if line.startswith("|"):
            rows = []
            while i < n and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if cur:
                cur.blocks.append(("table", rows))
            continue

        # цитаты — собираем блок целиком
        if line.startswith(">"):
            quote: list[str] = []
            while i < n and (lines[i].startswith(">") or
                             (quote and lines[i].strip() == "" and
                              i + 1 < n and lines[i + 1].startswith(">"))):
                quote.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            text = "\n".join(quote).strip()
            if not cur:
                if not head["lead"]:
                    head["lead"] = text
                i += 1 if i < n and lines[i - 1].strip() == "" else 0
                continue
            low = text.lower()
            # маркеры распознаются на обоих языках: русский документ и
            # английский используют одни и те же врезки
            if low.startswith("**зачем") or low.startswith("**why"):
                cur.blocks.append(("why", text))
            elif "как это выглядит в жизни" in low or "what it looks like" in low:
                cur.blocks.append(("ex_start", ""))
            elif text.startswith("⚠️"):
                cur.blocks.append(("edge", text.lstrip("⚠️ ").strip()))
            else:
                cur.blocks.append(("callout", text))
            continue

        # списки
        if re.match(r"^[-*]\s+", line):
            items = []
            while i < n and re.match(r"^[-*]\s+", lines[i]):
                items.append(re.sub(r"^[-*]\s+", "", lines[i]).strip())
                i += 1
                # продолжение пункта на следующих строках с отступом: без
                # этого хвост многострочного пункта молча ТЕРЯЛСЯ, а
                # разорванный переносом **жирный** оставлял звёздочки в вёрстке
                while (i < n and lines[i].strip()
                       and not re.match(r"^[-*]\s+", lines[i])
                       and lines[i].startswith(("  ", "\t"))):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            if cur:
                kpi = all(re.match(r"^\*\*[~\d][^*]*\*\*\s+—", x) for x in items)
                cur.blocks.append(("kpi" if kpi else "list", items))
            continue

        # абзац
        if line.strip():
            para = [line.strip()]
            i += 1
            while i < n and lines[i].strip() and not lines[i].startswith(
                    ("#", ">", "|", "-", "*   ")):
                para.append(lines[i].strip())
                i += 1
            text = " ".join(para)
            if not cur:
                # горизонтальные линейки и повтор вводного абзаца
                # (он в документе продублирован) в шапку не идут
                if not re.fullmatch(r"[-*_]{3,}", text) and text not in intro:
                    intro.append(text)
            else:
                # «**Название** — описание» → карточка возможности
                m2 = re.match(r"^\*\*(.+?)\*\*\s*(?:—|--)\s*(.+)$", text, re.S)
                if m2 and len(m2.group(1)) < 90:
                    cur.blocks.append(("cap", (m2.group(1), m2.group(2))))
                else:
                    m3 = re.match(r"^\*\*(.+?)\.\*\*\s*(.+)$", text, re.S)
                    if m3 and len(m3.group(1)) < 90:
                        cur.blocks.append(("case", (m3.group(1), m3.group(2))))
                    else:
                        cur.blocks.append(("p", text))
            continue
        i += 1

    if intro:
        head["lead"] = head["lead"] or intro[0]
        head["intro"] = intro[0]
        head["meta"] = intro[1] if len(intro) > 1 else ""
    return head, sections


def render_section(s: Section) -> str:
    anchor = ANCHORS.get(s.num, "s" + s.num.replace(".", "-"))
    out = [f'<section id="{anchor}">',
           f'<h2><span class="num">{esc(s.num)}</span> {inline(s.title)}</h2>']
    caps: list[tuple[str, str]] = []
    cases: list[tuple[str, str]] = []
    in_ex = False

    def flush_caps():
        nonlocal caps
        if caps:
            out.append('<div class="caps">')
            for t, d in caps:
                out.append(f'<div class="cap"><h3>{inline(t)}</h3>'
                           f'<p>{inline(d)}</p></div>')
            out.append('</div>')
            caps = []

    def flush_cases():
        nonlocal cases
        if cases:
            out.append('<div class="ex"><div class="lbl">'
                       + LANGS[LANG]["ex_label"] + '</div>')
            for t, d in cases:
                out.append(f'<p><b>{inline(t)}.</b> {inline(d)}</p>')
            out.append('</div>')
            cases = []

    for kind, val in s.blocks:
        if kind == "cap":
            if in_ex:
                cases.append(val)
            else:
                caps.append(val)
            continue
        if kind == "case":
            (cases if in_ex else caps).append(val)
            continue
        flush_caps()
        if kind == "ex_start":
            in_ex = True
            continue
        if kind != "case":
            flush_cases()
        if kind == "p":
            out.append(f'<p class="sub">{inline(val)}</p>')
        elif kind == "why":
            out.append(f'<div class="why">{inline(val)}</div>')
        elif kind == "edge":
            out.append(f'<div class="honest"><h3>Честно о зрелости</h3>'
                       f'<p>{inline(val)}</p></div>')
        elif kind == "callout":
            out.append(f'<div class="callout">{inline(val)}</div>')
        elif kind == "kpi":
            out.append('<div class="kpi">')
            for it in val:
                m = re.match(r"^\*\*(.+?)\*\*\s*—\s*(.+)$", it)
                if m:
                    out.append(f'<div><div class="n">{esc(m.group(1))}</div>'
                               f'<div class="l">{inline(m.group(2))}</div></div>')
            out.append('</div>')
        elif kind == "list":
            if s.num == "16":
                out.append('<div class="honest"><ul>')
                for it in val:
                    out.append(f'<li>{inline(it)}</li>')
                out.append('</ul></div>')
            else:
                out.append('<ul class="plain">')
                for it in val:
                    out.append(f'<li>{inline(it)}</li>')
                out.append('</ul>')
        elif kind == "table":
            if not val:
                continue
            out.append('<div class="twrap"><table><thead><tr>')
            for c in val[0]:
                out.append(f'<th>{inline(c)}</th>')
            out.append('</tr></thead><tbody>')
            for row in val[1:]:
                out.append('<tr>' + ''.join(
                    f'<td>{inline(c)}</td>' for c in row) + '</tr>')
            out.append('</tbody></table></div>')
    flush_caps()
    flush_cases()
    out.append('</section>')
    return "\n".join(out)


CSS = """
  :root {
    /* Светлая книга — фиксированная палитра на весь набор страниц.
       Раньше страница следовала теме ОС и на тёмном экране показывалась
       тёмной; для единого «книжного» набора она всегда светлая. */
    --bg:#f3f1ea; --surface:#fff; --surface-2:#f7f5ef; --surface-3:#efece2;
    --border:#ddd8cb; --border-soft:#e6e2d5;
    --text:#21201b; --text-dim:#5c584e; --text-faint:#8b8677;
    --accent:#a97a2c; --accent-soft:rgba(169,122,44,.10);
    --good:#3f7a56; --risk:#a13f34; --risk-soft:rgba(161,63,52,.09);
    --info:#4b6a92;
    --serif:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,ui-serif,serif;
    --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
    --mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:var(--sans);-webkit-font-smoothing:antialiased}
  body{padding:56px 24px 90px}
  .page{max-width:1120px;margin:0 auto}
  .eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--accent);font-weight:600;margin-bottom:14px}
  h1{font-family:var(--serif);font-weight:600;font-size:clamp(30px,4.4vw,46px);
    line-height:1.12;margin:0 0 16px;text-wrap:balance}
  .lead{font-size:17px;color:var(--text-dim);line-height:1.6;max-width:760px;margin:0 0 8px}
  .lead b{color:var(--text);font-weight:600}
  .meta{font-size:12.5px;color:var(--text-faint);margin-top:22px;padding-top:20px;
    border-top:1px solid var(--border-soft);line-height:1.75;max-width:840px}
  .meta b{color:var(--text-dim)}
  nav.toc{display:flex;flex-wrap:wrap;gap:7px;margin-top:30px}
  nav.toc a{font-size:12.5px;color:var(--text-dim);text-decoration:none;
    background:var(--surface);border:1px solid var(--border);border-radius:999px;
    padding:6px 13px;transition:color .15s,border-color .15s}
  nav.toc a:hover{color:var(--accent);border-color:var(--accent)}
  section{margin-top:64px;scroll-margin-top:20px}
  h2{font-family:var(--serif);font-weight:600;font-size:25px;margin:0 0 8px;
    display:flex;align-items:baseline;gap:12px;text-wrap:balance}
  h2 .num{font-family:var(--sans);font-size:11px;color:var(--accent);
    border:1px solid var(--accent);border-radius:999px;min-width:24px;height:24px;
    padding:0 6px;display:inline-flex;align-items:center;justify-content:center;
    flex-shrink:0;font-weight:700}
  .sub{font-size:14.5px;color:var(--text-dim);line-height:1.65;max-width:790px;margin:0 0 12px}
  .sub b{color:var(--text)}
  .why{font-size:14.5px;line-height:1.65;max-width:790px;margin:14px 0 24px;
    padding:14px 18px;background:var(--accent-soft);border-left:3px solid var(--accent);
    border-radius:0 8px 8px 0;color:var(--text)}
  .why b{color:var(--accent);font-weight:700}
  .callout{font-size:14px;line-height:1.7;max-width:830px;margin:16px 0;
    padding:14px 18px;background:var(--surface-2);border:1px solid var(--border-soft);
    border-radius:10px;color:var(--text-dim)}
  .callout b{color:var(--text)}
  .callout ul{margin:8px 0 0;padding-left:19px}
  .caps{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));
    gap:12px;margin:14px 0 22px}
  .cap{background:var(--surface);border:1px solid var(--border);border-radius:11px;
    padding:16px 18px}
  .cap h3{font-size:14.5px;font-weight:650;margin:0 0 7px;line-height:1.35;color:var(--text)}
  .cap p{font-size:13px;color:var(--text-dim);line-height:1.55;margin:0}
  .cap p b,.cap h3 b{color:var(--text);font-weight:650}
  .cap p em{color:var(--text);font-style:normal;font-weight:600}
  .ex{background:var(--surface-2);border:1px solid var(--border-soft);
    border-radius:11px;padding:16px 20px;margin:14px 0}
  .ex .lbl{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
    color:var(--accent);font-weight:700;margin-bottom:8px}
  .ex p{font-size:13.5px;color:var(--text);line-height:1.65;margin:0 0 9px}
  .ex p:last-child{margin-bottom:0}
  .ex p+p{padding-top:9px;border-top:1px solid var(--border-soft)}
  table{border-collapse:collapse;width:100%;font-size:13px;margin:0}
  .twrap{overflow-x:auto;border:1px solid var(--border);border-radius:11px;
    background:var(--surface);margin:6px 0 18px}
  th,td{padding:11px 15px;border-bottom:1px solid var(--border-soft);
    text-align:left;vertical-align:top}
  thead th{background:var(--surface-2);font-size:11px;text-transform:uppercase;
    letter-spacing:.04em;color:var(--text-faint);font-weight:650;white-space:nowrap}
  tbody tr:last-child td{border-bottom:none}
  td b{color:var(--text)}
  .honest{background:var(--surface);border:1px solid var(--border);
    border-left:3px solid var(--risk);border-radius:0 11px 11px 0;
    padding:18px 22px;margin:16px 0}
  .honest h3{font-size:13px;font-weight:650;margin:0 0 10px;color:var(--risk);
    text-transform:uppercase;letter-spacing:.06em}
  .honest p{font-size:13.5px;color:var(--text-dim);line-height:1.7;margin:0;max-width:830px}
  .honest p b{color:var(--text)}
  .honest ul{margin:0;padding-left:19px}
  .honest li{font-size:13.5px;color:var(--text-dim);line-height:1.68;margin-bottom:9px}
  .honest li:last-child{margin-bottom:0}
  .honest li b{color:var(--text)}
  ul.plain{margin:10px 0 18px;padding-left:19px;max-width:830px}
  ul.plain li{font-size:14px;color:var(--text-dim);line-height:1.65;margin-bottom:7px}
  ul.plain li b{color:var(--text)}
  .kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:12px;margin:20px 0 8px}
  .kpi>div{background:var(--surface);border:1px solid var(--border);
    border-radius:11px;padding:15px 17px}
  .kpi .n{font-family:var(--serif);font-size:26px;font-weight:600;color:var(--accent);
    font-variant-numeric:tabular-nums;line-height:1.1}
  .kpi .l{font-size:12px;color:var(--text-dim);margin-top:5px;line-height:1.45}
  code{font-family:var(--mono);font-size:12px;background:var(--surface-3);
    padding:1px 6px;border-radius:4px;color:var(--accent)}
  a{color:var(--accent)}
  footer{margin-top:70px;padding-top:26px;border-top:1px solid var(--border-soft);
    font-size:12.5px;color:var(--text-faint);line-height:1.85}
  footer a{color:var(--text-dim)}
  @media (max-width:640px){body{padding:36px 16px 60px}}
"""


def build() -> str:
    md = open(SRC, encoding="utf-8").read()
    head, sections = parse(md)
    toc = "\n".join(
        f'    <a href="#{ANCHORS.get(s.num, "s" + s.num.replace(".", "-"))}">'
        f'{esc(s.num)}. {esc(s.title.split(":")[0].split("—")[0].strip())}</a>'
        for s in sections)
    body = "\n\n".join(render_section(s) for s in sections)
    return f"""<meta charset="utf-8">
<title>{esc(head["title"])}</title>
<style>{CSS}</style>

<div class="page">
  <div class="eyebrow">{LANGS[LANG]["eyebrow"]}</div>
  <h1>{LANGS[LANG]["h1"]}</h1>
  <p class="lead">{inline(head.get("intro", ""))}</p>
  <div class="meta">{inline(head.get("meta", ""))}</div>

  <nav class="toc">
{toc}
  </nav>

{body}

  <footer>{LANGS[LANG]["footer"]}</footer>
</div>
"""


def main() -> int:
    global LANG, SRC, DST
    if len(sys.argv) > 1:
        if sys.argv[1] not in LANGS:
            print(f"язык {sys.argv[1]!r} неизвестен; есть: {', '.join(LANGS)}",
                  file=sys.stderr)
            return 2
        LANG = sys.argv[1]
        SRC = LANGS[LANG]["src"]
        DST = LANGS[LANG]["dst"]
    if not os.path.exists(SRC):
        print(f"нет исходного документа: {SRC}", file=sys.stderr)
        return 2
    out = build()
    with open(DST, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"собрано: {DST} ({len(out) // 1024} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
