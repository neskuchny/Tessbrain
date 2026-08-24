"""W41: SQL migration safety linter.

Сканирует `backend/db/migrations/*.sql` на dangerous-pattern'ы которые
ломают rolling deploy / zero-downtime updates:

- DROP TABLE / DROP COLUMN — старый код всё ещё может писать туда
- ALTER TABLE ... ALTER COLUMN ... TYPE — full rewrite + lock
- ALTER TABLE ... SET NOT NULL без backfill — упадёт на existing nulls
- ALTER TABLE ... ADD COLUMN ... NOT NULL без DEFAULT — то же
- CREATE INDEX без CONCURRENTLY — Postgres lock'ает таблицу
- RENAME TABLE / RENAME COLUMN — старый код использует старое имя

Как использовать:
    python -m backend.db.migration_lint              # все файлы
    python -m backend.db.migration_lint <file.sql>   # один файл
    python -m backend.db.migration_lint --strict     # warning тоже = fail

Marker comment чтобы override:
    -- LINT-IGNORE: drop_table_safe (объяснение почему OK)

Дизайн:
- Чисто regex-based, никакой полный SQL parser. Достаточно для catch
  90% случаев + low false-positive
- Каждое правило → код типа `D001` для grep-friendly suppression
- Severity: error / warning. По default error блокирует CI, warning
  только сообщает (можно поднять до error через --strict)
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Severity levels
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

LINT_IGNORE_RE = re.compile(r"--\s*LINT-IGNORE:\s*(\w+)", re.IGNORECASE)


@dataclass
class Finding:
    code: str
    severity: str
    line: int
    statement: str
    message: str
    suggestion: str = ""

    def format(self, file: Path) -> str:
        out = f"{file}:{self.line}: [{self.severity.upper()}] {self.code} {self.message}"
        if self.suggestion:
            out += f"\n    → {self.suggestion}"
        if self.statement:
            short = self.statement[:140].replace("\n", " ")
            out += f"\n    statement: {short}"
        return out


@dataclass
class Rule:
    code: str
    severity: str
    pattern: re.Pattern
    message: str
    suggestion: str
    # Если эта строка содержится в матче — игнорируем (например "ADD COLUMN ... DEFAULT").
    skip_if: list[re.Pattern] = None  # type: ignore[assignment]


# === Rules ==================================================================
# Каждый pattern применяется к каждому SQL statement (split по ';').

_RULES: list[Rule] = [
    Rule(
        code="D001",
        severity=SEVERITY_ERROR,
        pattern=re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
        message="DROP TABLE is unsafe during rolling deploy — старый код может всё ещё писать в таблицу",
        suggestion=(
            "Сделай несколько шагов: 1) удали все код-ссылки и задеплой, "
            "2) ALTER TABLE ... RENAME TO _deprecated_X, 3) после observation "
            "period (1-7 дней) — DROP. Если уверен что таблица не используется, "
            "добавь -- LINT-IGNORE: D001 с обоснованием."
        ),
    ),
    Rule(
        code="D002",
        severity=SEVERITY_ERROR,
        pattern=re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE),
        message="DROP COLUMN ломает старый код",
        suggestion=(
            "Сначала задеплой код без чтения колонки, потом отдельной миграцией "
            "DROP. Или: ALTER COLUMN ... DROP NOT NULL + ignore в коде, потом DROP."
        ),
    ),
    Rule(
        code="D003",
        severity=SEVERITY_ERROR,
        pattern=re.compile(
            r"\bALTER\s+TABLE\s+\S+\s+ALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b",
            re.IGNORECASE,
        ),
        message="ALTER COLUMN TYPE требует full table rewrite + AccessExclusiveLock",
        suggestion=(
            "Add new column → backfill batch'ами → swap (UPDATE ... SET old=new) → "
            "drop old (отдельной миграцией). Для small таблиц (<10K rows) — OK с "
            "-- LINT-IGNORE: D003"
        ),
    ),
    Rule(
        code="D004",
        severity=SEVERITY_WARNING,
        pattern=re.compile(
            r"\bALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+[^\n;]+\bNOT\s+NULL\b",
            re.IGNORECASE,
        ),
        message="ADD COLUMN ... NOT NULL без DEFAULT упадёт на existing rows",
        suggestion=(
            "Используй pattern: ADD COLUMN nullable → UPDATE batch'ами → "
            "ALTER COLUMN SET NOT NULL. Или ADD COLUMN ... NOT NULL DEFAULT <value>."
        ),
        skip_if=[
            re.compile(r"\bDEFAULT\b", re.IGNORECASE),
        ],
    ),
    Rule(
        code="D005",
        severity=SEVERITY_WARNING,
        pattern=re.compile(
            r"\bALTER\s+TABLE\s+\S+\s+ALTER\s+COLUMN\s+\S+\s+SET\s+NOT\s+NULL\b",
            re.IGNORECASE,
        ),
        message="SET NOT NULL без проверки existing nulls упадёт",
        suggestion=(
            "Перед этим в этой же миграции (или предыдущей) сделай UPDATE для "
            "очистки nulls + добавь CHECK constraint NOT VALID + VALIDATE CONSTRAINT."
        ),
    ),
    Rule(
        code="D006",
        severity=SEVERITY_WARNING,
        pattern=re.compile(r"\bCREATE\s+INDEX\b", re.IGNORECASE),
        message="CREATE INDEX без CONCURRENTLY locks таблицу",
        suggestion=(
            "Используй CREATE INDEX CONCURRENTLY. Минусы: нельзя в transaction, "
            "и если Postgres падает — индекс остаётся INVALID и его надо REINDEX."
        ),
        skip_if=[
            re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE),
            # CREATE INDEX внутри CREATE TABLE statement — OK, таблица только что создана.
            re.compile(r"\bIF\s+NOT\s+EXISTS\b", re.IGNORECASE),
        ],
    ),
    Rule(
        code="D007",
        severity=SEVERITY_ERROR,
        pattern=re.compile(r"\bRENAME\s+(?:TABLE|TO|COLUMN)\b", re.IGNORECASE),
        message="RENAME TABLE/COLUMN ломает запросы старого кода",
        suggestion=(
            "Используй pattern: 1) ADD new column/view с новым именем, 2) backfill, "
            "3) deploy код использующий новое имя, 4) после observation — DROP старое."
        ),
    ),
    Rule(
        code="D008",
        severity=SEVERITY_WARNING,
        pattern=re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
        message="TRUNCATE — деструктивно, не имеет undo",
        suggestion=(
            "Уверен? Если да — добавь -- LINT-IGNORE: D008 с обоснованием. "
            "Иначе используй DELETE с WHERE."
        ),
    ),
]


# === Statement splitting ====================================================
# Простой split по ';' с уважением к функциональным $$ блокам.

_DOLLAR_BLOCK_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
# Line comments `-- ...` до конца строки. Block comments /* ... */
# редко встречаются в наших миграциях, но на всякий тоже выпиливаем.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(sql: str) -> str:
    """Удалить line-comments (-- ...) и block-comments (/* ... */).

    Важно для statement splitting: rollback блоки в наших миграциях
    обычно идут как `-- DROP TABLE ...` и не должны триггерить linter.
    """
    sql = _BLOCK_COMMENT_RE.sub("", sql)
    out_lines: list[str] = []
    for line in sql.splitlines():
        # `--` срезаем но сохраняем перевод строки чтобы line numbers не сбить.
        idx = line.find("--")
        if idx >= 0:
            out_lines.append(line[:idx])
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def split_statements(sql: str) -> list[tuple[int, str]]:
    """Вернуть [(line_number, statement)]. Уважает $$ ... $$ блоки.

    Comments удаляются до split'а, line numbers сохраняются.
    """
    sql = _strip_comments(sql)
    # Заменяем $$ ... $$ на placeholder чтобы не split'ить внутри.
    placeholders: dict[str, str] = {}

    def _stash(m):
        key = f"__DOLLAR_BLOCK_{len(placeholders)}__"
        placeholders[key] = m.group(0)
        return key

    safe = _DOLLAR_BLOCK_RE.sub(_stash, sql)

    out: list[tuple[int, str]] = []
    line = 1
    buf: list[str] = []
    for ch in safe:
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                # Восстанавливаем dollar-блоки.
                for k, v in placeholders.items():
                    stmt = stmt.replace(k, v)
                out.append((line, stmt))
            buf = []
        else:
            if ch == "\n":
                line += 1
            buf.append(ch)
    # Trailing без ';'
    tail = "".join(buf).strip()
    if tail:
        for k, v in placeholders.items():
            tail = tail.replace(k, v)
        out.append((line, tail))
    return out


def _file_ignores(raw_content: str) -> set[str]:
    """Все `-- LINT-IGNORE: CODE` маркеры из файла применяются глобально.

    Statement-level granularity не делаем чтобы не усложнять — rollback
    блок и так работает (комментарии полностью strip'аются перед
    pattern match), а явные suppressions редки и удобнее ставить в шапке.
    """
    return set(LINT_IGNORE_RE.findall(raw_content) or [])


def lint_sql(content: str) -> list[Finding]:
    findings: list[Finding] = []
    file_ignores = _file_ignores(content)
    for line, stmt in split_statements(content):
        for rule in _RULES:
            if rule.code in file_ignores:
                continue
            if not rule.pattern.search(stmt):
                continue
            if rule.skip_if:
                if any(p.search(stmt) for p in rule.skip_if):
                    continue
            findings.append(Finding(
                code=rule.code,
                severity=rule.severity,
                line=line,
                statement=stmt,
                message=rule.message,
                suggestion=rule.suggestion,
            ))
    return findings


def lint_file(path: Path) -> list[Finding]:
    if not path.exists():
        return []
    return lint_sql(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lint SQL migrations for zero-downtime safety (W41).",
    )
    parser.add_argument(
        "files", nargs="*", type=Path,
        help="SQL files (default: all migrations dir)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="treat WARNING as ERROR (CI fail on any finding)",
    )
    parser.add_argument(
        "--migrations-dir", type=Path,
        default=Path(__file__).resolve().parent / "migrations",
        help="default scan dir",
    )
    args = parser.parse_args()

    files: list[Path]
    if args.files:
        files = list(args.files)
    else:
        files = sorted(args.migrations_dir.glob("*.sql"))

    if not files:
        print("no SQL files to lint", file=sys.stderr)
        return 0

    total_errors = 0
    total_warnings = 0
    for f in files:
        findings = lint_file(f)
        for fnd in findings:
            print(fnd.format(f), file=sys.stderr)
            if fnd.severity == SEVERITY_ERROR:
                total_errors += 1
            else:
                total_warnings += 1

    print(
        f"\n{len(files)} file(s) scanned: "
        f"{total_errors} error(s), {total_warnings} warning(s)",
        file=sys.stderr,
    )

    if total_errors > 0:
        return 1
    if args.strict and total_warnings > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
