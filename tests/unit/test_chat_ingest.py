# -*- coding: utf-8 -*-
"""Переписки как источник мозга: приватность по конструкции.

Главные инварианты: (1) пока /brain_on не прозвучал — на диск не пишется
НИЧЕГО; (2) привязать группу может только участник, чей Telegram уже
связан с системой; (3) флаг ENABLE_CHAT_INGEST fail-closed; (4) дайджест
строится из сырых сообщений без LLM.
"""
import asyncio
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.messengers import chat_ingest as ci  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_store_dir", lambda: tmp_path)
    monkeypatch.setenv("ENABLE_CHAT_INGEST", "1")
    yield tmp_path


def _tg_message(chat_id="-100777", text="привет", from_id="555",
                title="Отдел продаж", date=1753600000):
    return {"chat": {"id": chat_id, "type": "supergroup", "title": title},
            "from": {"id": from_id, "first_name": "Иван"},
            "text": text, "date": date}


def _link(monkeypatch, mapping):
    """lookup_by_external: tg_user_id → user_id системы (или None)."""
    class _Link:
        def __init__(self, uid):
            self.user_id = uid

    class _Svc:
        async def lookup_by_external(self, platform, external_id):
            uid = mapping.get(str(external_id))
            return _Link(uid) if uid else None

    async def _get():
        return _Svc()

    monkeypatch.setattr(
        "backend.core.messengers.links.get_messenger_links_service", _get)


def test_unbound_group_messages_are_never_stored(_env, monkeypatch):
    _link(monkeypatch, {})
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="секретная переписка")))
    assert out is None, "бот в чужой группе молчит"
    stored = [p for p in _env.iterdir() if p.suffix == ".json"]
    assert stored == [], \
        "до /brain_on на диск не пишется ничего, включая название группы"


def _integrations(monkeypatch, rows):
    """user_integrations (provider=telegram) для reverse-lookup владельца."""
    class _SB:
        async def _request(self, method, path, params=None):
            # два запроса: контакты (все провайдеры) + default_chat_id
            if params.get("key_name") == "like.contact_*":
                return [r for r in rows
                        if str(r.get("key_name", "")).startswith("contact_")]
            return [r for r in rows
                    if r.get("key_name") == "default_chat_id"]

    monkeypatch.setattr(
        "backend.db.supabase_client.get_supabase_client", lambda: _SB())
    monkeypatch.setattr(
        "backend.api.routes.integrations.decrypt_secret", lambda v: v)


def test_brain_on_requires_known_telegram(_env, monkeypatch):
    _link(monkeypatch, {})          # привязки нет
    _integrations(monkeypatch, [])  # и в интеграциях не найден
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_on")))
    assert "не найден" in out
    assert ci.list_sources("u1") == []


def test_brain_on_accepts_contact_from_integrations(_env, monkeypatch):
    """Владелец вписал свой Telegram в «Интеграции → Контакты» — этого
    достаточно: отдельная привязка «Подключить» не обязательна."""
    _link(monkeypatch, {})
    _integrations(monkeypatch, [
        {"user_id": "u1", "provider": "telegram", "key_name": "contact_anton",
         "secret_enc": "284540446",
         "meta": {"contact_type": "user_id", "display_name": "Антон"}},
        {"user_id": "u1", "provider": "telegram", "key_name": "default_chat_id",
         "secret_enc": "-5061236158", "meta": {}},
    ])
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_on", from_id="284540446")))
    assert "подключена" in out
    assert "контакт «Антон»" in out, "видно, КАК определён аккаунт"
    src = ci.list_sources("u1")[0]
    assert src["identity_source"] == "контакт «Антон»"


def test_ambiguous_telegram_claim_refused(_env, monkeypatch):
    """Один и тот же Telegram записан в двух аккаунтах — привязка группы
    отклоняется (иначе чужая переписка уехала бы не туда)."""
    _link(monkeypatch, {})
    _integrations(monkeypatch, [
        {"user_id": "u1", "provider": "telegram", "key_name": "contact_a", "secret_enc": "284540446",
         "meta": {"display_name": "Антон"}},
        {"user_id": "u2", "provider": "telegram", "key_name": "contact_b", "secret_enc": "284540446",
         "meta": {"display_name": "Тот же ID"}},
    ])
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_on", from_id="284540446")))
    assert "не найден" in out
    assert ci.list_sources("u1") == [] and ci.list_sources("u2") == []


