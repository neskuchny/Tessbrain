'use client'

/**
 * TzDocumentView — слой «ТЗ» как бумажный документ (эталон Sima Remix,
 * экран 4): центрированная «бумага», шапка «собрано из N источников и
 * M блоков», секции по `## `-заголовкам с чипами источников (честная
 * трассировка `источники: s1, s3` из генератора), оглавление справа.
 *
 * Инструменты генерации (выбор встреч/документов, промо, решения) живут
 * в нижней панели «Нарратив» — сюда ведут кнопки, ничего не дублируем.
 */
import { useMemo } from 'react'
import { useTranslations } from 'next-intl'
import ReactMarkdown from 'react-markdown'
import { FileText, RefreshCw, Wrench } from 'lucide-react'
import { useSimaStore } from '@/sima/lib/store'

interface TzSection { n: number; title: string; body: string; sources: string[]; anchor: string }
interface SourceInfo { id: string; name: string; kind?: string }

const REGISTRY_RE = /<!--\s*sima:sources\s*(\[[\s\S]*?\])\s*-->/
const SOURCES_LINE_RE = /(?:^|\n)\s*`?источники:\s*([sS][\d,\ssS]*?)`?\s*$/

function parseTz(md: string): { sections: TzSection[]; registry: SourceInfo[]; intro: string } {
  let registry: SourceInfo[] = []
  const rm = md.match(REGISTRY_RE)
  if (rm) {
    try { registry = JSON.parse(rm[1]) } catch { /* реестра нет — чипы без имён */ }
    md = md.replace(REGISTRY_RE, '')
  }
  const lines = md.split('\n')
  const sections: TzSection[] = []
  let intro: string[] = []
  let cur: { title: string; body: string[] } | null = null
  const flush = () => {
    if (!cur) return
    let body = cur.body.join('\n').trim()
    let sources: string[] = []
    const sm = body.match(SOURCES_LINE_RE)
    if (sm) {
      sources = sm[1].split(',').map(x => x.trim().toLowerCase())
        .filter(x => /^s\d+$/.test(x))
      body = body.replace(SOURCES_LINE_RE, '').trim()
    }
    const n = sections.length + 1
    sections.push({
      n, title: cur.title, body, sources,
      anchor: `tz-sec-${n}`,
    })
    cur = null
  }
  for (const line of lines) {
    const h = line.match(/^##\s+(?!#)(.+)$/)
    if (h) {
      flush()
      cur = { title: h[1].replace(/^\d+[.)]\s*/, '').trim(), body: [] }
    } else if (cur) {
      cur.body.push(line)
    } else if (!line.startsWith('# ')) {
      intro.push(line)
    }
  }
  flush()
  return { sections, registry, intro: intro.join('\n').trim() }
}

