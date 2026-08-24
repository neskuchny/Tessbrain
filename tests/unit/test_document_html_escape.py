# -*- coding: utf-8 -*-
"""Сырой HTML из встречи не должен доезжать до content_html.

Документы собираются из содержимого встреч, а встречи пишут люди.
`_markdown_to_html` раскладывал markdown регулярками и НИЧЕГО не
экранировал — значит `<img src=x onerror=…>`, сказанный на созвоне,
доезжал до поля content_html как настоящий тег, а фронт выводит это поле
через dangerouslySetInnerHTML. Код выполнялся у каждого, кто открыл
документ.

Проверяем обе стороны: опасное экранируется, обычный markdown продолжает
размечаться. Функция берётся из живого файла (вырезаем метод и исполняем),
чтобы не тащить зависимости всего агента.

Запуск:  python tests/unit/test_document_html_escape.py
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "backend", "core", "documents", "document_writer_agent.py")

# Любой ОТКРЫВАЮЩИЙ тег, кроме тех, что регулярки разметки создают сами.
_ALLOWED = {"p", "h1", "h2", "h3", "strong", "em", "li", "blockquote", "hr"}
_TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)")


def load_converter():
    src = io.open(SRC, encoding="utf-8").read()
    start = src.index("    def _markdown_to_html")
    end = src.index("    def _generate_title")
    ns: dict = {}
    exec("class _Holder:\n" + src[start:end], ns)  # noqa: S102 — свой же исходник
    return ns["_Holder"]()._markdown_to_html


def stray_tags(html: str) -> list[str]:
    """Теги в выводе, которых разметка сама породить не могла."""
    return sorted({t.lower() for t in _TAG_RE.findall(html)} - _ALLOWED)


def main() -> int:
    convert = load_converter()
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(name)

    print("сырой HTML из встречи не превращается в теги:")
    dangerous = [
        "<script>alert(1)</script>",
        "текст <img src=x onerror=alert(1)>",
        '<iframe src="javascript:alert(1)"></iframe>',
        '<a href="javascript:alert(1)">клик</a>',
        '<svg onload="alert(1)"></svg>',
        '<div style="background:url(javascript:alert(1))">t</div>',
        "<style>body{display:none}</style>",
        "<form action=/x><input name=q></form>",
    ]
    for raw in dangerous:
        out = convert(raw)
        left = stray_tags(out)
        check(raw[:44], not left, f"остались теги {left}: {out[:80]}")

    print("а исходный текст при этом сохраняется читаемым:")
    out = convert("текст <img src=x onerror=alert(1)>")
    check("содержимое видно как текст", "&lt;img" in out and "текст" in out, out[:80])
    check("угловые скобки экранированы", "<img" not in out, out[:80])

    print("настоящий markdown продолжает размечаться:")
    for md, tag in (
        ("# Заголовок", "<h1>"),
        ("## Раздел", "<h2>"),
        ("### Пункт", "<h3>"),
        ("**жирно**", "<strong>"),
        ("*курсив*", "<em>"),
        ("- пункт списка", "<li>"),
        ("> цитата", "<blockquote>"),
        ("---", "<hr>"),
    ):
        out = convert(md)
        check(f"{md} → {tag}", tag in out, out[:60])

    print("амперсанд не ломается дважды:")
    out = convert("Иванов & Партнёры")
    check("& → &amp; ровно один раз", "&amp;" in out and "&amp;amp;" not in out, out[:60])

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
