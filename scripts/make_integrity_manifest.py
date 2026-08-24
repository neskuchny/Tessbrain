#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка подписанного манифеста целостности ядра (у ПОСТАВЩИКА, на сборке).

Считает sha256 всех crown-jewel модулей, пишет
deploy/onprem/integrity_manifest.json и печатает Ed25519-подпись для
TESSENT_INTEGRITY_SIG (тот же приватный ключ, что у лицензии).

  python scripts/make_integrity_manifest.py --priv <license_privkey_b64>

Запускать ПОСЛЕ компиляции crown-jewels (чтобы хэши считались по .so),
но ДО финального образа. Манифест и подпись кладутся в образ.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--priv", required=True, help="license Ed25519 privkey (b64)")
    ap.add_argument("--version", default="1")
    args = ap.parse_args()

    sys.path.insert(0, str(_ROOT))
    from backend.core.integrity import compute_hashes

    manifest_file = _ROOT / "deploy" / "onprem" / "integrity_manifest.json"
    jewels = _ROOT / "deploy" / "onprem" / "crown_jewels.txt"
    files = [ln.strip() for ln in jewels.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]

    hashes = compute_hashes(files)
    missing = [f for f in files if f not in hashes]
    if missing:
        print(f"⚠ не найдено (пропущены): {missing}", file=sys.stderr)

    manifest = {"version": args.version, "files": hashes}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False,
                                sort_keys=True).encode("utf-8")
    manifest_file.write_bytes(manifest_bytes)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    priv = Ed25519PrivateKey.from_private_bytes(
        base64.urlsafe_b64decode(args.priv + "=" * (-len(args.priv) % 4)))
    sig = priv.sign(manifest_bytes)

    print(f"Манифест: {manifest_file} ({len(hashes)} модулей)")
    print(f"TESSENT_INTEGRITY_SIG={_b64e(sig)}")


if __name__ == "__main__":
    main()
