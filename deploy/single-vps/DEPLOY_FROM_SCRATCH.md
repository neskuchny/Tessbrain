# Single-VPS deploy — практический runbook (с нуля)

Боевой опыт деплоя `tessent_brain` (backend + UI) на одну 8 GB VPS.
Здесь — рабочая последовательность, реальные грабли и как их обойти.
Подробный справочник: `tessent_brain/docs/ru/DEPLOYMENT.md`. Этот файл — про
то, что **реально ломалось** и чего ждать в следующий раз.

> Все перечисленные ниже баги **уже исправлены в репозитории** — если
> деплоишь с актуальной ветки, большинство «грабель» не повторятся. Раздел
> «Грабли» оставлен, чтобы при ошибке быстро понять причину.

---

## 0. Что нужно заранее

- **VPS**: 8 GB RAM (меньше — будет OOM, см. ниже), Docker + compose plugin.
- **Два DNS A-record** на IP VPS (оба обязательны ещё ДО первого `up`, иначе
  Caddy не выпишет TLS):
  - `api.<домен>`  → IP  (API)
  - `app.<домен>`  → IP  (веб-интерфейс)
- **Ключ LLM**: Google Gemini (`GOOGLE_API_KEY`) или OpenAI.
  ⚠️ **Проверь, какие модели реально доступны твоему ключу** — имена в Google
  API меняются. См. шаг 3.5 и грабли по моделям. Если хардкод в коде
  устарел — backend будет падать с маскированной ошибкой стрима.
- **Supabase**: проект с таблицами MeetFlow (см. раздел Supabase) +
  `service_role`-ключ (НЕ anon).
- **Открытые порты** (ufw): 22, 80, 443.

---

## 1. Клон и ветка

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/neskuchny/tessent-test_new.git tessent
cd tessent
git checkout <ветка>
```

## 2. .env

```bash
cd tessent_brain/deploy/single-vps
cp .env.example .env
```

Сгенерь секреты прямо на VPS (в чат не отправляй):

```bash
python3 - <<'PY'
import re, secrets, base64
vals = {
  "DOMAIN": "api.<домен>",
  "APP_DOMAIN": "app.<домен>",
  "ACME_EMAIL": "<email>",
  "POSTGRES_PASSWORD": secrets.token_hex(24),
  "REDIS_PASSWORD":    secrets.token_hex(24),
  "QDRANT_API_KEY":    secrets.token_hex(24),
  "NEO4J_PASSWORD":    secrets.token_hex(24),
  "SECRET_KEY":        base64.b64encode(secrets.token_bytes(48)).decode(),
  "JWT_SECRET_KEY":    base64.b64encode(secrets.token_bytes(48)).decode(),
  "CORS_ALLOWED_ORIGINS": "https://app.<домен>",
}
t = open(".env").read()
for k,v in vals.items():
    t = re.sub(rf"^{k}=.*$", f"{k}={v}", t, flags=re.M)
open(".env","w").write(t)
print("written")
PY
chmod 600 .env
```

Руками впиши: `GOOGLE_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY` (**service_role!**).

Проверь без раскрытия значений:
```bash
grep -E '^[A-Za-z0-9_]+=' .env | awk -F= '{print $1, "len="length($2)}'
```
Все секреты должны быть len>0; `HERMES_*` — короткие (`off`/`30`), а НЕ
длинные (иначе зацепился inline-комментарий, см. грабли).

## 3. Подъём backend

```bash
docker compose build          # первый build api ~8-9 мин (torch+deps)
docker compose up -d
docker compose ps             # migrate=Exited(0), остальное healthy/Up
```

Проверка:
```bash
curl -sS -w "\n%{http_code}\n" https://api.<домен>/api/v1/readyz
# ждём {"status":"ready", все сервисы true}, HTTP 200
```

## 3.5. Проверь, что Gemini-модели в коде существуют для твоего ключа

⚠️ **Критически важный шаг** — пропуск ломает ВСЕ агенты (с маскированной
ошибкой `Exception caught after response started`):

```bash
# Какие модели реально доступны твоему ключу:
docker compose exec api python -c "
import os, google.generativeai as g
g.configure(api_key=os.environ['GOOGLE_API_KEY'])
for m in g.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(m.name)
" | head -20

# Какие имена жёстко зашиты в коде:
docker compose exec api grep -rhoE 'gemini-[a-z0-9.-]+' backend --include=*.py | sort -u
```

Если хардкод не пересекается со списком доступных — обнови имена через
`sed` (см. грабли) и пересобери api. Текущий код использует
`gemini-flash-lite-latest` (standard) / `gemini-flash-latest` (premium) —
проверь, что они есть у тебя.

## 4. Подъём frontend (UI)

DNS `app.` уже должен резолвиться. Затем:
```bash
docker compose build frontend   # npm ci + next build ~4 мин
docker compose up -d
curl -sS -I https://app.<домен>/    # 307 → /ru или /en, БЕЗ :3000 в Location
```
Открой `https://app.<домен>` в браузере.

