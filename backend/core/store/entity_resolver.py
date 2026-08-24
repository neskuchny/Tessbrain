"""
TESSENT BRAIN - Entity Resolver
===============================

Модуль для резолвинга и дедупликации сущностей.

Проблема: При анализе встреч одни и те же люди/проекты/задачи
могут упоминаться по-разному:
- "Иван", "Ваня", "И. Петров", "ivan@company.com"
- "Проект Альфа", "Alpha", "альфа-проект"

Решение:
1. Canonical ID - каждая сущность имеет уникальный canonical_id
2. Aliases - список альтернативных названий
3. External IDs - ссылки на внешние системы (email, HR ID, etc.)
4. Fuzzy Matching - нечёткий поиск похожих сущностей
5. **LLM-based Matching** - ИИ определяет, являются ли две сущности одним лицом

Поддерживает два бэкенда:
- Neo4j (персистентный граф)
- NetworkX (in-memory граф для разработки)

Архитектура (без дублирования):
┌─────────────────────────────────────────────────────────────┐
│                    Entity (canonical)                        │
│  id: UUID (canonical_id)                                    │
│  name: "Иван Петров"                                        │
│  aliases: ["Ваня", "И. Петров"]                            │
│  external_ids: {email: "ivan@...", hr_id: "123"}           │
│                                                              │
│  → Все упоминания ссылаются на ОДИН узел                   │
│  → Связи создаются к canonical_id                          │
│  → При merge - aliases объединяются                        │
└─────────────────────────────────────────────────────────────┘
"""
import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)



# Попытка импорта structlog (опционально)
try:
    import structlog
    logger = structlog.get_logger(__name__)
except ImportError:
    logger.debug("suppressed exception", exc_info=True)


@dataclass
class ResolvedEntity:
    """Результат резолвинга сущности"""
    canonical_id: str
    name: str
    entity_type: str
    is_new: bool = False
    confidence: float = 1.0
    matched_by: Optional[str] = None  # "exact", "alias", "external_id", "fuzzy", "semantic"
    aliases: List[str] = field(default_factory=list)
    external_ids: Dict[str, str] = field(default_factory=dict)
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MergeCandidate:
    """Кандидат на слияние"""
    entity1_id: str
    entity1_name: str
    entity2_id: str
    entity2_name: str
    entity_type: str
    similarity_score: float
    match_reason: str  # "fuzzy", "alias", "semantic"
    auto_merge: bool = False  # Можно ли автоматически слить


def _parse_cluster_json(text: str) -> list:
    """Устойчивый парс JSON-массива кластеров из ответа LLM.

    Сначала пробуем массив целиком. Если не вышло (обрезка по max_tokens или
    один битый объект посреди) — вытаскиваем отдельные сбалансированные {...}
    и парсим по одному, пропуская битые. Так недописанный хвост просто
    отбрасывается, а валидные группы сохраняются (раньше любая ошибка → []).
    """
    if not text:
        return []
    start = text.find("[")
    if start == -1:
        return []
    body = text[start:]
    try:
        val = json.loads(body)
        if isinstance(val, list):
            return val
    except Exception:
        pass
    out: list = []
    depth = 0
    obj_start = None
    in_str = False
    esc = False
    for i, ch in enumerate(body):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start is not None:
                    try:
                        out.append(json.loads(body[obj_start:i + 1]))
                    except Exception:
                        pass
                    obj_start = None
    return out


# Карта уменьшительных (рус.) — для ПОСТ-фильтра кластеров дедупа: LLM склонен
# лумпить со-работников («общий проект») в одного человека. Фильтр оставляет в
# группе только записи, чьё имя реально совпадает с каноничным (токен/диминутив/
# фаззи), выкидывая чужих. Лучше отбросить легитимный вариант, чем слить разных.
_DIMINUTIVES = {
    "екатерина": {"катя", "катюша", "катенька", "катерина"},
    "александр": {"саша", "шура", "саня", "алекс"},
    "александра": {"саша", "шура", "аля"},
    "михаил": {"миша", "мишаня"},
    "мария": {"маша", "маруся", "маня"},
    "милана": {"мила"},
    "дарья": {"даша", "дашенька"},
    "евгений": {"женя", "жека"}, "евгения": {"женя"},
    "дмитрий": {"дима", "димон", "митя"},
    "анастасия": {"настя", "ася"},
    "алексей": {"лёша", "леша", "алёша", "алеша"},
    "павел": {"паша"}, "наталья": {"наташа", "ната"}, "наталия": {"наташа"},
    "роман": {"рома"}, "татьяна": {"таня"}, "ольга": {"оля"},
    "иван": {"ваня"}, "николай": {"коля"}, "станислав": {"стас"},
    "юлия": {"юля"}, "виктория": {"вика"}, "ксения": {"ксюша"},
    "елена": {"лена"}, "светлана": {"света"}, "борис": {"боря"},
    "владимир": {"вова", "володя"}, "сергей": {"серёжа", "сережа", "серёга"},
    "андрей": {"андрюша"}, "константин": {"костя"}, "григорий": {"гриша"},
    "максим": {"макс"}, "жанна": set(), "антон": set(),
}
# Обратный индекс: любая форма -> корневой набор {каноническое + все короткие}.
_NAME_ROOTS: Dict[str, frozenset] = {}
for _canon, _shorts in _DIMINUTIVES.items():
    _group = frozenset({_canon, *_shorts})
    for _form in _group:
        _NAME_ROOTS[_form] = _group

# Латинский индекс тех же корней (заполняется ниже, после определения _latin):
# 'sasha' → группа Александра, чтобы латинские написания сортировались в
# батчи дедупа рядом с кириллическими формами.
_LAT_NAME_ROOTS: Dict[str, frozenset] = {}


# Кириллица → латиница для кросс-алфавитного матчинга. Данные содержат ОДНОГО
# человека в двух написаниях («Ahlin»/«Ахлина», «Alexey Giyazov»/«Алексей
# Гиязов»), и дедуп их не ловил: токены на разных алфавитах не совпадают по
# SequenceMatcher. Транслитерируем кириллицу в латиницу и сравниваем в общем
# алфавите. х→h (а не kh) и кс→x — под фактические латинские написания в данных.
_CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _latin(token: str) -> str:
    """Транслитерировать токен в латиницу (латинские буквы — как есть).
    Нормализуем 'ks'→'x' ('алексей'→'aleksey'→'alexey' ≈ 'Alexey')."""
    if not token:
        return ""
    out = "".join(_CYR2LAT.get(ch, ch) for ch in token)
    return out.replace("ks", "x")


def _is_cyr(token: str) -> bool:
    return any("а" <= ch <= "я" or ch == "ё" for ch in token)


# Заполняем латинский индекс корней (объявлен выше, до определения _latin).
for _form, _group in list(_NAME_ROOTS.items()):
    _LAT_NAME_ROOTS.setdefault(_latin(_form), _group)


def _name_tokens(name: str) -> list:
    """Нормализовать имя в значимые токены: убрать (...), таймстемпы, цифры,
    пунктуацию. 'Саша (6:45)' → ['саша']; 'Катя Пустовалова' → ['катя',...]."""
    n = (name or "").lower()
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\d+[:.]\d+", " ", n)
    n = re.sub(r"[^а-яёa-z\s\-]", " ", n)
    return [t for t in re.split(r"[\s\-]+", n) if len(t) >= 2]


_COMBINED_PERSON_RE = re.compile(r"[а-яёa-z]\s*/\s*[а-яёa-z]")


def _is_combined_person(name: str) -> bool:
    """Узел из НЕСКОЛЬКИХ людей: «Александр Шитов / Екатерина Кустова»,
    «Юля Артёмова, Анна Миронова», «Катя, Максим», «Александр и Екатерина».

    Такой узел не должен участвовать в дедупе людей вообще: слияние В него
    прячет всех (view-фильтр скрывает составные узлы из списков), слияние
    ЕГО в одного человека приписывает тому чужие упоминания."""
    s = (name or "").strip().lower()
    if _COMBINED_PERSON_RE.search(s):
        return True
    toks = _name_tokens(s)
    if "," in s and len(toks) >= 2:
        return True
    if " и " in f" {s} " and len(toks) >= 2:
        return True
    return False


def _split_combined_person(name: str) -> list:
    """Разбить составное имя на отдельных людей.

    «Александр Шитов / Екатерина Кустова» → ["Александр Шитов",
    "Екатерина Кустова"]. Используется на ингесте: экстракция из
    транскрипта/названия встречи иногда выдаёт двух людей одной строкой
    («1:1 Шитов/Кустова»), и без разбиения рождается узел-сиамец с
    id person_<оба_имени>, который прячется view-фильтром и травит дедуп."""
    parts = [p.strip() for p in re.split(r"/|,|\bи\b|\band\b", name or "")]
    return [p for p in parts if _name_tokens(p)]


def _keep_name_rank(name: str) -> tuple:
    """Ранг имени для выбора канонического/keep-узла при слиянии.

    Чистое полное имя («Александр Шитов») лучше длинной строки с
    квалификатором («Александр (управляющий партнер КПД)») и тем более
    составного узла. Раньше keep выбирался по СЫРОЙ длине строки — мусорные
    длинные имена побеждали, и реальные люди исчезали из иерархии."""
    n = (name or "").strip()
    clean = "(" not in n and "/" not in n and "," not in n
    stripped = re.sub(r"\([^)]*\)", "", n).strip()
    return (0 if _is_combined_person(n) else 1,
            len(_name_tokens(n)), clean, len(stripped))


def _tokens_same_person(a: str, b: str) -> bool:
    """Один ли человек по токену: равенство / диминутив / вложенность / фаззи /
    кросс-алфавитная транслитерация."""
    if a == b:
        return True
    ra, rb = _NAME_ROOTS.get(a), _NAME_ROOTS.get(b)
    if ra and (b in ra):
        return True
    if rb and (a in rb):
        return True
    if ra and rb and (ra & rb):
        return True
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    if SequenceMatcher(None, a, b).ratio() >= 0.88:
        return True
    # Кросс-алфавит: один токен кириллица, другой латиница → сравниваем в
    # латинице с чуть более мягким порогом (транслитерация лоссовая).
    if _is_cyr(a) != _is_cyr(b):
        la, lb = _latin(a), _latin(b)
        if la and lb and la == lb:
            return True
        if len(la) >= 4 and len(lb) >= 4 and (la.startswith(lb) or lb.startswith(la)):
            return True
        if SequenceMatcher(None, la, lb).ratio() >= 0.80:
            return True
    return False


def _name_matches(member: str, canonical: str) -> bool:
    """True, если member — тот же человек, что canonical.

    Если ОБА имени полные (2+ токена) — должны совпасть И ИМЯ (первый токен),
    И фамилия. Совпадения одной лишь фамилии НЕ достаточно: «Сергей Янкович»
    и «Александр Янкович», «Мария Речкалова» и «Елена Речкалова» — РАЗНЫЕ люди.
    Если одно из имён — одиночный токен (голое имя «Катя» или голая фамилия
    «Ахмина»), допускаем совпадение с любым токеном другого (присоединяется к
    полному варианту).
    """
    mt, ct = _name_tokens(member), _name_tokens(canonical)
    if not mt or not ct:
        return False
    # Оба полные: имя И фамилия должны совпасть.
    if len(mt) >= 2 and len(ct) >= 2:
        if not _tokens_same_person(mt[0], ct[0]):
            return False  # разные имена (Сергей ≠ Александр) → разные люди
        if not any(_tokens_same_person(x, y) for x in mt[1:] for y in ct[1:]):
            return False  # конфликт фамилий (Белухин ≠ Фельдман) → разные
        return True
    # Одно из имён — одиночный токен: совпадение с любым токеном другого.
    return any(_tokens_same_person(x, y) for x in mt for y in ct)