def test_brain_on_binds_and_collects(_env, monkeypatch):
    _link(monkeypatch, {"555": "u1"})
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_on")))
    assert "подключена" in out
    src = ci.list_sources("u1")[0]
    assert src["platform"] == "telegram" and src["enabled"] is True
    assert src["title"] == "Отдел продаж"

    # обычное сообщение теперь копится
    assert asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="созвон с Ромашкой в четверг"))) is None
    src = ci.list_sources("u1")[0]
    assert src["message_count"] == 1

    # /brain_off — стоп и отписка
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_off")))
    assert "отключена" in out.lower()
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="ещё")))
    assert ci.list_sources("u1")[0]["message_count"] == 1, \
        "после /brain_off сообщения не копятся"


def test_flag_off_is_fail_closed(_env, monkeypatch):
    monkeypatch.setenv("ENABLE_CHAT_INGEST", "")
    _link(monkeypatch, {"555": "u1"})
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_on")))
    assert "выключен" in out
    assert ci.list_sources("u1") == []
    sync = asyncio.run(ci.sync_slack("u1", ["general"]))
    assert sync["status"] == "disabled"


def test_append_dedups_and_caps(_env):
    key = ci.source_key("telegram", "-1")
    ci._register_source("u1", platform="telegram", chat_id="-1",
                        title="Т", enabled=True)
    msgs = [{"ts": "2026-07-28T10:00:00+00:00", "author": "Иван",
             "text": "один"}]
    assert ci.append_messages("u1", key, msgs) == 1
    assert ci.append_messages("u1", key, msgs) == 0, "дубль не пишется"


def test_digest_builds_raw_kb_document(_env, monkeypatch):
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="решили: скидка Ромашке 10%")))

    saved = {}

    async def _upsert(user_id, *, title, content):
        saved.update({"user_id": user_id, "title": title,
                      "content": content})
        return {"id": "doc1", "updated": False}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)
    key = ci.source_key("telegram", "-100777")
    out = asyncio.run(ci.digest_to_brain("u1", key))
    assert out["status"] == "success" and out["document_id"] == "doc1"
    assert "Отдел продаж" in saved["title"]
    assert "скидка Ромашке 10%" in saved["content"], "сырая цитата, не пересказ"
    assert "Иван" in saved["content"]


def test_digest_refuses_empty_buffer(_env, monkeypatch):
    ci._register_source("u1", platform="slack", chat_id="general",
                        title="#general", enabled=True)
    out = asyncio.run(ci.digest_to_brain("u1", "slack:general"))
    assert out["status"] == "error"


def test_sync_slack_resolves_names_and_stores(_env, monkeypatch):
    import json as _json

    class _Slack:
        def __init__(self, uid):
            pass

        async def load_credentials(self):
            return True

        def get_key(self, k, default=""):
            return "xoxb-1" if k == "bot_token" else default

        async def list_channels(self, limit=500):
            return _json.dumps({"success": True, "channels": [
                {"id": "C012ABCDEF", "name": "sales", "topic": ""}]})

        async def read_messages(self, channel, limit=200):
            assert channel == "C012ABCDEF", "имя канала резолвится в ID"
            return _json.dumps({"success": True, "messages": [
                {"user": "U1", "text": "клиент просит счёт",
                 "ts": "1753600000.000100"}]})

    monkeypatch.setattr(
        "backend.integrations.slack_integration.SlackIntegration", _Slack)
    out = asyncio.run(ci.sync_slack("u1", ["#sales"]))
    assert out["status"] == "success"
    assert out["synced"][0] == {"channel": "sales", "fetched": 1, "added": 1,
                                "files": 0}
    src = ci.list_sources("u1")[0]
    assert src["platform"] == "slack" and src["title"] == "#sales"


# ── очерёдность, импорт старых диалогов, сшивка, анализ ─────────────────

def test_buffer_stays_chronological_after_late_import(_env):
    key = ci.source_key("telegram", "-2")
    ci._register_source("u1", platform="telegram", chat_id="-2",
                        title="Т", enabled=True)
    ci.append_messages("u1", key, [
        {"ts": "2026-07-28T10:00:00+00:00", "author": "Б", "text": "новое"}])
    ci.append_messages("u1", key, [
        {"ts": "2026-07-01T09:00:00+00:00", "author": "А", "text": "старое"}])
    msgs = ci.read_buffer("u1", key)["messages"]
    assert [m["text"] for m in msgs] == ["старое", "новое"], \
        "догруженная старая переписка встаёт по времени, а не в конец"


