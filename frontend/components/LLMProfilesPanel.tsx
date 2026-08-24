'use client'

import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import {
  Cpu,
  X,
  Plus,
  RefreshCw,
  Loader2,
  AlertCircle,
  CheckCircle,
  KeyRound,
  Info,
  User,
  Pencil,
} from 'lucide-react'

interface LLMProfile {
  id: string
  name: string
  provider: string
  model: string
  base_url?: string | null
  is_default: boolean
  has_api_key: boolean
}

const PROVIDERS = [
  'openai',
  'gemini',
  'anthropic',
  'openrouter',
  'groq',
  'together',
  'mistral',
  'deepseek',
  'qwen',
  'ollama',
  'local_vllm',
  'claude_cli',
  'codex_cli',
] as const

const OLLAMA_BASE_URL_PLACEHOLDER = 'http://host.docker.internal:11434/v1'

// API functions
const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''  // '' → Next.js rewrites proxy /api → backend

function getAuthHeaders(): Record<string, string> {
  const token =
    typeof window !== 'undefined'
      ? localStorage.getItem('tessent_access_token') || localStorage.getItem('access_token')
      : null
  if (token) {
    return { Authorization: `Bearer ${token}` }
  }
  return {}
}

interface LLMProfilesPanelProps {
  userId: string | null
  isOpen: boolean
  onClose: () => void
}

