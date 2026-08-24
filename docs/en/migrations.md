# Migrations

Schema changes are plain SQL files applied by a runner that records what it has
already done.

`backend/db/migrate.py` · migration files live under `backend/db/migrations/`
(71 at the time of writing)

---

## Applying

```bash
python -m backend.db.migrate
```

| Flag | Effect |
|---|---|
| `--list` | show discovered migrations and their checksums, then exit |
| `--dry-run` | resolve what would run without applying |
| `--target <version>` | apply up to and including a version |
| `--dsn <dsn>` | override the connection string from settings |
| `--verbose` / `-v` | debug logging |

> There is no `apply` subcommand — running the module *is* the apply. Older
> internal notes show `migrate apply`; that form fails with an unrecognised
> argument.

## How it protects itself

- **Applied versions are recorded** in a migrations table, so a rerun is a
  no-op rather than a double-apply.
- **Files are checksummed.** Editing a migration that has already run is
  detected instead of silently diverging between environments.
- **Insertion is conflict-safe** (`ON CONFLICT (version) DO NOTHING`), so two
  processes racing to migrate cannot corrupt the ledger.

## Writing one

1. Add `backend/db/migrations/<NNN>_<short_name>.sql`.
2. Make it idempotent where you reasonably can (`IF NOT EXISTS`).
3. Verify with `--dry-run`, then `--list` to confirm the checksum.
4. Never edit a migration that has shipped — add a new one.

## In deployment

The single-VPS compose file runs a dedicated `migrate` service that applies the
schema and exits before the API starts, so the API never serves against a
half-migrated database.
