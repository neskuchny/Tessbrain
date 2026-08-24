# -*- coding: utf-8 -*-
"""Mark «Исследования»: происхождение инсайтов (field/hypothesis) назначает
КОД по дословной цитате из корпуса; лексикон — частоты кодом; t.me/s
парсится без ключей; e2e — фейковые коллекторы и LLM."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.marketing.collectors import parse_tme_html, strip_html
from backend.core.marketing.research_engine import (
    build_lexicon,
    execute_research,
    get_run,
    list_runs,
    mark_insights_origin,
    start_research,
    verify_quote,
    _norm_text,
)


def test_strip_html_and_tme_parser():
    html = """
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      Наши сметы <b>постоянно</b> переделываем — теряем контекст встреч.
    </div>
    <div class="tgme_widget_message_text js-message_text">короткое</div>
    <div class="tgme_widget_message_text js-message_text">
      Онбординг новых сотрудников занимает месяц, никто не следит за задачами.
    </div>
    """
    posts = parse_tme_html(html)
    assert len(posts) == 2  # «короткое» отсечено по длине
    assert "переделываем" in posts[0] and "<b>" not in posts[0]
    assert strip_html("<script>x</script><p>Привет &amp; мир</p>") == "Привет & мир"


def test_lexicon_counts_by_code():
    lex = build_lexicon([
        "онбординг сотрудников это боль, онбординг занимает месяц",
        "онбординг долгий, сотрудников много, контекст теряется",
    ])
    top = {u["term"]: u["count"] for u in lex["unigrams"]}
    assert top["онбординг"] == 3
    assert top["сотрудников"] == 2
    assert "это" not in top  # стоп-слово


def test_verify_quote_exact_and_fabricated():
    corpus = _norm_text("Мы постоянно переделываем задачи, потому что "
                        "информация теряется между отделами. Испорченный "
                        "телефон в чистом виде.")
    assert verify_quote("информация теряется между отделами", corpus)
    assert not verify_quote("сотрудники обожают наш продукт", corpus)
    marked = mark_insights_origin([
        {"text": "боль: потеря информации",
         "quote": "информация теряется между отделами", "source": "S1"},
        {"text": "гипотеза без цитаты", "quote": "", "source": ""},
        {"text": "выдуманная цитата",
         "quote": "все мечтают купить немедленно", "source": "S1"},
    ], corpus)
    assert [m["origin"] for m in marked] == ["field", "hypothesis", "hypothesis"]


class _FakeLLM:
    async def generate_json(self, prompt=None, **kw):
        if "сегменты-ГИПОТЕЗЫ" in prompt or "исследователь рынка" in prompt:
            assert "доноры" in prompt.lower()  # доноры обязательны в промпте
            return {"segments": [
                {"name": "Digital-агентства", "type": "direct",
                 "who": "руководители агентств 5-30 чел",
                 "pains_hypothesis": "теряют контекст встреч",
                 "queries": ["как не терять договорённости со встреч"],
                 "tg_channels": ["agencylife"], "why": "прямая боль"},
                {"name": "Пользователи таск-трекеров", "type": "donor",
                 "who": "команды на Notion/YouGile",
                 "pains_hypothesis": "задачи есть, контекста нет",
                 "queries": ["notion для команды минусы"],
                 "tg_channels": [], "why": "сформированное поведение"},
            ]}
        # инсайты: одна честная цитата из корпуса, одна выдуманная
        return {
            "pains": [
                {"text": "переделки из-за потери контекста",
                 "quote": "постоянно переделываем макеты потому что бриф "
                          "не дошёл до дизайнера",
                 "source": "S1", "segment": "Digital-агентства"},
                {"text": "выдуманная боль",
                 "quote": "мы ненавидим совещания всей душой",
                 "source": "S2", "segment": ""},
            ],
            "language": [
                {"text": "говорят «бриф не дошёл»",
                 "quote": "бриф не дошёл до дизайнера", "source": "S1",
                 "segment": ""},
            ],
            "triggers": [], "objections": [], "competitors_say": [],
            "donot": [{"text": "не продавать «ИИ ради ИИ»", "quote": "",
                       "source": "", "segment": ""}],
        }


class _FakeCollectors:
    """Фейковое поле: два фрагмента корпуса, один источник падает."""
    @staticmethod
    async def telegram_channel_posts(channel, limit=30):
        if channel == "agencylife":
            return [{"kind": "telegram", "url": "https://t.me/agencylife",
                     "title": "t.me/agencylife",
                     "text": "Опять постоянно переделываем макеты потому что "
                             "бриф не дошёл до дизайнера. Знакомо?"}]
        return []

    @staticmethod
    async def vk_group_posts(group, limit=30, user_id=None):
        return []

    @staticmethod
    async def fetch_page(url, cap=4000):
        return ""

    @staticmethod
    async def review_search(q, max_results=4):
        return [{"kind": "review", "url": "https://otzovik.com/x",
                 "title": "отзыв",
                 "text": "Таск-трекер норм, но контекст решений всё равно "
                         "теряется, каждый раз пересказываем заново."}]

    @staticmethod
    async def collect_web(q, max_results=3):
        return []


@pytest.fixture
def _tmp_store(tmp_path, monkeypatch):
    import backend.core.marketing.research_engine as re_mod
    monkeypatch.setattr(re_mod, "_store_dir", lambda: tmp_path)
    # не пишем документ/заготовку в реальные сторы; get_document отдаёт
    # последний сохранённый — append_doc_section дописывает секции
    async def fake_save(doc, user_id):
        fake_save.saved = doc

    async def fake_get(document_id, user_id):
        d = getattr(fake_save, "saved", None)
        if not d or d.document_id != document_id:
            return None
        return {"document_id": d.document_id, "title": d.title,
                "document_type": d.document_type, "version": d.version,
                "created_at": d.created_at.isoformat(),
                "updated_at": d.updated_at.isoformat(),
                "summary": d.summary,
                "content_markdown": d.content_markdown, "topic": d.topic,
                "keywords": d.keywords,
                "source_meetings": d.source_meetings,
                "sections": d.sections, "status": d.status,
                "folder": getattr(d, "folder", "")}
    import backend.core.documents.doc_store as ds
    monkeypatch.setattr(ds, "save_document", fake_save)
    monkeypatch.setattr(ds, "get_document", fake_get)
    return fake_save


def test_execute_research_end_to_end(_tmp_store):
    run = start_research(
        "u1", product="Tessbrain — корпоративный мозг",
        market="РФ, digital-агентства", tg_channels=["ownchannel"])
    assert get_run("u1", run["id"])["status"] == "running"

    res = asyncio.run(execute_research(
        "u1", run["id"], llm=_FakeLLM(), collectors=_FakeCollectors()))
    assert res["success"] is True

    done = get_run("u1", run["id"])
    assert done["status"] == "done"
    assert done["corpus_count"] == 2
    md = done["report_markdown"]
    # сегменты с донором
    assert "Digital-агентства" in md and "донор" in md
    # честная цитата → 📍 с цитатой; выдуманная → 💡 без цитаты
    assert "📍 переделки из-за потери контекста" in md
    assert "«постоянно переделываем макеты" in md.lower() or \
           "«Опять постоянно переделываем".lower() in md.lower() or \
           "постоянно переделываем макеты" in md
    assert "💡 выдуманная боль" in md
    # свой канал не отдал постов → честная заметка
    assert any("ownchannel" in n for n in
               [l for l in md.splitlines() if "ownchannel" in l]) or \
           "ownchannel" in md
    # лексикон посчитан кодом
    assert "Лексикон" in md
    # документ сохранён в папку «Исследования»
    doc = _tmp_store.saved
    assert doc.document_type == "research"
    assert getattr(doc, "folder", "") == "Исследования"
    # этапы логировались
    stages = [s["stage"] for s in done["stages_log"]]
    assert "segments" in stages and "collect" in stages and "report" in stages


def test_execute_research_empty_corpus_honest(_tmp_store, monkeypatch):
    class _NoField(_FakeCollectors):
        @staticmethod
        async def telegram_channel_posts(channel, limit=30):
            return []

        @staticmethod
        async def review_search(q, max_results=4):
            return []

    run = start_research("u1", product="Новый продукт X")
    res = asyncio.run(execute_research(
        "u1", run["id"], llm=_FakeLLM(), collectors=_NoField()))
    assert res["success"] is True
    done = get_run("u1", run["id"])
    assert done["corpus_count"] == 0
    assert "корпус пуст" in done["report_markdown"]


def test_run_store_roundtrip(_tmp_store):
    r1 = start_research("u2", product="Продукт А")
    r2 = start_research("u2", product="Продукт Б")
    ids = [r["id"] for r in list_runs("u2")]
    assert r1["id"] in ids and r2["id"] in ids
    assert get_run("u2", "nope") is None


# ── Фаза 2: персоны из корпуса + панель реакций ─────────────────────────

class _PersonaLLM:
    """Персоны: одна боль с честной цитатой, одна выдумана; панель:
    реакции + скептик."""
    async def generate_json(self, prompt=None, **kw):
        if "Собери" in prompt and "ПЕРСОН" in prompt:
            assert "Не приукрашивай" in prompt
            return {"personas": [
                {"name": "Марина, руководитель агентства",
                 "segment": "Digital-агентства",
                 "portrait": "12 человек, всё на ней",
                 "goals": ["меньше ручного контроля"],
                 "pains": [
                     {"text": "переделки из-за потери брифа",
                      "quote": "бриф не дошёл до дизайнера", "source": "S1"},
                     {"text": "выдуманная боль",
                      "quote": "я плачу каждый день в подушку", "source": "S1"},
                 ],
                 "lexicon": ["бриф", "переделываем", "контекст"],
                 "objections": ["ещё один сервис — никто не будет заполнять"],
                 "media": ["t.me/agencylife"],
                 "tone": "по-деловому, без пафоса"},
            ]}
        if "Панель реакций" in prompt:
            assert "Не льсти" in prompt
            return {"reactions": [
                {"persona": "Марина, руководитель агентства",
                 "first_impression": "опять обещают магию, но про бриф — в точку",
                 "hooks": ["контекст встреч сохраняется"],
                 "repels": ["слово «революционный»"],
                 "objections": ["кто будет это внедрять?"],
                 "trust_1_5": 3, "would_respond": True,
                 "say_it_better": "покажите, как бриф доходит до дизайнера без пересказов"},
            ]}
        # скептик
        assert "скептик" in prompt.lower()
        return {"attacks": ["нет цифр — «сохраняет контекст» звучит голословно"],
                "panel_flattery": ["would_respond=true при доверии 3 — подозрительно"],
                "reality_check": "холодный трафик закроет вкладку за 5 секунд",
                "fix_first": "заменить «революционный» на конкретный кейс с брифом"}


def _mk_done_run(user_id="u3"):
    run = start_research(user_id, product="Tessbrain",
                         tg_channels=["agencylife"])
    asyncio.run(execute_research(user_id, run["id"], llm=_FakeLLM(),
                                 collectors=_FakeCollectors()))
    return run["id"]


def test_build_personas_quotes_verified(_tmp_store):
    from backend.core.marketing.persona_engine import (
        build_personas, list_personas)
    rid = _mk_done_run()
    res = asyncio.run(build_personas("u3", run_id=rid, llm=_PersonaLLM()))
    assert res["success"] is True
    p = res["personas"][0]
    origins = {x["text"]: x["origin"] for x in p["pains"]}
    assert origins["переделки из-за потери брифа"] == "field"
    assert origins["выдуманная боль"] == "hypothesis"
    assert p["field_pains"] == 1
    # сохранены и переиспользуемы
    saved = list_personas("u3")
    assert saved and saved[0]["name"].startswith("Марина")
    # пересборка того же рана не плодит дубли
    asyncio.run(build_personas("u3", run_id=rid, llm=_PersonaLLM()))
    assert len([p for p in list_personas("u3") if p["run_id"] == rid]) == 1


def test_build_personas_requires_corpus(_tmp_store):
    from backend.core.marketing.persona_engine import build_personas
    run = start_research("u4", product="X без сбора")
    # ран не завершён
    res = asyncio.run(build_personas("u4", run_id=run["id"],
                                     llm=_PersonaLLM()))
    assert res["success"] is False


def test_simulate_panel_with_skeptic(_tmp_store):
    from backend.core.marketing.persona_engine import (
        build_personas, list_personas, simulate_reactions)
    rid = _mk_done_run("u5")
    asyncio.run(build_personas("u5", run_id=rid, llm=_PersonaLLM()))
    ids = [p["id"] for p in list_personas("u5")]
    res = asyncio.run(simulate_reactions(
        "u5", persona_ids=ids,
        message="Революционный AI-мозг сохраняет контекст всех встреч",
        llm=_PersonaLLM()))
    assert res["success"] is True
    r = res["reactions"][0]
    assert r["trust_1_5"] == 3 and r["would_respond"] is True
    assert "бриф" in r["say_it_better"]
    # скептик обязателен и атакует
    assert res["skeptic"] and res["skeptic"]["fix_first"]
    assert res["avg_trust"] == 3.0 and res["would_respond"] == 1
    # честный дисклеймер
    assert "не измерение" in res["disclaimer"]


def test_simulate_needs_personas_and_message(_tmp_store):
    from backend.core.marketing.persona_engine import simulate_reactions
    res = asyncio.run(simulate_reactions(
        "u6", persona_ids=["nope"], message="Оффер длиннее десяти символов",
        llm=_PersonaLLM()))
    assert res["success"] is False and "персоны" in res["error"]
    res2 = asyncio.run(simulate_reactions(
        "u6", persona_ids=["x"], message="короткий", llm=_PersonaLLM()))
    assert res2["success"] is False


# ── Фаза 3: Wordstat-размер + факт-гейт ─────────────────────────────────

class _FakeHttp:
    """Wordstat v4: create → list(Pending→Done) → get → delete."""
    def __init__(self):
        self.calls = []
        self._polls = 0

    class _Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    async def post(self, url, json=None, **kw):
        method = json["method"]
        self.calls.append(method)
        if method == "CreateNewWordstatReport":
            return self._Resp({"data": 77})
        if method == "GetWordstatReportList":
            self._polls += 1
            status = "Done" if self._polls >= 2 else "Pending"
            return self._Resp({"data": [{"ReportID": 77,
                                         "StatusReport": status}]})
        if method == "GetWordstatReport":
            return self._Resp({"data": [
                {"Phrase": "как не терять договорённости со встреч",
                 "SearchedWith": [
                     {"Phrase": "как не терять договорённости со встреч",
                      "Shows": 12500}]},
                {"Phrase": "notion для команды минусы",
                 "SearchedWith": [{"Phrase": "notion для команды минусы",
                                   "Shows": 640}]},
            ]})
        return self._Resp({"data": 1})  # delete


def test_sizing_tiers_and_doc_append(_tmp_store, monkeypatch):
    from backend.core.marketing.sizing import estimate_segments
    import backend.core.marketing.sizing as sz
    monkeypatch.setattr(sz, "_POLL_DELAY", 0)
    rid = _mk_done_run("u7")
    http = _FakeHttp()
    res = asyncio.run(estimate_segments("u7", rid, token="t", http=http))
    assert res["success"] is True
    by_name = {s["name"]: s for s in res["sizing"]}
    assert by_name["Digital-агентства"]["tier"] == "крупный"      # 12500
    assert by_name["Пользователи таск-трекеров"]["tier"] == "узкий"  # 640
    # отчёт рана дополнен, отчёт-документ переписан с секцией
    run = get_run("u7", rid)
    assert "Размер сегментов (Wordstat" in run["report_markdown"]
    assert "12 500" in run["report_markdown"]
    assert "Размер сегментов" in _tmp_store.saved.content_markdown
    # отчёт Wordstat удалён за собой
    assert "DeleteWordstatReport" in http.calls


def test_sizing_without_token_honest(_tmp_store, monkeypatch):
    from backend.core.marketing.sizing import estimate_segments
    monkeypatch.delenv("YANDEX_DIRECT_TOKEN", raising=False)
    rid = _mk_done_run("u8")
    res = asyncio.run(estimate_segments("u8", rid))
    assert res["success"] is False and "YANDEX_DIRECT_TOKEN" in res["error"]


def test_fact_rows_computed_by_code():
    from backend.core.marketing.fact_gate import compute_fact_rows
    rows = compute_fact_rows([
        {"label": "Вариант А", "impressions": 10000, "clicks": 250,
         "leads": 10, "spend": 5000},
        {"label": "", "impressions": 1},           # без label — вон
        {"label": "Б", "impressions": 0, "clicks": 5},
    ])
    assert rows[0]["ctr_pct"] == 2.5
    assert rows[0]["cr_pct"] == 4.0
    assert rows[0]["cpa"] == 500.0
    assert rows[1]["ctr_pct"] is None              # показов нет — не делим
    assert len(rows) == 2


def test_fact_gate_rank_agreement_and_drift(_tmp_store):
    """Панель: А (4.0) > Б (2.0). Факт: CTR Б > А → дрейф, agreement 0."""
    from backend.core.marketing.fact_gate import apply_campaign_facts
    from backend.core.marketing.persona_engine import (
        build_personas, list_personas, simulate_reactions)

    rid = _mk_done_run("u9")
    asyncio.run(build_personas("u9", run_id=rid, llm=_PersonaLLM()))
    ids = [p["id"] for p in list_personas("u9")]

    class _TrustLLM(_PersonaLLM):
        def __init__(self, trust):
            self.trust = trust

        async def generate_json(self, prompt=None, **kw):
            if "Панель реакций" in prompt:
                return {"reactions": [
                    {"persona": "Марина", "first_impression": "ок",
                     "hooks": [], "repels": [], "objections": [],
                     "trust_1_5": self.trust, "would_respond": True,
                     "say_it_better": ""}]}
            return await super().generate_json(prompt=prompt, **kw)

    asyncio.run(simulate_reactions(
        "u9", persona_ids=ids,
        message="Вариант А: бриф доходит до дизайнера без пересказов",
        llm=_TrustLLM(4)))
    asyncio.run(simulate_reactions(
        "u9", persona_ids=ids,
        message="Вариант Б: революционный корпоративный интеллект",
        llm=_TrustLLM(2)))

    res = asyncio.run(apply_campaign_facts("u9", rid, [
        {"label": "Вариант А: бриф доходит до дизайнера без пересказов",
         "impressions": 10000, "clicks": 100},   # CTR 1%
        {"label": "Вариант Б: революционный корпоративный интеллект",
         "impressions": 10000, "clicks": 300},   # CTR 3% — факт против панели
    ]))
    assert res["success"] is True
    assert res["agreement"] == 0.0
    assert res["verdicts"][0]["verdict"] == "дрейф"
    run = get_run("u9", rid)
    assert "Факт-гейт кампании" in run["report_markdown"]
    assert "🔴" in run["report_markdown"]
    assert "доверять рано" in run["report_markdown"]


def test_fact_gate_agreement_holds(_tmp_store):
    """Панель и факт согласны → держится, agreement 1.0."""
    from backend.core.marketing.fact_gate import (
        match_simulations, rank_agreement)
    rows = match_simulations(
        [{"label": "Вариант А про бриф", "ctr_pct": 3.0},
         {"label": "Вариант Б про революцию", "ctr_pct": 1.0}],
        [{"message": "Вариант А про бриф", "avg_trust": 4.0},
         {"message": "Вариант Б про революцию", "avg_trust": 2.0}])
    verdicts, agreement = rank_agreement(rows)
    assert agreement == 1.0 and verdicts[0]["verdict"] == "держится"


# ── История панелей (полная) + мост «персоны → контекст чата» ────────────

def test_simulation_history_full_results(_tmp_store):
    from backend.core.marketing.persona_engine import (
        build_personas, list_personas, list_simulations, simulate_reactions)
    rid = _mk_done_run("u10")
    asyncio.run(build_personas("u10", run_id=rid, llm=_PersonaLLM()))
    ids = [p["id"] for p in list_personas("u10")]
    asyncio.run(simulate_reactions(
        "u10", persona_ids=ids,
        message="Тестовый посыл про бриф и контекст", llm=_PersonaLLM()))
    hist = list_simulations("u10")
    assert len(hist) == 1
    # полный результат, не выжимка: реакции и скептик на месте
    assert hist[0]["reactions"][0]["persona"].startswith("Марина")
    assert hist[0]["skeptic"]["fix_first"]
    assert hist[0]["panel"] and "message" in hist[0]


def test_export_audience_to_chat(_tmp_store, monkeypatch):
    from backend.core.marketing.persona_engine import (
        build_personas, list_personas, simulate_reactions)
    from backend.core.marketing.chat_context import export_audience_to_chat

    rid = _mk_done_run("u11")
    asyncio.run(build_personas("u11", run_id=rid, llm=_PersonaLLM()))
    ids = [p["id"] for p in list_personas("u11")]
    asyncio.run(simulate_reactions(
        "u11", persona_ids=ids,
        message="Покажите, как бриф доходит до дизайнера", llm=_PersonaLLM()))

    saved = {}

    async def fake_upsert(user_id, *, title, content):
        saved["title"], saved["content"] = title, content
        return {"id": "doc123", "updated": False}

    import backend.core.marketing.chat_context as cc
    monkeypatch.setattr(cc, "upsert_kb_document", fake_upsert)

    res = asyncio.run(export_audience_to_chat("u11", run_id=rid))
    assert res["success"] is True and res["document_id"] == "doc123"
    md = saved["content"]
    # персоны с болями и пометками происхождения
    assert "Марина, руководитель агентства" in md
    assert "📍" in md
    # панель реакций с посылом и правкой скептика
    assert "как бриф доходит до дизайнера" in md
    assert "Скептик" in md
    # полный отчёт исследования приложен
    assert "Исследование аудитории" in md or "Сегменты-гипотезы" in md
    # подсказка ведёт в контекст чата
    assert "Контекст" in res["hint"] and "Документы" in res["hint"]


def test_export_audience_requires_material(_tmp_store):
    from backend.core.marketing.chat_context import export_audience_to_chat
    res = asyncio.run(export_audience_to_chat("u12"))
    assert res["success"] is False
