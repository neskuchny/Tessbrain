# Contributing to Tessbrain

Thanks for considering it. A few things that make a pull request easy to
accept.

## Before you write code

Open an issue first for anything larger than a bug fix. It saves you from
building something we were about to change.

## The bar

- **Tests come with the change.** Run them with `python tests/unit/<name>.py`
  — the suite is dependency-free on purpose, so a fresh clone can verify
  itself without installing anything.
- **A benchmark claim needs a reproducible run.** If a change moves a
  published number, say which command produced the new one.
- **No ranking logic in benchmark scripts.** Rules belong in product
  modules behind kill-switches. There is a contract test that fails if a
  ranking rule reappears in `scripts/brainbench_run.py`; it exists because
  we made that mistake once and published a number we had to correct
  downward.
- **New behaviour that changes what a search returns ships off by
  default**, behind an environment flag, until it is measured.

## Style

Match the file you are editing. Comments explain constraints the code
cannot show — not what the next line does.
