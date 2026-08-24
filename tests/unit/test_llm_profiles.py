"""Unit-тесты для core.llm.profiles (W42, in-memory режим)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.llm.profiles import (
    EXTERNAL_PROVIDERS,
    OPENAI_COMPAT_PROVIDERS,
    SUPPORTED_PROVIDERS,
    LLMProfile,
    LLMProfileService,
    ProfileConflict,
    ProfileNotFound,
    ProfileValidationError,
    validate_profile_input,
)


def _run(coro):
    return asyncio.run(coro)


# === Constants =========================================================


def test_supported_providers_set() -> None:
    assert "gemini" in SUPPORTED_PROVIDERS
    assert "openai" in SUPPORTED_PROVIDERS
    assert "local_vllm" in SUPPORTED_PROVIDERS
    assert "runpod" in SUPPORTED_PROVIDERS
    assert "turboquant" in SUPPORTED_PROVIDERS


def test_openai_compat_includes_runpod_turboquant() -> None:
    assert "runpod" in OPENAI_COMPAT_PROVIDERS
    assert "turboquant" in OPENAI_COMPAT_PROVIDERS
    assert "local_vllm" in OPENAI_COMPAT_PROVIDERS
    # Gemini не OpenAI-compatible.
    assert "gemini" not in OPENAI_COMPAT_PROVIDERS


def test_external_providers_excludes_local() -> None:
    """Air-gap mode должен заблокировать только external."""
    assert "runpod" in EXTERNAL_PROVIDERS
    assert "openai" in EXTERNAL_PROVIDERS
    assert "gemini" in EXTERNAL_PROVIDERS
    assert "local_vllm" not in EXTERNAL_PROVIDERS
    assert "turboquant" not in EXTERNAL_PROVIDERS


# === validate_profile_input ============================================


def test_validate_ok_managed() -> None:
    validate_profile_input(
        name="OpenAI prod", provider="openai", model="gpt-4o-mini",
        base_url=None, api_key="sk-test-xxx",
    )


def test_validate_ok_runpod() -> None:
    validate_profile_input(
        name="RP Qwen3", provider="runpod", model="Qwen/Qwen3-30B",
        base_url="https://abc.proxy.runpod.net/v1", api_key="rp_xxx",
    )


def test_validate_ok_turboquant_no_key() -> None:
    """TurboQuant in-VPC обычно без auth."""
    validate_profile_input(
        name="TQ local", provider="turboquant", model="qwen3-tq-int4",
        base_url="http://gpu01.internal:8000/v1", api_key=None,
    )


def test_validate_ok_local_vllm_no_key() -> None:
    validate_profile_input(
        name="vLLM dev", provider="local_vllm", model="qwen3",
        base_url="http://localhost:8000/v1", api_key=None,
    )


def test_validate_unsupported_provider() -> None:
    # anthropic теперь поддерживается (native SDK) — берём заведомо чужой.
    with pytest.raises(ProfileValidationError):
        validate_profile_input(
            name="x", provider="cohere_made_up", model="cmd-r",
            base_url=None, api_key="x",
        )


def test_validate_empty_name() -> None:
    with pytest.raises(ProfileValidationError):
        validate_profile_input(
            name="  ", provider="openai", model="gpt-4o-mini",
            base_url=None, api_key="x",
        )


def test_validate_long_name() -> None:
    with pytest.raises(ProfileValidationError):
        validate_profile_input(
            name="x" * 200, provider="openai", model="gpt-4o-mini",
            base_url=None, api_key="x",
        )


def test_validate_empty_model() -> None:
    with pytest.raises(ProfileValidationError):
        validate_profile_input(
            name="x", provider="openai", model="",
            base_url=None, api_key="x",
        )


def test_validate_runpod_missing_base_url() -> None:
    with pytest.raises(ProfileValidationError) as exc:
        validate_profile_input(
            name="x", provider="runpod", model="qwen",
            base_url=None, api_key="x",
        )
    assert "base_url" in str(exc.value)


def test_validate_turboquant_missing_base_url() -> None:
    with pytest.raises(ProfileValidationError):
        validate_profile_input(
            name="x", provider="turboquant", model="qwen",
            base_url=None, api_key=None,
        )


def test_validate_runpod_missing_api_key() -> None:
    with pytest.raises(ProfileValidationError):
        validate_profile_input(
            name="x", provider="runpod", model="qwen",
            base_url="https://x/v1", api_key=None,
        )


def test_validate_openai_missing_key() -> None:
    with pytest.raises(ProfileValidationError):
        validate_profile_input(
            name="x", provider="openai", model="gpt-4o-mini",
            base_url=None, api_key=None,
        )


# === LLMProfile.from_row + to_dict =====================================


def test_profile_from_row_basic() -> None:
    p = LLMProfile.from_row({
        "id": "llm_1", "name": "X", "provider": "openai",
        "model": "gpt-4o", "is_default": True,
        "config": {"temperature": 0.5},
    })
    assert p.id == "llm_1"
    assert p.is_default is True
    assert p.config == {"temperature": 0.5}


def test_profile_from_row_string_jsonb() -> None:
    p = LLMProfile.from_row({
        "id": "x", "name": "y", "provider": "openai", "model": "z",
        "config": '{"a": 1}',
    })
    assert p.config == {"a": 1}


def test_profile_from_row_invalid_jsonb() -> None:
    p = LLMProfile.from_row({
        "id": "x", "name": "y", "provider": "openai", "model": "z",
        "config": "{not json",
    })
    assert p.config == {}


def test_profile_to_dict_excludes_api_key() -> None:
    p = LLMProfile.from_row({
        "id": "x", "name": "y", "provider": "openai",
        "model": "z", "api_key_encrypted": b"ciphertext",
    })
    d = p.to_dict()
    assert "api_key" not in d
    assert "api_key_encrypted" not in d
    assert d["has_api_key"] is True


# === LLMProfileService CRUD (in-memory) ================================


def test_create_profile_basic() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(
        name="OpenAI prod", provider="openai", model="gpt-4o-mini",
        api_key="sk-test",
    ))
    assert p.id.startswith("llm_")
    assert p.name == "OpenAI prod"
    assert p.has_api_key is True


def test_create_runpod_with_url() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(
        name="RP", provider="runpod", model="Qwen/Qwen3-30B",
        base_url="https://abc.proxy.runpod.net/v1", api_key="rp_x",
    ))
    assert p.provider == "runpod"
    assert p.base_url == "https://abc.proxy.runpod.net/v1"


def test_create_validation_error_propagates() -> None:
    svc = LLMProfileService()
    with pytest.raises(ProfileValidationError):
        _run(svc.create(name="", provider="openai", model="x", api_key="y"))


def test_create_default_clears_others() -> None:
    svc = LLMProfileService()
    a = _run(svc.create(
        name="A", provider="openai", model="gpt-4o-mini",
        api_key="x", is_default=True,
    ))
    b = _run(svc.create(
        name="B", provider="openai", model="gpt-4o-mini",
        api_key="y", is_default=True,
    ))
    a_after = _run(svc.get(profile_id=a.id))
    b_after = _run(svc.get(profile_id=b.id))
    assert a_after.is_default is False
    assert b_after.is_default is True


# === get / list / get_default =========================================


def test_get_unknown_returns_none() -> None:
    svc = LLMProfileService()
    assert _run(svc.get(profile_id="bogus")) is None
    assert _run(svc.get(profile_id="")) is None


def test_list_returns_all_profiles() -> None:
    svc = LLMProfileService()
    _run(svc.create(name="A", provider="openai", model="x", api_key="k"))
    _run(svc.create(name="B", provider="openai", model="y", api_key="k"))
    items = _run(svc.list())
    assert len(items) == 2


def test_get_default_initially_none() -> None:
    svc = LLMProfileService()
    assert _run(svc.get_default()) is None


def test_get_default_after_create() -> None:
    svc = LLMProfileService()
    _run(svc.create(
        name="A", provider="openai", model="gpt-4o-mini",
        api_key="x", is_default=True,
    ))
    d = _run(svc.get_default())
    assert d is not None
    assert d.name == "A"


# === set_default ======================================================


def test_set_default_switches_active() -> None:
    svc = LLMProfileService()
    a = _run(svc.create(name="A", provider="openai", model="x", api_key="k", is_default=True))
    b = _run(svc.create(name="B", provider="runpod", model="qwen",
                        base_url="https://x/v1", api_key="rp"))
    assert _run(svc.get_default()).id == a.id
    _run(svc.set_default(profile_id=b.id))
    assert _run(svc.get_default()).id == b.id


def test_set_default_unknown_raises() -> None:
    svc = LLMProfileService()
    with pytest.raises(ProfileNotFound):
        _run(svc.set_default(profile_id="bogus"))


# === delete ===========================================================


def test_delete_removes_profile() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(name="A", provider="openai", model="x", api_key="k"))
    assert _run(svc.delete(profile_id=p.id)) is True
    assert _run(svc.get(profile_id=p.id)) is None


def test_delete_unknown_returns_false_in_memory() -> None:
    svc = LLMProfileService()
    # In-memory: вернёт False но не raises.
    assert _run(svc.delete(profile_id="bogus")) is False


# === update (partial) =================================================


def test_update_name_only_keeps_key() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(name="A", provider="openai", model="gpt-4o-mini", api_key="sk-1"))
    upd = _run(svc.update(profile_id=p.id, name="A renamed"))
    assert upd.name == "A renamed"
    assert upd.has_api_key is True  # ключ не тронут
    assert _run(svc.get_decrypted_api_key(profile_id=p.id)) == "sk-1"


def test_update_unknown_raises() -> None:
    svc = LLMProfileService()
    with pytest.raises(ProfileNotFound):
        _run(svc.update(profile_id="bogus", name="x"))


def test_update_provider_switch_requires_key_present() -> None:
    """local_vllm (без ключа) → openai (ключ нужен) без ключа = ошибка."""
    svc = LLMProfileService()
    p = _run(svc.create(name="L", provider="local_vllm", model="qwen",
                        base_url="http://x/v1"))
    with pytest.raises(ProfileValidationError):
        _run(svc.update(profile_id=p.id, provider="openai", base_url=None))


def test_update_provider_switch_with_key_ok() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(name="L", provider="local_vllm", model="qwen",
                        base_url="http://x/v1"))
    upd = _run(svc.update(profile_id=p.id, provider="openai",
                          base_url=None, api_key="sk-new"))
    assert upd.provider == "openai"
    assert upd.has_api_key is True


def test_update_existing_key_lets_provider_switch() -> None:
    """Профиль уже с ключом → смена provider без нового ключа проходит."""
    svc = LLMProfileService()
    p = _run(svc.create(name="O", provider="openai", model="gpt-4o-mini", api_key="sk-1"))
    upd = _run(svc.update(profile_id=p.id, provider="deepseek",
                          model="deepseek-chat"))
    assert upd.provider == "deepseek"
    assert upd.has_api_key is True


def test_update_clear_key_with_empty_string() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(name="L", provider="local_vllm", model="qwen",
                        base_url="http://x/v1", api_key="optional"))
    upd = _run(svc.update(profile_id=p.id, api_key=""))
    assert upd.has_api_key is False
    assert _run(svc.get_decrypted_api_key(profile_id=p.id)) is None


def test_update_is_default_clears_others() -> None:
    svc = LLMProfileService()
    a = _run(svc.create(name="A", provider="openai", model="x", api_key="k", is_default=True))
    b = _run(svc.create(name="B", provider="openai", model="y", api_key="k"))
    _run(svc.update(profile_id=b.id, is_default=True))
    assert _run(svc.get(profile_id=a.id)).is_default is False
    assert _run(svc.get_default()).id == b.id


def test_update_config_and_fallback() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(name="A", provider="openai", model="x", api_key="k"))
    upd = _run(svc.update(profile_id=p.id, config={"temperature": 0.9},
                          is_fallback=True))
    assert upd.config == {"temperature": 0.9}
    assert upd.is_fallback is True


def test_update_no_fields_is_noop_reload() -> None:
    """Пустой апдейт валиден (валидирует существующее) и возвращает профиль."""
    svc = LLMProfileService()
    p = _run(svc.create(name="A", provider="openai", model="x", api_key="k"))
    upd = _run(svc.update(profile_id=p.id))
    assert upd.id == p.id and upd.name == "A"


# === API key handling =================================================


def test_decrypted_api_key_returned_for_owner() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(
        name="A", provider="openai", model="x", api_key="sk-secret-1234",
    ))
    plain = _run(svc.get_decrypted_api_key(profile_id=p.id))
    assert plain == "sk-secret-1234"


def test_decrypted_api_key_for_no_key_profile() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(
        name="local", provider="local_vllm", model="qwen",
        base_url="http://x/v1",
    ))
    plain = _run(svc.get_decrypted_api_key(profile_id=p.id))
    assert plain is None


# === health-check tracking ============================================


def test_record_health_check_updates_fields() -> None:
    svc = LLMProfileService()
    p = _run(svc.create(name="A", provider="openai", model="x", api_key="k"))
    _run(svc.record_health_check(profile_id=p.id, ok=True))
    after = _run(svc.get(profile_id=p.id))
    assert after.last_health_ok is True
    assert after.last_health_check_at is not None


def test_record_health_check_unknown_no_op() -> None:
    svc = LLMProfileService()
    # Не должно raise.
    _run(svc.record_health_check(profile_id="bogus", ok=False))
    _run(svc.record_health_check(profile_id="", ok=True))
