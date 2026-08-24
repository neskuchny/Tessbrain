/**
 * SIMA API Client для Tessbrain
 * Централизованный клиент для вызова Python бэкенда SIMA.
 * Все запросы идут через /api/v1/sima/ с JWT авторизацией.
 */

const API_BASE = '/api/v1/sima'

function getHeaders(): HeadersInit {
  const token = typeof window !== 'undefined' ? localStorage.getItem('tessent_access_token') : null
  const headers: HeadersInit = { 'Content-Type': 'application/json' }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: getHeaders(),
    ...options,
  })
  if (!res.ok) {
    const error = await res.text()
    throw new Error(`SIMA API Error ${res.status}: ${error}`)
  }
  return res.json()
}

/**
 * Low-level fetch wrapper для SIMA API.
 * Добавляет базовый URL (/api/v1/sima) и JWT авторизацию.
 * Используется в компонентах вместо прямого fetch('/api/...').
 * Возвращает нативный Response (в отличие от request<T>, который парсит JSON).
 * 
 * Пример: simaFetch('/blocks', { method: 'POST', ... })
 *   → fetch('/api/v1/sima/blocks', { headers: { Authorization: ... }, ... })
 */
export async function simaFetch(path: string, options?: RequestInit): Promise<Response> {
  const authHeaders = getHeaders()
  const mergedHeaders = options?.headers
    ? { ...authHeaders, ...(options.headers as Record<string, string>) }
    : authHeaders
  return fetch(`${API_BASE}${path}`, {
    ...options,
    headers: mergedHeaders,
  })
}

// ═══════════════════════════════════════════
// Projects
// ═══════════════════════════════════════════

export async function listProjects() {
  return request<any[]>('/projects')
}

export async function getProject(id: string) {
  return request<any>(`/projects/${id}`)
}

export async function createProject(data: Record<string, any>) {
  return request<any>('/projects', { method: 'POST', body: JSON.stringify(data) })
}

export async function updateProject(id: string, data: Record<string, any>) {
  return request<any>(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(data) })
}

export async function deleteProject(id: string) {
  return request<any>(`/projects/${id}`, { method: 'DELETE' })
}

// ═══════════════════════════════════════════
// Blocks
// ═══════════════════════════════════════════

export async function createBlock(data: Record<string, any>) {
  return request<any>('/blocks', { method: 'POST', body: JSON.stringify(data) })
}

export async function updateBlock(data: Record<string, any>) {
  return request<any>('/blocks', { method: 'PATCH', body: JSON.stringify(data) })
}

export async function deleteBlock(id: string) {
  return request<any>(`/blocks?id=${id}`, { method: 'DELETE' })
}

// ═══════════════════════════════════════════
// Connections
// ═══════════════════════════════════════════

export async function createConnection(data: Record<string, any>) {
  return request<any>('/connections', { method: 'POST', body: JSON.stringify(data) })
}

export async function updateConnection(data: Record<string, any>) {
  return request<any>('/connections', { method: 'PATCH', body: JSON.stringify(data) })
}

export async function deleteConnection(id: string) {
  return request<any>(`/connections?id=${id}`, { method: 'DELETE' })
}

// ═══════════════════════════════════════════
// AI Chat
// ═══════════════════════════════════════════

export async function getChatMessages(projectId: string, sessionId?: string) {
  const params = new URLSearchParams({ projectId })
  if (sessionId) params.append('sessionId', sessionId)
  return request<any[]>(`/ai/chat?${params}`)
}

export async function sendChatMessage(data: {
  projectId: string
  message: string
  mode?: string
  sessionId?: string
  subschemaContext?: string
}) {
  return request<any>('/ai/chat', { method: 'POST', body: JSON.stringify(data) })
}

// ═══════════════════════════════════════════
// AI Generate
// ═══════════════════════════════════════════

export async function generateDescription(data: {
  projectId: string
  blockId: string
  sections?: string[]
}) {
  return request<any>('/ai/generate-description', { method: 'POST', body: JSON.stringify(data) })
}

export async function generateNarrative(projectId: string) {
  return request<any>('/ai/generate-narrative', { method: 'POST', body: JSON.stringify({ projectId }) })
}

export async function generateTZ(projectId: string) {
  return request<{ tz: string }>('/ai/generate-tz', { method: 'POST', body: JSON.stringify({ projectId }) })
}

// ═══════════════════════════════════════════
// Strategist
// ═══════════════════════════════════════════

export async function analyzeProject(projectId: string) {
  return request<any>('/ai/strategist', { method: 'POST', body: JSON.stringify({ projectId, action: 'analyze' }) })
}

export async function analyzeBlock(projectId: string, blockId: string) {
  return request<any>('/ai/strategist', { method: 'POST', body: JSON.stringify({ projectId, blockId, action: 'analyze-block' }) })
}

