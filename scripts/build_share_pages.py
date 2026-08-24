#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Версии страниц для отправки: чтобы язык не превратился в кракозябры.

Исходные страницы лежат фрагментами — без <!DOCTYPE>, <html> и <head>.
Так и надо: при публикации артефактом обёртку добавляет сам сервис, и
свои теги там были бы лишними. Но у файла, который отправляют письмом,
кладут на диск или открывают из мессенджера, обёртки нет — и всплывают
две беды:

1. КОДИРОВКА. Браузер определяет её по порядку: метка BOM в начале файла →
   заголовок сервера → <meta charset> в первом килобайте. Почтовые
   клиенты и файлообменники сплошь и рядом отдают файл с чужим
   заголовком (windows-1251), и тогда <meta charset> проигрывает — весь
   русский текст превращается в «Ð¼Ð¾Ð·Ð³». Побеждает это только BOM:
   он стоит выше заголовка сервера.

2. РЕЖИМ СОВМЕСТИМОСТИ. Без <!DOCTYPE> браузер разбирает страницу по
   правилам двадцатилетней давности — отступы и размеры плывут.

Сборщик заворачивает каждый фрагмент в полноценный документ: BOM,
DOCTYPE, lang="ru", charset первой строкой головы, плюс http-equiv для
совсем старых клиентов. Плюс собирает страницу-оглавление, чтобы папку
можно было отдать целиком.

Исходники при этом не трогаются — они остаются фрагментами для
публикации артефактом.

Запуск:  python scripts/build_share_pages.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(DOCS, "share")   # внутри — подпапки по языкам

# Двуязычно: у каждого языка свой источник и свои подписи в оглавлении.
# Английский набор — то, что выкладывается в опенсорс.
LANGS = {
    "ru": {
        "title": "Tessbrain — материалы",
        "eyebrow": "Tessbrain · август 2026",
        "h1": "Цифровой мозг компании",
        "lead": ("Четыре страницы о продукте. Каждая открывается сама по себе — "
                 "ничего скачивать не нужно, интернет не требуется."),
        "foot": ("Страницы самодостаточны: шрифты и стили внутри файлов, "
                 "внешних загрузок нет. Открываются двойным кликом в любом браузере; "
                 "можно переслать письмом или положить в общую папку."),
        "other": ("en", "English version →"),
        "pages": [
            ("tessent_capabilities.html", "Карта возможностей",
             "Что умеет цифровой мозг компании: 22 раздела, от графовой памяти "
             "до честных границ."),
            ("tessent_article.html", "Что такое Tessbrain",
             "Объяснение с живыми примерами: разбор встречи по шагам, где обычный "
             "поиск теряет ответ, откуда берётся уверенность."),
            ("tessent_map.html", "Схема контура",
             "Движущаяся карта: вход, разбор, память, граница доступа, работа "
             "и возврат опыта."),
            ("tessent_cost.html", "Расчёт стоимости",
             "Поставьте свои условия — страница посчитает из наших измерений."),
        ],
    },
    "en": {
        "title": "Tessbrain — materials",
        "eyebrow": "Tessbrain · August 2026",
        "h1": "The company brain",
        "lead": ("Four pages about the product. Each one opens on its own — "
                 "nothing to download, no internet required."),
        "foot": ("These pages are self-contained: fonts and styles live inside the "
                 "files, with no external loads. Open them with a double click in "
                 "any browser; mail them or drop them in a shared folder."),
        "other": ("ru", "Русская версия →"),
        "pages": [
            ("tessent_capabilities.html", "Capability map",
             "What the company brain does: 22 sections, from graph memory to "
             "honest limits."),
            ("tessent_article.html", "What Tessbrain is",
             "An explanation with live examples: a meeting taken apart step by "
             "step, where ordinary search loses the answer, where confidence "
             "comes from."),
            ("tessent_map.html", "The loop diagram",
             "A moving map: intake, extraction, memory, the access boundary, "
             "work and the experience loop."),
            ("tessent_cost.html", "Cost calculator",
             "Set your own conditions — the page computes from our measurements."),
        ],
    },
}

BOM = "﻿"


def title_of(src: str, fallback: str) -> str:
    m = re.search(r"<title>(.*?)</title>", src, re.S)
    return m.group(1).strip() if m else fallback


def wrap(fragment: str, title: str, lang: str = "ru") -> str:
    """Фрагмент → самодостаточный документ.

    Порядок в голове важен: charset обязан попасть в первый килобайт,
    иначе браузер решает кодировку раньше, чем до него дочитает. Поэтому
    charset идёт ПЕРВЫМ, до любых стилей и тем более до вшитого шрифта
    (он весит триста килобайт и вытолкнул бы метку за границу).
    """
    # у фрагмента свои <meta charset> и <title> — убираем, чтобы не
    # дублировать: второй charset ниже по файлу уже ничего не решает,
    # а дублированный заголовок сбивает вкладку.
    body = re.sub(r'^\s*<meta charset=[^>]*>\s*', "", fragment, count=1)
    body = re.sub(r"<title>.*?</title>\s*", "", body, count=1, flags=re.S)
    return (
        BOM
        + "<!DOCTYPE html>\n"
        + f'<html lang="{lang}">\n<head>\n'
        + '<meta charset="utf-8">\n'
        + '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
        + '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + f"<title>{title}</title>\n"
        + "</head>\n<body>\n"
        + body.strip()
        + "\n</body>\n</html>\n"
    )