## 5. Supabase (важно — почти всё уже есть)

Этот деплой использует **общий Supabase MeetFlow**. Таблицы `documents`,
`knowledge_sync_subscriptions`, `processed_meetings`, `agent_automations`,
`projects`, `folders` **уже созданы MeetFlow'ом** — НЕ применяй миграции
`001`/`003` (упадут на дублях RLS-политик; локальный migrate их и так
пропускает, см. `backend/db/migrate.py` `_SKIP_NAMES`).

Единственное, чего может не хватать — **skill-колонки в `agent_automations`**
(миграция `070`). Проверь и при отсутствии добавь (аддитивно, безопасно):
```sql
-- если columns is_skill/parameters_schema/... отсутствуют:
ALTER TABLE public.agent_automations ADD COLUMN IF NOT EXISTS is_skill boolean NOT NULL DEFAULT false;
ALTER TABLE public.agent_automations ADD COLUMN IF NOT EXISTS instruction_template text;
ALTER TABLE public.agent_automations ADD COLUMN IF NOT EXISTS parameters_schema jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.agent_automations ADD COLUMN IF NOT EXISTS parent_skill_id text;
ALTER TABLE public.agent_automations ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1;
ALTER TABLE public.agent_automations ADD COLUMN IF NOT EXISTS shared_in_tenant boolean NOT NULL DEFAULT false;
ALTER TABLE public.agent_automations ADD COLUMN IF NOT EXISTS source_conversation_id text;
```
(Нужны только если включаешь `HERMES_SKILLS_ENABLED`; по умолчанию off.)

## 6. AI Coach (отдельный сервис)

В `.env` AI Coach: `TESSENT_API_BASE_URL=https://api.<домен>` — **без**
`/api/v1` (клиент добавляет сам). Затем рестарт ai-coach.

## 7. Smoke-тест после деплоя

Минимальный сценарий, по которому видно, что всё живо:

1. Открой `https://app.<домен>` → должно редиректнуть на `/ru` или `/en` **без `:3000`** в URL.
2. Войди по существующему юзеру Supabase (он должен быть в `auth.users` твоего Supabase-проекта).
3. **Brain** (главный чат, облако) — отправь «привет», должен ответить (это проверка LLM + БД + сохранения чата).
4. Открой созданный чат → закрой/перезайди → чат должен быть в сайдбаре (это проверка `app_data` volume, см. ниже).
5. **Задачи / Web / MeetFlow / Calls** — облачные режимы; должны отвечать.
6. **Tess** (🧩) и **Private LLM (Transcripts)** — **по дизайну требуют локальный LLM (Ollama)**. Без него падают. Это **не баг**, а отдельная опция on-prem. Если Ollama не поднята — не используй эти вкладки.

---

## Архитектурная карта режимов (что облако, что локалка)

| Режим UI | Бэкенд `agent_mode` | LLM | Работает без Ollama |
|---|---|---|---|
| Brain (главный чат) | `brain` | Облачный Gemini | ✅ |
| Задачи | `automation` / `tasks` | Облачный Gemini | ✅ |
| Web | `automation` / `web` | Облачный Gemini | ✅ |
| MeetFlow | `automation` / `meetflow` | Облачный Gemini | ✅ |
| Calls | `automation` / `calls` | Облачный Gemini | ✅ |
| Mark / Transcripts | `mark` / `transcripts` | Облачный Gemini (через GemmaClient → Gemini fallback) | ✅ |
| Private LLM (welcome для transcripts) | `transcripts` | Брендирован как «локальная», де-факто требует приватный LLM | ❌ без Ollama |
| **Tess 🧩** | `automation` / `tess` | **FunctionGemma в Ollama** (`OLLAMA_HOST`, дефолт `localhost:11434`) | ❌ без Ollama |

**Эмбеддинги** — Hugging Face `intfloat/multilingual-e5-base` (sentence-transformers,
загружается локально на первом запуске). Cloud-независимы, Ollama для них **не нужен**.

---

## Грабли (что ломалось и как выглядит)