export async function chatWithStrategist(projectId: string, question: string, history: any[]) {
  return request<{ answer: string }>('/ai/strategist', { method: 'POST', body: JSON.stringify({ projectId, question, history, action: 'chat' }) })
}

export async function getLastStrategistReport(projectId: string) {
  return request<any>('/ai/strategist', { method: 'POST', body: JSON.stringify({ projectId, action: 'last-report' }) })
}

// ═══════════════════════════════════════════
// Simulator
// ═══════════════════════════════════════════

export async function getPersonas(projectId: string) {
  return request<any[]>('/ai/simulator', { method: 'POST', body: JSON.stringify({ projectId, action: 'get-personas' }) })
}

export async function createPersonaFromText(projectId: string, text: string) {
  return request<any>('/ai/simulator', { method: 'POST', body: JSON.stringify({ projectId, text, action: 'create-persona' }) })
}

export async function generatePersonas(projectId: string) {
  return request<any[]>('/ai/simulator', { method: 'POST', body: JSON.stringify({ projectId, action: 'generate-personas' }) })
}

export async function deletePersona(projectId: string, personaId: string) {
  return request<any>('/ai/simulator', { method: 'POST', body: JSON.stringify({ projectId, personaId, action: 'delete-persona' }) })
}

export async function simulate(projectId: string, personaId: string, layerSlug: string) {
  return request<any>('/ai/simulator', { method: 'POST', body: JSON.stringify({ projectId, personaId, layerSlug, action: 'simulate' }) })
}

export async function simulateAll(projectId: string, layerSlug: string) {
  return request<any[]>('/ai/simulator', { method: 'POST', body: JSON.stringify({ projectId, layerSlug, action: 'simulate-all' }) })
}

// ═══════════════════════════════════════════
// Research
// ═══════════════════════════════════════════

export async function conductResearch(projectId: string) {
  return request<any>('/ai/research', { method: 'POST', body: JSON.stringify({ projectId }) })
}

// ═══════════════════════════════════════════
// Data Sources
// ═══════════════════════════════════════════

export async function getDataSources(projectId: string) {
  return request<any[]>(`/data-sources?projectId=${projectId}`)
}

export async function uploadDataSources(projectId: string, sources: any[], autoAnalyze = true) {
  return request<any[]>('/data-sources', {
    method: 'POST',
    body: JSON.stringify({ projectId, sources, autoAnalyze, action: 'upload' }),
  })
}

export async function analyzeDataSource(projectId: string, dataSourceId: string) {
  return request<any[]>('/data-sources', {
    method: 'POST',
    body: JSON.stringify({ projectId, dataSourceId, action: 'analyze' }),
  })
}

export async function deleteDataSource(id: string) {
  return request<any>(`/data-sources?id=${id}`, { method: 'DELETE' })
}

export async function linkInsightToBlock(projectId: string, insightId: string, blockId: string) {
  return request<any>('/data-sources', {
    method: 'POST',
    body: JSON.stringify({ projectId, insightId, blockId, action: 'link' }),
  })
}

export async function unlinkInsight(projectId: string, insightId: string) {
  return request<any>('/data-sources', {
    method: 'POST',
    body: JSON.stringify({ projectId, insightId, action: 'unlink' }),
  })
}

// ═══════════════════════════════════════════
// Tessbrain Data Integration
// ═══════════════════════════════════════════

export async function listTessentMeetings(project?: string) {
  const params = project ? `?project=${project}` : ''
  return request<any[]>(`/data-sources/tessent-meetings${params}`)
}

export async function importTessentMeeting(projectId: string, meetingId: string, autoAnalyze = true) {
  return request<any>('/data-sources/tessent-meetings', {
    method: 'POST',
    body: JSON.stringify({ projectId, meetingId, autoAnalyze }),
  })
}

export async function importTessentKnowledge(projectId: string, keywords?: string[], entityTypes?: string[]) {
  return request<any>('/data-sources/tessent-knowledge', {
    method: 'POST',
    body: JSON.stringify({ projectId, keywords, entityTypes, autoAnalyze: true }),
  })
}

// ═══════════════════════════════════════════
// Snapshots & Sessions
// ═══════════════════════════════════════════

export async function listSnapshots(projectId: string) {
  return request<any[]>(`/snapshots?projectId=${projectId}`)
}

export async function createSnapshot(data: Record<string, any>) {
  return request<any>('/snapshots', { method: 'POST', body: JSON.stringify(data) })
}

export async function listSessions(projectId: string) {
  return request<any[]>(`/sessions?projectId=${projectId}`)
}

export async function createSession(data: Record<string, any>) {
  return request<any>('/sessions', { method: 'POST', body: JSON.stringify(data) })
}