# Юрформы и generic-слова компаний: шум, а не идентичность контрагента.
# «ООО Ромашка» == «Ромашка» == «Romashka LLC». Набор консервативный:
# отраслевые слова («банк», «агентство») НЕ убираем — они различают.
_COMPANY_STOP = {
    # RU юрформы
    "ооо", "ао", "пао", "зао", "оао", "ип", "нко", "тоо", "уп", "чуп",
    # EN/международные
    "ooo", "llc", "ltd", "inc", "gmbh", "corp", "co", "jsc", "pjsc",
    "plc", "sa", "bv", "ag", "oy", "srl", "sarl", "kk", "pte",
    # generic-обёртки
    "гк", "группа", "компаний", "компания", "холдинг", "holding",
    "фирма", "group",
}


def _company_core(name: str) -> list:
    """Ядро имени контрагента: без кавычек, юрформ и generic-обёрток.
    Цифры сохраняются («1С», «Авто49»)."""
    n = (name or "").lower()
    n = re.sub(r"[«»\"\'""'']+", " ", n)
    toks = re.findall(r"[а-яёa-z0-9]+", n)
    core = [t for t in toks if t not in _COMPANY_STOP]
    return core or toks  # имя целиком из «стоп-слов» («Группа компаний») — не пустим


def _company_matches(a: str, b: str) -> bool:
    """Один ли контрагент: равенство ядер (в т.ч. кросс-алфавитное через
    транслит) или почти-равенство (опечатка). Вложенность ядер («Альфа» ⊂
    «Альфа Банк») — НЕ совпадение: голое слово неоднозначно (Альфа Банк vs
    Альфа Страхование), а «Ромашка Москва» vs «Ромашка Питер» — филиалы."""
    ca, cb = _company_core(a), _company_core(b)
    if not ca or not cb:
        return False
    sa, sb = set(ca), set(cb)
    if sa == sb:
        return True
    la, lb = {_latin(t) for t in ca}, {_latin(t) for t in cb}
    if la == lb:
        return True  # «Ромашка» == «Romashka»
    ja, jb = " ".join(sorted(la)), " ".join(sorted(lb))
    # 0.92 ловит мелкую правку в ДЛИННОМ/многословном ядре; одиночная
    # опечатка в коротком имени («Ромашка»/«Рамашка», 0.875) осознанно НЕ
    # сливается — на том же расстоянии «Василёк»/«Васильев» (разные
    # компании). Лучше видимый дубль, чем неверное слияние.
    if len(ja) >= 5 and SequenceMatcher(None, ja, jb).ratio() >= 0.92:
        return True
    return False


def _entity_matches(member: str, canonical: str, entity_type: str = "person") -> bool:
    """Пост-фильтр совпадения для кластера дедупа, зависит от типа.

    person → _name_matches (учёт имён/фамилий/диминутивов/транслита).
    прочее (project/product/…) → только ПОЧТИ идентичные имена: LLM склонен
    лумпить разные-но-похожие проекты («Третий модуль»↔«Четвёртый модуль»,
    «Выход на рынки»↔«Увеличение доли»). Людская логика имён тут не применима.
    """
    if entity_type == "person":
        return _name_matches(member, canonical)
    if entity_type in ("company", "organization"):
        return _company_matches(member, canonical)
    a = re.sub(r"\s+", " ", (member or "").strip().lower())
    b = re.sub(r"\s+", " ", (canonical or "").strip().lower())
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.90


