/**
 * authFetch — обёртка над fetch, проставляющая Bearer токен из localStorage.
 *
 * Зачем: после issue #107 (enable_strict_chat_auth=True) backend отклоняет
 * запросы без Bearer (PermissionError → 403). Многие компоненты исторически
 * звали `fetch(url, { headers: { 'Content-Type': 'application/json' } })`
 * без токена → весь чат / HomeTab / agents вылетали в "authentication required".
 *
 * Помимо токена: понимает оба ключа в localStorage:
 *  - 'tessent_access_token' (новый, login пишет сюда)
 *  - 'access_token' (legacy)
 * Возвращает обычный Response, можно использовать как обычный fetch.
 *
 * АВТО-REFRESH: Supabase-токен живёт ~1 час. Раньше после истечения все
 * запросы тихо становились «безтокенными», и легаси-путь бэкенда отдавал
 * данные по стейлному ?user_id= из localStorage (симптом «подгрузились данные
 * другого аккаунта»). Теперь authFetch ПЕРЕД каждым запросом проверяет exp:
 * истёк → single-flight refresh через /api/v1/auth/refresh; refresh не удался
 * → полная очистка сессии и редирект на /login (работать со стейлным id
 * без токена запрещено).
 */

export function getAccessToken(): string | null {
  if (typeof window === 'undefined') return null
  return (
    localStorage.getItem('tessent_access_token')
    || localStorage.getItem('access_token')
  )
}

function decodeJwtPayload(token: string): any | null {
  try {
    const payload = token.split('.')[1]
    if (!payload) return null
    return JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')))
  } catch {
    return null
  }
}

/** Истёк ли токен (с запасом skewSec, чтобы не отправить запрос на грани). */
export function isTokenExpired(token: string, skewSec = 30): boolean {
  const payload = decodeJwtPayload(token)
  const exp = payload && Number(payload.exp)
  if (!exp || !isFinite(exp)) return false // нет exp → считаем вечным (сервисный)
  return Date.now() / 1000 > exp - skewSec
}

// Single-flight: параллельные authFetch не должны запускать N refresh'ей —
// Supabase refresh_token одноразовый, второй запрос инвалидировал бы сессию.
let refreshInFlight: Promise<string | null> | null = null

async function refreshAccessToken(): Promise<string | null> {
  try {
    const rt = localStorage.getItem('tessent_refresh_token')
    if (!rt) return null
    const res = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    })
    if (!res.ok) return null
    const data = await res.json()
    if (data && data.access_token) {
      localStorage.setItem('tessent_access_token', data.access_token)
      if (data.refresh_token) {
        localStorage.setItem('tessent_refresh_token', data.refresh_token)
      }
      return data.access_token as string
    }
  } catch {
    // сеть/бэк недоступны — не разлогиниваем, вернём null и запрос уйдёт
    // со старым токеном (бэкенд сам его отвергнет, данные не утекут)
    return getAccessToken()
  }
  return null
}

function clearSessionAndGoLogin(): void {
  try {
    ;['tessent_access_token', 'tessent_refresh_token', 'tessent_user',
      'tessent_user_id', 'access_token'].forEach((k) => localStorage.removeItem(k))
  } catch { /* noop */ }
  if (typeof window !== 'undefined'
      && !window.location.pathname.includes('/login')) {
    console.warn('🔑 Сессия истекла и refresh не удался — переход на /login')
    window.location.href = '/login'
  }
}

/**
 * Вернуть валидный (не истёкший) токен, освежив при необходимости.
 * - токена нет вовсе → null (легаси-локальный режим без авторизации);
 * - токен жив → как есть;
 * - истёк → refresh; провал refresh → очистка сессии + редирект на /login.
 */
export async function ensureFreshToken(): Promise<string | null> {
  const token = getAccessToken()
  if (!token) return null
  if (!isTokenExpired(token)) return token
  if (!refreshInFlight) {
    refreshInFlight = refreshAccessToken().finally(() => {
      setTimeout(() => { refreshInFlight = null }, 0)
    })
  }
  const fresh = await refreshInFlight
  if (!fresh || isTokenExpired(fresh)) {
    clearSessionAndGoLogin()
    return null
  }
  return fresh
}

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) }
  const token = getAccessToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

export async function authFetch(input: RequestInfo, init?: RequestInit): Promise<Response> {
  const existing = (init?.headers || {}) as Record<string, string>
  const merged: Record<string, string> = { ...existing }
  const token = await ensureFreshToken()
  if (token) merged['Authorization'] = `Bearer ${token}`
  return fetch(input, { ...(init || {}), headers: merged })
}

// ── Глобальный патч fetch ─────────────────────────────────────────────
// Десятки компонентов исторически зовут «голый» fetch('/api/v1/…') без
// Authorization. В строгом режиме бэкенда (STRICT_USER_AUTH=1) все они
// получают 401 и показывают нули/пустоту. Чинить по одному — whack-a-mole;
// патчим КЛАСС: window.fetch для same-origin /api/* сам подставляет Bearer
// (с авто-refresh), если заголовок ещё не задан. /api/v1/auth/* исключены —
// иначе refresh зациклился бы сам на себе.
function installAuthFetchPatch(): void {
  if (typeof window === 'undefined') return
  const w = window as any
  if (w.__tessentFetchPatched) return
  w.__tessentFetchPatched = true
  const orig = window.fetch.bind(window)
  window.fetch = (async (input: any, init?: RequestInit) => {
    try {
      const url = typeof input === 'string'
        ? input
        : (input instanceof URL ? input.href : (input?.url || ''))
      // Кроме same-origin покрываем и АБСОЛЮТНЫЕ URL на известный бэкенд
      // (NEXT_PUBLIC_API_URL / localhost:8000): десятки панелей ходят на
      // `${API_BASE}/api/v1/…` напрямую, такие запросы не начинаются с
      // location.origin и раньше уходили без Bearer → strict-401 → «пусто».
      const apiOrigin = (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000')
        .replace(/\/+$/, '')
      let path = url
      if (url.startsWith(window.location.origin)) {
        path = url.slice(window.location.origin.length)
      } else if (apiOrigin && url.startsWith(apiOrigin)) {
        path = url.slice(apiOrigin.length)
      }
      if (path.startsWith('/api/') && !path.startsWith('/api/v1/auth/')) {
        const headers = new Headers(
          init?.headers || (typeof input === 'object' && !(input instanceof URL)
            ? input?.headers : undefined) || {})
        if (!headers.has('Authorization')) {
          const token = await ensureFreshToken()
          if (token) headers.set('Authorization', `Bearer ${token}`)
        }
        return orig(input, { ...(init || {}), headers })
      }
    } catch { /* не мешаем запросу при любой ошибке патча */ }
    return orig(input, init)
  }) as typeof window.fetch
}
installAuthFetchPatch()

// ЕДИНЫЙ источник userId — сам токен (JWT payload.sub). Отдельный ключ
// tessent_user_id может протухнуть при смене аккаунта → запросы уходят с
// чужим id при валидном токене («данные из другого аккаунта» / отказы
// анти-IDOR). uid из токена разойтись с токеном не может по построению.
export function getUserIdFromToken(): string | null {
  try {
    const token = getAccessToken()
    if (!token) return null
    const json = decodeJwtPayload(token)
    if (!json) return null
    return (json.sub || json.user_id || null) as string | null
  } catch {
    return null
  }
}
