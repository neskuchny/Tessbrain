# -*- coding: utf-8 -*-
"""Брендовые схемы (§9 фаза 3): style-reference + фирменная палитра.

Образцы фирменного стиля (kind=style_ref, до 3, только image/*) уходят
референсами в image-модель с директивой «стиль повтори, содержание — из
данных»; фирменная палитра красит и Pillow-код-рендер."""
import base64

import backend.core.board.brand_assets as ba
from backend.core.board.meeting_infographic import build_infographic_prompt

_UID = "11111111-1111-4111-8111-111111111111"
_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepngdata").decode()


def _isolate(monkeypatch, tmp_path):
    d = tmp_path / "assets"
    d.mkdir()
    monkeypatch.setattr(ba, "_dir", lambda uid: str(d))
    monkeypatch.setattr(ba, "_index_path",
                        lambda uid: str(tmp_path / "index.json"))


def test_style_ref_cap_three_and_bytes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ids = []
    for i in range(4):  # 4-я загрузка вытесняет старейшую
        rec = ba.save_asset(_UID, "style_ref", f"ref{i}.png", _PNG,
                            content_type="image/png", now=1000.0 + i)
        assert rec, f"save {i} failed"
        ids.append(rec["asset_id"])
    refs = [x for x in ba.list_assets(_UID) if x["kind"] == "style_ref"]
    assert len(refs) == 3
    assert ids[0] not in [x["asset_id"] for x in refs]   # старейший вылетел

    got = ba.get_style_ref_bytes(_UID, limit=2)
    assert len(got) == 2
    assert all(b.startswith(b"\x89PNG") for b, _ct in got)


def test_style_ref_skips_non_image(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    ba.save_asset(_UID, "style_ref", "style.svg", _PNG,
                  content_type="image/svg+xml", now=1.0)
    assert ba.get_style_ref_bytes(_UID) == []   # SVG референсом не годится
    # логотип не попадает в style-референсы
    ba.save_asset(_UID, "logo", "logo.png", _PNG,
                  content_type="image/png", now=2.0)
    assert ba.get_style_ref_bytes(_UID) == []


def test_prompt_gets_style_ref_directive():
    data = {"title": "Итоги", "branches": [{"label": "A", "items": ["x"]}],
            "_style_ref": True}
    p = build_infographic_prompt(data, lang="ru")
    assert "ФИРМЕННОГО СТИЛЯ" in p and "НЕ копируй" in p
    p_en = build_infographic_prompt({**data}, lang="en")
    assert "STYLE reference" in p_en
    # без референсов директивы нет
    p_off = build_infographic_prompt({"title": "И", "branches": []}, lang="ru")
    assert "ФИРМЕННОГО СТИЛЯ" not in p_off


def test_code_render_uses_brand_palette():
    from backend.core.board.meeting_infographic import render_infographic_png
    data = {"title": "Итоги", "branches": [
        {"label": "Ветка", "items": ["пункт"]}],
        "_brand": {"palette": ["#112233", "#AABBCC"]}}
    png = render_infographic_png(data, seed=42, lang="ru")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"      # реальный PNG собрался
    # невалидные цвета не ломают рендер (палитра остаётся вариативной)
    data_bad = {"title": "И", "branches": [{"label": "B", "items": ["y"]}],
                "_brand": {"palette": ["красный", "#GGHHII"]}}
    assert render_infographic_png(data_bad, seed=42,
                                  lang="ru")[:8] == b"\x89PNG\r\n\x1a\n"