class EntityResolver:
    """
    Сервис резолвинга сущностей.

    Гарантирует что одна и та же сущность не дублируется в графе.

    Поддерживает:
    - Neo4j (персистентный граф)
    - NetworkX (in-memory граф через GraphBuilder)

    Пример использования:
    ```python
    resolver = EntityResolver(graph_builder=graph_builder)

    # Резолвинг сущности
    entity = await resolver.resolve(
        name="Иван Петров",
        entity_type="person"
    )

    # Поиск кандидатов на слияние
    candidates = await resolver.find_merge_candidates()

    # Слияние сущностей
    await resolver.merge_entities(entity1_id, entity2_id)
    ```
    """

    # Паттерны для нормализации имён
    NAME_PATTERNS = {
        # Русские имена
        "person_ru": [
            (r"(\w+)\s+(\w)\.\s*(\w+)", r"\1 \3"),  # "Иван И. Петров" -> "Иван Петров"
            (r"(\w)\.\s*(\w+)", r"\2"),              # "И. Петров" -> "Петров"
        ],
        # Проекты
        "project": [
            (r"проект\s+", ""),                       # "Проект Alpha" -> "Alpha"
            (r"project\s+", ""),                      # "Project Alpha" -> "Alpha"
        ],
    }

    # LLM клиент для интеллектуального сравнения
    # Все сравнения имён делаются через LLM - он понимает уменьшительные формы,
    # транслитерацию, опечатки и контекст лучше любого словаря
    _llm_client = None

    # Процессный кеш LLM-вердиктов, общий для всех инстансов (инстанс живёт
    # одну встречу). Грузится с диска один раз, дозаписывается по мере новых
    # вердиктов. Ключ: "name1|name2|entity_type" (lowercase).
    _llm_verdicts_shared: Optional[Dict[str, Dict]] = None
    _llm_verdicts_path = Path("data/dedup_cache/llm_verdicts.json")

    @classmethod
    def _load_llm_verdict_cache(cls) -> Dict[str, Dict]:
        if cls._llm_verdicts_shared is None:
            verdicts: Dict[str, Dict] = {}
            try:
                if cls._llm_verdicts_path.exists():
                    with open(cls._llm_verdicts_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        verdicts = data
            except Exception as e:
                logger.warning(f"LLM verdict cache load failed (start empty): {e}")
            cls._llm_verdicts_shared = verdicts
        return cls._llm_verdicts_shared

    @classmethod
    def _persist_llm_verdict_cache(cls) -> None:
        """Сбросить кеш на диск (best-effort). Зовётся на каждый новый вердикт:
        после прогрева новых вердиктов единицы за встречу, IO незаметен."""
        try:
            cls._llm_verdicts_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cls._llm_verdicts_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cls._llm_verdicts_shared or {}, f, ensure_ascii=False)
            tmp.replace(cls._llm_verdicts_path)
        except Exception as e:
            logger.debug(f"LLM verdict cache persist failed: {e}")

    def __init__(
        self,
        neo4j_client=None,
        qdrant_client=None,
        graph_builder=None,
        vector_indexer=None
    ):
        """
        Args:
            neo4j_client: Neo4j клиент (опционально)
            qdrant_client: Qdrant клиент (опционально)
            graph_builder: GraphBuilder instance (для NetworkX)
            vector_indexer: VectorIndexer instance (для семантического поиска)
        """
        self.neo4j = neo4j_client
        self.qdrant = qdrant_client
        self.graph = graph_builder
        self.vectors = vector_indexer

        # Порог для fuzzy matching
        self.fuzzy_threshold = 0.85

        # Порог для semantic matching
        self.semantic_threshold = 0.90

        # --- Guardrail B: умный dedup ---
        # Пороги конфигурируемы через env — можно ослабить дедуп, не трогая код.
        import os as _os

        def _envf(name: str, default: float) -> float:
            try:
                return float(_os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        # >= этого fuzzy-порога сливаем сразу (очевидный дубль).
        self.auto_merge_fuzzy = _envf("TESSENT_DEDUP_AUTO_FUZZY", 0.95)
        # Полоса [llm_band_low, auto_merge_fuzzy): неоднозначно → решает LLM.
        self.llm_band_low = _envf("TESSENT_DEDUP_LLM_BAND_LOW", 0.80)
        # LLM должен подтвердить с такой уверенностью, чтобы авто-слить.
        # 0.85 по умолчанию строг (отклоняет даже Катя/Екатерина, если LLM
        # даёт 0.8). Поставь TESSENT_DEDUP_LLM_CONFIDENCE=0.7 чтобы сливать
        # уверенные диминутивы; разные фамилии LLM всё равно отклонит.
        self.llm_auto_merge_confidence = _envf("TESSENT_DEDUP_LLM_CONFIDENCE", 0.85)

        # Кеш для ускорения повторных резолвингов
        self._cache: Dict[str, ResolvedEntity] = {}

        # Индекс aliases для быстрого поиска
        self._alias_index: Dict[str, str] = {}  # normalized_alias -> canonical_id

        # Кеш LLM-вердиктов. Резолвер создаётся ЗАНОВО на каждую встречу
        # (knowledge_sync), поэтому инстансовый кеш умирал сразу и одни и те
        # же пары («Олег»/«Олежа») переспрашивались у LLM на КАЖДОЙ встрече —
        # тысячи запросов в день, рост O(пары×встречи). Кеш процессный
        # (class-level) + персистентный на диске: вердикт спрашивается один раз.
        self._llm_cache: Dict[str, Dict] = self._load_llm_verdict_cache()
        # Кап на НОВЫЕ (некешированные) LLM-сверки за один auto_merge-прогон:
        # предохранитель от runaway-расхода на большом графе.
        self.llm_max_new_checks = int(_envf("TESSENT_DEDUP_LLM_MAX_CALLS", 40))
        self._llm_new_checks = 0

    # Служебные слова: если названия отличаются ТОЛЬКО ими — это один объект
    # («Customer Development интервью» ≡ «Customer Development»).
    _FILLER_TOKENS = {"интервью", "проект", "проекта", "встреча", "встречи",
                      "the", "a", "и"}
    # Канонизация частых вариантов одного термина (транслит/ослышки).
    # «Кастдев/Касдев/Каздев/CustDev/Customer Development» плодили 4 проекта
    # с 4 снапшотами каждый.
    _TOKEN_CANON = {
        "кастдев": "custdev", "касдев": "custdev", "каздев": "custdev",
        "custdev": "custdev", "кастомдев": "custdev",
    }

    def _canon_tokens(self, s: str) -> set:
        """Множество канонических токенов имени: без скобочных уточнений и
        пунктуации, с приведением известных вариантов к одному терму."""
        s = s.lower()
        s = re.sub(r"\([^)]*\)", " ", s)  # «(CustDev)» — уточнение, не имя
        s = s.replace("customer development", " custdev ")
        s = re.sub(r"[^\w\s]", " ", s)
        return {self._TOKEN_CANON.get(w, w) for w in s.split() if w}

    def _fuzzy_score(self, name1: str, name2: str) -> float:
        """Fuzzy score двух имён: посимвольное сходство + токен-логика.

        Плоский SequenceMatcher не видел очевидные дубли с перестановкой
        слов и скобками: «Customer Development (CustDev)» vs
        «Касдев (Customer Development)» давал ~0.6 и пара не сливалась."""
        n1 = name1.lower().strip()
        n2 = name2.lower().strip()
        base = SequenceMatcher(None, n1, n2).ratio()
        try:
            t1, t2 = self._canon_tokens(n1), self._canon_tokens(n2)
            if t1 and t2:
                if t1 == t2:
                    return 1.0
                diff = t1 ^ t2
                if (t1 <= t2 or t2 <= t1) and diff <= self._FILLER_TOKENS:
                    return max(base, 0.96)
                base = max(base, len(t1 & t2) / len(t1 | t2))
        except Exception:
            logger.debug("token fuzzy skipped", exc_info=True)
        return base

    def _get_llm_client(self):
        """Получить LLM клиент (lazy initialization)"""
        if EntityResolver._llm_client is None:
            try:
                from backend.core.llm import get_llm_router
                EntityResolver._llm_client = get_llm_router()
            except ImportError:
                logger.warning("LLM router not available, using fuzzy matching only")
                return None
        return EntityResolver._llm_client

    async def check_same_entity_llm(
        self,
        name1: str,
        name2: str,
        entity_type: str,
        context1: Optional[Dict[str, Any]] = None,
        context2: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, float, str]:
        """
        Использовать LLM для определения, являются ли две сущности одним лицом.

        Args:
            name1: Первое имя/название
            name2: Второе имя/название
            entity_type: Тип сущности (person, project, etc.)
            context1: Контекст первой сущности (роль, отдел, встречи)
            context2: Контекст второй сущности

        Returns:
            Tuple[is_same: bool, confidence: float, reasoning: str]
        """
        # Проверяем кеш
        cache_key = f"{name1.lower()}|{name2.lower()}|{entity_type}"
        cache_key_rev = f"{name2.lower()}|{name1.lower()}|{entity_type}"

        if cache_key in self._llm_cache:
            cached = self._llm_cache[cache_key]
            return cached["is_same"], cached["confidence"], cached["reasoning"]
        if cache_key_rev in self._llm_cache:
            cached = self._llm_cache[cache_key_rev]
            return cached["is_same"], cached["confidence"], cached["reasoning"]

        llm = self._get_llm_client()
        if not llm:
            # Fallback на fuzzy matching
            score = self._fuzzy_score(name1, name2)
            is_same = score >= self.fuzzy_threshold
            return is_same, score, "fuzzy_fallback"

        # Кап на новые LLM-сверки за жизнь инстанса (одна встреча/один прогон):
        # некешированных пар на большом графе могут быть сотни — не жжём токены,
        # решаем консервативно по fuzzy (не сливаем), пара дождётся кеша/лимита
        # следующего прогона.
        if self._llm_new_checks >= self.llm_max_new_checks:
            score = self._fuzzy_score(name1, name2)
            return False, score, "llm_budget_exhausted"
        self._llm_new_checks += 1

        # Формируем контекст
        ctx1_str = ""
        ctx2_str = ""
        if context1:
            ctx1_str = f"\nКонтекст 1: роль={context1.get('role', '?')}, отдел={context1.get('department', '?')}, встречи={context1.get('meetings', [])}"
        if context2:
            ctx2_str = f"\nКонтекст 2: роль={context2.get('role', '?')}, отдел={context2.get('department', '?')}, встречи={context2.get('meetings', [])}"

        prompt = f"""Определи, являются ли эти два имени/названия ОДНОЙ И ТОЙ ЖЕ сущностью.

Тип сущности: {entity_type}
Имя 1: "{name1}"{ctx1_str}
Имя 2: "{name2}"{ctx2_str}

ВАЖНО: Ты эксперт по русским и английским именам. Учитывай ВСЕ возможные варианты:

1. УМЕНЬШИТЕЛЬНЫЕ ФОРМЫ (примеры, но не ограничивайся ими):
   - Александр = Саша, Шура, Алекс
   - Анастасия = Настя, Ася
   - Дмитрий = Дима, Митя
   - Евгений/Евгения = Женя
   - Екатерина = Катя
   - Михаил = Миша
   - Николай = Коля, Ник
   - Владимир = Вова, Володя
   - Сергей = Серёжа
   - Антон = Тоша
   - Максим = Макс
   - Кирилл = Кир
   - Олег = Олежа
   - Иван = Ваня
   - Павел = Паша
   - И ЛЮБЫЕ другие уменьшительные формы, которые ты знаешь!

2. ТРАНСЛИТЕРАЦИЯ:
   - Ян Унбаку = Jan Unbaku = Yan Unbaku
   - Яэль = Yael
   - Евгений = Eugene, Evgeny
   - Михаил = Michael, Mikhail

3. СОКРАЩЕНИЯ:
   - И. Петров = Иван Петров
   - А. Иванов = Александр Иванов / Алексей Иванов / Антон Иванов

4. ОПЕЧАТКИ И ВАРИАЦИИ:
   - Синлапс = Synlaps = Sinlaps
   - Незначительные опечатки

5. КОНТЕКСТ: Если роли/отделы совпадают - это увеличивает вероятность совпадения.

Ответь СТРОГО в формате JSON:
{{"is_same": true/false, "confidence": 0.0-1.0, "reasoning": "краткое объяснение"}}"""

        try:
            response = await llm.generate(
                prompt=prompt,
                temperature=0.1,  # Низкая температура для детерминированности
                max_tokens=200
            )

            # Парсим JSON из ответа
            text = response.get("text", "") if isinstance(response, dict) else str(response)

            # Ищем JSON в ответе
            json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                is_same = result.get("is_same", False)
                confidence = float(result.get("confidence", 0.5))
                reasoning = result.get("reasoning", "LLM decision")

                # Кешируем результат (кеш процессный + на диске: пара
                # спрашивается у LLM один раз за всю жизнь тенанта)
                self._llm_cache[cache_key] = {
                    "is_same": is_same,
                    "confidence": confidence,
                    "reasoning": reasoning
                }
                self._persist_llm_verdict_cache()

                return is_same, confidence, reasoning
            else:
                logger.warning(f"Could not parse LLM response: {text[:100]}")
                # Fallback
                score = self._fuzzy_score(name1, name2)
                return score >= self.fuzzy_threshold, score, "parse_error_fallback"

        except Exception as e:
            logger.error(f"LLM error in entity comparison: {e}")
            # Fallback на fuzzy
            score = self._fuzzy_score(name1, name2)
            return score >= self.fuzzy_threshold, score, f"error_fallback: {e}"

    async def find_duplicates_llm(
        self,
        entity_type: Optional[str] = None,
        batch_size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Найти дубликаты с помощью LLM.

        Сначала делает предварительную фильтрацию по fuzzy score,
        затем проверяет кандидатов через LLM.

        Args:
            entity_type: Тип сущности (None = все)
            batch_size: Сколько пар проверять за раз

        Returns:
            Список найденных дубликатов с reasoning от LLM
        """
        duplicates = []

        # Получаем все сущности из графа
        # GraphBuilder хранит NetworkX граф в nx_graph
        graph_data = None
        if self.graph:
            if hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
                graph_data = self.graph.nx_graph
            elif hasattr(self.graph, 'graph') and self.graph.graph:
                graph_data = self.graph.graph

        if not graph_data:
            logger.warning("No graph available for duplicate search")
            return duplicates

        # Собираем сущности по типам
        entities_by_type: Dict[str, List[Dict]] = {}

        for node_id, data in graph_data.nodes(data=True):
            label = data.get("_label", data.get("label", ""))
            if entity_type and label.lower() != entity_type.lower():
                continue

            name = data.get("name", "")
            if not name:
                continue

            if label not in entities_by_type:
                entities_by_type[label] = []

            entities_by_type[label].append({
                "id": node_id,
                "name": name,
                "role": data.get("role", ""),
                "department": data.get("department", ""),
                "aliases": data.get("aliases", [])
            })

        # Для каждого типа ищем дубликаты
        for etype, entities in entities_by_type.items():
            if len(entities) < 2:
                continue

            # Предварительная фильтрация - сравниваем все пары
            candidates = []
            for i, e1 in enumerate(entities):
                for e2 in entities[i+1:]:
                    # Быстрая проверка fuzzy score
                    fuzzy = self._fuzzy_score(e1["name"], e2["name"])
                    if fuzzy >= 0.5:  # Низкий порог для предварительной фильтрации
                        candidates.append((e1, e2, fuzzy))

            # Сортируем по fuzzy score (сначала более похожие)
            candidates.sort(key=lambda x: -x[2])

            # Проверяем через LLM (батчами)
            for e1, e2, fuzzy in candidates[:batch_size]:
                is_same, confidence, reasoning = await self.check_same_entity_llm(
                    name1=e1["name"],
                    name2=e2["name"],
                    entity_type=etype,
                    context1={"role": e1.get("role"), "department": e1.get("department")},
                    context2={"role": e2.get("role"), "department": e2.get("department")}
                )

                if is_same and confidence >= 0.7:
                    duplicates.append({
                        "entity1": e1,
                        "entity2": e2,
                        "entity_type": etype,
                        "confidence": confidence,
                        "reasoning": reasoning,
                        "fuzzy_score": fuzzy
                    })

        return duplicates

    async def resolve(
        self,
        name: str,
        entity_type: str,
        organization_id: str,
        external_ids: dict[str, str] | None = None,
        properties: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> ResolvedEntity:
        """
        Резолвить сущность - найти существующую или создать новую

        Args:
            name: Название сущности
            entity_type: Тип (person, project, task, etc.)
            organization_id: ID организации
            external_ids: Внешние ID (email, hr_id, etc.)
            properties: Дополнительные свойства
            project_id: ID проекта (для привязки)

        Returns:
            ResolvedEntity с canonical_id
        """
        external_ids = external_ids or {}
        properties = properties or {}

        # Составное имя двух+ людей («Шитов / Кустова», «Юля, Аня») — НЕ
        # создаём узел-сиамец (id person_<оба_имени> потом прячется
        # view-фильтром и травит дедуп). Резолвим каждого по отдельности,
        # возвращаем первого. external_ids/properties не передаём частям —
        # неизвестно, к кому из двоих они относятся.
        if entity_type == "person" and _is_combined_person(name):
            try:
                from backend.core.sleep.enhanced_snapshot import _is_person_junk
            except Exception:
                _is_person_junk = None
            parts = [p for p in _split_combined_person(name)
                     if not (_is_person_junk and _is_person_junk(p))]
            if len(parts) >= 2:
                logger.info(
                    f"resolve: составное имя «{name}» → по частям: {parts}")
                first: ResolvedEntity | None = None
                for p in parts:
                    try:
                        ent = await self.resolve(
                            p, entity_type, organization_id,
                            project_id=project_id)
                        first = first or ent
                    except Exception:
                        logger.debug("combined part resolve failed",
                                     exc_info=True)
                if first:
                    return first

        # Нормализуем имя
        normalized_name = self._normalize_name(name)

        # Анти-протечка (общий Neo4j): резолв ищет ТОЛЬКО среди своих
        # tenant-ов (user + его организации + коллеги) и legacy-узлов без
        # штампа. Без этого одинаковое имя в двух аккаунтах матчилось в
        # ЧУЖОЙ узел, и его свойства утекали в per-tenant хранилища.
        tenants = self._tenant_scope(organization_id)

        # Проверяем кеш
        cache_key = f"{organization_id}:{entity_type}:{normalized_name}"
        if cache_key in self._cache:
            logger.debug("Entity resolved from cache", name=name)
            return self._cache[cache_key]

        # 1. Поиск по external_ids (самый точный)
        if external_ids:
            result = await self._find_by_external_ids(
                entity_type, organization_id, external_ids
            )
            if result:
                result.matched_by = "external_id"
                self._cache[cache_key] = result
                return result

        # 2. Точное совпадение по имени
        result = await self._find_by_exact_name(
            normalized_name, entity_type, organization_id, tenants=tenants
        )
        if result:
            result.matched_by = "exact"
            self._cache[cache_key] = result
            return result

        # 3. Поиск по aliases
        result = await self._find_by_alias(
            normalized_name, entity_type, organization_id, tenants=tenants
        )
        if result:
            result.matched_by = "alias"
            self._cache[cache_key] = result
            return result

        # 4. Fuzzy matching (для людей и проектов)
        if entity_type in ("person", "project"):
            result = await self._find_by_fuzzy(
                normalized_name, entity_type, organization_id, tenants=tenants
            )
            if result and result.confidence >= self.fuzzy_threshold:
                result.matched_by = "fuzzy"
                # Добавляем как alias
                await self._add_alias(result.canonical_id, normalized_name)
                self._cache[cache_key] = result
                return result

        # 5. Создаём новую сущность
        new_entity = await self._create_entity(
            name=name,
            normalized_name=normalized_name,
            entity_type=entity_type,
            organization_id=organization_id,
            external_ids=external_ids,
            properties=properties,
            project_id=project_id,
        )

        self._cache[cache_key] = new_entity
        return new_entity

    def _tenant_scope(self, organization_id: str | None) -> list[str] | None:
        """Список своих tenant-ов для read-фильтра резолва.

        None (нет контекста) → фильтр выключен, поведение прежнее. Never-raise."""
        try:
            from backend.core.store.tenant_scope import allowed_tenants
            allowed = allowed_tenants(organization_id)
            return sorted(allowed) if allowed else None
        except Exception:
            return None

    @staticmethod
    def _tenant_pass(attrs: dict, tenants: list[str] | None) -> bool:
        """True, если узел свой или legacy (без штампа tenant_id)."""
        if not tenants:
            return True
        t = attrs.get("tenant_id")
        return t in (None, "") or str(t) in tenants

    def _normalize_name(self, name: str, entity_type: str = "unknown") -> str:
        """Нормализовать имя для сравнения"""
        # Убираем лишние пробелы, приводим к нижнему регистру
        normalized = " ".join(name.lower().split())

        # Применяем паттерны для типа сущности
        patterns = self.NAME_PATTERNS.get(entity_type, [])
        for pattern, replacement in patterns:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

        # Убираем знаки препинания
        normalized = re.sub(r'[^\w\s]', '', normalized)

        return normalized.strip()

    def _get_name_variants(self, name: str, entity_type: str = "person") -> List[str]:
        """
        Получить варианты имени.

        Примечание: Основное сравнение имён делается через LLM в check_same_entity_llm().
        Этот метод оставлен для базовой нормализации.
        """
        # Возвращаем только нормализованное имя
        # LLM сам разберётся с уменьшительными формами, транслитерацией и т.д.
        return [name.lower().strip()]

    async def _find_by_external_ids(
        self,
        entity_type: str,
        organization_id: str,
        external_ids: dict[str, str],
    ) -> ResolvedEntity | None:
        """Поиск по внешним ID"""
        if not self.neo4j:
            return None

        # Cypher для поиска по любому из external_ids
        for key, value in external_ids.items():
            query = f"""
            MATCH (e:{entity_type.title()})
            WHERE e.organization_id = $org_id
              AND e.external_ids.{key} = $value
            RETURN e, elementId(e) as id
            LIMIT 1
            """
            results = await self.neo4j.execute_query(
                query, {"org_id": organization_id, "value": value}
            )

            if results:
                node = results[0]
                return ResolvedEntity(
                    canonical_id=node["id"],
                    name=node["e"].get("name", ""),
                    entity_type=entity_type,
                    is_new=False,
                    confidence=1.0,
                    aliases=node["e"].get("aliases", []),
                    external_ids=node["e"].get("external_ids", {}),
                )

        return None

    async def _find_by_exact_name(
        self,
        normalized_name: str,
        entity_type: str,
        organization_id: str,
        tenants: list[str] | None = None,
    ) -> ResolvedEntity | None:
        """Точное совпадение по имени"""
        # Сначала пробуем NetworkX
        if self.graph and hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
            return self._find_by_exact_name_networkx(
                normalized_name, entity_type, tenants=tenants)

        # Пробуем Neo4j через graph_builder.driver (если graph_builder подключён к Neo4j)
        if self.graph and hasattr(self.graph, 'driver') and self.graph.driver:
            return await self._find_by_exact_name_neo4j_via_graph(
                normalized_name, entity_type, organization_id, tenants=tenants
            )

        if not self.neo4j:
            return None

        query = f"""
        MATCH (e:{entity_type.title()})
        WHERE e.organization_id = $org_id
          AND toLower(e.name) = $name
        RETURN e, elementId(e) as id
        LIMIT 1
        """
        results = await self.neo4j.execute_query(
            query, {"org_id": organization_id, "name": normalized_name}
        )

        if results:
            node = results[0]
            return ResolvedEntity(
                canonical_id=node["id"],
                name=node["e"].get("name", ""),
                entity_type=entity_type,
                is_new=False,
                confidence=1.0,
                aliases=node["e"].get("aliases", []),
                external_ids=node["e"].get("external_ids", {}),
            )

        return None

    async def _find_by_exact_name_neo4j_via_graph(
        self,
        normalized_name: str,
        entity_type: str,
        organization_id: str,
        tenants: list[str] | None = None,
    ) -> ResolvedEntity | None:
        """Точное совпадение по имени через graph_builder Neo4j driver."""
        try:
            label = entity_type.title()
            if entity_type == "person":
                label = "Person"

            tenant_clause = ""
            params: dict[str, Any] = {"name": normalized_name}
            if tenants:
                tenant_clause = ("AND (e.tenant_id IS NULL OR e.tenant_id = '' "
                                 "OR e.tenant_id IN $tenants)")
                params["tenants"] = tenants
            query = f"""
            MATCH (e:{label})
            WHERE toLower(e.name) = $name {tenant_clause}
            RETURN e, e.id as node_id
            LIMIT 1
            """
            async with self.graph.driver.session() as session:
                result = await session.run(query, params)
                rows = await result.data()

                if rows:
                    row = rows[0]
                    node = row.get("e")
                    node_id = row.get("node_id") or (dict(node).get("id") if node else None)
                    props = dict(node) if node else {}

                    if node_id:
                        aliases = props.get("aliases", [])
                        if isinstance(aliases, str):
                            try:
                                aliases = json.loads(aliases)
                            except (json.JSONDecodeError, TypeError):
                                aliases = [aliases] if aliases else []

                        external_ids = props.get("external_ids", {})
                        if isinstance(external_ids, str):
                            try:
                                external_ids = json.loads(external_ids)
                            except (json.JSONDecodeError, TypeError):
                                external_ids = {}

                        return ResolvedEntity(
                            canonical_id=node_id,
                            name=props.get("name", ""),
                            entity_type=entity_type,
                            is_new=False,
                            confidence=1.0,
                            aliases=aliases if isinstance(aliases, list) else [],
                            external_ids=external_ids if isinstance(external_ids, dict) else {},
                        )
        except Exception as e:
            logger.warning(f"Neo4j exact name search failed: {e}")

        return None

    def _find_by_exact_name_networkx(
        self,
        normalized_name: str,
        entity_type: str,
        tenants: list[str] | None = None,
    ) -> ResolvedEntity | None:
        """Точное совпадение по имени в NetworkX"""
        if not self.graph or not self.graph.nx_graph:
            return None

        # Определяем метку для поиска
        label = entity_type.title()
        if entity_type == "person":
            label = "Person"
        elif entity_type in ("project", "product", "technology", "company", "team"):
            label = "Entity"

        for node_id, attrs in self.graph.nx_graph.nodes(data=True):
            node_label = attrs.get("_label", "")
            if node_label != label:
                continue

            # Для Entity проверяем entity_type
            if label == "Entity" and attrs.get("entity_type") != entity_type:
                continue

            if not self._tenant_pass(attrs, tenants):
                continue

            existing_name = attrs.get("name", "").lower().strip()
            if existing_name == normalized_name:
                return ResolvedEntity(
                    canonical_id=node_id,
                    name=attrs.get("name", ""),
                    entity_type=entity_type,
                    is_new=False,
                    confidence=1.0,
                    aliases=attrs.get("aliases", []),
                    external_ids=attrs.get("external_ids", {}),
                )

        return None

    async def _find_by_alias(
        self,
        normalized_name: str,
        entity_type: str,
        organization_id: str,
        tenants: list[str] | None = None,
    ) -> ResolvedEntity | None:
        """Поиск по aliases"""
        # Сначала пробуем NetworkX
        if self.graph and hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
            return self._find_by_alias_networkx(
                normalized_name, entity_type, tenants=tenants)

        # Пробуем Neo4j через graph_builder.driver
        if self.graph and hasattr(self.graph, 'driver') and self.graph.driver:
            return await self._find_by_alias_neo4j_via_graph(
                normalized_name, entity_type, tenants=tenants
            )

        if not self.neo4j:
            return None

        query = f"""
        MATCH (e:{entity_type.title()})
        WHERE e.organization_id = $org_id
          AND $name IN [alias IN e.aliases | toLower(alias)]
        RETURN e, elementId(e) as id
        LIMIT 1
        """
        results = await self.neo4j.execute_query(
            query, {"org_id": organization_id, "name": normalized_name}
        )

        if results:
            node = results[0]
            return ResolvedEntity(
                canonical_id=node["id"],
                name=node["e"].get("name", ""),
                entity_type=entity_type,
                is_new=False,
                confidence=0.95,
                aliases=node["e"].get("aliases", []),
                external_ids=node["e"].get("external_ids", {}),
            )

        return None

    async def _find_by_alias_neo4j_via_graph(
        self,
        normalized_name: str,
        entity_type: str,
        tenants: list[str] | None = None,
    ) -> ResolvedEntity | None:
        """Поиск по aliases через graph_builder Neo4j driver."""
        try:
            label = entity_type.title()
            if entity_type == "person":
                label = "Person"

            tenant_clause = ""
            params: dict[str, Any] = {"name": normalized_name}
            if tenants:
                tenant_clause = ("AND (e.tenant_id IS NULL OR e.tenant_id = '' "
                                 "OR e.tenant_id IN $tenants)")
                params["tenants"] = tenants
            # aliases хранятся как list<string> или JSON-строка
            query = f"""
            MATCH (e:{label})
            WHERE $name IN [alias IN e.aliases | toLower(alias)] {tenant_clause}
            RETURN e, e.id as node_id
            LIMIT 1
            """
            async with self.graph.driver.session() as session:
                result = await session.run(query, params)
                rows = await result.data()

                if rows:
                    row = rows[0]
                    node = row.get("e")
                    node_id = row.get("node_id") or (dict(node).get("id") if node else None)
                    props = dict(node) if node else {}

                    if node_id:
                        return ResolvedEntity(
                            canonical_id=node_id,
                            name=props.get("name", ""),
                            entity_type=entity_type,
                            is_new=False,
                            confidence=0.95,
                            aliases=props.get("aliases", []) if isinstance(props.get("aliases"), list) else [],
                            external_ids={},
                        )
        except Exception as e:
            logger.debug(f"Neo4j alias search failed (non-critical): {e}")

        return None

    def _find_by_alias_networkx(
        self,
        normalized_name: str,
        entity_type: str,
        tenants: list[str] | None = None,
    ) -> ResolvedEntity | None:
        """Поиск по aliases в NetworkX"""
        if not self.graph or not self.graph.nx_graph:
            return None

        # Определяем метку для поиска
        label = entity_type.title()
        if entity_type == "person":
            label = "Person"
        elif entity_type in ("project", "product", "technology", "company", "team"):
            label = "Entity"

        for node_id, attrs in self.graph.nx_graph.nodes(data=True):
            node_label = attrs.get("_label", "")
            if node_label != label:
                continue

            # Для Entity проверяем entity_type
            if label == "Entity" and attrs.get("entity_type") != entity_type:
                continue

            if not self._tenant_pass(attrs, tenants):
                continue

            # Проверяем aliases
            aliases = attrs.get("aliases", [])
            if isinstance(aliases, list):
                for alias in aliases:
                    if alias.lower().strip() == normalized_name:
                        return ResolvedEntity(
                            canonical_id=node_id,
                            name=attrs.get("name", ""),
                            entity_type=entity_type,
                            is_new=False,
                            confidence=0.95,
                            aliases=aliases,
                            external_ids=attrs.get("external_ids", {}),
                        )

        return None

    async def _find_by_fuzzy(
        self,
        normalized_name: str,
        entity_type: str,
        organization_id: str,
        tenants: list[str] | None = None,
    ) -> Optional[ResolvedEntity]:
        """Нечёткий поиск"""
        # Сначала пробуем NetworkX
        if self.graph and hasattr(self.graph, 'use_networkx') and self.graph.use_networkx:
            return await self._find_by_fuzzy_networkx(
                normalized_name, entity_type, tenants=tenants)

        # Пробуем Neo4j через graph_builder.driver
        if self.graph and hasattr(self.graph, 'driver') and self.graph.driver:
            return await self._find_by_fuzzy_neo4j_via_graph(
                normalized_name, entity_type, tenants=tenants
            )

        # Иначе Neo4j (legacy)
        if not self.neo4j:
            return None

        # Получаем все сущности типа
        query = f"""
        MATCH (e:{entity_type.title()})
        WHERE e.organization_id = $org_id
        RETURN e, elementId(e) as id
        """
        results = await self.neo4j.execute_query(
            query, {"org_id": organization_id}
        )

        best_match = None
        best_score = 0.0

        for node in results:
            existing_name = node["e"].get("name", "").lower()
            score = SequenceMatcher(None, normalized_name, existing_name).ratio()

            if score > best_score:
                best_score = score
                best_match = node

        if best_match and best_score >= self.fuzzy_threshold:
            return ResolvedEntity(
                canonical_id=best_match["id"],
                name=best_match["e"].get("name", ""),
                entity_type=entity_type,
                is_new=False,
                confidence=best_score,
                aliases=best_match["e"].get("aliases", []),
                external_ids=best_match["e"].get("external_ids", {}),
            )

        return None

    async def _find_by_fuzzy_neo4j_via_graph(
        self,
        normalized_name: str,
        entity_type: str,
        tenants: list[str] | None = None,
    ) -> Optional[ResolvedEntity]:
        """Нечёткий поиск через graph_builder Neo4j driver."""
        try:
            label = entity_type.title()
            if entity_type == "person":
                label = "Person"

            tenant_clause = ""
            params: dict[str, Any] = {}
            if tenants:
                tenant_clause = ("WHERE (e.tenant_id IS NULL OR e.tenant_id = '' "
                                 "OR e.tenant_id IN $tenants)")
                params["tenants"] = tenants
            query = f"""
            MATCH (e:{label})
            {tenant_clause}
            RETURN e.name as name, e.id as node_id, e.aliases as aliases
            LIMIT 500
            """
            async with self.graph.driver.session() as session:
                result = await session.run(query, params)
                rows = await result.data()

            best_match = None
            best_score = 0.0

            for row in rows:
                existing_name = (row.get("name") or "").lower()
                if not existing_name:
                    continue

                score = SequenceMatcher(None, normalized_name, existing_name).ratio()

                if score > best_score:
                    best_score = score
                    best_match = row

            if best_match and best_score >= self.fuzzy_threshold:
                node_id = best_match.get("node_id", "")
                aliases = best_match.get("aliases", [])
                if isinstance(aliases, str):
                    try:
                        aliases = json.loads(aliases)
                    except (json.JSONDecodeError, TypeError):
                        aliases = []

                return ResolvedEntity(
                    canonical_id=node_id,
                    name=best_match.get("name", ""),
                    entity_type=entity_type,
                    is_new=False,
                    confidence=best_score,
                    aliases=aliases if isinstance(aliases, list) else [],
                    external_ids={},
                )
        except Exception as e:
            logger.debug(f"Neo4j fuzzy search failed (non-critical): {e}")

        return None

    async def _find_by_fuzzy_networkx(
        self,
        normalized_name: str,
        entity_type: str,
        tenants: list[str] | None = None,
    ) -> Optional[ResolvedEntity]:
        """Нечёткий поиск в NetworkX графе"""
        if not self.graph or not self.graph.nx_graph:
            return None

        # Определяем метку для поиска
        label = entity_type.title()
        if entity_type == "person":
            label = "Person"
        elif entity_type == "project":
            label = "Entity"

        best_match = None
        best_score = 0.0

        # Перебираем узлы нужного типа
        for node_id, attrs in self.graph.nx_graph.nodes(data=True):
            node_label = attrs.get("_label", "")
            if node_label != label:
                continue

            # Для Entity проверяем entity_type
            if label == "Entity" and attrs.get("entity_type") != entity_type:
                continue

            if not self._tenant_pass(attrs, tenants):
                continue

            existing_name = attrs.get("name", "").lower()

            # Вычисляем схожесть
            score = SequenceMatcher(None, normalized_name, existing_name).ratio()

            # Также проверяем варианты имён
            name_variants = self._get_name_variants(normalized_name, entity_type)
            for variant in name_variants:
                variant_score = SequenceMatcher(None, variant, existing_name).ratio()
                score = max(score, variant_score)

            if score > best_score:
                best_score = score
                best_match = (node_id, attrs)

        if best_match and best_score >= self.fuzzy_threshold:
            node_id, attrs = best_match
            return ResolvedEntity(
                canonical_id=node_id,
                name=attrs.get("name", ""),
                entity_type=entity_type,
                is_new=False,
                confidence=best_score,
                aliases=attrs.get("aliases", []),
                external_ids=attrs.get("external_ids", {}),
            )

        return None

    async def _add_alias(self, entity_id: str, alias: str) -> bool:
        """Добавить alias к существующей сущности"""
        if not self.neo4j:
            return False

        query = """
        MATCH (e)
        WHERE elementId(e) = $id
        SET e.aliases = coalesce(e.aliases, []) + $alias
        RETURN e
        """
        results = await self.neo4j.execute_query(
            query, {"id": entity_id, "alias": alias}
        )
        return len(results) > 0

    async def _create_entity(
        self,
        name: str,
        normalized_name: str,
        entity_type: str,
        organization_id: str,
        external_ids: dict[str, str],
        properties: dict[str, Any],
        project_id: str | None,
    ) -> ResolvedEntity:
        """Создать новую сущность"""
        # Для NetworkX используем стабильный ID на основе имени и типа
        # Это позволяет находить ту же сущность при повторной обработке
        if self.graph and hasattr(self.graph, 'nx_graph'):
            # Стабильный ID: person_имя или entity_тип_имя
            if entity_type == "person":
                canonical_id = f"person_{normalized_name.replace(' ', '_')}"
            else:
                canonical_id = f"entity_{entity_type}_{normalized_name.replace(' ', '_')}"
            # Анти-коллизия общего Neo4j: create_node MERGE-ит по id, поэтому
            # одинаковое имя в двух аккаунтах без суффикса склеивалось в ОДИН
            # узел (кросс-tenant утечка свойств). Новым узлам — суффикс
            # канонического тенанта; существующие находятся по имени раньше,
            # их id не меняются.
            try:
                from backend.core.store.tenant_scope import canonical_tenant
                _t = canonical_tenant(organization_id)
                if _t:
                    canonical_id = f"{canonical_id}__{_t}"
            except Exception:
                pass
        else:
            canonical_id = str(uuid4())

        # Подготавливаем свойства
        node_props = {
            "canonical_id": canonical_id,
            "name": name,
            "normalized_name": normalized_name,
            "organization_id": organization_id,
            "aliases": [],
            "external_ids": external_ids,
            **properties,
        }

        if project_id:
            node_props["project_id"] = project_id

        if self.neo4j:
            # Создаём узел в Neo4j
            label = entity_type.title()
            neo4j_id = await self.neo4j.create_node(label, node_props)

            logger.info(
                "✅ New entity created",
                canonical_id=canonical_id[:8],
                name=name,
                entity_type=entity_type,
            )

            return ResolvedEntity(
                canonical_id=neo4j_id or canonical_id,
                name=name,
                entity_type=entity_type,
                is_new=True,
                confidence=1.0,
                matched_by=None,
                aliases=[],
                external_ids=external_ids,
                properties=properties,
            )

        # Для NetworkX возвращаем со стабильным ID
        return ResolvedEntity(
            canonical_id=canonical_id,
            name=name,
            entity_type=entity_type,
            is_new=True,
            confidence=1.0,
            matched_by=None,
            aliases=[],
            external_ids=external_ids,
            properties=properties,
        )

    async def merge_entities(
        self,
        source_id: str,
        target_id: str,
    ) -> bool:
        """
        Объединить две сущности

        Все связи source переносятся на target,
        aliases объединяются, source удаляется.
        """
        if not self.neo4j:
            return False

        # 1. Перенести aliases
        query_aliases = """
        MATCH (source), (target)
        WHERE elementId(source) = $source_id
          AND elementId(target) = $target_id
        SET target.aliases = target.aliases + source.aliases + [source.name]
        """
        await self.neo4j.execute_query(
            query_aliases, {"source_id": source_id, "target_id": target_id}
        )

        # 2. Перенести связи
        query_rels = """
        MATCH (source)-[r]->(other)
        WHERE elementId(source) = $source_id
        WITH source, r, other, type(r) as rel_type, properties(r) as rel_props
        MATCH (target)
        WHERE elementId(target) = $target_id
        CREATE (target)-[new_r:MERGED_REL]->(other)
        SET new_r = rel_props, new_r.original_type = rel_type
        DELETE r
        """
        await self.neo4j.execute_query(
            query_rels, {"source_id": source_id, "target_id": target_id}
        )

        # 3. Удалить source
        query_delete = """
        MATCH (source)
        WHERE elementId(source) = $source_id
        DETACH DELETE source
        """
        await self.neo4j.execute_query(
            query_delete, {"source_id": source_id}
        )

        logger.info(
            "🔗 Entities merged",
            source_id=source_id[:8],
            target_id=target_id[:8],
        )

        # Очищаем кеш
        self._cache.clear()

        return True

    def clear_cache(self):
        """Очистить кеш резолвинга"""
        self._cache.clear()
        self._alias_index.clear()

    async def find_merge_candidates(
        self,
        entity_type: Optional[str] = None,
        threshold: float = 0.80
    ) -> List[MergeCandidate]:
        """
        Найти кандидатов на слияние.

        Args:
            entity_type: Тип сущности для поиска (None = все типы)
            threshold: Минимальный порог схожести

        Returns:
            Список кандидатов на слияние
        """
        candidates = []

        # Используем NetworkX напрямую если доступен (быстрее)
        if self.graph and self.graph.connected and self.graph.use_networkx:
            candidates = await self._find_candidates_networkx(entity_type, threshold)
        elif self.graph and self.graph.connected:
            # Neo4j (или иной бэкенд) через абстракцию graph_builder
            candidates = await self._find_candidates_via_builder(entity_type, threshold)
        elif self.neo4j:
            candidates = await self._find_candidates_neo4j(entity_type, threshold)

        # Сортируем по схожести
        candidates.sort(key=lambda x: x.similarity_score, reverse=True)

        return candidates

    # Метки графа для типов, которые умеем дедуплицировать.
    _DEDUP_TYPE_LABELS = {
        "person": "Person",
        "project": "Project",
        "product": "Product",
        "team": "Team",
        "department": "Department",
        "idea": "Idea",
        # Контрагенты/партнёры B2B: живут под меткой Entity с
        # entity_type company|organization (клиент/партнёр/конкурент
        # нормализуются экстракцией в organization)
        "company": "Entity",
    }

    @staticmethod
    def _first_token_census(names: List[str]) -> Dict[str, int]:
        """Сколько сущностей начинаются с каждого первого токена."""
        census: Dict[str, int] = {}
        for nm in names:
            tok = (nm or "").strip().lower().split()
            if tok:
                census[tok[0]] = census.get(tok[0], 0) + 1
        return census

    def _bare_name_score(self, name1: str, name2: str,
                         census: Dict[str, int]) -> float:
        """«Максим» ↔ «Максим Белухин»: голое имя сливается с полным ТОЛЬКО
        когда это имя в графе уникально (ровно у этих двух узлов). Если есть
        ещё «Максим Фельдман» — не трогаем: слияние по голому имени опасно."""
        a = name1.strip().lower().split()
        b = name2.strip().lower().split()
        if not a or not b:
            return 0.0
        bare, full = (a, b) if len(a) == 1 else ((b, a) if len(b) == 1 else (None, None))
        if bare is None or len(full) < 2:
            return 0.0
        if bare[0] != full[0]:
            return 0.0
        # ровно 2 носителя первого токена: сам голый + этот полный
        return 0.96 if census.get(bare[0], 0) == 2 else 0.0

    async def _find_candidates_via_builder(
        self,
        entity_type: Optional[str],
        threshold: float,
    ) -> List[MergeCandidate]:
        """Backend-agnostic поиск кандидатов через graph_builder.

        Работает и на Neo4j, и на NetworkX (раньше Neo4j-ветка была заглушкой
        `return []`, поэтому dedup на Neo4j вообще не находил дублей).
        """
        if not self.graph:
            return []

        # Определяем какие типы перебирать
        if entity_type:
            type_labels = {entity_type: self._DEDUP_TYPE_LABELS.get(
                entity_type, entity_type.capitalize())}
        else:
            type_labels = dict(self._DEDUP_TYPE_LABELS)

        candidates: List[MergeCandidate] = []

        for node_type, label in type_labels.items():
            try:
                nodes = await self.graph.find_nodes_by_label(label, limit=1000)
            except Exception as e:
                logger.debug(f"find_nodes_by_label({label}) failed: {e}")
                continue

            # Оставляем только узлы с именем
            named = [
                (n.get("id"), n)
                for n in nodes
                if n.get("id") and (n.get("name") or "").strip()
            ]

            # Перепись первых токенов — для безопасного слияния голых имён
            census = self._first_token_census(
                [(a.get("name") or "") for _id, a in named])

            for i in range(len(named)):
                id1, attrs1 = named[i]
                name1 = (attrs1.get("name") or "").lower()
                for j in range(i + 1, len(named)):
                    id2, attrs2 = named[j]
                    name2 = (attrs2.get("name") or "").lower()

                    # токен-aware сравнение (скобки, перестановка слов,
                    # канонизация терминов) вместо плоского SequenceMatcher
                    score = self._fuzzy_score(name1, name2)

                    if node_type == "person":
                        variants1 = self._get_name_variants(name1, "person")
                        variants2 = self._get_name_variants(name2, "person")
                        for v1 in variants1:
                            for v2 in variants2:
                                score = max(
                                    score,
                                    SequenceMatcher(None, v1, v2).ratio(),
                                )
                        score = max(score, self._bare_name_score(
                            name1, name2, census))

                    if score >= threshold:
                        candidates.append(MergeCandidate(
                            entity1_id=id1,
                            entity1_name=attrs1.get("name", ""),
                            entity2_id=id2,
                            entity2_name=attrs2.get("name", ""),
                            entity_type=node_type,
                            similarity_score=score,
                            match_reason="fuzzy",
                            auto_merge=score >= self.auto_merge_fuzzy,
                        ))

        return candidates

    async def _merge_via_builder(self, keep_id: str, merge_id: str) -> bool:
        """Backend-agnostic слияние, СОХРАНЯЮЩЕЕ типы связей.

        graph_builder.merge_nodes на Neo4j переименовывает все рёбра в
        RELATES_TO (теряет DECIDED/ASSIGNED_TO/...). Здесь переносим связи
        через create_relationship с исходным типом, поэтому запросы по
        конкретным типам рёбер продолжают работать.
        """
        if not self.graph:
            return False
        try:
            keep_node = await self.graph.get_node_by_id(keep_id)
            merge_node = await self.graph.get_node_by_id(merge_id)
            if not keep_node or not merge_node:
                return False

            # 1. Переносим aliases / имя merge → keep
            keep_aliases = keep_node.get("aliases") or []
            if isinstance(keep_aliases, str):
                import json as _json
                try:
                    keep_aliases = _json.loads(keep_aliases)
                except (ValueError, TypeError):
                    keep_aliases = [keep_aliases] if keep_aliases else []
            merge_aliases = merge_node.get("aliases") or []
            if isinstance(merge_aliases, str):
                import json as _json
                try:
                    merge_aliases = _json.loads(merge_aliases)
                except (ValueError, TypeError):
                    merge_aliases = [merge_aliases] if merge_aliases else []
            merge_name = merge_node.get("name", "")
            new_aliases = list({*keep_aliases, *merge_aliases, merge_name} - {"", None})

            # Заполняем пустые свойства keep значениями из merge (ничего не теряем)
            fill = {}
            for k, v in merge_node.items():
                if k in ("id",) or k.startswith("_"):
                    continue
                if k == "aliases":
                    continue
                existing = keep_node.get(k)
                if (existing in ("", None, [], {})) and v not in ("", None, [], {}):
                    fill[k] = v
            fill["aliases"] = new_aliases
            # Вовлечённость СУММИРУЕМ, а не «заполняем если пусто»: иначе
            # канонический узел с 1-2 упоминаниями съедает частый узел,
            # выпадает из топ-N по engagement — и реальные руководители
            # исчезали из иерархии/снапшотов.
            try:
                _tm = (int(keep_node.get("total_mentions") or 0)
                       + int(merge_node.get("total_mentions") or 0))
                if _tm:
                    fill["total_mentions"] = _tm
            except (TypeError, ValueError):
                pass
            await self.graph.update_node(keep_id, fill)

            # 2. Переносим связи с сохранением типа
            rels = await self.graph.get_node_relationships(merge_id)
            for edge in rels.get("outgoing", []):
                tgt = edge.get("target")
                rtype = edge.get("type")
                if not tgt or not rtype or tgt == keep_id:
                    continue
                props = {k: v for k, v in edge.items()
                         if k not in ("target", "source", "type") and not k.startswith("_")}
                await self.graph.create_relationship(keep_id, tgt, rtype, props)
            for edge in rels.get("incoming", []):
                src = edge.get("source")
                rtype = edge.get("type")
                if not src or not rtype or src == keep_id:
                    continue
                props = {k: v for k, v in edge.items()
                         if k not in ("target", "source", "type") and not k.startswith("_")}
                await self.graph.create_relationship(src, keep_id, rtype, props)

            # 3. Удаляем merge-узел (вместе с его старыми связями)
            await self.graph.remove_node(merge_id)

            # 4. Обновляем индекс/кеш
            if merge_name:
                self._alias_index[merge_name.lower()] = keep_id
            for a in new_aliases:
                if a:
                    self._alias_index[a.lower()] = keep_id
            self._cache.clear()

            logger.info(
                f"🔗 Merged (builder): {merge_name} -> {keep_node.get('name', '')}"
            )
            return True
        except Exception as e:
            logger.error(f"❌ _merge_via_builder error {keep_id}+{merge_id}: {e}")
            return False

    async def _find_candidates_networkx(
        self,
        entity_type: Optional[str],
        threshold: float
    ) -> List[MergeCandidate]:
        """Поиск кандидатов в NetworkX"""
        if not self.graph or not self.graph.nx_graph:
            return []

        candidates = []
        processed_pairs = set()

        # Собираем узлы по типам
        nodes_by_type: Dict[str, List[Tuple[str, Dict]]] = {}

        for node_id, attrs in self.graph.nx_graph.nodes(data=True):
            label = attrs.get("_label", "")

            # Определяем тип
            if label == "Person":
                node_type = "person"
            elif label == "Entity":
                node_type = attrs.get("entity_type", "entity")
            elif label == "Task":
                node_type = "task"
            elif label == "Decision":
                node_type = "decision"
            else:
                continue

            # Фильтруем по типу если указан
            if entity_type and node_type != entity_type:
                continue

            if node_type not in nodes_by_type:
                nodes_by_type[node_type] = []
            nodes_by_type[node_type].append((node_id, attrs))

        # Сравниваем узлы одного типа
        for node_type, nodes in nodes_by_type.items():
            census = self._first_token_census(
                [a.get("name", "") for _id, a in nodes])
            for i, (id1, attrs1) in enumerate(nodes):
                for j, (id2, attrs2) in enumerate(nodes):
                    if i >= j:
                        continue

                    # Пропускаем уже обработанные пары
                    pair_key = tuple(sorted([id1, id2]))
                    if pair_key in processed_pairs:
                        continue
                    processed_pairs.add(pair_key)

                    # Сравниваем имена
                    name1 = attrs1.get("name", "").lower()
                    name2 = attrs2.get("name", "").lower()

                    if not name1 or not name2:
                        continue

                    # Fuzzy matching (токен-aware: скобки, перестановки,
                    # канонизация терминов — см. _fuzzy_score)
                    score = self._fuzzy_score(name1, name2)

                    # Проверяем варианты имён для людей
                    if node_type == "person":
                        variants1 = self._get_name_variants(name1, "person")
                        variants2 = self._get_name_variants(name2, "person")

                        for v1 in variants1:
                            for v2 in variants2:
                                variant_score = SequenceMatcher(None, v1, v2).ratio()
                                score = max(score, variant_score)
                        score = max(score, self._bare_name_score(
                            name1, name2, census))

                    if score >= threshold:
                        # Определяем можно ли автоматически слить
                        auto_merge = score >= 0.95

                        candidates.append(MergeCandidate(
                            entity1_id=id1,
                            entity1_name=attrs1.get("name", ""),
                            entity2_id=id2,
                            entity2_name=attrs2.get("name", ""),
                            entity_type=node_type,
                            similarity_score=score,
                            match_reason="fuzzy",
                            auto_merge=auto_merge
                        ))

        return candidates

    async def _find_candidates_neo4j(
        self,
        entity_type: Optional[str],
        threshold: float
    ) -> List[MergeCandidate]:
        """Поиск кандидатов в Neo4j"""
        if not self.neo4j:
            return []

        # TODO: Реализовать для Neo4j
        return []

    async def merge_entities_networkx(
        self,
        source_id: str,
        target_id: str
    ) -> bool:
        """
        Слить две сущности в NetworkX графе.

        Args:
            source_id: ID сущности-источника (будет удалена)
            target_id: ID целевой сущности (останется)

        Returns:
            True если слияние успешно
        """
        if not self.graph or not self.graph.nx_graph:
            return False

        g = self.graph.nx_graph

        if not g.has_node(source_id) or not g.has_node(target_id):
            logger.warning(f"Nodes not found: {source_id}, {target_id}")
            return False

        source_attrs = dict(g.nodes[source_id])
        target_attrs = dict(g.nodes[target_id])

        # 1. Объединяем aliases
        source_aliases = source_attrs.get("aliases", [])
        target_aliases = target_attrs.get("aliases", [])
        source_name = source_attrs.get("name", "")

        new_aliases = list(set(target_aliases + source_aliases + [source_name]))
        g.nodes[target_id]["aliases"] = new_aliases

        # 2. Объединяем external_ids
        source_ext = source_attrs.get("external_ids", {})
        target_ext = target_attrs.get("external_ids", {})
        if isinstance(source_ext, dict) and isinstance(target_ext, dict):
            g.nodes[target_id]["external_ids"] = {**source_ext, **target_ext}

        # 3. Переносим связи
        # Исходящие связи
        for _, target_node, _key, edge_data in list(g.out_edges(source_id, keys=True, data=True)):
            if target_node != target_id:  # Не создаём петлю
                g.add_edge(target_id, target_node, **edge_data)

        # Входящие связи
        for source_node, _, _key, edge_data in list(g.in_edges(source_id, keys=True, data=True)):
            if source_node != target_id:  # Не создаём петлю
                g.add_edge(source_node, target_id, **edge_data)

        # 4. Удаляем source узел
        g.remove_node(source_id)

        # 5. Обновляем индекс
        self._alias_index[source_name.lower()] = target_id
        for alias in source_aliases:
            self._alias_index[alias.lower()] = target_id

        logger.info(
            f"🔗 Merged entities: {source_attrs.get('name', '')} -> {target_attrs.get('name', '')}"
        )

        # Очищаем кеш
        self._cache.clear()

        return True

    async def auto_merge_duplicates(
        self,
        entity_type: Optional[str] = None,
        threshold: float = 0.80,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """
        Автоматически слить дубликаты (guardrail B: умный dedup).

        Логика по полосам схожести:
        - score >= auto_merge_fuzzy (0.95): сливаем сразу (очевидный дубль);
        - llm_band_low (0.80) <= score < 0.95: спрашиваем LLM — он понимает
          уменьшительные («Катя»/«Екатерина»), транслитерацию, опечатки;
          сливаем только если LLM подтвердил с confidence >= порога;
        - score < 0.80: не трогаем.

        Раньше авто-слияние было только при >= 0.95, поэтому безопасные
        варианты имён не сливались и дубли копились.

        Args:
            entity_type: Тип сущности (None = все)
            threshold: Нижняя граница рассмотрения (по умолчанию 0.80)
            use_llm: Использовать LLM для неоднозначной полосы

        Returns:
            Статистика слияния
        """
        stats = {
            "candidates_found": 0,
            "merged": 0,
            "merged_high_fuzzy": 0,
            "merged_llm": 0,
            "llm_rejected": 0,
            "skipped": 0,
            "errors": 0,
        }

        # Рассматриваем всё от нижней границы (0.80), решение — по полосам
        band_low = min(threshold, self.llm_band_low)
        candidates = await self.find_merge_candidates(entity_type, band_low)
        stats["candidates_found"] = len(candidates)

        merged_ids = set()
        # От самых похожих к менее похожим — высокая уверенность раньше
        candidates.sort(key=lambda c: c.similarity_score, reverse=True)

        for candidate in candidates:
            if candidate.entity1_id in merged_ids or candidate.entity2_id in merged_ids:
                stats["skipped"] += 1
                continue

            score = candidate.similarity_score
            do_merge = False
            merge_kind = None

            if score >= self.auto_merge_fuzzy:
                do_merge = True
                merge_kind = "high_fuzzy"
            elif use_llm and score >= self.llm_band_low:
                try:
                    is_same, conf, reason = await self.check_same_entity_llm(
                        candidate.entity1_name,
                        candidate.entity2_name,
                        candidate.entity_type,
                    )
                    if is_same and conf >= self.llm_auto_merge_confidence:
                        do_merge = True
                        merge_kind = "llm"
                    else:
                        stats["llm_rejected"] += 1
                except Exception as e:
                    logger.debug(f"LLM dedup check failed: {e}")
                    stats["skipped"] += 1
                    continue
            else:
                stats["skipped"] += 1
                continue

            if not do_merge:
                continue

            try:
                if self.graph and self.graph.connected:
                    # Единый backend-agnostic путь, сохраняющий типы связей
                    success = await self._merge_via_builder(
                        candidate.entity1_id, candidate.entity2_id
                    )
                elif self.neo4j:
                    success = await self.merge_entities(
                        candidate.entity1_id, candidate.entity2_id
                    )
                else:
                    success = False

                if success:
                    merged_ids.add(candidate.entity2_id)
                    stats["merged"] += 1
                    if merge_kind == "high_fuzzy":
                        stats["merged_high_fuzzy"] += 1
                    elif merge_kind == "llm":
                        stats["merged_llm"] += 1
                else:
                    stats["errors"] += 1
            except Exception as e:
                logger.error(f"Merge error: {e}")
                stats["errors"] += 1

        # Сохраняем граф (NetworkX)
        if stats["merged"] and self.graph and self.graph.use_networkx:
            self.graph.save_graph()

        if stats["merged"]:
            logger.info(
                f"🔗 Dedup {entity_type or 'all'}: merged {stats['merged']} "
                f"({stats['merged_high_fuzzy']} fuzzy, {stats['merged_llm']} llm), "
                f"{stats['llm_rejected']} llm-rejected"
            )

        return stats

    async def _build_activity_profiles(
        self,
        named: List[Tuple[str, Dict[str, Any]]],
        tenant: Optional[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Компактный профиль активности на каждую запись — для content-aware
        дедупа. Дёшево: 1 label-скан проектов + get_node_relationships на
        запись. Расшифровываем соседей через id→name карты (без get_node_by_id
        на каждого соседа)."""
        id_to_name = {nid: (n.get("name") or "") for nid, n in named}
        project_names: Dict[str, str] = {}
        try:
            projects = await self.graph.find_nodes_by_label(
                "Project", limit=2000, tenant_id=tenant, strict_tenant=False)
            for pr in projects:
                if pr.get("id"):
                    project_names[pr["id"]] = pr.get("name", "")
        except Exception:
            logger.debug("project scan for profiles failed", exc_info=True)

        profiles: Dict[str, Dict[str, Any]] = {}
        for nid, node in named:
            projects_set, colleagues_set, degree = set(), set(), 0
            try:
                rels = await self.graph.get_node_relationships(nid)
                edges = (rels.get("outgoing") or []) + (rels.get("incoming") or [])
                degree = len(edges)
                for e in edges:
                    other = e.get("target") or e.get("source")
                    if not other or other == nid:
                        continue
                    if other in project_names and project_names[other]:
                        projects_set.add(project_names[other])
                    elif other in id_to_name and id_to_name[other]:
                        colleagues_set.add(id_to_name[other])
            except Exception:
                logger.debug(f"relationships for {nid} failed", exc_info=True)
            profiles[nid] = {
                "role": node.get("role") or "",
                "department": node.get("department") or "",
                "projects": sorted(projects_set)[:4],
                "colleagues": sorted(colleagues_set)[:5],
                "degree": degree,
            }
        return profiles

    async def cluster_duplicates_llm(
        self,
        entity_type: str = "person",
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Content-aware кластеризация дублей ОДНИМ LLM-вызовом.

        Вместо N попарных проверок собираем все записи с профилем активности
        (роль/отдел/проекты/коллеги) и просим LLM сгруппировать те, что относятся
        к ОДНОМУ человеку — учитывая уменьшительные/транслит, но НЕ сливая разных
        людей с похожим именем (проверка по активности). Возвращает кластеры
        (2+ записи) с confidence и обоснованием. НЕ мержит.
        """
        label = self._DEDUP_TYPE_LABELS.get(entity_type, entity_type.capitalize())
        if not self.graph or not getattr(self.graph, "connected", False):
            return []
        # tenant_id передаётся вызывающим (user_id). Если None — find_nodes_by_
        # label сам возьмёт из tenant_context. (_resolve_tenant_for_query — метод
        # GraphBuilder, не EntityResolver: раньше тут был AttributeError →
        # cluster_duplicates_llm молча возвращал [] → «0 clusters» везде.)
        tenant = tenant_id
        try:
            # non-strict: tenant + legacy(NULL). Люди могут быть без tenant-
            # штампа (strict их отсекал → 0 кластеров, хотя дубли есть).
            nodes = await self.graph.find_nodes_by_label(
                label, limit=2000, tenant_id=tenant, strict_tenant=False)
        except Exception as e:
            logger.debug(f"find_nodes_by_label({label}) failed: {e}")
            return []
        named = [(n.get("id"), n) for n in nodes
                 if n.get("id") and (n.get("name") or "").strip()]
        if entity_type == "company":
            # Метка Entity общая (технологии/концепты/фичи) — берём только
            # контрагентов по свойству entity_type
            named = [(nid, n) for nid, n in named
                     if (n.get("entity_type") or "").lower()
                     in ("company", "organization")]
        if len(named) < 2:
            return []
        llm = self._get_llm_client()
        if not llm:
            return []

        profiles = await self._build_activity_profiles(named, tenant)
        id_to_node = {nid: n for nid, n in named}

        # Карта: имя → множество фамилий среди ВСЕХ людей. Нужна, чтобы НЕ сливать
        # голое имя («Максим») в конкретную фамилию, когда на это имя приходится
        # несколько разных фамилий (Максим Белухин/Никитин/Фельдман) — какая из
        # них «Максим», неизвестно → не угадываем.
        surnames_by_first: Dict[str, set] = {}
        for _nid, _n in named:
            _t = _name_tokens(_n.get("name", ""))
            if len(_t) >= 2:
                surnames_by_first.setdefault(_t[0], set()).add(_t[1])

        # ЧАНКИНГ: один вызов на весь список не масштабируется — на ~40 людях
        # JSON-ответ уже упирался в 1500 токенов, на сотнях обрезался бы и при
        # 4096 (→ тихо «0 clusters»). Сортируем записи по корню имени, чтобы
        # варианты одного человека оказались СОСЕДЯМИ («Катя»/«Екатерина» — общий
        # диминутив-корень; «Ahlin»/«Ахлина» — общая транслитерация), и режем на
        # батчи по TESSENT_DEDUP_CHUNK (default 120) → 1 LLM-вызов на батч.
        import os as _os
        try:
            _chunk = max(20, int(_os.getenv("TESSENT_DEDUP_CHUNK", "120")))
        except (TypeError, ValueError):
            _chunk = 120

        def _sort_key(item):
            toks = _name_tokens(item[1].get("name", ""))
            if not toks:
                return "~"
            first = toks[0]
            group = _NAME_ROOTS.get(first)
            if group is None:
                # Латинское написание («Sasha») в кириллический корень:
                # иначе Sasha и Саша разъедутся по разным батчам.
                group = _LAT_NAME_ROOTS.get(_latin(first))
            root = min(group) if group else first
            return _latin(root)  # кириллица и латиница — в один ряд
        named_sorted = sorted(named, key=_sort_key)
        batches = [named_sorted[i:i + _chunk]
                   for i in range(0, len(named_sorted), _chunk)]
        if len(batches) > 1:
            logger.info(
                f"  dedup {entity_type}: {len(named_sorted)} записей → "
                f"{len(batches)} LLM-батчей по ≤{_chunk}")

        # Негативная память: пары, которые человек пометил «разные» —
        # уходит в промпт (не предлагать) и в пост-фильтр (не пропускать).
        try:
            from backend.core.store.dedup_negative import (
                negative_pairs_set, negatives_prompt_block)
            _neg_pairs = negative_pairs_set(tenant_id or "")
            _negatives_block = negatives_prompt_block(
                tenant_id or "", entity_type)
        except Exception:
            _neg_pairs = set()
            _negatives_block = ""

        raw: List[Dict[str, Any]] = []
        idx_to_id: Dict[int, str] = {}
        _next_idx = 1
        for batch in batches:
            batch_idx_to_id: Dict[int, str] = {}
            lines: List[str] = []
            for i, (nid, node) in enumerate(batch, _next_idx):
                batch_idx_to_id[i] = nid
                p = profiles.get(nid, {})
                parts = [f'{i}. "{node.get("name", "")}"']
                if p.get("role"):
                    parts.append(f'роль: {p["role"]}')
                if p.get("department"):
                    parts.append(f'отдел: {p["department"]}')
                if p.get("projects"):
                    parts.append(f'проекты: {", ".join(p["projects"])}')
                if p.get("colleagues"):
                    parts.append(f'коллеги: {", ".join(p["colleagues"])}')
                lines.append(" | ".join(parts))
            _next_idx += len(batch)
            listing = "\n".join(lines)

            if entity_type == "company":
                prompt = (
                    "Ты дедуплицируешь список КОМПАНИЙ-контрагентов (клиенты, "
                    "партнёры, конкуренты). Одну компанию часто записывают "
                    "по-разному: с юрформой и без (ООО «Ромашка» = Ромашка), "
                    "транслитом (Ромашка = Romashka LLC), с опечаткой, с "
                    "обёрткой (ГК Ромашка = Группа компаний Ромашка).\n\n"
                    "СТРОГО ЗАПРЕЩЕНО:\n"
                    "- Объединять РАЗНЫЕ компании одной отрасли или одного "
                    "проекта: «Альфа Банк» и «Альфа Страхование» — РАЗНЫЕ; "
                    "совместные упоминания НЕ делают компании одной.\n"
                    "- Сливать головную компанию с филиалами/регионами "
                    "(«Ромашка Москва» vs «Ромашка Питер») — если не уверен, "
                    "что это одно юрлицо, ставь confidence не выше 0.7 "
                    "(уйдёт человеку на ревью).\n\n"
                    "confidence 0..1. Возвращай только группы 2+.\n"
                    f"{_negatives_block}\n"
                    f"Список:\n{listing}\n\n"
                    "Ответь СТРОГО JSON-массивом (без текста вокруг):\n"
                    '[{"members": [номера], "canonical": "самое полное имя", '
                    '"confidence": 0.0-1.0, "reason": "кратко: какие формы"}]\n'
                    "Если дублей нет — []."
                )
            else:
                prompt = (
                "Ты дедуплицируешь список ЛЮДЕЙ. Одного человека часто записывают\n"
                "по-разному: уменьшительное (Катя = Екатерина), транслит (Саша = Sasha),\n"
                "с таймстемпом/спикером (Саша 6:45, Максим (8:09)), с опечаткой.\n\n"
                "ЕДИНСТВЕННЫЙ критерий объединения — ИМЯ обозначает одного человека.\n\n"
                "СТРОГО ЗАПРЕЩЕНО:\n"
                "- Объединять РАЗНЫЕ имена, даже если у них общие проекты/коллеги/отдел.\n"
                "  Совместная работа НЕ делает людей одним человеком!\n"
                "  «Катя» и «Рома Тышковский» — РАЗНЫЕ. «Максим Белухин» и «Максим\n"
                "  Фельдман» — РАЗНЫЕ (разные фамилии). «Александр» и «Антон» — РАЗНЫЕ.\n"
                "- Сваливать в одну группу много разных людей из одного проекта.\n\n"
                "АКТИВНОСТЬ (проекты/коллеги) используй ТОЛЬКО чтобы РАЗЛИЧИТЬ двух\n"
                "людей с ОДИНАКОВЫМ именем (два «Максим» с разными проектами — разные),\n"
                "а НЕ чтобы объединять.\n\n"
                "Группа = записи ОДНОГО И ТОГО ЖЕ имени (его формы). Обычно 2-6 записей.\n"
                "confidence 0..1. Возвращай только группы 2+.\n"
                f"{_negatives_block}\n"
                f"Список:\n{listing}\n\n"
                "Ответь СТРОГО JSON-массивом (без текста вокруг):\n"
                '[{"members": [номера], "canonical": "самое полное имя", '
                '"confidence": 0.0-1.0, "reason": "кратко: какие формы имени"}]\n'
                "Если дублей нет — []."
                )

            try:
                # max_tokens высокий: JSON-массив групп большой; при 1500 ответ
                # обрезался на полуслове → json.loads падал (0 clusters).
                response = await llm.generate(prompt=prompt, temperature=0.1, max_tokens=4096)
                text = response.get("text", "") if isinstance(response, dict) else str(response)
                raw.extend(_parse_cluster_json(text) or [])
                idx_to_id.update(batch_idx_to_id)
            except Exception as e:
                # Батч упал — остальные всё равно обрабатываем (дедуп идемпотентен,
                # пропущенный батч догонится следующей ночью).
                logger.warning(f"cluster dedup LLM failed for batch: {e}")
                continue

        clusters: List[Dict[str, Any]] = []
        for c in raw if isinstance(raw, list) else []:
            nums = c.get("members") or []
            ids = [idx_to_id[n] for n in nums if isinstance(n, int) and n in idx_to_id]
            ids = list(dict.fromkeys(ids))  # уникализируем, сохраняя порядок
            if len(ids) < 2:
                continue
            # Составные узлы двух людей («Шитов / Кустова») выкидываем из
            # кластера ДО выбора канона: они не сливаются ни в какую сторону.
            if entity_type == "person":
                pairs = [(i, id_to_node[i].get("name", "")) for i in ids]
                pairs = [(i, nm) for i, nm in pairs if not _is_combined_person(nm)]
                if len(pairs) < 2:
                    continue
                ids = [i for i, _ in pairs]
                names = [nm for _, nm in pairs]
            else:
                names = [id_to_node[i].get("name", "") for i in ids]
            # canonical = самое ПОЛНОЕ ЧИСТОЕ имя среди участников (не то, что
            # выбрал LLM): иначе при каноничном «Максим» фамилии не видны и
            # surname-conflict не сработает, слив Белухина с Фельдманом; а
            # длинное имя-с-квалификатором не должно побеждать чистое ФИО.
            canonical = max(names, key=_keep_name_rank)

            # ПОСТ-ФИЛЬТР (безопасность): LLM склонен лумпить со-работников
            # («общий проект») в одного человека — «Катя» вбирала 60 разных
            # людей. Оставляем в группе только записи, чьё ИМЯ реально совпадает
            # с каноничным (токен/диминутив/фаззи). Так «Рома Тышковский»,
            # «Максим Фельдман» и т.п. выпадают из группы «Катя».
            c_full = len(_name_tokens(canonical)) >= 2
            kept = []
            from backend.core.store.dedup_negative import is_negative as _is_neg
            for i, nm in zip(ids, names):
                if not _entity_matches(nm, canonical, entity_type):
                    continue
                # Человек уже сказал «это разные» — уважаем всегда
                if _neg_pairs and _is_neg(tenant_id or "", nm, canonical,
                                          pairs=_neg_pairs):
                    continue
                # Голое имя + полный canonical + несколько фамилий на это имя →
                # неоднозначно, не сливаем (иначе прицепим «Максима» к случайной
                # фамилии). Варианты с таймстемпом/ролью (после _name_tokens дают
                # 1 значимый токен) тоже сюда попадают — и это правильно.
                # Только для людей: у проектов surnames_by_first не осмыслен.
                if entity_type == "person":
                    m_toks = _name_tokens(nm)
                    if c_full and len(m_toks) == 1:
                        if len(surnames_by_first.get(m_toks[0], ())) > 1:
                            continue
                kept.append((i, nm))
            if len(kept) < 2:
                continue
            f_ids = [i for i, _ in kept]
            f_names = [nm for _, nm in kept]
            clusters.append({
                "member_ids": f_ids,
                "member_names": f_names,
                "canonical": canonical,
                "confidence": float(c.get("confidence", 0.5)),
                "reason": c.get("reason", ""),
                "dropped": len(ids) - len(f_ids),  # сколько отфильтровано
            })
        return clusters

    async def repair_combined_persons(self, tenant_id: Optional[str] = None) -> int:
        """Починка УЖЕ испорченных составных Person-узлов («Шитов / Кустова»).

        Старый выбор keep-узла (по длине имени) сливал реальных людей в
        составные узлы двух человек — view-фильтр их прячет, и люди исчезали
        из иерархии/снапшотов. Новые слияния так больше не делают, а этот
        repair возвращает старым узлам чистое имя: берём лучший ЧИСТЫЙ alias
        (после слияния он там есть) и переименовываем узел в него.
        Идемпотентно: после rename имя перестаёт матчиться как составное."""
        if not self.graph:
            return 0
        # Junk-детектор витрины (составные, generic вроде «Женщина (Катя)»,
        # тайм-фрагменты) — lazy-импорт, чтобы не завести цикл модулей.
        try:
            from backend.core.sleep.enhanced_snapshot import _is_person_junk
        except Exception:
            _is_person_junk = None
        repaired = 0
        try:
            nodes = await self.graph.find_nodes_by_label(
                "Person", limit=2000, tenant_id=tenant_id, strict_tenant=True)
        except Exception:
            nodes = []
        for n in nodes or []:
            name = (n.get("name") or "").strip()
            if not name:
                continue
            bad = _is_combined_person(name) or (
                _is_person_junk is not None and _is_person_junk(name))
            # Имя с мусорным квалификатором («Екатерина (confirming counting
            # logic)») тоже чиним — но только если среди алиасов есть более
            # ПОЛНОЕ чистое имя этого же человека («Катя Пустовалова»).
            needs_upgrade = (not bad and "(" in name)
            if bad or needs_upgrade:
                aliases = n.get("aliases") or []
                if isinstance(aliases, str):
                    import json as _json
                    try:
                        aliases = _json.loads(aliases)
                    except (ValueError, TypeError):
                        aliases = [aliases] if aliases else []
                clean = [a for a in aliases
                         if a and not _is_combined_person(a) and _name_tokens(a)
                         and "(" not in a
                         and not (_is_person_junk and _is_person_junk(a))]
                if needs_upgrade:
                    _own = len(_name_tokens(name))
                    clean = [a for a in clean if len(_name_tokens(a)) > _own]
                if clean:
                    best = max(clean, key=_keep_name_rank)
                    try:
                        await self.graph.update_node(n.get("id"), {"name": best})
                        repaired += 1
                        logger.info(
                            f"🩹 repaired junk person: «{name}» → «{best}»")
                        name = best
                    except Exception:
                        logger.debug("repair rename failed", exc_info=True)
            # Восстановление обнулённой вовлечённости: старый merge не
            # суммировал total_mentions, и человек «на каждой встрече» мог
            # остаться с 1-2 упоминаниями → выпадал из топ-N снапшотов.
            # Явно повреждённый случай: упоминаний почти нет, а связей много.
            try:
                tm = int(n.get("total_mentions") or 0)
            except (TypeError, ValueError):
                tm = 0
            if tm <= 2:
                try:
                    rels = await self.graph.get_node_relationships(n.get("id"))
                    deg = len(rels.get("outgoing", [])) + len(rels.get("incoming", []))
                    if deg >= 10:
                        await self.graph.update_node(
                            n.get("id"), {"total_mentions": deg})
                        repaired += 1
                        logger.info(
                            f"🩹 restored engagement: «{name}» "
                            f"mentions {tm} → {deg} (по связям)")
                except Exception:
                    logger.debug("engagement restore failed", exc_info=True)
        return repaired

    async def cluster_and_merge(
        self,
        entity_type: str = "person",
        tenant_id: Optional[str] = None,
        auto_confidence: Optional[float] = None,
        review_confidence: float = 0.55,
    ) -> Dict[str, Any]:
        """Content-aware дедуп с тирингом: LLM кластеризует записи (1 вызов),
        кластеры с confidence>=auto — сливаем сразу; [review, auto) — отдаём на
        ревью пользователю (не мержим); ниже review — игнор."""
        if auto_confidence is None:
            auto_confidence = self.llm_auto_merge_confidence
        clusters = await self.cluster_duplicates_llm(entity_type, tenant_id)
        stats: Dict[str, Any] = {
            "clusters": len(clusters), "merged": 0, "errors": 0, "review": [],
        }
        for c in clusters:
            conf = c.get("confidence", 0.0)
            ids = c.get("member_ids") or []
            if len(ids) < 2:
                continue
            if conf >= auto_confidence:
                # keep = самое ПОЛНОЕ ЧИСТОЕ имя (тот же ранг, что у канона).
                # Раньше брали max по сырой длине строки — побеждали мусорные
                # «Александр (управляющий партнер КПД)» / составные узлы.
                _names_by_id = dict(zip(ids, c.get("member_names", [])))
                keep = max(ids, key=lambda i: _keep_name_rank(_names_by_id.get(i, "")))
                for mid in ids:
                    if mid == keep:
                        continue
                    try:
                        ok = await self._merge_via_builder(keep, mid)
                        stats["merged"] += 1 if ok else 0
                        stats["errors"] += 0 if ok else 1
                    except Exception as e:
                        logger.error(f"cluster merge error: {e}")
                        stats["errors"] += 1
            elif conf >= review_confidence:
                stats["review"].append(c)
        if stats["merged"] and self.graph and self.graph.use_networkx:
            self.graph.save_graph()
        return stats

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику"""
        stats = {
            "cache_size": len(self._cache),
            "alias_index_size": len(self._alias_index),
            "fuzzy_threshold": self.fuzzy_threshold,
            "semantic_threshold": self.semantic_threshold
        }

        if self.graph and self.graph.connected:
            stats["graph_backend"] = self.graph.backend
            stats["graph_nodes"] = self.graph.nx_graph.number_of_nodes() if self.graph.nx_graph else 0

        return stats


# Singleton
_entity_resolver: EntityResolver | None = None


async def get_entity_resolver() -> EntityResolver:
    """Получить Entity Resolver"""
    global _entity_resolver
    if _entity_resolver is None:
        from backend.db import get_neo4j, get_qdrant
        neo4j = await get_neo4j()
        qdrant = await get_qdrant()
        _entity_resolver = EntityResolver(neo4j, qdrant)
    return _entity_resolver