def test_import_telegram_export(_env):
    import json as _json
    export = _json.dumps({"name": "Отдел продаж (архив)", "messages": [
        {"type": "message", "date": "2026-06-01T10:00:00",
         "from": "Иван Петров", "from_id": "user555",
         "text": ["скидка Ромашке ", {"type": "bold", "text": "10%"}]},
        {"type": "service", "date": "2026-06-01T10:01:00",
         "actor": "x", "text": ""},
    ]})
    out = ci.import_dialog("u1", title="", content=export)
    assert out["status"] == "success" and out["platform"] == "telegram-export"
    assert out["parsed"] == 1, "service-события не тянутся"
    msgs = ci.read_buffer("u1", out["key"])["messages"]
    assert msgs[0]["text"] == "скидка Ромашке 10%", \
        "rich-text из экспорта склеивается"
    assert msgs[0]["author_id"] == "user555"
    src = ci.list_sources("u1")[0]
    assert src["title"] == "Отдел продаж (архив)"


def test_import_whatsapp_txt(_env):
    txt = ("28.07.2026, 14:03 - Иван Петров: счёт отправил\n"
           "продолжение строки\n"
           "[28.07.2026, 14:05:10] Мария: приняла\n"
           "28.07.2026, 14:06 - Иван Петров: <Без медиафайлов>")
    out = ci.import_dialog("u1", title="WA клиент", content=txt)
    assert out["status"] == "success" and out["platform"] == "whatsapp-export"
    msgs = ci.read_buffer("u1", out["key"])["messages"]
    assert msgs[0]["text"] == "счёт отправил\nпродолжение строки"
    assert msgs[1]["author"] == "Мария"


def test_import_unknown_format_refused(_env):
    out = ci.import_dialog("u1", title="x", content="просто текст без структуры")
    assert out["status"] == "error" and "не распознан" in out["message"], \
        "нераспознанное — честный отказ, а не свалка"


def test_identity_suggested_not_asserted(_env, monkeypatch):
    """Система ПРЕДЛАГАЕТ кандидата из встреч, но не утверждает сшивку:
    до confirm в дайджесте нет пометки «= …»."""
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="отчёт готов")))

    class _Gen:
        async def get_all_people_profiles(self, tenant_id=None):
            return [{"id": "p_ivan", "name": "Иван Петров",
                     "role": "директор", "category": "management"}]

    class _GB:
        async def close(self, save=False):
            pass

    async def _mv(uid, use_networkx=None):
        return _GB()

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)
    monkeypatch.setattr(
        "backend.core.sleep.enhanced_snapshot.get_enhanced_snapshot_generator",
        lambda g=None, user_id=None: _Gen())

    key = ci.source_key("telegram", "-100777")
    out = asyncio.run(ci.suggest_participant_matches("u1", key))
    row = out["participants"][0]
    assert row["name"] == "Иван"
    assert row["candidates"][0]["person_name"] == "Иван Петров", \
        "«Иван» из чата — кандидат на «Иван Петров» из встреч"
    assert "confirmed" not in row

    saved = {}

    async def _upsert(user_id, *, title, content):
        saved["content"] = content
        return {"id": "d1", "updated": False}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)
    asyncio.run(ci.digest_to_brain("u1", key))
    assert "(= Иван Петров)" not in saved["content"], \
        "неподтверждённая сшивка в документ не попадает"

    # подтверждаем — теперь аннотация появляется
    assert ci.confirm_participant("u1", key, row["author"], "p_ivan",
                                  "Иван Петров")
    asyncio.run(ci.digest_to_brain("u1", key))
    assert "(= Иван Петров)" in saved["content"]