export default function TzDocumentView() {
  const t = useTranslations('sima_tzdoc')
  const project = useSimaStore(s => s.project)
  const blocks = useSimaStore(s => s.blocks)
  const setBottomPanelTab = useSimaStore(s => s.setBottomPanelTab)
  const setBottomPanelOpen = useSimaStore(s => s.setBottomPanelOpen)

  const tz = project?.generatedTZ || ''
  const { sections, registry, intro } = useMemo(() => parseTz(tz), [tz])
  const regById = useMemo(
    () => Object.fromEntries(registry.map(r => [r.id.toLowerCase(), r])),
    [registry])

  const openTools = () => { setBottomPanelTab('narrative'); setBottomPanelOpen(true) }

  if (!project) return null

  if (!tz.trim()) {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="text-center max-w-md">
          <FileText className="w-12 h-12 mx-auto mb-3 text-sima-textDim opacity-40" />
          <p className="text-[13px] text-sima-textMuted mb-4">
            {blocks.length === 0 ? t('empty_no_blocks') : t('empty_hint')}
          </p>
          <button onClick={openTools}
            className="px-4 py-2 rounded-lg bg-sima-primary hover:bg-sima-primaryHover text-white text-sm font-medium inline-flex items-center gap-2 transition-colors">
            <Wrench className="w-4 h-4" /> {t('open_tools')}
          </button>
        </div>
      </div>
    )
  }

  const chip = (sid: string) => (
    <span key={sid}
      title={regById[sid]?.name || t('source_unknown')}
      className="inline-flex items-center px-1.5 py-0.5 rounded bg-sima-primary/10 text-sima-primary text-[10px] font-mono cursor-default">
      {sid}
    </span>
  )

  return (
    <div className="h-full overflow-y-auto bg-sima-bg">
      <div className="max-w-6xl mx-auto flex gap-6 p-6 items-start">
        {/* Бумага */}
        <article className="flex-1 min-w-0 rounded-2xl border border-sima-border bg-sima-surface shadow-sm px-10 py-8">
          <div className="flex items-center gap-2 flex-wrap mb-4">
            <span className="px-2 py-0.5 rounded bg-sima-primary/10 text-sima-primary text-[10px] font-semibold uppercase">
              {t('kicker')}
            </span>
            <span className="text-[11px] text-sima-textDim">
              {t('assembled_from', {
                sources: registry.length, blocks: blocks.length,
              })}
            </span>
            <span className="flex-1" />
            <button onClick={openTools}
              title={t('regen_hint')}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-sima-border text-[11px] text-sima-textMuted hover:text-sima-text hover:bg-sima-surfaceLight transition-colors">
              <RefreshCw className="w-3 h-3" /> {t('regen_button')}
            </button>
          </div>

          <h1 className="text-2xl font-semibold text-sima-text mb-2">
            {t('title_prefix')} · {project.name}
          </h1>

          {intro && (
            <div className="prose-sima text-[13px] text-sima-textMuted leading-relaxed mb-6">
              <ReactMarkdown>{intro}</ReactMarkdown>
            </div>
          )}

          <div className="space-y-8">
            {sections.map(sec => (
              <section key={sec.anchor} id={sec.anchor}>
                <div className="flex items-baseline gap-2.5 mb-2">
                  <span className="text-[11px] font-mono text-sima-textDim">{sec.n}</span>
                  <h2 className="text-lg font-semibold text-sima-text">{sec.title}</h2>
                </div>
                <div className="prose-sima text-[13px] text-sima-text/90 leading-relaxed [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_code]:text-[12px] [&_pre]:overflow-x-auto [&_h3]:font-semibold [&_h3]:mt-3">
                  <ReactMarkdown>{sec.body}</ReactMarkdown>
                </div>
                {sec.sources.length > 0 && (
                  <div className="flex items-center gap-1.5 mt-2">
                    <span className="text-[10px] text-sima-textDim">{t('sources_label')}</span>
                    {sec.sources.map(chip)}
                  </div>
                )}
              </section>
            ))}
          </div>
        </article>

        {/* Мета + оглавление */}
        <aside className="hidden lg:block w-64 shrink-0 sticky top-6 space-y-4">
          <div className="rounded-xl border border-sima-border bg-sima-surface p-4">
            <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-2">
              {t('meta_title')}
            </div>
            <p className="text-[11px] text-sima-textMuted leading-relaxed">
              {t('meta_text')}
            </p>
          </div>
          {sections.length > 0 && (
            <nav className="rounded-xl border border-sima-border bg-sima-surface p-4">
              <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-2">
                {t('toc_title')}
              </div>
              <ol className="space-y-1.5">
                {sections.map(sec => (
                  <li key={sec.anchor}>
                    <a href={`#${sec.anchor}`}
                      className="flex items-baseline gap-2 text-[12px] text-sima-textMuted hover:text-sima-primary transition-colors">
                      <span className="font-mono text-[10px] text-sima-textDim">{sec.n}</span>
                      <span className="leading-snug">{sec.title}</span>
                    </a>
                  </li>
                ))}
              </ol>
            </nav>
          )}
          {registry.length > 0 && (
            <div className="rounded-xl border border-sima-border bg-sima-surface p-4">
              <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-2">
                {t('registry_title')}
              </div>
              <ul className="space-y-1">
                {registry.map(r => (
                  <li key={r.id} className="flex items-start gap-1.5 text-[11px]">
                    <span className="font-mono text-sima-primary shrink-0">{r.id}</span>
                    <span className="text-sima-textMuted leading-snug truncate">{r.name}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