INDEX_CSS = """
  :root{
    /* Светлая книга — фиксированная палитра, как и на самих страницах */
    --bg:#f3f1ea;--surface:#fff;--surface-2:#faf8f2;--border:#ddd8cb;
    --border-soft:#e7e3d7;--text:#211f1a;--dim:#5c584e;--faint:#8b8677;
    --accent:#a97a2c;--accent-soft:rgba(169,122,44,.10);
    --serif:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',ui-serif,serif;
    --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}
  *{box-sizing:border-box}
  body{margin:0;padding:76px 24px 96px;background:var(--bg);color:var(--text);
    font-family:var(--sans);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
  .page{max-width:720px;margin:0 auto}
  .eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--accent);font-weight:700;margin-bottom:16px}
  h1{font-family:var(--serif);font-weight:600;font-size:clamp(32px,5vw,48px);
    margin:0 0 16px;line-height:1.12;letter-spacing:-.01em;text-wrap:balance}
  .lead{font-family:var(--serif);font-size:clamp(18px,2.2vw,22px);color:var(--dim);
    line-height:1.5;margin:0 0 44px;font-style:italic;max-width:40ch}
  .idx{display:flex;flex-direction:column;gap:0;border-top:1px solid var(--border)}
  a.card{display:flex;gap:20px;align-items:baseline;text-decoration:none;
    padding:22px 6px;border-bottom:1px solid var(--border);transition:padding-left .18s,background .18s}
  a.card:hover{padding-left:14px;background:linear-gradient(90deg,var(--accent-soft),transparent 60%)}
  a.card .no{font-family:var(--serif);font-size:15px;color:var(--accent);font-weight:600;
    min-width:26px;font-variant-numeric:tabular-nums;letter-spacing:.02em}
  a.card .body b{display:block;font-family:var(--serif);font-size:20px;color:var(--text);
    font-weight:600;margin-bottom:5px;line-height:1.2}
  a.card:hover .body b{color:var(--accent)}
  a.card .body span{font-size:13.5px;color:var(--dim);line-height:1.55}
  a.card .arr{margin-left:auto;color:var(--faint);font-size:18px;align-self:center;
    transition:transform .18s,color .18s}
  a.card:hover .arr{transform:translateX(4px);color:var(--accent)}
  .lang{margin-top:30px;font-size:13.5px}\n  .lang a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(169,122,44,.35)}\n  .lang a:hover{border-bottom-color:var(--accent)}\n  footer{margin-top:26px;padding-top:24px;border-top:1px solid var(--border);
    font-size:12.5px;color:var(--faint);line-height:1.8;max-width:60ch}
"""

_ROMAN = ["I", "II", "III", "IV", "V", "VI"]


def build_index(lang: str) -> str:
    L = LANGS[lang]
    other_lang, other_label = L["other"]
    cards = "\n".join(
        f'    <a class="card" href="{fn}">'
        f'<span class="no">{_ROMAN[i]}</span>'
        f'<span class="body"><b>{t}</b><span>{d}</span></span>'
        f'<span class="arr">&rarr;</span></a>'
        for i, (fn, t, d) in enumerate(L["pages"]))
    return (
        BOM
        + "<!DOCTYPE html>\n"
        + f'<html lang="{lang}">\n<head>\n'
        + '<meta charset="utf-8">\n'
        + '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\n'
        + '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + f'<title>{L["title"]}</title>\n'
        + f"<style>{INDEX_CSS}</style>\n"
        + '</head>\n<body>\n<div class="page">\n'
        + f'  <div class="eyebrow">{L["eyebrow"]}</div>\n'
        + f'  <h1>{L["h1"]}</h1>\n'
        + f'  <p class="lead">{L["lead"]}</p>\n'
        + '  <div class="idx">\n' + cards + "\n  </div>\n"
        + f'  <p class="lang"><a href="../{other_lang}/index.html">{other_label}</a></p>\n'
        + f'  <footer>{L["foot"]}</footer>\n'
        + "</div>\n</body>\n</html>\n"
    )


def build_lang(lang: str) -> list:
    """Собрать один язык: docs/share/<lang>/. Возвращает список (файл, размер)."""
    out_dir = os.path.join(OUT, lang)
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for fn, t, _d in LANGS[lang]["pages"]:
        # английские страницы уже лежат переведёнными в docs/share/en;
        # русские собираются из docs/*.html
        src_path = (os.path.join(DOCS, fn) if lang == "ru"
                    else os.path.join(out_dir, fn))
        if lang == "en" and fn == "tessent_capabilities.html":
            src_path = os.path.join(DOCS, "tessent_capabilities_en.html")
        if not os.path.exists(src_path):
            print(f"пропущено (нет файла): {lang}/{fn}", file=sys.stderr)
            continue
        src = open(src_path, encoding="utf-8").read()
        # уже завёрнутую страницу второй раз не заворачиваем
        out = src if src.lstrip("\ufeff").startswith("<!DOCTYPE") else wrap(
            src, title_of(src, t), lang)
        dst = os.path.join(out_dir, fn)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(out)
        made.append((fn, len(out)))
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index(lang))
    return made


def main() -> int:
    langs = sys.argv[1:] or list(LANGS)
    for lang in langs:
        if lang not in LANGS:
            print(f"язык {lang!r} неизвестен; есть: {', '.join(LANGS)}",
                  file=sys.stderr)
            return 2
        made = build_lang(lang)
        print(f"{lang}: собрано в {os.path.join(OUT, lang)}")
        for fn, size in made:
            print(f"  {fn:34s} {size // 1024} КБ")
        print(f"  {'index.html':34s} оглавление")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
