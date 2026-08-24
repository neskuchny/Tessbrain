#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выпуск офлайн-лицензий Tessbrain (у ПОСТАВЩИКА, не у клиента).

  keygen                       — пара ключей Ed25519 (privkey держим в
                                 сейфе; pubkey → backend/core/licensing.py
                                 или env TESSENT_LICENSE_PUBKEY образа)
  sign --priv <b64> --org "ООО Ромашка" --deployment romashka-prod \
       --plan enterprise_onprem --seats 5000 --expires 2027-07-01
                                 — печатает токен для TESSENT_LICENSE
  verify --token <...>         — локальная проверка (нужен pubkey в env)
"""
from __future__ import annotations

import argparse
import base64
import json
import sys


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def keygen() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    print("PRIVATE (в сейф, НЕ в репозиторий):", _b64e(priv_raw))
    print("PUBLIC  (в licensing.py / env):    ", _b64e(pub_raw))


def sign(args: argparse.Namespace) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    payload = {
        "org": args.org,
        "deployment_id": args.deployment,
        "plan": args.plan,
        "seats": args.seats,
        "expires_at": args.expires + "T23:59:59+00:00",
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False,
                               sort_keys=True).encode("utf-8")
    priv = Ed25519PrivateKey.from_private_bytes(
        base64.urlsafe_b64decode(args.priv + "=" * (-len(args.priv) % 4)))
    sig = priv.sign(payload_bytes)
    print(f"{_b64e(payload_bytes)}.{_b64e(sig)}")


def verify(args: argparse.Namespace) -> None:
    sys.path.insert(0, ".")
    from backend.core.licensing import check_license
    print(json.dumps(check_license(args.token), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    sp = sub.add_parser("sign")
    sp.add_argument("--priv", required=True)
    sp.add_argument("--org", required=True)
    sp.add_argument("--deployment", required=True)
    sp.add_argument("--plan", default="enterprise_onprem")
    sp.add_argument("--seats", type=int, default=5000)
    sp.add_argument("--expires", required=True, help="YYYY-MM-DD")
    vp = sub.add_parser("verify")
    vp.add_argument("--token", required=True)
    args = ap.parse_args()
    {"keygen": lambda a: keygen(), "sign": sign, "verify": verify}[args.cmd](args)


if __name__ == "__main__":
    main()