export default function LLMProfilesPanel({ userId, isOpen, onClose }: LLMProfilesPanelProps) {
  const t = useTranslations('llm_profiles')
  const [profiles, setProfiles] = useState<LLMProfile[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [settingDefaultId, setSettingDefaultId] = useState<string | null>(null)
  const [showAddForm, setShowAddForm] = useState(false)
  // null → форма создаёт; id → форма редактирует существующий профиль (PATCH).
  const [editingId, setEditingId] = useState<string | null>(null)

  // Personal LLM preference (per-user, separate from admin default)
  const [myProfileId, setMyProfileId] = useState<string | null>(null)
  const [settingMyId, setSettingMyId] = useState<string | null>(null)
  // BYOK: «мой ключ из Интеграций» для ИИ Tessent (думающий слой)
  const [myKey, setMyKey] = useState<{ enabled: boolean; provider?: string | null; available_keys?: Record<string, string>; effective_model?: Record<string, string> } | null>(null)
  const [togglingKey, setTogglingKey] = useState(false)

  // Add form state
  const [newName, setNewName] = useState('')
  const [newProvider, setNewProvider] = useState<string>('openai')
  const [newModel, setNewModel] = useState('')
  const [newBaseUrl, setNewBaseUrl] = useState('')
  const [newApiKey, setNewApiKey] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const isCliProvider = newProvider === 'claude_cli' || newProvider === 'codex_cli'

  const fetchProfiles = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/api/v1/admin/llm-profiles`, {
        headers: getAuthHeaders(),
      })
      if (res.ok) {
        const data = await res.json()
        setProfiles(data.items || [])
      } else if (res.status === 401 || res.status === 403) {
        throw new Error(t('err_forbidden'))
      } else {
        throw new Error(t('err_loading'))
      }

      // Fetch the user's personal LLM preference (separate from admin default)
      if (userId) {
        try {
          const prefRes = await fetch(
            `${API_BASE}/api/v1/chat/llm-preference?user_id=${encodeURIComponent(userId)}`,
            { headers: getAuthHeaders() }
          )
          if (prefRes.ok) {
            const pref = await prefRes.json()
            setMyProfileId(pref?.llm_profile_id ?? null)
          } else if (prefRes.status === 401 || prefRes.status === 403) {
            throw new Error(t('err_forbidden'))
          } else {
            throw new Error(t('err_loading_preference'))
          }
        } catch (e: any) {
          setError(e.message || t('err_loading_preference'))
        }
        // BYOK-статус (некритично — при ошибке просто не показываем секцию)
        try {
          const keyRes = await fetch(
            `${API_BASE}/api/v1/chat/my-llm-key?user_id=${encodeURIComponent(userId)}`,
            { headers: getAuthHeaders() }
          )
          if (keyRes.ok) setMyKey(await keyRes.json())
        } catch { /* ignore */ }
      }
    } catch (e: any) {
      setError(e.message || t('err_loading'))
    } finally {
      setLoading(false)
    }
  }, [t, userId])

  // Включить/выключить «мой ключ из Интеграций» для ИИ Tessent
  const toggleMyKey = async (enabled: boolean) => {
    if (!userId || togglingKey) return
    setTogglingKey(true)
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/chat/my-llm-key?user_id=${encodeURIComponent(userId)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
          body: JSON.stringify({ enabled }),
        }
      )
      if (res.ok) setMyKey(await res.json())
    } catch { /* ignore */ } finally { setTogglingKey(false) }
  }

  useEffect(() => {
    if (isOpen) {
      fetchProfiles()
    }
  }, [isOpen, fetchProfiles])

  const handleSetDefault = async (profileId: string) => {
    setSettingDefaultId(profileId)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/api/v1/admin/llm-profiles/${profileId}/set-default`, {
        method: 'POST',
        headers: getAuthHeaders(),
      })
      if (res.ok) {
        await fetchProfiles()
      } else if (res.status === 401 || res.status === 403) {
        throw new Error(t('err_forbidden'))
      } else {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || t('err_set_default'))
      }
    } catch (e: any) {
      setError(e.message || t('err_set_default'))
    } finally {
      setSettingDefaultId(null)
    }
  }

  // Set or clear the user's personal LLM preference.
  // Pass null to reset back to the admin default.
  const handleSetMyProfile = async (profileId: string | null) => {
    if (!userId) return
    setSettingMyId(profileId ?? '__reset__')
    setError(null)
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/chat/llm-preference?user_id=${encodeURIComponent(userId)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
          body: JSON.stringify({ llm_profile_id: profileId }),
        }
      )
      if (res.ok) {
        setMyProfileId(profileId)
      } else if (res.status === 401 || res.status === 403) {
        throw new Error(t('err_forbidden'))
      } else {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || t('err_set_preference'))
      }
    } catch (e: any) {
      setError(e.message || t('err_set_preference'))
    } finally {
      setSettingMyId(null)
    }
  }

  const resetForm = () => {
    setNewName('')
    setNewProvider('openai')
    setNewModel('')
    setNewBaseUrl('')
    setNewApiKey('')
    setCreateError(null)
    setEditingId(null)
  }

  // Открыть форму в режиме редактирования, предзаполнив поля профиля.
  // API-ключ НЕ предзаполняем (сервер его не отдаёт) — пусто = «не менять».
  const startEdit = (profile: LLMProfile) => {
    setEditingId(profile.id)
    setNewName(profile.name)
    setNewProvider(profile.provider)
    setNewModel(profile.model)
    setNewBaseUrl(profile.base_url || '')
    setNewApiKey('')
    setCreateError(null)
    setShowAddForm(true)
  }

  const handleSubmit = async () => {
    if (!newName.trim() || !newModel.trim()) return
    setCreating(true)
    setCreateError(null)
    try {
      const payload: Record<string, any> = {
        name: newName.trim(),
        provider: newProvider,
        model: newModel.trim(),
        // При редактировании base_url шлём всегда (в т.ч. пустой → очистка);
        // при создании — только если задан.
        ...(editingId || newBaseUrl.trim() ? { base_url: newBaseUrl.trim() || null } : {}),
      }
      // Ключ шлём только если введён непустой (иначе сервер не трогает существующий).
      if (newApiKey.trim()) payload.api_key = newApiKey.trim()

      const url = editingId
        ? `${API_BASE}/api/v1/admin/llm-profiles/${editingId}`
        : `${API_BASE}/api/v1/admin/llm-profiles`
      const res = await fetch(url, {
        method: editingId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify(payload),
      })
      if (res.ok) {
        resetForm()
        setShowAddForm(false)
        await fetchProfiles()
      } else if (res.status === 401 || res.status === 403) {
        throw new Error(t('err_forbidden'))
      } else {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || t(editingId ? 'err_update' : 'err_create'))
      }
    } catch (e: any) {
      setCreateError(e.message || t(editingId ? 'err_update' : 'err_create'))
    } finally {
      setCreating(false)
    }
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-3xl max-h-[85vh] overflow-hidden shadow-2xl flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-brain-700/50 flex-shrink-0">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-purple-500/20 rounded-xl">
                <Cpu className="w-6 h-6 text-purple-400" />
              </div>
              <div>
                <h2 className="text-xl font-semibold text-white">{t('title')}</h2>
                <p className="text-sm text-brain-400">{t('description')}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={fetchProfiles}
                className="p-2 rounded-lg bg-brain-800/50 hover:bg-brain-700/50 text-brain-400 hover:text-brain-200 transition-colors"
                title={t('refresh_button_title')}
              >
                <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              </button>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-brain-800/50 text-brain-400 hover:text-brain-200 transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          {error && (
            <div className="flex items-center gap-2 text-sm text-red-400 bg-red-400/5 rounded-lg p-3 border border-red-400/20">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {error}
            </div>
          )}

          {loading && profiles.length === 0 ? (
            <div className="flex items-center justify-center py-12 text-brain-500">
              <Loader2 className="w-5 h-5 animate-spin mr-2" />
              {t('loading')}
            </div>
          ) : !error && profiles.length === 0 ? (
            <div className="text-center py-12">
              <Cpu className="w-12 h-12 mx-auto text-brain-600 mb-3" />
              <p className="text-brain-400 text-sm mb-1">{t('empty_title')}</p>
              <p className="text-brain-600 text-xs">{t('empty_hint')}</p>
            </div>
          ) : (
            <div className="space-y-2">
              {userId && (
                <div className="flex items-start gap-2 text-xs text-brain-400 bg-emerald-500/5 rounded-lg px-3 py-2 border border-emerald-500/20">
                  <Info className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-0.5" />
                  <span>{t('my_profile_note')}</span>
                </div>
              )}

              {/* BYOK: мой ключ из Интеграций для ИИ Tessent */}
              {userId && myKey && Object.keys(myKey.available_keys || {}).length > 0 && (
                <div className="flex items-start justify-between gap-3 text-xs bg-brain-900/60 rounded-lg px-3 py-2 border border-brain-700/30">
                  <div className="min-w-0">
                    <label className="flex items-center gap-2 cursor-pointer text-brain-200">
                      <input type="checkbox" checked={!!myKey.enabled} disabled={togglingKey}
                        onChange={(e) => toggleMyKey(e.target.checked)} />
                      {t('byok_toggle_label')}
                    </label>
                    <p className="text-brain-500 mt-1">
                      {t('byok_billing_note')}{' '}
                      {Object.entries(myKey.available_keys || {}).map(([p, m]) => `${p} · ${m}`).join(' / ')}
                      {myKey.enabled && myKey.effective_model && (
                        <> · {t('byok_model_label')}: {Object.values(myKey.effective_model)[0]}</>
                      )}
                    </p>
                    <p className="text-brain-600 mt-0.5">{t('byok_priority_note')}</p>
                  </div>
                </div>
              )}
              {profiles.map((profile) => (
                <div
                  key={profile.id}
                  className={`flex items-center gap-3 p-3 rounded-xl border transition-colors ${
                    profile.is_default
                      ? 'bg-purple-500/10 border-purple-500/30'
                      : 'bg-brain-900/60 border-brain-700/30 hover:bg-brain-800/30'
                  }`}
                >
                  {/* Radio "make active" */}
                  <button
                    onClick={() => !profile.is_default && handleSetDefault(profile.id)}
                    disabled={profile.is_default || settingDefaultId !== null}
                    className="shrink-0"
                    title={profile.is_default ? t('active_badge') : t('make_default_title')}
                  >
                    {settingDefaultId === profile.id ? (
                      <Loader2 className="w-4 h-4 animate-spin text-purple-400" />
                    ) : (
                      <span
                        className={`block w-4 h-4 rounded-full border-2 transition-colors ${
                          profile.is_default
                            ? 'border-purple-400 bg-purple-400'
                            : 'border-brain-500 hover:border-purple-400'
                        }`}
                      />
                    )}
                  </button>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-brain-100 truncate">{profile.name}</span>
                      {profile.is_default && (
                        <span className="flex items-center gap-1 text-xs text-purple-300 bg-purple-500/20 border border-purple-500/30 px-1.5 py-0.5 rounded-full shrink-0">
                          <CheckCircle className="w-3 h-3" />
                          {t('active_badge')}
                        </span>
                      )}
                      {myProfileId === profile.id && (
                        <span className="flex items-center gap-1 text-xs text-emerald-300 bg-emerald-500/20 border border-emerald-500/30 px-1.5 py-0.5 rounded-full shrink-0">
                          <User className="w-3 h-3" />
                          {t('my_choice_badge')}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-xs text-brain-500 mt-0.5">
                      <span className="bg-brain-800/50 px-1.5 py-0.5 rounded font-mono">{profile.provider}</span>
                      <span className="truncate">{profile.model}</span>
                      {profile.has_api_key && (
                        <span className="flex items-center gap-1 text-brain-600 shrink-0">
                          <KeyRound className="w-3 h-3" />
                          {t('has_key')}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      onClick={() => startEdit(profile)}
                      title={t('edit_button_title')}
                      className="p-1.5 rounded-lg bg-brain-800/50 border border-brain-700/30 text-brain-400 hover:bg-brain-700/50 hover:text-brain-200 transition-colors shrink-0"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                    {!profile.is_default && (
                      <button
                        onClick={() => handleSetDefault(profile.id)}
                        disabled={settingDefaultId !== null}
                        className="px-3 py-1.5 rounded-lg text-xs bg-brain-800/50 border border-brain-700/30 text-brain-300 hover:bg-purple-600/20 hover:text-purple-300 hover:border-purple-500/40 transition-colors disabled:opacity-50 shrink-0"
                      >
                        {t('make_default_button')}
                      </button>
                    )}
                    {userId &&
                      (myProfileId === profile.id ? (
                        <button
                          onClick={() => handleSetMyProfile(null)}
                          disabled={settingMyId !== null}
                          title={t('reset_my_profile_title')}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs bg-emerald-600/20 border border-emerald-500/40 text-emerald-300 hover:bg-emerald-600/30 transition-colors disabled:opacity-50 shrink-0"
                        >
                          {settingMyId !== null ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : (
                            <User className="w-3.5 h-3.5" />
                          )}
                          {t('reset_my_profile_button')}
                        </button>
                      ) : (
                        <button
                          onClick={() => handleSetMyProfile(profile.id)}
                          disabled={settingMyId !== null}
                          title={t('make_my_profile_title')}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs bg-brain-800/50 border border-brain-700/30 text-brain-300 hover:bg-emerald-600/20 hover:text-emerald-300 hover:border-emerald-500/40 transition-colors disabled:opacity-50 shrink-0"
                        >
                          {settingMyId === profile.id ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : (
                            <User className="w-3.5 h-3.5" />
                          )}
                          {t('make_my_profile_button')}
                        </button>
                      ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Add profile */}
          {!showAddForm ? (
            <button
              onClick={() => {
                resetForm()
                setShowAddForm(true)
              }}
              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white transition-colors text-sm"
            >
              <Plus className="w-4 h-4" />
              {t('add_button')}
            </button>
          ) : (
            <div className="bg-brain-900/60 border border-brain-700/30 rounded-xl p-4 space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-brain-100">{t(editingId ? 'edit_form_title' : 'add_form_title')}</h3>
                <button
                  onClick={() => {
                    setShowAddForm(false)
                    resetForm()
                  }}
                  className="p-1.5 rounded-lg hover:bg-brain-800/50 text-brain-400"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">{t('field_name_label')}</label>
                  <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder={t('field_name_placeholder')}
                    className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                  />
                </div>
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">{t('field_provider_label')}</label>
                  <select
                    value={newProvider}
                    onChange={(e) => setNewProvider(e.target.value)}
                    className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 focus:outline-none focus:border-purple-500/50"
                  >
                    {PROVIDERS.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-sm text-brain-300 mb-1.5">{t('field_model_label')}</label>
                <input
                  type="text"
                  value={newModel}
                  onChange={(e) => setNewModel(e.target.value)}
                  placeholder={t('field_model_placeholder')}
                  className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">
                    {t('field_base_url_label')} <span className="text-brain-600">{t('field_optional')}</span>
                  </label>
                  <input
                    type="text"
                    value={newBaseUrl}
                    onChange={(e) => setNewBaseUrl(e.target.value)}
                    placeholder={newProvider === 'ollama' ? OLLAMA_BASE_URL_PLACEHOLDER : 'https://...'}
                    className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                  />
                </div>
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">
                    {t('field_api_key_label')} <span className="text-brain-600">{t('field_optional')}</span>
                  </label>
                  <input
                    type="password"
                    value={newApiKey}
                    onChange={(e) => setNewApiKey(e.target.value)}
                    placeholder={editingId ? '••••••••' : 'sk-...'}
                    autoComplete="new-password"
                    className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                  />
                  {editingId && (
                    <p className="text-xs text-brain-600 mt-1">{t('field_api_key_edit_hint')}</p>
                  )}
                </div>
              </div>

              {/* Provider hints */}
              {newProvider === 'ollama' && (
                <div className="flex items-start gap-2 text-xs text-brain-400 bg-brain-900/40 rounded-lg px-3 py-2 border border-brain-700/20">
                  <Info className="w-3.5 h-3.5 text-purple-400 shrink-0 mt-0.5" />
                  <span>
                    {t('ollama_hint')}{' '}
                    <code className="font-mono text-brain-300">{OLLAMA_BASE_URL_PLACEHOLDER}</code>
                  </span>
                </div>
              )}
              {isCliProvider && (
                <div className="flex items-start gap-2 text-xs text-brain-400 bg-brain-900/40 rounded-lg px-3 py-2 border border-brain-700/20">
                  <Info className="w-3.5 h-3.5 text-purple-400 shrink-0 mt-0.5" />
                  <span>
                    {t('cli_note_intro')}{' '}
                    <code className="font-mono text-brain-300">
                      docker exec -it &lt;api-container&gt; claude login
                    </code>
                    . {t('cli_note_outro')}
                  </span>
                </div>
              )}

              {createError && (
                <div className="flex items-center gap-2 text-sm text-red-400 bg-red-400/5 rounded-lg p-3 border border-red-400/20">
                  <AlertCircle className="w-4 h-4 shrink-0" />
                  {createError}
                </div>
              )}

              <div className="flex items-center justify-end gap-2">
                <button
                  onClick={() => {
                    setShowAddForm(false)
                    resetForm()
                  }}
                  className="px-4 py-2 rounded-lg text-sm text-brain-400 hover:bg-brain-800/50 transition-colors"
                >
                  {t('cancel_button')}
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={!newName.trim() || !newModel.trim() || creating}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm bg-purple-600 hover:bg-purple-500 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {creating ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : editingId ? (
                    <CheckCircle className="w-4 h-4" />
                  ) : (
                    <Plus className="w-4 h-4" />
                  )}
                  {t(editingId ? 'save_button' : 'create_button')}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