def test_analysis_drops_unverified_quotes(_env, monkeypatch):
    """Главное правило анализа: пункт без ДОСЛОВНОЙ цитаты из переписки
    отбрасывается — «правильные наборы данных, а не сохранение всего подряд»."""
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="договорились: скидка Ромашке 10% до пятницы")))

    class _LLM:
        async def generate_json(self, prompt="", temperature=0.2):
            return {"agreements": [
                {"text": "скидка Ромашке",
                 "quote": "скидка Ромашке 10% до пятницы"},
                {"text": "выдуманное соглашение",
                 "quote": "этой фразы в переписке нет"},
            ], "tasks": [], "client_facts": [], "decisions": []}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM())
    key = ci.source_key("telegram", "-100777")
    out = asyncio.run(ci.analyze_source("u1", key))
    assert out["status"] == "success"
    assert len(out["agreements"]) == 1
    assert out["agreements"][0]["text"] == "скидка Ромашке"
    assert out["dropped_unverified"] == 1, "пункт без опоры отброшен и посчитан"
    src = ci.list_sources("u1")[0]
    assert src["analysis"]["agreements"][0]["quote"].startswith("скидка")


def test_auto_cycle_processes_only_fresh_sources(_env, monkeypatch):
    """Автоцикл (джоба планировщика): анализ+дайджест ТОЛЬКО для источников
    с новыми сообщениями после последнего дайджеста; выключенные — мимо."""
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="договорились: скидка Ромашке 10%")))

    class _LLM:
        async def generate_json(self, prompt="", temperature=0.2):
            return {"agreements": [{"text": "скидка",
                                    "quote": "скидка Ромашке 10%"}],
                    "tasks": [], "client_facts": [], "decisions": []}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM())

    async def _upsert(user_id, *, title, content):
        return {"id": "d1", "updated": False}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)

    assert ci.list_ingest_users() == ["u1"], \
        "планировщик находит пользователя с источниками"
    out = asyncio.run(ci.auto_cycle("u1"))
    assert out["analyzed"] == 1 and out["digested"] == 1

    # второй прогон без новых сообщений — ничего не переделывается
    out2 = asyncio.run(ci.auto_cycle("u1"))
    assert out2["analyzed"] == 0 and out2["digested"] == 0
    assert out2["skipped"] >= 1

    # флаг выключен → честный disabled
    monkeypatch.setenv("ENABLE_CHAT_INGEST", "")
    assert asyncio.run(ci.auto_cycle("u1"))["status"] == "disabled"


def test_contact_from_other_provider_counts_when_type_is_telegram(_env,
                                                                  monkeypatch):
    """Контакт мог быть заведён смежным продуктом на той же базе (провайдер
    другой) — если тип контакта телеграмный, он тоже считается."""
    _link(monkeypatch, {})
    _integrations(monkeypatch, [
        {"user_id": "u1", "provider": "meetflow", "key_name": "contact_anton",
         "secret_enc": "user_id_284540446",
         "meta": {"contact_type": "user_id", "display_name": "Антон"}},
    ])
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_on", from_id="284540446")))
    assert "подключена" in out and "контакт «Антон»" in out


def test_foreign_provider_phone_does_not_match(_env, monkeypatch):
    """Телефон WhatsApp-контакта не должен «совпасть цифрами» с telegram-id."""
    _link(monkeypatch, {})
    _integrations(monkeypatch, [
        {"user_id": "u1", "provider": "whatsapp", "key_name": "contact_x",
         "secret_enc": "+7 (284) 540-44-6", "meta": {"contact_type": "phone"}},
    ])
    out = asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="/brain_on", from_id="284540446")))
    assert "не найден" in out


def test_analysis_is_incremental_and_merges(_env, monkeypatch):
    """Второй прогон анализа не жжёт LLM на уже разобранных сообщениях и не
    теряет находки предыдущего прогона — ровно то, о чём просил владелец:
    «только новые сообщения» касается и анализа, не только буфера."""
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="договорились: скидка Ромашке 10%")))

    calls = []

    class _LLM1:
        async def generate_json(self, prompt="", temperature=0.2):
            calls.append(prompt)
            return {"agreements": [{"text": "скидка",
                                    "quote": "скидка Ромашке 10%"}],
                    "tasks": [], "client_facts": [], "decisions": []}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM1())
    key = ci.source_key("telegram", "-100777")
    out1 = asyncio.run(ci.analyze_source("u1", key))
    assert out1["status"] == "success" and len(out1["agreements"]) == 1
    assert len(calls) == 1

    # без новых сообщений — LLM не вызывается вообще, находка не теряется
    out2 = asyncio.run(ci.analyze_source("u1", key))
    assert out2["status"] == "success"
    assert len(out2["agreements"]) == 1
    assert out2["new_messages_analyzed"] == 0
    assert len(calls) == 1, "LLM не вызван повторно без новых сообщений"

    # новое сообщение → LLM видит ТОЛЬКО его, не всю историю заново
    asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="решили: доставка завтра", date=1753600100)))

    class _LLM2:
        async def generate_json(self, prompt="", temperature=0.2):
            calls.append(prompt)
            assert "скидка Ромашке 10%" not in prompt, \
                "уже разобранное сообщение повторно не отправляется в LLM"
            assert "доставка завтра" in prompt
            return {"agreements": [{"text": "доставка",
                                    "quote": "решили: доставка завтра"}],
                    "tasks": [], "client_facts": [], "decisions": []}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM2())
    out3 = asyncio.run(ci.analyze_source("u1", key))
    assert len(calls) == 2
    assert out3["new_messages_analyzed"] == 1
    texts = {r["text"] for r in out3["agreements"]}
    assert texts == {"скидка", "доставка"}, \
        "старая и новая находки сосуществуют"


