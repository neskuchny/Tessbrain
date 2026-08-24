# TESSENT BRAIN - Dockerfile
# Multi-stage build for production

# === Build Stage ===
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# pyproject.toml собирается hatchling: ему НУЖНЫ README.md (readme=...)
# и сам пакет backend/ ДО `pip install .`, иначе
# metadata-generation-failed (OSError: Readme file does not exist).
COPY pyproject.toml README.md requirements.txt ./
# CPU-only torch ПЕРЕД основным resolve. sentence-transformers тянет
# torch, а torch на Linux по умолчанию подтягивает ~1.5 ГБ CUDA-колёс
# (cudnn/nccl/cusparselt/nvshmem/...). Для single-VPS (CPU) это
# бессмысленно: раздувает образ на гигабайты и рушит сборку на
# медленной сети. Ставим CPU-вариант заранее → pip видит torch
# удовлетворённым и НЕ тянет CUDA. На работу embeddings (CPU) не
# влияет.
#
# requirements.txt — это actual source of truth по deps (ag2, croniter,
# supabase, python-telegram-bot, pyjwt, tiktoken, pypdf, ...). pyproject.toml
# содержит только подмножество; `pip install .` сам по себе не вытянет
# autogen/ag2 и поэтому api падает на `from autogen import ConversableAgent`.
#
# ВАЖНО для кеша слоёв: тяжёлый install зависимостей идёт ДО `COPY backend`,
# поэтому правки кода НЕ инвалидируют этот ~8-мин слой (torch+requirements).
# Зависит только от requirements.txt/pyproject.toml.
RUN pip install --no-cache-dir build && \
    pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Chromium для визуальной приёмки (VisualDiffValidator): без него валидатор
# всегда отвечает tool_missing, и визуальная проверка результата не работает.
# --with-deps ставит системные библиотеки рендера. Путь фиксируем, чтобы
# runtime-стадия могла его скопировать.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN python -m playwright install --with-deps chromium

# Код приложения и установка локального пакета (--no-deps, чтобы не
# переразрешать зависимости) — последним, дешёвым слоём. Любое изменение
# в backend/ пересобирает только этот шаг, а не весь dependency-resolve.
COPY backend ./backend/
RUN pip install --no-cache-dir --no-deps .

# === Production Stage ===
FROM python:3.11-slim as production

WORKDIR /app

# Install runtime dependencies. Библиотеки для chromium ставим и в runtime:
# COPY бинарей браузера без системных .so дал бы «browser failed to launch».
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
COPY --from=builder /opt/pw-browsers /opt/pw-browsers

# Copy application code
COPY backend/ ./backend/

# Create non-root user
RUN useradd --create-home --shell /bin/bash tessent && \
    chown -R tessent:tessent /app

USER tessent

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/ping || exit 1

# Expose port
EXPOSE 8000

# Run with Granian (high-performance ASGI server)
# 2 воркера (не 4): каждый грузит полный ML-стек (~0.5G), а на single-VPS
# память — узкое место (см. memory limits в deploy/single-vps). Compose может
# переопределить это число под конкретный бокс.
CMD ["granian", "--interface", "asgi", "backend.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]



