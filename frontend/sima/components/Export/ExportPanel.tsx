'use client'

import { useState, useMemo, useRef, useCallback, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { cn } from '@/sima/lib/utils'
import {
  Download,
  Copy,
  Check,
  Loader2,
  FileCode,
  Github,
  Heart,
  Terminal,
  ExternalLink,
  FolderTree,
  ChevronDown,
  ChevronRight,
  GitBranch,
  Rocket,
  FolderOpen,
  Eye,
  Image,
  Maximize2,
  X,
} from 'lucide-react'
import type { BlockData, ConnectionData } from '@/sima/types'
import { simaFetch } from '@/sima/lib/api'

type ExportTarget = 'cursor' | 'github' | 'lovable' | 'claude-code'

interface ExportResult {
  type: ExportTarget
  files?: Record<string, string>
  prompt?: string
  repoName?: string
  slug?: string
}

const buildTargets = (t: (k: string) => string): { id: ExportTarget; label: string; description: string; icon: typeof FileCode }[] => [
  {
    id: 'cursor',
    label: 'Cursor IDE',
    description: t('target_cursor_desc'),
    icon: FileCode,
  },
  {
    id: 'github',
    label: 'GitHub',
    description: t('target_full_desc'),
    icon: Github,
  },
  {
    id: 'lovable',
    label: 'Lovable.dev',
    description: t('target_lovable_desc'),
    icon: Heart,
  },
  {
    id: 'claude-code',
    label: 'Claude Code CLI',
    description: t('target_claude_desc'),
    icon: Terminal,
  },
]

// ═══════════════════════════════════════════════════
// Mermaid-генератор
// ═══════════════════════════════════════════════════

function generateMermaid(blocks: BlockData[], connections: ConnectionData[], allBlocks: BlockData[], allConnections: ConnectionData[]): string {
  const lines: string[] = []
  lines.push('graph TD')
  lines.push('')

  // Стиль-классы для типов
  lines.push('  classDef core fill:#6366F1,stroke:#4F46E5,color:#fff')
  lines.push('  classDef feature fill:#22D3EE,stroke:#06B6D4,color:#000')
  lines.push('  classDef integration fill:#34D399,stroke:#10B981,color:#000')
  lines.push('  classDef data fill:#FBBF24,stroke:#F59E0B,color:#000')
  lines.push('  classDef agent fill:#A78BFA,stroke:#8B5CF6,color:#fff')
  lines.push('')

  // Рекурсивно обрабатываем блоки
  function renderLevel(parentId: string | null, indent: string) {
    const levelBlocks = allBlocks.filter(b => (b.parentBlockId || null) === parentId)
    const children = allBlocks.filter(b => b.parentBlockId)
    const parentIds = new Set(children.map(c => c.parentBlockId!))

    for (const block of levelBlocks) {
      const hasChildren = parentIds.has(block.id)
      const sanitized = block.name.replace(/"/g, "'").replace(/[[\]{}()]/g, '')

      if (hasChildren) {
        // Подсхема → subgraph
        lines.push(`${indent}subgraph ${block.id}["${block.label}: ${sanitized}"]`)
        renderLevel(block.id, indent + '  ')
        lines.push(`${indent}end`)
      } else {
        // Обычный блок
        const shape = block.type === 'data' ? `[("${block.label}: ${sanitized}")]`
          : block.type === 'agent' ? `{{"${block.label}: ${sanitized}"}}`
          : `["${block.label}: ${sanitized}"]`
        lines.push(`${indent}${block.id}${shape}`)
      }
      // Класс
      lines.push(`${indent}class ${block.id} ${block.type}`)
    }
  }

  renderLevel(null, '  ')
  lines.push('')

  // Связи
  for (const conn of allConnections) {
    const from = allBlocks.find(b => b.id === conn.fromBlockId)
    const to = allBlocks.find(b => b.id === conn.toBlockId)
    if (!from || !to) continue

    const label = conn.label ? `|"${conn.label.replace(/"/g, "''")}"|` : ''
    const arrow = conn.type === 'dependency' ? '-.->' : conn.type === 'trigger' ? '==>' : conn.animated ? '-->' : '-->'
    lines.push(`  ${conn.fromBlockId} ${arrow}${label} ${conn.toBlockId}`)
  }

  return lines.join('\n')
}

export default function ExportPanel() {
  const t = useTranslations('sima_export')
  const TARGETS = buildTargets(t)
  const { project, allBlocks, allConnections, blocks, connections, setAllBlocks, setAllConnections, setProject, currentParentBlockId } = useSimaStore()
  const setKanonVerdicts = useSimaStore(s => s.setKanonVerdicts)
  const [loading, setLoading] = useState<ExportTarget | null>(null)
  const [result, setResult] = useState<ExportResult | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set())
  const [showMermaid, setShowMermaid] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [showRendered, setShowRendered] = useState(false)
  const [renderingSvg, setRenderingSvg] = useState(false)
  const [renderedSvg, setRenderedSvg] = useState<string>('')
  const [fullscreen, setFullscreen] = useState(false)
  const mermaidContainerRef = useRef<HTMLDivElement>(null)
  
  // Прямой запуск
  const [launching, setLaunching] = useState<string | null>(null)
  const [launchResult, setLaunchResult] = useState<{ success: boolean; message: string; folder?: string } | null>(null)
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState<{ success: boolean; message: string } | null>(null)

  // Mermaid-код
  const mermaidCode = useMemo(
    () => allBlocks.length > 0 ? generateMermaid(blocks, connections, allBlocks, allConnections) : '',
    [allBlocks, allConnections, blocks, connections]
  )

  if (!project) {
    return (
      <div className="p-4 text-sm text-sima-textDim text-center">
        {t('no_project')}
      </div>
    )
  }

  async function handleExport(target: ExportTarget) {
    setLoading(target)
    setResult(null)
    try {
      const res = await simaFetch('/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project!.id, target }),
      })
      const data = await res.json()
      if (!res.ok || data.error) {
        throw new Error(data.error || t('err_export'))
      }
      setResult({ type: target, ...data })
    } catch (e) {
      alert(e instanceof Error ? e.message : t('err_export'))
    } finally {
      setLoading(null)
    }
  }

  async function copyToClipboard(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      setTimeout(() => setCopied(null), 2000)
    } catch {
      // Fallback
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      setCopied(key)
      setTimeout(() => setCopied(null), 2000)
    }
  }

  function copyAllFiles(files: Record<string, string>) {
    const allContent = Object.entries(files)
      .map(([name, content]) => `=== ${name} ===\n${content}`)
      .join('\n\n')
    copyToClipboard(allContent, 'all-files')
  }

  function downloadFile(filename: string, content: string) {
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  function downloadAllFiles(files: Record<string, string>) {
    Object.entries(files).forEach(([name, content]) => {
      downloadFile(name.replace(/\//g, '_'), content)
    })
  }

  async function handleImport() {
    if (!importText.trim() || !project) return
    setImporting(true)
    setImportResult(null)
    try {
      const res = await simaFetch('/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          content: importText.trim(),
          parentBlockId: currentParentBlockId || null,
        }),
      })
      const data = await res.json()
      if (!res.ok) {
        setImportResult({ success: false, message: data.error || t('err_import') })
        return
      }
      // Обновляем проект
      if (data.project) {
        setAllBlocks(data.project.blocks || [])
        setAllConnections(data.project.connections || [])
        setProject(data.project)
      }
      setImportResult({
        success: true,
        message: t('import_success', { format: data.format, blocks: data.importedBlocks, connections: data.importedConnections }),
      })
      setImportText('')
    } catch (e) {
      setImportResult({ success: false, message: (e as Error).message })
    } finally {
      setImporting(false)
    }
  }

  // === Kanon: автономная реализация через двухфазный handoff ===
  // Фаза 1 создаёт PENDING-запись (агент НЕ запущен); запуск — только
  // после явного подтверждения пользователя кнопкой ниже.
  const [kanonStatus, setKanonStatus] = useState<{ ready: number; total: number; error?: string } | null>(null)
  const [kanonAgent, setKanonAgent] = useState<'claude' | 'cursor' | 'codex'>('claude')
  const [kanonRunCmds, setKanonRunCmds] = useState(false)  // cmd:-проверки приёмки
  const [kanonJudge, setKanonJudge] = useState(false)      // семантический LLM-судья
  // Установлены ли помощники на сервере (чтобы «Подтвердить» не жал в пустоту)
  // + привязан ли СВОЙ ключ пользователя (BYO).
  const [cliHealth, setCliHealth] = useState<{ agents?: Record<string, { installed: boolean }>; exec_enabled?: boolean; my_key?: { mode?: string | null; has_key: boolean; provider?: string; masked?: string; agents?: Record<string, { connected: boolean; command?: string }> } } | null>(null)
  const [keyProvider, setKeyProvider] = useState<'anthropic' | 'openai'>('anthropic')
  const [keyInput, setKeyInput] = useState('')
  const [keyBusy, setKeyBusy] = useState(false)
  const [byoChoice, setByoChoice] = useState<null | 'api' | 'sub'>(null)

  const authHdrs = (): Record<string, string> => {
    const token = typeof window !== 'undefined' ? localStorage.getItem('tessent_access_token') : null
    const h: Record<string, string> = { 'Content-Type': 'application/json' }
    if (token) h['Authorization'] = `Bearer ${token}`
    return h
  }

  const loadCliHealth = async () => {
    try {
      const res = await fetch('/api/v1/task-analysis/cli-health', { headers: authHdrs() })
      setCliHealth(await res.json())
    } catch { /* необязательная информация */ }
  }

  useEffect(() => { loadCliHealth() }, [])

  const saveOwnKey = async () => {
    if (!keyInput.trim() || keyBusy) return
    setKeyBusy(true)
    try {
      await fetch('/api/v1/task-analysis/my-coding-key', {
        method: 'POST', headers: authHdrs(),
        body: JSON.stringify({ provider: keyProvider, api_key: keyInput.trim() }),
      })
      setKeyInput(''); setByoChoice(null)
      await loadCliHealth()
    } catch { /* ignore */ } finally { setKeyBusy(false) }
  }

  const useOwnSubscription = async () => {
    if (keyBusy) return
    setKeyBusy(true)
    try {
      await fetch('/api/v1/task-analysis/my-coding-subscription', { method: 'POST', headers: authHdrs() })
      setByoChoice(null)
      await loadCliHealth()
    } catch { /* ignore */ } finally { setKeyBusy(false) }
  }

  const removeOwnKey = async () => {
    if (keyBusy) return
    setKeyBusy(true)
    try {
      await fetch('/api/v1/task-analysis/my-coding-key/delete', { method: 'POST', headers: authHdrs() })
      await loadCliHealth()
    } catch { /* ignore */ } finally { setKeyBusy(false) }
  }
  const [kanonHandoff, setKanonHandoff] = useState<any | null>(null)
  const [kanonBusy, setKanonBusy] = useState<string | null>(null)
  const [kanonMsg, setKanonMsg] = useState<{ ok: boolean; text: string } | null>(null)

  async function handleKanonStatus() {
    if (!project) return
    setKanonBusy('status')
    setKanonMsg(null)
    try {
      const res = await simaFetch(`/kanon/status?projectId=${project.id}`)
      const data = await res.json()
      if (data.success === false) {
        setKanonStatus({ ready: 0, total: 0, error: data.error })
      } else {
        setKanonStatus({ ready: data.blocks_ready_for_handoff, total: data.blocks_total })
        // вердикты → store: канвас красит блоки (pass/desync/inconclusive/ready)
        const verdicts: Record<string, { verdict?: string; cascadeState?: string; ready?: boolean }> = {}
        for (const b of data.blocks || []) {
          verdicts[b.block_id] = {
            verdict: b.last_verdict,
            cascadeState: b.cascade_state,
            ready: b.ready_for_handoff,
          }
        }
        setKanonVerdicts(verdicts)
      }
    } catch (e) {
      setKanonMsg({ ok: false, text: (e as Error).message })
    } finally {
      setKanonBusy(null)
    }
  }

  async function handleKanonHandoff() {
    if (!project) return
    setKanonBusy('handoff')
    setKanonMsg(null)
    try {
      const res = await simaFetch('/kanon/handoff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, agent: kanonAgent, runCommands: kanonRunCmds, judge: kanonJudge }),
      })
      const data = await res.json()
      if (data.success === false || data.error) {
        setKanonMsg({ ok: false, text: data.error || t('unknown_error') })
      } else {
        setKanonHandoff(data)
        setKanonMsg({ ok: true, text: t('kanon_pending') })
      }
    } catch (e) {
      setKanonMsg({ ok: false, text: (e as Error).message })
    } finally {
      setKanonBusy(null)
    }
  }

  // confirm/reject живут в /api/v1/task-analysis (общий двухфазный handoff)
  async function kanonHandoffAction(action: 'confirm' | 'reject') {
    if (!kanonHandoff?.id) return
    setKanonBusy(action)
    setKanonMsg(null)
    try {
      const token = typeof window !== 'undefined' ? localStorage.getItem('tessent_access_token') : null
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (token) headers['Authorization'] = `Bearer ${token}`
      const res = await fetch(`/api/v1/task-analysis/handoff/${kanonHandoff.id}/${action}`, {
        method: 'POST',
        headers,
        body: JSON.stringify({}),
      })
      const data = await res.json()
      if (action === 'reject') {
        setKanonHandoff(null)
        setKanonMsg({ ok: true, text: t('kanon_rejected') })
      } else if (data.status === 'success') {
        // P3: показать результат авто-приёмки по контрактам (kanon-луп)
        const k = data.kanon
        if (k && k.accepted === true) {
          setKanonHandoff(null)
          setKanonMsg({ ok: true, text: t('kanon_accepted', { verified: k.verified }) })
        } else if (k && k.accepted === false && k.refine_handoff_id) {
          // подставляем рефайн-handoff — можно подтвердить следующий круг
          setKanonHandoff({ ...(kanonHandoff || {}), id: k.refine_handoff_id })
          const names = (k.failing || []).map((f: any) => f.name).filter(Boolean).join(', ')
          setKanonMsg({ ok: false, text: t('kanon_failing_refine', { names }) })
        } else if (k && k.accepted === false) {
          setKanonHandoff(null)
          setKanonMsg({ ok: false, text: `${t('kanon_failing_prefix')} ${k.refine || t('kanon_failing_fallback')}` })
        } else {
          setKanonHandoff(null)
          setKanonMsg({ ok: true, text: t('kanon_confirmed') })
        }
      } else {
        setKanonMsg({ ok: false, text: data.message || data.error || t('unknown_error') })
      }
    } catch (e) {
      setKanonMsg({ ok: false, text: (e as Error).message })
    } finally {
      setKanonBusy(null)
    }
  }

  async function handleLaunch(target: 'cursor' | 'claude-code') {
    if (!project) return
    setLaunching(target)
    setLaunchResult(null)
    try {
      const res = await simaFetch('/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, target }),
      })
      const data = await res.json()
      setLaunchResult({
        success: data.success && data.launched,
        message: data.message || data.error || t('unknown_error'),
        folder: data.projectFolder,
      })
    } catch (e) {
      setLaunchResult({ success: false, message: (e as Error).message })
    } finally {
      setLaunching(null)
    }
  }

  async function openFolder(folder: string) {
    try {
      await simaFetch('/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: 'open-folder', projectId: project?.id || 'x', folder }),
      })
    } catch {}
  }

  // Запуск Lovable — генерируем промпт и открываем lovable.dev с ним в URL
  async function handleLaunchLovable() {
    if (!project) return
    setLaunching('lovable')
    setLaunchResult(null)
    try {
      // Генерируем промпт через API
      const res = await simaFetch('/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, target: 'lovable' }),
      })
      const data = await res.json()
      if (data.error) throw new Error(data.error)
      const prompt = data.prompt as string

      if (!prompt) throw new Error(t('err_prompt_not_generated'))

      // Формируем URL для Lovable Build with URL API
      // Документация: https://docs.lovable.dev/integrations/build-with-url
      const encodedPrompt = encodeURIComponent(prompt)
      const lovableUrl = `https://lovable.dev/?autosubmit=true#prompt=${encodedPrompt}`

      // Открываем в новой вкладке
      window.open(lovableUrl, '_blank')

      setLaunchResult({
        success: true,
        message: t('lovable_opened'),
      })
    } catch (e) {
      setLaunchResult({ success: false, message: (e as Error).message })
    } finally {
      setLaunching(null)
    }
  }

  // Рендер Mermaid в SVG
  const renderMermaid = useCallback(async () => {
    if (!mermaidCode) return
    setRenderingSvg(true)
    try {
      const mermaid = (await import('mermaid')).default
      mermaid.initialize({
        startOnLoad: false,
        // Результат render() уходит в dangerouslySetInnerHTML, а код диаграммы
        // приходит из данных проекта. В mermaid 11 'strict' и так умолчание
        // (он сам чистит вывод), но умолчание — не гарантия: обновление
        // библиотеки может его сменить, и тогда htmlLabels ниже пропустит
        // разметку из подписи узла. Ставим явно.
        securityLevel: 'strict',
        theme: 'dark',
        themeVariables: {
          primaryColor: '#6366F1',
          primaryTextColor: '#E2E8F0',
          primaryBorderColor: '#4F46E5',
          lineColor: '#6366F1',
          secondaryColor: '#1E293B',
          tertiaryColor: '#0F172A',
          background: '#0A0E1A',
          mainBkg: '#1E293B',
          nodeBkg: '#1E293B',
          nodeBorder: '#6366F1',
          clusterBkg: '#0F172A',
          clusterBorder: '#6366F1',
          titleColor: '#E2E8F0',
          edgeLabelBackground: '#1E293B',
        },
        flowchart: {
          htmlLabels: true,
          curve: 'basis',
          padding: 15,
        },
      })
      const id = 'sima-mermaid-' + Date.now()
      const { svg } = await mermaid.render(id, mermaidCode)
      setRenderedSvg(svg)
      setShowRendered(true)
    } catch (e) {
      console.error('Mermaid render error:', e)
      alert(t('err_render_mermaid') + (e as Error).message)
    } finally {
      setRenderingSvg(false)
    }
  }, [mermaidCode])

  // Скачать как PNG
  const downloadPng = useCallback(async () => {
    if (!renderedSvg) return
    try {
      const svgBlob = new Blob([renderedSvg], { type: 'image/svg+xml;charset=utf-8' })
      const url = URL.createObjectURL(svgBlob)

      const img = new window.Image()
      img.onload = () => {
        const canvas = document.createElement('canvas')
        const scale = 2 // retina
        canvas.width = img.width * scale
        canvas.height = img.height * scale
        const ctx = canvas.getContext('2d')!
        ctx.scale(scale, scale)
        ctx.fillStyle = '#0A0E1A'
        ctx.fillRect(0, 0, img.width, img.height)
        ctx.drawImage(img, 0, 0)
        canvas.toBlob((blob) => {
          if (!blob) return
          const pngUrl = URL.createObjectURL(blob)
          const a = document.createElement('a')
          a.href = pngUrl
          a.download = `${project?.name || 'diagram'}_mermaid.png`
          a.click()
          URL.revokeObjectURL(pngUrl)
          URL.revokeObjectURL(url)
        }, 'image/png')
      }
      img.src = url
    } catch (e) {
      console.error('PNG export error:', e)
    }
  }, [renderedSvg, project])

  // Скачать как SVG
  const downloadSvg = useCallback(() => {
    if (!renderedSvg) return
    const blob = new Blob([renderedSvg], { type: 'image/svg+xml;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${project?.name || 'diagram'}_mermaid.svg`
    a.click()
    URL.revokeObjectURL(url)
  }, [renderedSvg, project])

  function toggleFile(filename: string) {
    setExpandedFiles(prev => {
      const next = new Set(prev)
      if (next.has(filename)) next.delete(filename)
      else next.add(filename)
      return next
    })
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header with tabs */}
      <div className="px-4 py-2 border-b border-sima-border shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={() => setShowImport(false)}
            className={cn(
              'flex items-center gap-1.5 text-xs font-semibold transition-colors',
              !showImport ? 'text-sima-primary' : 'text-sima-textDim hover:text-sima-text'
            )}
          >
            <ExternalLink className="w-3.5 h-3.5" />
            {t('tab_export')}
          </button>
          <span className="text-sima-textDim">|</span>
          <button
            onClick={() => setShowImport(true)}
            className={cn(
              'flex items-center gap-1.5 text-xs font-semibold transition-colors',
              showImport ? 'text-green-400' : 'text-sima-textDim hover:text-sima-text'
            )}
          >
            <Download className="w-3.5 h-3.5" />
            {t('tab_import')}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Import UI */}
        {showImport && (
          <div className="space-y-3">
            <div className="text-xs text-sima-textDim">
              {t('import_hint')}
            </div>
            <textarea
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
              placeholder={t("import_placeholder")}
              className="w-full h-40 bg-sima-bg border border-sima-border rounded-lg px-3 py-2 text-xs text-sima-text placeholder-sima-textDim font-mono resize-none focus:outline-none focus:border-green-400/50"
            />
            <button
              onClick={handleImport}
              disabled={!importText.trim() || importing}
              className="w-full py-2 bg-green-500/20 text-green-400 rounded-lg text-xs font-medium flex items-center justify-center gap-2 hover:bg-green-500/30 transition-colors disabled:opacity-50"
            >
              {importing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
              {importing ? t('importing') : t('import_button')}
            </button>
            {importResult && (
              <div className={cn(
                'text-xs p-2 rounded-lg border',
                importResult.success
                  ? 'text-green-400 bg-green-500/10 border-green-500/20'
                  : 'text-red-400 bg-red-500/10 border-red-500/20'
              )}>
                {importResult.message}
              </div>
            )}
          </div>
        )}

        {!showImport && (<>  {/* Export content */}

        {/* === ПРЯМОЙ ЗАПУСК === */}
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Rocket className="w-3.5 h-3.5 text-sima-primary" />
            <span className="text-xs font-semibold text-sima-text">{t('direct_run_heading')}</span>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {/* Cursor Launch */}
            <button
              onClick={() => handleLaunch('cursor')}
              disabled={launching !== null}
              className={cn(
                'flex items-center gap-2.5 p-3 rounded-lg border transition-all text-left',
                'border-cyan-400/30 bg-cyan-400/5 hover:bg-cyan-400/10 hover:border-cyan-400/50',
                launching === 'cursor' && 'opacity-70'
              )}
            >
              {launching === 'cursor' ? (
                <Loader2 className="w-5 h-5 animate-spin text-cyan-400 shrink-0" />
              ) : (
                <FileCode className="w-5 h-5 text-cyan-400 shrink-0" />
              )}
              <div>
                <div className="text-xs font-medium text-sima-text">Cursor</div>
                <div className="text-[10px] text-sima-textDim leading-tight">{t('direct_cursor_hint')}</div>
              </div>
            </button>

            {/* Claude Code Launch */}
            <button
              onClick={() => handleLaunch('claude-code')}
              disabled={launching !== null}
              className={cn(
                'flex items-center gap-2.5 p-3 rounded-lg border transition-all text-left',
                'border-orange-400/30 bg-orange-400/5 hover:bg-orange-400/10 hover:border-orange-400/50',
                launching === 'claude-code' && 'opacity-70'
              )}
            >
              {launching === 'claude-code' ? (
                <Loader2 className="w-5 h-5 animate-spin text-orange-400 shrink-0" />
              ) : (
                <Terminal className="w-5 h-5 text-orange-400 shrink-0" />
              )}
              <div>
                <div className="text-xs font-medium text-sima-text">Claude Code</div>
                <div className="text-[10px] text-sima-textDim leading-tight">{t('direct_claude_hint')}</div>
              </div>
            </button>

            {/* Lovable Launch */}
            <button
              onClick={handleLaunchLovable}
              disabled={launching === 'lovable'}
              className={cn(
                'flex items-center gap-2.5 p-3 rounded-lg border transition-all text-left',
                'border-pink-400/30 bg-pink-400/5 hover:bg-pink-400/10 hover:border-pink-400/50',
                launching === 'lovable' && 'opacity-70'
              )}
            >
              {launching === 'lovable' ? (
                <Loader2 className="w-5 h-5 animate-spin text-pink-400 shrink-0" />
              ) : (
                <Heart className="w-5 h-5 text-pink-400 shrink-0" />
              )}
              <div>
                <div className="text-xs font-medium text-sima-text">Lovable</div>
                <div className="text-[10px] text-sima-textDim leading-tight">{t('direct_lovable_hint')}</div>
              </div>
            </button>
          </div>

          {/* Launch result */}
          {launchResult && (
            <div className={cn(
              'p-3 rounded-lg border text-xs',
              launchResult.success
                ? 'bg-green-400/5 border-green-400/20 text-green-400'
                : 'bg-amber-400/5 border-amber-400/20 text-amber-400'
            )}>
              <p>{launchResult.message}</p>
              {launchResult.folder && (
                <button
                  onClick={() => openFolder(launchResult.folder!)}
                  className="flex items-center gap-1 mt-1.5 text-[10px] text-sima-textMuted hover:text-sima-text transition-colors"
                >
                  <FolderOpen className="w-3 h-3" />
                  {launchResult.folder}
                </button>
              )}
            </div>
          )}
        </div>

        {/* === KANON: автономная реализация (двухфазный handoff) === */}
        <div className="border-t border-sima-border pt-3 space-y-2">
          <div className="flex items-center gap-2">
            <GitBranch className="w-3.5 h-3.5 text-violet-400" />
            <span className="text-xs font-semibold text-sima-text">{t('kanon_heading')}</span>
          </div>
          <p className="text-[10px] text-sima-textDim leading-tight">{t('kanon_hint')}</p>
          <div className="flex items-center gap-2">
            <button
              onClick={handleKanonStatus}
              disabled={kanonBusy !== null}
              className="px-2.5 py-1.5 rounded-lg border border-violet-400/30 bg-violet-400/5 hover:bg-violet-400/10 text-[11px] text-sima-text transition-all"
            >
              {kanonBusy === 'status' ? <Loader2 className="w-3.5 h-3.5 animate-spin inline" /> : t('kanon_check_readiness')}
            </button>
            {kanonStatus && !kanonStatus.error && (
              <span className={cn('text-[11px]', kanonStatus.ready > 0 ? 'text-green-400' : 'text-amber-400')}>
                {t('kanon_ready', { ready: kanonStatus.ready, total: kanonStatus.total })}
              </span>
            )}
            {kanonStatus?.error && (
              <span className="text-[11px] text-amber-400">{kanonStatus.error}</span>
            )}
          </div>
          {!kanonHandoff && (
            <div className="flex items-center gap-2">
              <select
                value={kanonAgent}
                onChange={(e) => setKanonAgent(e.target.value as any)}
                className="px-2 py-1.5 rounded-lg bg-sima-bg border border-sima-border text-[11px] text-sima-text"
              >
                <option value="claude">{t('agent_claude_option')}</option>
                <option value="cursor">Cursor Agent</option>
                <option value="codex">Codex CLI</option>
              </select>
              <button
                onClick={handleKanonHandoff}
                disabled={kanonBusy !== null}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-violet-400/40 bg-violet-400/10 hover:bg-violet-400/20 text-[11px] font-medium text-sima-text transition-all"
              >
                {kanonBusy === 'handoff' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Rocket className="w-3.5 h-3.5 text-violet-400" />}
                {t('kanon_create_handoff')}
              </button>
            </div>
          )}
          {/* Свой доступ помощника (BYO): сборка идёт на твой аккаунт/биллинг,
              а не на аккаунт разработчиков сервера. Два способа: свой ключ
              или своя подписка через свой CLI. */}
          {!kanonHandoff && (
            <div className="mt-1.5 p-2 rounded-lg border border-sima-border bg-sima-bg/40 text-[11px]">
              {cliHealth?.my_key?.mode === 'api_key' ? (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sima-textMuted">
                    {t('own_key_connected_prefix')} <span className="text-sima-text font-mono">{cliHealth.my_key.provider} · {cliHealth.my_key.masked}</span> {t('own_key_suffix')}
                  </span>
                  <button onClick={removeOwnKey} disabled={keyBusy} className="text-sima-textDim hover:text-red-400 disabled:opacity-50">{t('unlink')}</button>
                </div>
              ) : cliHealth?.my_key?.mode === 'subscription' ? (
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sima-textMuted">{t('own_sub_login_hint')}</span>
                    <button onClick={removeOwnKey} disabled={keyBusy} className="text-sima-textDim hover:text-red-400 disabled:opacity-50">{t('unlink')}</button>
                  </div>
                  {Object.entries(cliHealth.my_key.agents || {}).filter(([a]) => a !== 'cursor').map(([agent, info]) => (
                    <div key={agent} className="rounded bg-sima-bg border border-sima-border p-1.5">
                      <div className="flex items-center gap-1.5">
                        <span className={info.connected ? 'text-green-400' : 'text-amber-400'}>{info.connected ? '✓' : '○'}</span>
                        <span className="text-sima-text">{agent}</span>
                        <span className="text-sima-textDim">{info.connected ? t('sub_connected') : t('sub_login_once')}</span>
                      </div>
                      {!info.connected && info.command && (
                        <code className="block mt-1 text-[10px] text-sima-textMuted font-mono break-all select-all">{info.command}</code>
                      )}
                    </div>
                  ))}
                  <p className="text-[10px] text-sima-textDim">{t('sub_login_note')}</p>
                </div>
              ) : cliHealth?.my_key?.mode === 'integration' ? (
                <div className="space-y-1">
                  <span className="text-sima-textMuted">
                    {t('integration_key_prefix')} <span className="text-sima-text font-mono">{cliHealth.my_key.provider} · {cliHealth.my_key.masked}</span> {t('integration_key_suffix')}
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    <button onClick={() => setByoChoice('api')} className="px-2 py-1 rounded border border-sima-border hover:border-sima-accent text-sima-text text-[10px]">{t('set_other_key')}</button>
                    <button onClick={useOwnSubscription} disabled={keyBusy} className="px-2 py-1 rounded border border-sima-border hover:border-sima-accent text-sima-text text-[10px] disabled:opacity-50">{t('use_own_subscription')}</button>
                  </div>
                </div>
              ) : byoChoice === 'api' ? (
                <div className="space-y-1.5">
                  <p className="text-sima-textMuted">{t('paste_api_key_hint')}</p>
                  <div className="flex items-center gap-1.5">
                    <select value={keyProvider} onChange={(e) => setKeyProvider(e.target.value as any)}
                      className="px-1.5 py-1 rounded bg-sima-bg border border-sima-border text-sima-text">
                      <option value="anthropic">Anthropic (Claude)</option>
                      <option value="openai">OpenAI (Codex)</option>
                    </select>
                    <input type="password" value={keyInput} onChange={(e) => setKeyInput(e.target.value)}
                      placeholder="sk-…" className="flex-1 px-2 py-1 rounded bg-sima-bg border border-sima-border text-sima-text font-mono" />
                    <button onClick={saveOwnKey} disabled={keyBusy || !keyInput.trim()}
                      className="px-2 py-1 rounded bg-sima-primary/80 hover:bg-sima-primary text-white disabled:opacity-50">{t('save')}</button>
                  </div>
                  <button onClick={() => setByoChoice(null)} className="text-[10px] text-sima-textDim hover:text-sima-textMuted">{t('back')}</button>
                </div>
              ) : (
                <div className="space-y-1">
                  <p className="text-sima-textMuted">{t('use_own_account_hint')}</p>
                  <div className="flex flex-wrap gap-1.5">
                    <button onClick={() => setByoChoice('api')} className="px-2 py-1 rounded border border-sima-border hover:border-sima-accent text-sima-text">{t('own_key_api')}</button>
                    <button onClick={useOwnSubscription} disabled={keyBusy} className="px-2 py-1 rounded border border-sima-border hover:border-sima-accent text-sima-text disabled:opacity-50">{t('own_subscription_cli')}</button>
                  </div>
                  <p className="text-[10px] text-sima-textDim">{t('byo_key_note')}</p>
                </div>
              )}
            </div>
          )}
          {!kanonHandoff && (
            <div className="flex flex-col gap-1 mt-1.5 text-[11px] text-sima-textMuted">
              {cliHealth?.agents && !cliHealth.agents[kanonAgent]?.installed && (
                <p className="text-[11px] text-amber-400">
                  {t('agent_not_installed_warning')}
                </p>
              )}
              {cliHealth && cliHealth.exec_enabled === false && (
                <p className="text-[11px] text-sima-textDim">
                  {t('exec_disabled_note')}
                </p>
              )}
              <label className="inline-flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" checked={kanonRunCmds} onChange={(e) => setKanonRunCmds(e.target.checked)} />
                {t('run_tests_checkbox')}
              </label>
              <label className="inline-flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" checked={kanonJudge} onChange={(e) => setKanonJudge(e.target.checked)} />
                {t('judge_checkbox')}
              </label>
            </div>
          )}
          {kanonHandoff && (
            <div className="p-3 rounded-lg border border-violet-400/30 bg-violet-400/5 space-y-2">
              <p className="text-[11px] text-sima-text">{t('kanon_pending')}</p>
              {kanonHandoff.projectFolder && (
                <p className="text-[10px] text-sima-textDim font-mono">{kanonHandoff.projectFolder}</p>
              )}
              {kanonHandoff.command && (
                <p className="text-[10px] text-sima-textMuted font-mono break-all">
                  {t('kanon_command')}: {kanonHandoff.command}
                </p>
              )}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => kanonHandoffAction('confirm')}
                  disabled={kanonBusy !== null}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-green-400/40 bg-green-400/10 hover:bg-green-400/20 text-[11px] font-medium text-green-400 transition-all"
                >
                  {kanonBusy === 'confirm' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                  {t('kanon_confirm')}
                </button>
                <button
                  onClick={() => kanonHandoffAction('reject')}
                  disabled={kanonBusy !== null}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-red-400/30 bg-red-400/5 hover:bg-red-400/10 text-[11px] text-red-400 transition-all"
                >
                  <X className="w-3.5 h-3.5" />
                  {t('kanon_reject')}
                </button>
              </div>
            </div>
          )}
          {kanonMsg && (
            <p className={cn('text-[11px]', kanonMsg.ok ? 'text-green-400' : 'text-amber-400')}>{kanonMsg.text}</p>
          )}
        </div>

        <div className="border-t border-sima-border pt-3">
          <div className="flex items-center gap-2 mb-2">
            <ExternalLink className="w-3.5 h-3.5 text-sima-textDim" />
            <span className="text-xs font-semibold text-sima-textDim">{t('manual_export_heading')}</span>
          </div>
        </div>

        {/* Export targets */}
        <div className="grid grid-cols-2 gap-2">
          {TARGETS.map((target) => {
            const Icon = target.icon
            const isLoading = loading === target.id
            const isSelected = result?.type === target.id
            return (
              <button
                key={target.id}
                onClick={() => handleExport(target.id)}
                disabled={loading !== null}
                className={cn(
                  'flex flex-col items-start gap-1.5 p-3 rounded-lg border transition-all text-left',
                  isSelected
                    ? 'border-sima-primary bg-sima-primary/5'
                    : 'border-sima-border hover:border-sima-primary/50 bg-sima-bg',
                  loading !== null && !isLoading && 'opacity-50'
                )}
              >
                <div className="flex items-center gap-2 w-full">
                  {isLoading ? (
                    <Loader2 className="w-4 h-4 animate-spin text-sima-primary" />
                  ) : (
                    <Icon className="w-4 h-4 text-sima-primary" />
                  )}
                  <span className="text-xs font-medium text-sima-text">{target.label}</span>
                </div>
                <p className="text-[10px] text-sima-textDim leading-relaxed">{target.description}</p>
              </button>
            )
          })}
        </div>

        {/* Mermaid export */}
        {allBlocks.length > 0 && (
          <div className="border border-sima-border rounded-lg overflow-hidden">
            <button
              onClick={() => setShowMermaid(!showMermaid)}
              className="w-full flex items-center gap-2 px-3 py-2.5 hover:bg-sima-surfaceLight transition-colors"
            >
              <GitBranch className="w-4 h-4 text-emerald-400" />
              <span className="text-xs font-medium text-sima-text flex-1 text-left">{t('mermaid_diagram')}</span>
              <span className="text-[10px] text-sima-textDim">{t('blocks_connections_count', { blocks: allBlocks.length, connections: allConnections.length })}</span>
              {showMermaid ? <ChevronDown className="w-3.5 h-3.5 text-sima-textDim" /> : <ChevronRight className="w-3.5 h-3.5 text-sima-textDim" />}
            </button>
            {showMermaid && (
              <div className="border-t border-sima-border p-3 space-y-2">
                <div className="flex flex-wrap gap-1.5">
                  {/* Главная кнопка — рендер */}
                  <button
                    onClick={renderMermaid}
                    disabled={renderingSvg}
                    className="flex items-center gap-1 px-2.5 py-1.5 text-[10px] bg-emerald-400/20 text-emerald-400 border border-emerald-400/30 rounded-lg hover:bg-emerald-400/30 transition-colors font-medium"
                  >
                    {renderingSvg ? <Loader2 className="w-3 h-3 animate-spin" /> : <Eye className="w-3 h-3" />}
                    {t('show_diagram')}
                  </button>
                  <button
                    onClick={() => copyToClipboard(mermaidCode, 'mermaid')}
                    className="flex items-center gap-1 px-2 py-1 text-[10px] bg-sima-bg border border-sima-border rounded hover:bg-sima-surfaceLight transition-colors"
                  >
                    {copied === 'mermaid' ? <Check className="w-3 h-3 text-green-400" /> : <Copy className="w-3 h-3" />}
                    {t('code_label')}
                  </button>
                  <button
                    onClick={() => downloadFile(`${project!.name}_mermaid.md`, `\`\`\`mermaid\n${mermaidCode}\n\`\`\``)}
                    className="flex items-center gap-1 px-2 py-1 text-[10px] bg-sima-bg border border-sima-border rounded hover:bg-sima-surfaceLight transition-colors"
                  >
                    <Download className="w-3 h-3" />
                    .md
                  </button>
                  <button
                    onClick={() => downloadFile(`${project!.name}_diagram.mmd`, mermaidCode)}
                    className="flex items-center gap-1 px-2 py-1 text-[10px] bg-sima-bg border border-sima-border rounded hover:bg-sima-surfaceLight transition-colors"
                  >
                    <Download className="w-3 h-3" />
                    .mmd
                  </button>
                  {renderedSvg && (
                    <>
                      <button
                        onClick={downloadPng}
                        className="flex items-center gap-1 px-2 py-1 text-[10px] bg-blue-400/10 text-blue-400 border border-blue-400/20 rounded hover:bg-blue-400/20 transition-colors"
                      >
                        <Image className="w-3 h-3" />
                        PNG
                      </button>
                      <button
                        onClick={downloadSvg}
                        className="flex items-center gap-1 px-2 py-1 text-[10px] bg-purple-400/10 text-purple-400 border border-purple-400/20 rounded hover:bg-purple-400/20 transition-colors"
                      >
                        <Image className="w-3 h-3" />
                        SVG
                      </button>
                    </>
                  )}
                </div>

                {/* Rendered diagram */}
                {showRendered && renderedSvg && (
                  <div className="relative">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[10px] text-emerald-400 font-medium">{t('visualization_label')}</span>
                      <button
                        onClick={() => setFullscreen(true)}
                        className="flex items-center gap-1 text-[10px] text-sima-textDim hover:text-sima-text transition-colors"
                      >
                        <Maximize2 className="w-3 h-3" />
                        {t('fullscreen')}
                      </button>
                    </div>
                    <div
                      ref={mermaidContainerRef}
                      className="bg-sima-bg rounded-lg border border-sima-border p-4 max-h-[300px] overflow-auto"
                      dangerouslySetInnerHTML={{ __html: renderedSvg }}
                    />
                  </div>
                )}

                {/* Исходный код (скрыт когда показана диаграмма) */}
                {!showRendered && (
                  <div className="bg-sima-bg rounded-lg p-3 max-h-[200px] overflow-y-auto">
                    <pre className="text-[10px] text-emerald-400/80 whitespace-pre-wrap font-mono leading-relaxed">{mermaidCode}</pre>
                  </div>
                )}

                {showRendered && (
                  <button
                    onClick={() => setShowRendered(false)}
                    className="text-[10px] text-sima-textDim hover:text-sima-text transition-colors"
                  >
                    {t('show_source_code')}
                  </button>
                )}

                <p className="text-[9px] text-sima-textDim">
                  {t('paste_into_hint')}{' '}
                  <a href="https://mermaid.live" target="_blank" rel="noopener noreferrer" className="text-emerald-400 hover:underline">mermaid.live</a>
                  {' '}{t('for_visualization')}
                </p>
              </div>
            )}
          </div>
        )}

        {/* Results */}
        {result && (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h4 className="text-xs font-semibold text-sima-text">
                {TARGETS.find(target => target.id === result.type)?.label}{t('result_suffix')}
              </h4>
              <div className="flex gap-1.5">
                {result.files && (
                  <>
                    <button
                      onClick={() => copyAllFiles(result.files!)}
                      className="flex items-center gap-1 px-2 py-1 text-[10px] bg-sima-bg border border-sima-border rounded hover:bg-sima-surfaceLight transition-colors"
                    >
                      {copied === 'all-files' ? (
                        <Check className="w-3 h-3 text-green-400" />
                      ) : (
                        <Copy className="w-3 h-3" />
                      )}
                      {t('copy_all')}
                    </button>
                    <button
                      onClick={() => downloadAllFiles(result.files!)}
                      className="flex items-center gap-1 px-2 py-1 text-[10px] bg-sima-primary/10 text-sima-primary border border-sima-primary/20 rounded hover:bg-sima-primary/20 transition-colors"
                    >
                      <Download className="w-3 h-3" />
                      {t('download')}
                    </button>
                  </>
                )}
                {result.prompt && (
                  <button
                    onClick={() => copyToClipboard(result.prompt!, 'prompt')}
                    className="flex items-center gap-1 px-2 py-1 text-[10px] bg-sima-primary/10 text-sima-primary border border-sima-primary/20 rounded hover:bg-sima-primary/20 transition-colors"
                  >
                    {copied === 'prompt' ? (
                      <Check className="w-3 h-3 text-green-400" />
                    ) : (
                      <Copy className="w-3 h-3" />
                    )}
                    {t('copy_prompt')}
                  </button>
                )}
              </div>
            </div>

            {/* Files list */}
            {result.files && (
              <div className="space-y-1.5">
                {Object.entries(result.files).map(([filename, content]) => {
                  const isExpanded = expandedFiles.has(filename)
                  return (
                    <div key={filename} className="border border-sima-border rounded-lg overflow-hidden">
                      <button
                        onClick={() => toggleFile(filename)}
                        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-sima-surfaceLight transition-colors"
                      >
                        {isExpanded ? (
                          <ChevronDown className="w-3 h-3 text-sima-textDim" />
                        ) : (
                          <ChevronRight className="w-3 h-3 text-sima-textDim" />
                        )}
                        <FolderTree className="w-3 h-3 text-sima-accent" />
                        <span className="text-xs font-mono text-sima-text">{filename}</span>
                        <span className="text-[9px] text-sima-textDim ml-auto">
                          {t('lines_count', { count: content.split('\n').length })}
                        </span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            copyToClipboard(content, filename)
                          }}
                          className="p-1 hover:bg-sima-bg rounded"
                          title={t("copy_title")}
                        >
                          {copied === filename ? (
                            <Check className="w-3 h-3 text-green-400" />
                          ) : (
                            <Copy className="w-3 h-3 text-sima-textDim" />
                          )}
                        </button>
                      </button>
                      {isExpanded && (
                        <div className="border-t border-sima-border bg-sima-bg px-3 py-2 max-h-[200px] overflow-y-auto">
                          <pre className="text-[10px] text-sima-textMuted whitespace-pre-wrap font-mono leading-relaxed">
                            {content}
                          </pre>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {/* Prompt view */}
            {result.prompt && (
              <div className="border border-sima-border rounded-lg overflow-hidden">
                <div className="bg-sima-bg px-3 py-2 max-h-[300px] overflow-y-auto">
                  <pre className="text-[10px] text-sima-textMuted whitespace-pre-wrap font-mono leading-relaxed">
                    {result.prompt}
                  </pre>
                </div>
              </div>
            )}

            {/* Cursor-specific instructions */}
            {result.type === 'cursor' && (
              <div className="bg-cyan-500/5 border border-cyan-500/20 rounded-lg p-3">
                <p className="text-[11px] text-cyan-400 font-medium mb-1">{t('how_to_use_heading')}</p>
                <ol className="text-[10px] text-sima-textMuted space-y-1 list-decimal list-inside">
                  <li>{t("cursor_steps_1")}</li>
                  <li>{t("cursor_steps_2")}</li>
                  <li>{t("cursor_steps_3_prefix")} <code className="bg-sima-bg px-1 rounded">cursor .</code></li>
                  <li>{t("cursor_steps_4")}</li>
                </ol>
              </div>
            )}

            {result.type === 'github' && (
              <div className="bg-purple-500/5 border border-purple-500/20 rounded-lg p-3">
                <p className="text-[11px] text-purple-400 font-medium mb-1">{t('how_to_use_heading')}</p>
                <ol className="text-[10px] text-sima-textMuted space-y-1 list-decimal list-inside">
                  <li>{t("full_steps_1")}</li>
                  <li>{t("full_steps_2_prefix")} <code className="bg-sima-bg px-1 rounded">gh repo create {result.repoName} --public</code></li>
                  <li>{t("full_steps_3_prefix")} <code className="bg-sima-bg px-1 rounded">git init ; git add . ; git commit -m &quot;{t("full_steps_init_msg")}&quot;</code></li>
                  <li>{t("full_steps_4_prefix")} <code className="bg-sima-bg px-1 rounded">git push -u origin main</code></li>
                </ol>
              </div>
            )}

            {result.type === 'lovable' && (
              <div className="bg-pink-500/5 border border-pink-500/20 rounded-lg p-3">
                <p className="text-[11px] text-pink-400 font-medium mb-1">{t('how_to_use_heading')}</p>
                <ol className="text-[10px] text-sima-textMuted space-y-1 list-decimal list-inside">
                  <li>{t("lovable_steps_1")}</li>
                  <li>{t("lovable_steps_2_prefix")} <a href="https://lovable.dev" target="_blank" rel="noopener" className="text-pink-400 underline">lovable.dev</a></li>
                  <li>{t("lovable_steps_3")}</li>
                  <li>{t("lovable_steps_4")}</li>
                </ol>
              </div>
            )}

            {result.type === 'claude-code' && (
              <div className="bg-orange-500/5 border border-orange-500/20 rounded-lg p-3">
                <p className="text-[11px] text-orange-400 font-medium mb-1">{t('how_to_use_heading')}</p>
                <ol className="text-[10px] text-sima-textMuted space-y-1 list-decimal list-inside">
                  <li>{t("claude_steps_1")}</li>
                  <li>{t("claude_steps_2_prefix")} <code className="bg-sima-bg px-1 rounded">claude</code></li>
                  <li>{t("claude_steps_3")}</li>
                  <li>{t("claude_steps_4")}</li>
                </ol>
              </div>
            )}
          </div>
        )}
        </>)}
      </div>

      {/* Fullscreen Mermaid Modal */}
      {fullscreen && renderedSvg && (
        <div className="fixed inset-0 z-[100] bg-sima-bg/95 backdrop-blur-sm flex flex-col">
          <div className="flex items-center justify-between px-6 py-3 border-b border-sima-border bg-sima-surface/50">
            <div className="flex items-center gap-3">
              <GitBranch className="w-5 h-5 text-emerald-400" />
              <span className="text-sm font-semibold text-sima-text">{project?.name} — Mermaid</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={downloadPng}
                className="flex items-center gap-1 px-3 py-1.5 text-xs bg-blue-400/10 text-blue-400 border border-blue-400/20 rounded-lg hover:bg-blue-400/20 transition-colors"
              >
                <Image className="w-3.5 h-3.5" />
                PNG
              </button>
              <button
                onClick={downloadSvg}
                className="flex items-center gap-1 px-3 py-1.5 text-xs bg-purple-400/10 text-purple-400 border border-purple-400/20 rounded-lg hover:bg-purple-400/20 transition-colors"
              >
                <Image className="w-3.5 h-3.5" />
                SVG
              </button>
              <button
                onClick={() => setFullscreen(false)}
                className="p-2 hover:bg-sima-surfaceLight rounded-lg transition-colors text-sima-textMuted"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>
          <div
            className="flex-1 overflow-auto p-8 flex items-center justify-center"
            dangerouslySetInnerHTML={{ __html: renderedSvg }}
          />
        </div>
      )}
    </div>
  )
}