def test_analysis_batches_large_backlog(_env, monkeypatch):
    """Залповый импорт архива крупнее лимита разбирается порциями — маркер
    продвигается постепенно, ничего не теряется."""
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    key = ci.source_key("telegram", "-100777")
    # 1300 сообщений одним импортом (> _DIGEST_MSGS=1200), уникальные ts
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    big = [{"ts": (base + timedelta(minutes=i)).isoformat(),
           "author": "Иван", "text": f"пункт {i}"} for i in range(1300)]
    ci.append_messages("u1", key, big)

    async def _llm(prompt="", temperature=0.2):
        return {"agreements": [], "tasks": [], "client_facts": [],
               "decisions": []}

    monkeypatch.setattr(
        "backend.core.llm.router.get_llm_router",
        lambda: type("L", (), {"generate_json": staticmethod(_llm)})())
    out1 = asyncio.run(ci.analyze_source("u1", key))
    assert out1["new_messages_analyzed"] == ci._DIGEST_MSGS
    assert "запустите" in out1["note"]

    out2 = asyncio.run(ci.analyze_source("u1", key))
    assert out2["new_messages_analyzed"] == 1300 - ci._DIGEST_MSGS
    assert "запустите" not in out2["note"]


# ── задачи переписки → граф → пульс; документы из групп → база знаний ──

class _TaskGraph:
    """GraphBuilder-мок с реальной сигнатурой _merge_node/_create_relationship."""
    def __init__(self):
        self.connected = True
        self.nodes = []
        self.edges = []

    async def connect(self):
        pass

    async def _merge_node(self, label, key_prop, key_val, props):
        self.nodes.append((label, props))
        return f"{label}:{key_val}"

    async def _create_relationship(self, a, b, rel_type, props):
        self.edges.append((a, b, rel_type))

    def save_graph(self):
        pass

    async def close(self, save=True):
        pass


def test_analysis_tasks_go_to_graph_for_pulse(_env, monkeypatch):
    """Задачи из переписки → Task-узлы (CREATED_FROM → Chat) — дальше их
    видит пульс тем же конвейером, что задачи встреч."""
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="Тихон, собери материалы по конкурентам к пятнице")))

    class _LLM:
        async def generate_json(self, prompt="", temperature=0.2):
            return {"agreements": [], "client_facts": [], "decisions": [],
                    "tasks": [{"title": "Собрать материалы по конкурентам",
                               "assignee": "Тихон",
                               "quote": "Тихон, собери материалы по "
                                        "конкурентам к пятнице"}]}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM())
    g = _TaskGraph()
    monkeypatch.setattr("backend.core.store.graph_builder.GraphBuilder",
                        lambda **kw: g)
    monkeypatch.setattr("backend.core.store.tenant_paths.graph_path_for_user",
                        lambda uid: "/tmp/x")

    key = ci.source_key("telegram", "-100777")
    out = asyncio.run(ci.analyze_source("u1", key))
    assert out["tasks_persisted"] == 1
    labels = [l for l, _ in g.nodes]
    assert "Chat" in labels and "Task" in labels
    task_props = next(p for l, p in g.nodes if l == "Task")
    assert task_props["title"] == "Собрать материалы по конкурентам"
    assert task_props["assignee"] == "Тихон"
    assert task_props["origin"] == "chat_ingest"
    assert g.edges and g.edges[0][2] == "CREATED_FROM"

    # повторный прогон без новых сообщений — задачи НЕ дублируются в графе
    out2 = asyncio.run(ci.analyze_source("u1", key))
    assert out2.get("tasks_persisted", 0) == 0
    assert len([l for l, _ in g.nodes if l == "Task"]) == 1


