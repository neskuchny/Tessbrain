# -*- coding: utf-8 -*-
"""Временные файлы чата: чужой не читается, без авторизации не кладётся.

ЧТО БЫЛО. Ручка /agents/chat-upload не смотрела Authorization вообще —
принимала до 100 МБ от кого угодно и гоняла на них парсер офисных форматов
(docx/xlsx — это zip-архивы, распаковка «маленького» файла может занять
гигабайты). Бесплатный процессор и диск по запросу из интернета.

Хуже другое: файлы лежали ОДНОЙ ОБЩЕЙ КУЧЕЙ (`data/chat_uploads/<id>.txt`)
и читались в диалог просто по идентификатору, без всякой привязки к
владельцу. Десять hex-символов — это не право доступа, а надежда, что не
угадают. Узнал чужой id — втянул чужой файл в свой чат.

Теперь владелец зашит в путь, и чужое недостижимо по построению.

Проверяем две вещи: логику раскладки по папкам (вживую, на функции) и
структурные инварианты роутов (по исходнику — поднять litestar здесь
нечем). Второе честно проверяется как «в коде стоит именно это», а не как
поведение под нагрузкой.

Запуск:  python tests/unit/test_chat_upload_isolation.py
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENTS = os.path.join(ROOT, "backend", "api", "routes", "agents.py")


def load_dir_fn():
    """Вырезать _chat_upload_dir из живого файла и сделать вызываемым.

    Возвращает (функция|None, исходник). None означает, что раскладки по
    владельцам в коде нет вовсе — это провал проверки, а не поломка стенда,
    поэтому падать трейсбеком тут нельзя.
    """
    src = io.open(AGENTS, encoding="utf-8").read()
    if "def _chat_upload_dir" not in src or "\n_CHAT_UPLOAD_MAX_BYTES" not in src:
        return None, src
    start = src.index("def _chat_upload_dir")
    end = src.index("\n_CHAT_UPLOAD_MAX_BYTES", start)
    ns: dict = {}
    exec(src[start:end], ns)
    return ns["_chat_upload_dir"], src


def main() -> int:
    chat_upload_dir, src = load_dir_fn()
    failures: list[str] = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(name)

    if chat_upload_dir is None:
        print("  ПЛОХО раскладки временных файлов по владельцам нет — "
              "файлы лежат общей кучей и читаются по идентификатору")
        return 1

    print("раскладка по владельцам:")
    a = chat_upload_dir("user-aaa")
    b = chat_upload_dir("user-bbb")
    check("у разных пользователей разные папки", a != b, f"{a} == {b}")
    check("владелец присутствует в пути", "user-aaa" in str(a), str(a))
    check("один и тот же пользователь → одна папка",
          chat_upload_dir("user-aaa") == a)

    print("идентификатор пользователя обеззараживается:")
    for bad, why in (
        ("../../etc", "выход вверх по дереву"),
        ("a/b", "слэш внутри"),
        ("..", "только точки"),
        ("a\\b", "обратный слэш"),
        ("a\x00b", "нулевой байт"),
    ):
        d = chat_upload_dir(bad)
        s = str(d) if d is not None else ""
        ok = d is None or (".." not in s and "\x00" not in s
                           and s.count("chat_uploads") == 1
                           and not s.rstrip("/").endswith("chat_uploads"))
        check(f"{why}: {bad!r}", ok, s)

    check("пустой идентификатор → папки нет вовсе",
          chat_upload_dir("") is None and chat_upload_dir("!!!") is None)

    print("структура роутов (по исходнику):")

    up = src[src.index("@post(\"/chat-upload\")"):]
    up = up[:up.index("# === Файлы креативной студии Mark")]

    check("chat_upload принимает Authorization",
          "authorization: Optional[str] = Parameter(header=\"Authorization\"" in up)
    check("без пользователя — отказ до всякой работы",
          re.search(r"uid = _caller_user_id\(authorization\)\s*\n\s*if not uid:\s*\n\s*return", up)
          is not None)
    check("пишет в папку пользователя, а не в общую",
          "_chat_upload_dir(uid)" in up and 'Path("data") / "chat_uploads"' not in up)
    check("есть свой лимит на размер до разбора",
          "_CHAT_UPLOAD_MAX_BYTES" in up and "len(blob) >" in up)
    check("лимит уже общих 100 МБ",
          "25 * 1024 * 1024" in src)

    read = src[src.index("temp_context_text = \"\""):]
    read = read[:read.index("temp_count += 1") + 40]
    check("чтение идёт из папки пользователя",
          "_chat_upload_dir(context.get(\"user_id\")" in read)
    check("общий путь в чтении не остался",
          '"chat_uploads" / f"{safe}.txt"' not in read
          and 'Path("data") / "chat_uploads"' not in read)
    check("имя файла по-прежнему обрезается от путей",
          "Path(str(fid)).name" in read)

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
