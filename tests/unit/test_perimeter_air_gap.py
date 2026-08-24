# -*- coding: utf-8 -*-
"""Закрытый контур: обещание «данные не выходят наружу» под проверкой.

Аудит бизнес-карты нашёл обход: валидатор Settings проверял переменные
окружения при старте, но профиль модели заводится в рантайме и жил в базе —
через него вызов уходил в публичное облако при включённом enterprise_mode.
Личный ключ пользователя (BYOK) обходил контур так же.

Контракты под проверкой:
  1. «внутренний адрес» определён ОДИН раз на весь продукт;
  2. локальные адреса пропускаются, публичные — нет, пустой адрес — нет
     (отказ по умолчанию: «неизвестно куда» = «наружу»);
  3. провайдеры с зашитым облачным SDK не выпускаются никогда;
  4. роутер проверяет периметр ПОСЛЕ резолва профиля — то есть на всех трёх
     путях сразу (явный профиль, личный ключ, умолчание организации);
  5. профиль на внешнее облако не заводится в закрытом контуре — с причиной;
  6. фильтр выхода режет внешние ссылки в закрытом контуре и проходит по
     структурированным ответам, а не только по тексту;
  7. веб-поиск в закрытом контуре не выполняется.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.security"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_p = _load("backend.core.security.perimeter", "backend/core/security/perimeter.py")


def _src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


def _code_lines(src: str) -> str:
    """Только исполняемые строки — чтобы не ловить формулировки комментариев."""
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


# ── 1-2. Определение внутреннего адреса ─────────────────────────────────

def test_internal_addresses_pass():
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000",
                "http://10.1.2.3:8000/v1", "http://192.168.0.5/v1",
                "http://172.16.0.9/v1", "http://vllm:8000/v1",
                "http://ollama.svc.cluster.local/v1", "http://gpu.corp/v1"):
        assert _p.is_internal_url(url), url
    print("✅ локальные адреса признаются внутренними")


def test_public_and_empty_addresses_fail():
    for url in ("https://api.openai.com/v1", "https://api.deepseek.com/v1",
                "https://openrouter.ai/api/v1", "http://8.8.8.8/v1", "", None):
        assert not _p.is_internal_url(url), url
    print("✅ публичный и пустой адрес — не внутренние")


def test_single_definition_of_internal():
    """Правило «внутри контура» не должно быть переписано в config заново."""
    cfg = _code_lines(_src("backend/config.py"))
    assert "from backend.core.security.perimeter import is_internal_url" in cfg
    assert "internal_suffixes = (" not in cfg, (
        "второе определение «внутреннего» в config — источник расхождений"
    )
    print("✅ определение внутреннего адреса одно на весь продукт")


# ── 3. Что уводит запрос наружу ─────────────────────────────────────────

def test_local_target_stays_inside():
    assert _p.llm_target_leaves_perimeter("local_vllm", "http://vllm:8000/v1") is None
    assert _p.llm_target_leaves_perimeter("ollama", "http://10.0.0.5:11434/v1") is None
    print("✅ локальная модель внутри контура пропускается")


def test_cloud_target_blocked_with_reason():
    reason = _p.llm_target_leaves_perimeter("openai", "https://api.openai.com/v1")
    assert reason and "за пределы контура" in reason
    print("✅ облачный адрес отклоняется с причиной словами")


def test_native_sdk_providers_never_pass():
    """Их SDK ходит по зашитому адресу — «направить внутрь» нельзя."""
    for prov in ("gemini", "anthropic", "claude_cli", "codex_cli"):
        reason = _p.llm_target_leaves_perimeter(prov, "http://vllm:8000/v1")
        assert reason, f"{prov} обязан быть отклонён даже с внутренним адресом"
    print("✅ провайдеры с облачным SDK не выпускаются никогда")


def test_unknown_target_fails_closed():
    assert _p.llm_target_leaves_perimeter("", None)
    assert _p.llm_target_leaves_perimeter("some_new_cloud", None), (
        "нет адреса — значит неизвестно куда; в контуре это отказ"
    )
    print("✅ «неизвестно куда» трактуется как «наружу»")


def test_enterprise_mode_read_from_env():
    old = os.environ.get("ENTERPRISE_MODE")
    try:
        os.environ["ENTERPRISE_MODE"] = "true"
        assert _p.enterprise_mode_enabled() is True
        os.environ["ENTERPRISE_MODE"] = "off"
        assert _p.enterprise_mode_enabled() is False
    finally:
        if old is None:
            os.environ.pop("ENTERPRISE_MODE", None)
        else:
            os.environ["ENTERPRISE_MODE"] = old
    print("✅ режим контура читается из окружения")


# ── 4. Роутер: профиль не обходит контур ────────────────────────────────

def test_router_checks_perimeter_after_profile_resolve():
    src = _src("backend/core/llm/router.py")
    fn = src[src.index("async def _maybe_get_active_profile_client"):]
    fn = fn[:fn.index("def _get_client")]
    code = _code_lines(fn)
    assert "_enterprise_mode_on()" in code and "_perimeter_reason(active)" in code, (
        "проверка периметра обязана стоять в резолве профиля"
    )
    # Проверка должна идти ПОСЛЕ выбора клиента (иначе личный ключ и
    # per-request профиль её обойдут) и ДО возврата клиента.
    assert code.index("get_personal_client") < code.index("_perimeter_reason"), (
        "личный ключ (BYOK) обязан попадать под ту же проверку"
    )
    assert code.index("_perimeter_reason") < code.index("return active"), (
        "клиент не должен возвращаться до проверки периметра"
    )
    print("✅ роутер проверяет периметр на всех путях резолва профиля")


def test_router_perimeter_check_fails_closed_on_error():
    src = _src("backend/core/llm/router.py")
    fn = src[src.index("def _perimeter_reason"):]
    fn = fn[:fn.index("def _maybe_apply_dlp")]
    # В except мы обязаны вернуть ПРИЧИНУ (= блокировать), а не None.
    assert "return f\"проверка периметра не выполнена" in fn, (
        "сбой проверки обязан блокировать, а не пропускать"
    )
    assert "_resolved_base_url" in fn, (
        "адрес берём тот, по которому клиент реально пойдёт"
    )
    print("✅ сбой проверки периметра блокирует вызов, а не пропускает")


# ── 5. Профиль на облако не заводится в контуре ─────────────────────────

def test_profile_validation_blocks_external_in_air_gap():
    src = _code_lines(_src("backend/core/llm/profiles.py"))
    fn = src[src.index("def validate_profile_input"):]
    fn = fn[:fn.index("_INSERT_SQL")]
    assert "enterprise_mode_enabled()" in fn and "llm_target_leaves_perimeter" in fn
    assert "ProfileValidationError" in fn[fn.index("enterprise_mode_enabled"):], (
        "отказ обязан быть явной ошибкой с причиной, а не молчаливым пропуском"
    )
    print("✅ профиль на внешнее облако в контуре не заводится")


def test_external_providers_list_is_shared():
    """Один список «внешних» на профили и на проверку периметра."""
    src = _code_lines(_src("backend/core/llm/profiles.py"))
    assert "EXTERNAL_LLM_PROVIDERS as EXTERNAL_PROVIDERS" in src
    assert "runpod" in _p.EXTERNAL_LLM_PROVIDERS, (
        "аренда чужого GPU — выход из контура"
    )
    for local in ("local_vllm", "ollama", "turboquant"):
        assert local not in _p.EXTERNAL_LLM_PROVIDERS
    print("✅ «внешний провайдер» значит одно и то же в обоих местах")


# ── 6. Фильтр выхода ────────────────────────────────────────────────────

def test_dlp_config_modes():
    src = _src("backend/core/llm/router.py")
    fn = src[src.index("def _dlp_config"):]
    fn = fn[:fn.index("def _maybe_apply_dlp(")]
    code = _code_lines(fn)
    assert "redact_external_urls=True" in code, (
        "в закрытом контуре внешние ссылки обязаны резаться"
    )
    assert "redact_emails=False" in code, (
        "узкий набор контура не должен трогать контакты и числа"
    )
    assert "air_gap or bool(" in code, (
        "вне контура ссылки режутся только по отдельному флагу"
    )
    print("✅ три режима фильтра выхода различены явно")


def test_dlp_applies_to_structured_answers():
    src = _src("backend/core/llm/router.py")
    gj = src[src.index("async def generate_json"):]
    gj = gj[:gj.index("def get_stats")]
    returns = [ln.strip() for ln in gj.splitlines()
               if ln.strip().startswith("return ") and "_json.loads" not in ln]
    assert returns, "у generate_json должны быть возвраты"
    for r in returns:
        assert "_maybe_apply_dlp_json" in r, f"возврат мимо фильтра: {r}"
    assert "_maybe_apply_dlp_json(_json.loads(cached_raw)" in gj, (
        "ответ из кэша тоже проходит фильтр"
    )
    print("✅ структурированные ответы проходят фильтр на всех возвратах")


def test_dlp_json_walks_strings_only():
    """Маска по сериализованному тексту ломала бы JSON — идём по структуре."""
    src = _src("backend/core/llm/router.py")
    fn = src[src.index("def _maybe_apply_dlp_json"):]
    fn = fn[:fn.index("# Context variables")]
    assert "isinstance(node, str)" in fn and "isinstance(node, dict)" in fn
    assert "{k: _walk(v) for k, v in node.items()}" in fn, (
        "ключи не трогаем — контракт полей обязан сохраниться"
    )
    print("✅ фильтр JSON трогает только строковые значения")


def test_output_filter_cuts_external_urls_and_keeps_internal():
    of = _load("backend.core.llm.output_filter", "backend/core/llm/output_filter.py")
    cfg = of.DLPConfig(
        redact_emails=False, redact_phones=False, redact_iban=False,
        redact_credit_cards=False, redact_ru_passport=False,
        redact_ru_snils=False, redact_secret_tokens=True,
        redact_external_urls=True,
    )
    r = of.filter_output(
        "отчёт: http://vllm:8000/v1 внутри, https://evil.example.com/x снаружи, "
        "выручка 1 234 567 рублей", cfg)
    assert "vllm:8000" in r.text, "внутренний адрес не режем"
    assert "evil.example.com" not in r.text, "внешняя ссылка обязана быть срезана"
    assert "1 234 567" in r.text, (
        "узкий набор контура не должен портить числа в отчёте"
    )
    print("✅ контурный набор режет внешнее и не портит цифры")


# ── 7. Веб-поиск ────────────────────────────────────────────────────────

def test_web_search_disabled_in_air_gap():
    src = _code_lines(_src("backend/core/search/web_search.py"))
    head = src[:src.index("tav = os.environ.get")]
    assert "enterprise_mode_enabled()" in head and "return []" in head, (
        "поиск наружу обязан отключаться ДО обращения к провайдерам"
    )
    print("✅ в закрытом контуре веб-поиск не выполняется")


# ── Документ не обещает лишнего ─────────────────────────────────────────

def test_doc_does_not_claim_always_on_filter():
    doc = _src("docs/ru/PRODUCT_CAPABILITIES.md")
    assert "режет внешние ссылки в каждом ответе" not in doc, (
        "фильтр не работает «в каждом ответе» — он включается по режиму"
    )
    print("✅ документ не обещает фильтр в каждом ответе")




# ── 8. Мультимодальный endpoint — тот же периметр ───────────────────────

def test_multimodal_endpoint_blocked_outside_perimeter():
    """Дыра аудита: докстринг обещал проверку internal, кода не было —
    скриншоты экранов компании могли уйти в облако."""
    if "backend.core.llm" not in sys.modules:
        _m = types.ModuleType("backend.core.llm")
        _m.__path__ = [os.path.join(ROOT, "backend", "core", "llm")]
        sys.modules["backend.core.llm"] = _m
    mm = _load("backend.core.llm.multimodal", "backend/core/llm/multimodal.py")
    old = os.environ.get("ENTERPRISE_MODE")
    try:
        os.environ["ENTERPRISE_MODE"] = "true"
        bad = mm.MultimodalClient(base_url="https://api.openai.com/v1",
                                  model="gpt-4o", api_key="k")
        v = bad.perimeter_violation()
        assert v and "за периметр" in v, "публичный endpoint обязан блокироваться"
        good = mm.MultimodalClient(base_url="http://vllm:8000/v1",
                                   model="muse-glimmer", api_key="k")
        assert good.perimeter_violation() is None, (
            "локальный endpoint в контуре допустим — иначе визуальная "
            "приёмка невозможна там, где она особенно нужна"
        )
        os.environ["ENTERPRISE_MODE"] = "off"
        assert bad.perimeter_violation() is None, (
            "вне контура прежнее поведение"
        )
    finally:
        if old is None:
            os.environ.pop("ENTERPRISE_MODE", None)
        else:
            os.environ["ENTERPRISE_MODE"] = old
    print("✅ скриншоты за периметр не уходят; локальный endpoint работает")


def test_multimodal_blocked_before_any_request():
    src = _src("backend/core/llm/multimodal.py")
    fn = src[src.index("    async def judge("):]
    fn = fn[:fn.index("\n\n")] if "\n\n" in fn else fn
    body = src[src.index("    async def judge("):src.index("    async def judge(") + 1200]
    assert body.index("perimeter_violation") < body.index("if not self.enabled"), (
        "проверка периметра обязана стоять ДО любой попытки вызова"
    )
    cfg = _src("backend/config.py")
    assert "MULTIMODAL_BASE_URL" in cfg and "screenshots must not leave" in cfg, (
        "валидатор Settings обязан ронять старт при таком endpoint"
    )
    print("✅ блокировка до запроса + процесс не стартует с такой настройкой")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты периметра прошли.")