def test_context_only_tasks_stay_out_of_graph(_env, monkeypatch):
    _link(monkeypatch, {"555": "u1"})
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    asyncio.run(ci.handle_telegram_group_update(
        _tg_message(text="сделай отчёт到 завтра")))
    key = ci.source_key("telegram", "-100777")
    assert ci.set_source_mode("u1", key, "context_only")

    class _LLM:
        async def generate_json(self, prompt="", temperature=0.2):
            return {"agreements": [], "client_facts": [], "decisions": [],
                    "tasks": [{"title": "Сделать отчёт",
                               "quote": "сделай отчёт到 завтра"}]}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM())
    g = _TaskGraph()
    monkeypatch.setattr("backend.core.store.graph_builder.GraphBuilder",
                        lambda **kw: g)
    out = asyncio.run(ci.analyze_source("u1", key))
    assert out.get("tasks_persisted", 0) == 0
    assert g.nodes == [], "context_only в граф не пишет"


def test_telegram_document_goes_to_kb(_env, monkeypatch):
    """PDF из подключённой группы скачивается, парсится и ложится
    KB-документом с провенансом; в буфере остаётся след."""
    _link(monkeypatch, {"555": "u1"})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))

    class _Resp:
        def __init__(self, payload=None, content=b""):
            self._p = payload
            self.content = content

        def json(self):
            return self._p

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if "getFile" in url:
                return _Resp({"ok": True,
                              "result": {"file_path": "documents/kp.txt"}})
            return _Resp(content="КП: скидка 10% при годовом контракте"
                         .encode("utf-8"))

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _Client())

    saved = {}

    async def _upsert(user_id, *, title, content):
        saved.update({"title": title, "content": content})
        return {"id": "doc9", "updated": False}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)

    msg = _tg_message(text="")
    msg["document"] = {"file_id": "f1", "file_name": "kp.txt",
                       "file_size": 100}
    asyncio.run(ci.handle_telegram_group_update(msg))
    assert "Файл из переписки: kp.txt" in saved["title"]
    assert "скидка 10%" in saved["content"]
    assert "Отдел продаж" in saved["content"], "провенанс: из какой группы"
    src = ci.list_sources("u1")[0]
    assert src["files_ingested"] == 1
    msgs = ci.read_buffer("u1", src["key"])["messages"]
    assert any("kp.txt" in m["text"] for m in msgs), \
        "в переписке остался след о файле"


def test_unsupported_attachment_skipped(_env, monkeypatch):
    _link(monkeypatch, {"555": "u1"})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    called = {"n": 0}

    async def _upsert(user_id, *, title, content):
        called["n"] += 1
        return {"id": "d"}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)
    msg = _tg_message(text="")
    msg["document"] = {"file_id": "f2", "file_name": "video.mp4",
                       "file_size": 100}
    asyncio.run(ci.handle_telegram_group_update(msg))
    assert called["n"] == 0, "неподдержанный формат честно пропущен"


def test_collect_files_off_skips_documents(_env, monkeypatch):
    """Владелец выключил сбор файлов у источника — вложение не скачивается
    и в базу знаний не попадает (сообщения при этом собираются как раньше)."""
    _link(monkeypatch, {"555": "u1"})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    asyncio.run(ci.handle_telegram_group_update(_tg_message(text="/brain_on")))
    key = ci.source_key("telegram", "-100777")
    assert ci.set_source_files("u1", key, False)
    assert ci.source_collect_files(
        next(s for s in ci.list_sources("u1") if s["key"] == key)) is False

    called = {"n": 0}

    async def _upsert(user_id, *, title, content):
        called["n"] += 1
        return {"id": "d"}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)
    msg = _tg_message(text="")
    msg["document"] = {"file_id": "f3", "file_name": "kp.txt",
                       "file_size": 100}
    asyncio.run(ci.handle_telegram_group_update(msg))
    assert called["n"] == 0, "файлы выключены — вложение в мозг не идёт"
    # обратно включили — снова собирается (default: True)
    assert ci.set_source_files("u1", key, True)
    assert ci.source_collect_files(
        next(s for s in ci.list_sources("u1") if s["key"] == key)) is True