| Симптом | Причина | Фикс |
|---|---|---|
| `build` падает: `hatchling ... Unable to determine which files to ship` | `name=tessent-brain`, а код в `backend/` | в `pyproject.toml` есть `[tool.hatch.build.targets.wheel] packages=["backend"]` |
| `qdrant` вечно `unhealthy` | в образе Qdrant нет `wget` для healthcheck | healthcheck через `bash /dev/tcp` (уже в compose) |
| `migrate` exit 1: `Insecure default secrets ... NEO4J_PASSWORD` | у сервиса migrate не было `NEO4J_PASSWORD` | передан в compose (Settings валидирует ВСЕ 4 секрета) |
| `migrate` exit 1: `schema "auth" does not exist` / `relation agent_automations does not exist` | Supabase-миграции (001/003/070) в локальном runner | добавлены в `_SKIP_NAMES` в `migrate.py` |
| api крешит: `error parsing value for field cors_allowed_origins` | pydantic-settings JSON-парсит list-env до валидатора | поле помечено `NoDecode` (в `config.py`) |
| api `unhealthy`, `Connection refused`, в логах apscheduler крутится | **OOM**: 4 granian-воркера × ML-стек > 1.5G | `command` в compose: `--workers 2`. Проверка: `docker inspect --format '{{.State.OOMKilled}}' single-vps-api-1` |
| `readyz` → `qdrant:false`, в логах `SSL: WRONG_VERSION_NUMBER` | qdrant-client при api_key включает https=True; self-hosted = HTTP | `QDRANT_HTTPS=false` / `Settings.qdrant_https` (по умолчанию False) |
| UI: редирект `/` → `https://app...:3000/ru` (порт наружу закрыт) | Next standalone за прокси подмешивает внутренний порт | в Caddyfile `header_down Location ":3000" ""` |
| UI грузится, но ВСЕ `/api/*` (вкл. `/health`, `/auth/login`) → 500, при этом `docker compose logs api` ПУСТ | `next.config rewrites()` запекается на BUILD → `BACKEND_URL` обязан быть build-arg'ом, иначе в манифест попадёт дефолт `localhost:8000` и прокси бьёт в пустоту внутри контейнера фронта | `BACKEND_URL` передаётся build-arg'ом (Dockerfile + compose `build.args`). Проверка: `docker compose exec frontend grep -o 'localhost:8000\|api:8000' .next/routes-manifest.json` должно быть `api:8000` |
| Чат/агенты в UI: «Произошла ошибка при обработке запроса», лог api ПУСТ | ChatPanel/Automations хардкодили `http://localhost:8000` → в браузере это машина пользователя, запрос даже не доходил до сервера | заменено на относительные `/api/v1/...` (идут через next-rewrites → `api:8000`). Проверка: в DevTools Network запрос должен бить в `app.<домен>/api/v1/...`, не в `localhost:8000` |
| ВСЕ агенты падают, в логах `Exception caught after response started`, прямой тест `genai` → `gemini-2.0-flash` не существует | хардкод устаревших имён моделей Gemini (`gemini-2.0-flash`, `gemini-3-flash-preview`), которых уже нет в API → каждый облачный LLM-вызов 404 в середине стрима, обёртка маскирует | имена обновлены на `gemini-flash-lite-latest` (standard) / `gemini-flash-latest` (premium). См. шаг 3.5. ⚠️ Имена в Google API дрейфуют — при 404 на LLM проверь актуальные и замени по всему backend (`grep -rl 'gemini-' backend/` + sed). Это **частая причина мёртвых агентов** при деплое спустя время после написания кода |
| api крешит с `PermissionError: [Errno 13] Permission denied: 'data/chat_sessions'`, granian-воркеры падают, login через UI снова 500 | named volume `app_data` инициализируется как `root:root`, а api/worker бегут под не-root `tessent` (uid 1000); `sessions.py` на импорте делает `mkdir('data/chat_sessions')` | one-shot сервис `init-data` (`user: "0:0"`, `chown -R tessent:tessent /app/data`); api/worker зависят от него через `service_completed_successfully`. Проверка: `docker compose exec api stat -c '%U:%G' /app/data` → `tessent:tessent` |
| «Чаты не сохраняются» / граф знаний пустеет после `up -d` или ребилда | сессии (`data/chat_sessions`), граф (`data/tessent_brain_graph.json`), корректировки (`data/corrections`) пишутся в `/app/data` — если это **не volume**, всё стирается при каждом пересоздании контейнера | named volume `app_data` примонтирован к `/app/data` у api **и** worker (worker тоже пишет в граф). ⚠️ **Не запускай `docker compose down -v`** — флаг `-v` сотрёт `app_data`, `postgres_data`, `neo4j_data` и т.д. |
| Tess / Private LLM пишут «Произошла ошибка» даже когда облачные режимы работают | по дизайну требуют локальный LLM (Tess → FunctionGemma в Ollama; Private LLM брендирован «закрытый контур») — без Ollama-контейнера и `OLLAMA_HOST=<host>:11434` они работать не могут | не баг. Используй облачные режимы (Brain/Задачи/Web/MeetFlow/Calls) или подними отдельно Ollama + `OLLAMA_HOST` в env api/worker |
| каждая правка `backend/` = полный 8-мин ребилд | `COPY backend` стоял до `pip install` | в Dockerfile deps ставятся ДО `COPY backend` (кеш слоёв). Правки кода → ребилд ~30 сек–1 мин |

