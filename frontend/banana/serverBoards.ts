// Клиент серверного хранения досок («Мои доски», P1). Бэкенд резолвит
// user_id из валидного токена (анти-IDOR), поэтому ?user_id не нужен.
// Authorization подставляет ГЛОБАЛЬНЫЙ патч fetch (lib/authFetch) — с
// авто-refresh; ручной токен из localStorage тут был бы протухшим.

export interface BoardMeta {
  id: string;
  name: string;
  kind?: string;
  updated_at?: string;
}

function authHeaders(): Record<string, string> {
  return { "Content-Type": "application/json" };
}

export async function listBoards(): Promise<BoardMeta[]> {
  try {
    const r = await fetch("/api/v1/boards/", { headers: authHeaders() });
    if (!r.ok) {
      console.warn("boards: list failed", r.status, await r.text().catch(() => ""));
      return [];
    }
    const d = await r.json();
    return d.boards || [];
  } catch (e) {
    console.warn("boards: list error", e);
    return [];
  }
}

export async function getBoard(id: string): Promise<any | null> {
  try {
    const r = await fetch(`/api/v1/boards/${id}`, { headers: authHeaders() });
    if (!r.ok) return null;
    const d = await r.json();
    return d.board || null;
  } catch {
    return null;
  }
}

// Создать (без id) или обновить (с id). Возвращает метаданные (в т.ч. id).
export async function saveBoard(
  name: string,
  graph: any,
  id?: string,
  kind?: string
): Promise<BoardMeta | null> {
  try {
    const url = id ? `/api/v1/boards/${id}` : "/api/v1/boards/";
    const r = await fetch(url, {
      method: id ? "PATCH" : "POST",
      headers: authHeaders(),
      body: JSON.stringify({ name, graph, ...(kind ? { kind } : {}) }),
    });
    if (!r.ok) {
      console.warn("boards: save failed", r.status, await r.text().catch(() => ""));
      return null;
    }
    const d = await r.json();
    return d.board || null;
  } catch (e) {
    console.warn("boards: save error", e);
    return null;
  }
}

// Процесс из естественного языка (F): описание словами → готовый workflow +
// объяснения. Ничего не сохраняет — фронт кладёт workflow в редактор.
export interface DesignResult {
  success: boolean;
  workflow?: any;
  title?: string;
  summary?: string;
  blocks?: { id: string; type: string; label: string; comment: string }[];
  warnings?: string[];
  error?: string;
}

// current — текущий граф доски {nodes, edges}: тогда это ПРАВКА словами (F.2).
export async function designBoard(
  request: string,
  current?: { nodes: any[]; edges: any[] }
): Promise<DesignResult> {
  try {
    const r = await fetch("/api/v1/boards/design", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ request, ...(current ? { current } : {}) }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      return { success: false, error: d?.message || d?.error || `HTTP ${r.status}` };
    }
    return d as DesignResult;
  } catch (e) {
    return { success: false, error: String(e) };
  }
}

// Прогнать процесс-доску на сервере (P3). Возвращает {status, order?|log?}.
// lang: язык интерфейса пользователя → узлы генерации/мозга/инфографики
// отвечают на нём (аудит: раньше всё выводилось по-русски при любом языке).
export async function runBoard(id: string): Promise<any> {
  try {
    const lang = (typeof document !== "undefined"
      ? document.documentElement.lang : "").slice(0, 2);
    const r = await fetch(
      `/api/v1/boards/${id}/run${lang ? `?lang=${encodeURIComponent(lang)}` : ""}`, {
      method: "POST",
      headers: authHeaders(),
    });
    if (!r.ok) return { status: "error", message: `HTTP ${r.status}` };
    return await r.json();
  } catch (e: any) {
    return { status: "error", message: e?.message || "network" };
  }
}

export async function deleteBoard(id: string): Promise<boolean> {
  try {
    const r = await fetch(`/api/v1/boards/${id}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    return r.ok;
  } catch {
    return false;
  }
}