def test_slack_files_ingested_with_dedup(_env, monkeypatch):
    """Файлы Slack-канала уходят в базу знаний при синке; повторный синк
    не перезаливает уже собранные (дедуп по slack file id)."""
    import json as _json

    class _Slack:
        def __init__(self, uid):
            pass

        async def load_credentials(self):
            return True

        def get_key(self, k, default=""):
            return "xoxb-1" if k == "bot_token" else default

        async def list_channels(self, limit=500):
            return _json.dumps({"success": True, "channels": [
                {"id": "C012ABCDEF", "name": "sales", "topic": ""}]})

        async def read_messages(self, channel, limit=200):
            return _json.dumps({"success": True, "messages": [
                {"user": "U1", "text": "смотрите файл",
                 "ts": "1753600000.000100"}]})

        async def _api(self, method, **params):
            assert method == "files.list"
            assert params.get("channel") == "C012ABCDEF"
            return {"ok": True, "files": [
                {"id": "F001", "name": "usloviya.txt", "size": 100,
                 "user": "U1",
                 "url_private": "https://files.slack.com/usloviya.txt"},
                {"id": "F002", "name": "logo.png", "size": 100,
                 "url_private": "https://files.slack.com/logo.png"},
            ]}

    class _Resp:
        status_code = 200
        content = "Условия: постоплата 30 дней".encode("utf-8")

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            assert (headers or {}).get("Authorization") == "Bearer xoxb-1", \
                "url_private качается только с Bearer-токеном"
            return _Resp()

    monkeypatch.setattr(
        "backend.integrations.slack_integration.SlackIntegration", _Slack)
    monkeypatch.setattr("httpx.AsyncClient", _Client)

    saved = []

    async def _upsert(user_id, *, title, content):
        saved.append({"title": title, "content": content})
        return {"id": "doc1", "updated": False}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)

    out = asyncio.run(ci.sync_slack("u1", ["#sales"]))
    assert out["status"] == "success"
    assert out["synced"][0]["files"] == 1, "png отфильтрован, txt взят"
    assert "Файл из переписки: usloviya.txt" in saved[0]["title"]
    assert "постоплата 30 дней" in saved[0]["content"]
    assert "#sales" in saved[0]["content"], "провенанс: из какого канала"
    src = ci.list_sources("u1")[0]
    assert src["files_ingested"] == 1
    assert src["ingested_file_ids"] == ["F001"]

    # повторный синк: файл уже собран — не перезаливается
    out2 = asyncio.run(ci.sync_slack("u1", ["#sales"]))
    assert out2["synced"][0]["files"] == 0
    assert len(saved) == 1


def test_slack_files_respect_collect_files_off(_env, monkeypatch):
    """Тумблер «файлы» действует и на Slack: выключен — files.list даже
    не вызывается, в базу знаний ничего не уходит."""
    import json as _json

    api_calls = {"n": 0}

    class _Slack:
        def __init__(self, uid):
            pass

        async def load_credentials(self):
            return True

        def get_key(self, k, default=""):
            return "xoxb-1" if k == "bot_token" else default

        async def list_channels(self, limit=500):
            return _json.dumps({"success": True, "channels": [
                {"id": "C012ABCDEF", "name": "sales", "topic": ""}]})

        async def read_messages(self, channel, limit=200):
            return _json.dumps({"success": True, "messages": [
                {"user": "U1", "text": "смотрите файл",
                 "ts": "1753600000.000100"}]})

        async def _api(self, method, **params):
            api_calls["n"] += 1
            return {"ok": True, "files": []}

    monkeypatch.setattr(
        "backend.integrations.slack_integration.SlackIntegration", _Slack)
    # источник заводим заранее и выключаем сбор файлов
    key = ci._register_source("u1", platform="slack", chat_id="sales",
                              title="#sales", enabled=True)
    assert ci.set_source_files("u1", key, False)

    out = asyncio.run(ci.sync_slack("u1", ["#sales"]))
    assert out["status"] == "success"
    assert out["synced"][0]["files"] == 0
    assert api_calls["n"] == 0, "files.list не дёргается при выключенных файлах"