### Память (8 GB — впритык)
postgres 2G + qdrant 2G + neo4j 2G + redis .5G + api 1.5G + worker 1G +
frontend .5G — лимиты переподписаны. Реальный риск — **OOM при `next build`**
(жрёт ~1-1.5G). Если build фронта падает по памяти:
```bash
docker compose stop worker        # освободить ~1G на время сборки
docker compose build frontend
docker compose up -d
```
Либо собирать образы локально/в CI и тянуть готовые (`IMAGE_TAG`).

### Прочее (не блокеры)
- **Qdrant client/server version warning** (`1.18.0` vs `1.12.4`): базовые
  операции работают; при желании поднять образ Qdrant или пин клиента.
- **Litestar `DeprecationWarning` про `header=`/`query=`**: косметика, не ошибка.
- **`404 на /`**: на корне API нет страницы — это норма (UI на app-домене).

---

## Безопасность — RLS в Supabase (ВНИМАНИЕ)

В общем Supabase MeetFlow у части таблиц (включая `agent_automations`,
`coach_*`, `telegram_connect_tokens`, `notification_*`) **выключен RLS** —
любой с anon-ключом читает/пишет все строки. Это НЕ чинится автоматически:
`ENABLE ROW LEVEL SECURITY` без policies заблокирует доступ всем. Включать
по таблице, вместе с продуманными политиками. Бэкенд ходит под
`service_role` (RLS обходит), но клиентские пути с anon-ключом — дыра.

---

## Полезные команды (диагностика по факту падения)

```bash
# Статус всех контейнеров (что healthy/restarting/exited)
docker compose ps

# Здоровье снаружи (через HTTPS — заодно проверка Caddy + сертификата)
curl -sS -w "\n%{http_code}\n" https://api.<домен>/api/v1/readyz

# Логи api без спама scheduler/deprecation
docker compose logs api 2>&1 | grep -viE 'apscheduler|DeprecationWarning' | tail -80

# Сразу выцепить блок Traceback (25 строк после "Uncaught exception")
docker compose logs --tail=250 api 2>&1 | grep -viE 'apscheduler|DeprecationWarning' | grep -A 25 'Uncaught exception' | tail -120

# OOM-проверка
docker inspect --format 'OOM={{.State.OOMKilled}} restarts={{.RestartCount}}' single-vps-api-1

# Проверка прав на app_data volume (должно tessent:tessent)
docker compose exec api stat -c '%U:%G %a /app/data' /app/data

# Видит ли фронт-прокси правильный backend (должно api:8000, не localhost)
docker compose exec frontend grep -o 'localhost:8000\|api:8000' .next/routes-manifest.json | sort -u

# Какие модели Gemini реально доступны твоему ключу
docker compose exec api python -c "
import os, google.generativeai as g
g.configure(api_key=os.environ['GOOGLE_API_KEY'])
[print(m.name) for m in g.list_models() if 'generateContent' in m.supported_generation_methods]
"

# Прямой тест агентского endpoint (минует фронт-прокси, видна сырая ошибка)
curl -sS -X POST https://api.<домен>/api/v1/agents/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"привет","agent_mode":"brain","model_tier":"standard","context":{}}'

# Прямой smoke на Qdrant из api (видна реальная ошибка вроде SSL/auth)
docker compose exec api python -c "
import asyncio
from backend.db.qdrant import get_qdrant
async def t():
    c = await get_qdrant(); print('OK:', c.client.get_collections())
asyncio.run(t())
"
```

---

## Чек-лист «деплой с нуля» (TL;DR)

1. ✅ DNS: `api.` и `app.` — оба A-record на IP VPS, проверь `dig +short`.
2. ✅ `.env` сгенерирован, `SUPABASE_KEY` — это **service_role**, не anon.
3. ✅ `docker compose build` прошёл (если `hatchling` ругается — что-то с веткой).
4. ✅ `docker compose up -d` → `migrate=Exited(0)`, `init-data=Exited(0)`, всё healthy.
5. ✅ `readyz` → все 4 БД `true`, HTTP 200.
6. ✅ Модели Gemini в коде есть в `list_models()` твоего ключа (шаг 3.5).
7. ✅ `docker compose exec frontend grep ... routes-manifest.json` показывает `api:8000`.
8. ✅ `stat /app/data` → `tessent:tessent`.
9. ✅ `https://app.<домен>` → редирект на `/ru` или `/en` **без `:3000`**.
10. ✅ Вход → Brain → отправил сообщение → ответ пришёл → чат в сайдбаре после рефреша.

Если все 10 ✅ — деплой живой. Tess и Private LLM **не входят** в чек-лист (отдельная on-prem опция).

