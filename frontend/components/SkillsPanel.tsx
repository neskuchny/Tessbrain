'use client'

import { useState, useEffect, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import {
  Wrench,
  Play,
  Trash2,
  RefreshCw,
  AlertCircle,
  Loader2,
  ChevronDown,
  ChevronUp,
  Sparkles,
  CheckCircle,
  XCircle,
} from 'lucide-react'

interface SkillParameter {
  name: string
  type?: string
  required?: boolean
  default?: any
  description?: string
}

interface Skill {
  id: string
  name: string
  description?: string
  parameters_schema?: SkillParameter[]
  shared_in_tenant?: boolean
  created_at?: string
}

interface SkillRunResult {
  success: boolean
  result?: string
  error?: string
}

// API functions
const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''  // '' → Next.js rewrites proxy /api → backend

const PREINSTALLED_MARKER = /\[preinstalled:[^\]]*\]/g

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

function isPreinstalled(skill: Skill): boolean {
  return (skill.description || '').includes('[preinstalled:')
}

function cleanDescription(description?: string): string {
  return (description || '').replace(PREINSTALLED_MARKER, '').trim()
}

function SkillCard({
  skill,
  onDelete,
}: {
  skill: Skill
  onDelete: (skill: Skill) => void
}) {
  const t = useTranslations('skills_panel')
  const params = skill.parameters_schema || []

  const buildInitialArgs = useCallback(() => {
    const initial: Record<string, string> = {}
    for (const p of params) {
      initial[p.name] = p.default !== undefined && p.default !== null ? String(p.default) : ''
    }
    return initial
  }, [skill.id])

  const [expanded, setExpanded] = useState(false)
  const [args, setArgs] = useState<Record<string, string>>(buildInitialArgs)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState<SkillRunResult | null>(null)

  const handleRun = async () => {
    // Required check
    const missing = params.filter((p) => p.required && !String(args[p.name] ?? '').trim())
    if (missing.length > 0) {
      setRunResult({
        success: false,
        error: t('err_required_params', { params: missing.map((p) => p.name).join(', ') }),
      })
      return
    }

    setRunning(true)
    setRunResult(null)
    try {
      const payloadArgs: Record<string, any> = {}
      for (const p of params) {
        const value = args[p.name]
        if (value !== undefined && String(value).trim() !== '') {
          payloadArgs[p.name] = p.type === 'number' || p.type === 'integer' ? Number(value) : value
        }
      }

      const res = await fetch(`${API_BASE}/api/v1/skills/${skill.id}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({ args: payloadArgs }),
      })

      const data = await res.json().catch(() => ({}))

      if (!res.ok) {
        throw new Error(data?.detail || data?.error || t('err_run', { status: res.status }))
      }

      const resultText =
        data.result || data.response || data.final_answer || (data.success ? t('run_success_no_output') : '')

      setRunResult({
        success: data.success !== false,
        result: resultText,
        error: data.success === false ? data.error || data.detail || t('err_run_generic') : undefined,
      })
    } catch (e: any) {
      setRunResult({ success: false, error: e.message || t('err_run_generic') })
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="bg-brain-900/60 border border-brain-700/30 rounded-xl overflow-hidden">
      {/* Main row */}
      <div
        className="flex items-center gap-3 p-3 cursor-pointer hover:bg-brain-800/30 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="p-2 rounded-lg bg-purple-500/10 border border-purple-500/20 shrink-0">
          <Wrench className="w-4 h-4 text-purple-400" />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-brain-100 truncate">{skill.name}</span>
            {isPreinstalled(skill) && (
              <span className="text-xs text-purple-400 bg-purple-500/10 border border-purple-500/30 px-1.5 py-0.5 rounded-full shrink-0">
                {t('preinstalled_badge')}
              </span>
            )}
            {params.length > 0 && (
              <span className="text-xs text-brain-600 bg-brain-800/50 px-1.5 py-0.5 rounded shrink-0">
                {t('params_count', { count: params.length })}
              </span>
            )}
          </div>
          {cleanDescription(skill.description) && (
            <p className="text-xs text-brain-500 truncate mt-0.5">{cleanDescription(skill.description)}</p>
          )}
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={(e) => {
              e.stopPropagation()
              onDelete(skill)
            }}
            className="p-1.5 rounded-lg hover:bg-red-600/20 text-red-400 hover:text-red-300 transition-colors"
            title={t('delete_title')}
          >
            <Trash2 className="w-4 h-4" />
          </button>
          {expanded ? (
            <ChevronUp className="w-4 h-4 text-brain-500" />
          ) : (
            <ChevronDown className="w-4 h-4 text-brain-500" />
          )}
        </div>
      </div>

      {/* Expanded: parameters form + run */}
      {expanded && (
        <div className="border-t border-brain-700/30 p-4 space-y-4">
          {cleanDescription(skill.description) && (
            <div className="text-sm text-brain-200 bg-brain-800/40 rounded-lg p-3 border border-brain-700/20">
              {cleanDescription(skill.description)}
            </div>
          )}

          {/* Parameters form */}
          {params.length > 0 ? (
            <div>
              <div className="text-xs text-brain-500 mb-2">{t('params_title')}</div>
              <div className="space-y-3">
                {params.map((p) => (
                  <div key={p.name}>
                    <label className="block text-sm text-brain-300 mb-1.5">
                      {p.name}
                      {p.required && <span className="text-red-400 ml-1">*</span>}
                      {p.type && <span className="text-brain-600 text-xs ml-2">({p.type})</span>}
                    </label>
                    <input
                      type={p.type === 'number' || p.type === 'integer' ? 'number' : 'text'}
                      value={args[p.name] ?? ''}
                      onChange={(e) => setArgs((prev) => ({ ...prev, [p.name]: e.target.value }))}
                      placeholder={p.description || p.name}
                      className="w-full bg-brain-800/50 border border-brain-700/30 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-purple-500/50"
                    />
                    {p.description && <p className="text-xs text-brain-600 mt-1">{p.description}</p>}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="text-xs text-brain-600">{t('no_params')}</div>
          )}

          {/* Run button */}
          <div className="flex items-center justify-end">
            <button
              onClick={handleRun}
              disabled={running}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm bg-purple-600 hover:bg-purple-500 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              {running ? t('running_button') : t('run_button')}
            </button>
          </div>

          {/* Run result */}
          {runResult && (
            <div
              className={`text-sm rounded-lg p-3 border ${
                runResult.success
                  ? 'text-green-300 bg-green-400/5 border-green-400/20'
                  : 'text-red-400 bg-red-400/5 border-red-400/20'
              }`}
            >
              <div className="flex items-center gap-1.5 text-xs mb-1.5">
                {runResult.success ? (
                  <CheckCircle className="w-3.5 h-3.5 text-green-400" />
                ) : (
                  <XCircle className="w-3.5 h-3.5 text-red-400" />
                )}
                <span className={runResult.success ? 'text-green-400' : 'text-red-400'}>
                  {runResult.success ? t('result_title') : t('result_error_title')}
                </span>
              </div>
              <div className="whitespace-pre-wrap break-words max-h-64 overflow-auto">
                {runResult.success ? runResult.result : runResult.error}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

interface SkillsPanelProps {
  userId?: string
}

export default function SkillsPanel({ userId }: SkillsPanelProps) {
  const t = useTranslations('skills_panel')
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  const fetchSkills = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await fetch(`${API_BASE}/api/v1/skills/`, {
        headers: getAuthHeaders(),
      })
      if (res.ok) {
        const data = await res.json()
        setSkills(data.skills || [])
      } else if (res.status === 401) {
        throw new Error(t('err_unauthorized'))
      } else {
        throw new Error(t('err_loading'))
      }
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    fetchSkills()
  }, [fetchSkills])

  const handleDelete = async (skill: Skill) => {
    if (!confirm(t('delete_confirm', { name: skill.name }))) return
    try {
      setDeletingId(skill.id)
      const res = await fetch(`${API_BASE}/api/v1/skills/${skill.id}`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      })
      if (res.ok || res.status === 204) {
        setSkills((prev) => prev.filter((s) => s.id !== skill.id))
      } else {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || t('err_delete'))
      }
    } catch (e: any) {
      setError(e.message)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="h-full flex flex-col bg-brain-950/50">
      {/* Header */}
      <div className="p-4 border-b border-brain-700/30">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Wrench className="w-5 h-5 text-purple-400" />
            <h2 className="text-lg font-semibold text-brain-100">{t('title')}</h2>
            <span className="text-xs text-brain-500 bg-purple-500/10 border border-purple-500/30 px-2 py-0.5 rounded-full">
              {t('count_badge', { count: skills.length })}
            </span>
          </div>
          <button
            onClick={fetchSkills}
            className="p-2 rounded-lg bg-brain-800/50 hover:bg-brain-700/50 text-brain-400 hover:text-brain-200 transition-colors"
            title={t('refresh_button_title')}
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        {/* Info */}
        <div className="text-xs text-brain-500 bg-brain-900/40 rounded-lg px-3 py-2 border border-brain-700/20">
          <Sparkles className="w-3.5 h-3.5 inline mr-1 text-purple-400" />
          {t('info_banner')}
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-auto p-4 space-y-3">
        {loading && skills.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-brain-500">
            <Loader2 className="w-5 h-5 animate-spin mr-2" />
            {t('loading')}
          </div>
        ) : error ? (
          <div className="flex items-center justify-center py-12 text-red-400">
            <AlertCircle className="w-5 h-5 mr-2" />
            {error}
          </div>
        ) : skills.length === 0 ? (
          <div className="text-center py-12">
            <Wrench className="w-12 h-12 mx-auto text-brain-600 mb-3" />
            <p className="text-brain-400 text-sm mb-1">{t('empty_title')}</p>
            <p className="text-brain-600 text-xs">{t('empty_hint')}</p>
          </div>
        ) : (
          skills.map((skill) => (
            <div key={skill.id} className={deletingId === skill.id ? 'opacity-50 pointer-events-none' : ''}>
              <SkillCard skill={skill} onDelete={handleDelete} />
            </div>
          ))
        )}
      </div>
    </div>
  )
}
