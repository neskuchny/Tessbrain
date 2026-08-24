'use client'

import { useState, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { X, ChevronDown, ChevronRight, Copy, Check, Terminal, Boxes, Share2, Send } from 'lucide-react'

// Пошаговая справка «Как подключить»: исполнитель (Claude Code), MCP, шина
// данных, Telegram. Команды не переводятся (это код) — переводится только
// проза (t). Копирование команд в один клик. Полностью аддитивный компонент.

type Step = { textKey: string; code?: string }
type Section = { id: string; icon: React.ComponentType<{ className?: string }>; titleKey: string; steps: Step[]; noteKey?: string }

const SECTIONS: Section[] = [
  {
    id: 'claude',
    icon: Terminal,
    titleKey: 'sec_claude_title',
    steps: [
      { textKey: 'claude_s1', code: 'npm install -g @anthropic-ai/claude-code' },
      { textKey: 'claude_s2', code: 'claude' },
      { textKey: 'claude_s3' },
      { textKey: 'claude_s4', code: 'CLAUDE_CODE_CLI_PATH=C:\\path\\to\\claude.cmd\nTESSENT_HANDOFF_REPO_ROOT=E:\\projects' },
    ],
    noteKey: 'claude_note',
  },
  {
    id: 'mcp',
    icon: Boxes,
    titleKey: 'sec_mcp_title',
    steps: [
      { textKey: 'mcp_s1', code: 'claude mcp add tessent \\\n  -e TESSENT_API_URL=https://<host> \\\n  -e TESSENT_API_TOKEN=<токен> \\\n  -e TESSENT_USER_ID=<uid> \\\n  -- python3 <путь>/mcp_server.py' },
      { textKey: 'mcp_s2' },
      { textKey: 'mcp_s3', code: 'claude mcp add --transport http tessent \\\n  https://<host>/api/v1/mcp \\\n  -H "Authorization: Bearer <TESSENT_MCP_TOKEN>"' },
    ],
  },
  {
    id: 'databus',
    icon: Share2,
    titleKey: 'sec_databus_title',
    steps: [
      { textKey: 'databus_s1', code: 'curl https://<host>/api/v1/data-bus/health' },
      { textKey: 'databus_s2', code: 'POST /api/v1/data-bus/admin/consumers?user_id=<uid>\n{ "name": "Инвестор", "policy_template": "investor_readonly" }' },
      { textKey: 'databus_s3', code: 'POST /api/v1/data-bus/admin/consumers/<id>/test' },
    ],
  },
  {
    id: 'telegram',
    icon: Send,
    titleKey: 'sec_telegram_title',
    steps: [
      { textKey: 'telegram_s1' },
      { textKey: 'telegram_s2' },
    ],
  },
]

function CodeBlock({ code }: { code: string }) {
  const t = useTranslations('setup_guide')
  const [copied, setCopied] = useState(false)
  const copy = useCallback(() => {
    try {
      navigator.clipboard?.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard недоступен — просто выделите вручную */ }
  }, [code])
  return (
    <div className="relative group mt-1.5">
      <pre className="text-[11px] leading-relaxed text-emerald-200/90 bg-brain-950/70 border border-brain-700/60 rounded p-2 pr-8 overflow-x-auto whitespace-pre font-mono">{code}</pre>
      <button
        onClick={copy}
        title={t('copy')}
        className="absolute top-1.5 right-1.5 p-1 rounded text-brain-400 hover:text-white hover:bg-brain-800/70"
      >
        {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
      </button>
    </div>
  )
}

export default function SetupGuide({ initialSection = 'claude', onClose }: { initialSection?: string; onClose: () => void }) {
  const t = useTranslations('setup_guide')
  const [open, setOpen] = useState<Record<string, boolean>>({ [initialSection]: true })
  const toggle = (id: string) => setOpen((s) => ({ ...s, [id]: !s[id] }))

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="bg-brain-900 border border-brain-700/60 rounded-xl w-full max-w-2xl max-h-[88vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-brain-700/50 sticky top-0 bg-brain-900 z-10">
          <div>
            <h3 className="text-white font-semibold">{t('title')}</h3>
            <p className="text-[11px] text-brain-500">{t('subtitle')}</p>
          </div>
          <button onClick={onClose} className="p-1 text-brain-400 hover:text-white rounded"><X className="w-5 h-5" /></button>
        </div>

        <div className="p-3 space-y-2">
          {SECTIONS.map((sec) => {
            const Icon = sec.icon
            const isOpen = !!open[sec.id]
            return (
              <div key={sec.id} className="rounded-lg border border-brain-700/50 overflow-hidden">
                <button
                  onClick={() => toggle(sec.id)}
                  className="w-full flex items-center gap-2 px-3 py-2.5 text-left hover:bg-brain-800/40"
                >
                  <Icon className="w-4 h-4 text-brain-300 flex-none" />
                  <span className="flex-1 text-sm font-medium text-brain-100">{t(sec.titleKey)}</span>
                  {isOpen ? <ChevronDown className="w-4 h-4 text-brain-400" /> : <ChevronRight className="w-4 h-4 text-brain-400" />}
                </button>
                {isOpen && (
                  <div className="px-3 pb-3 pt-1 space-y-2.5 border-t border-brain-700/40">
                    {sec.steps.map((step, i) => (
                      <div key={i} className="flex gap-2">
                        <span className="flex-none w-5 h-5 rounded-full bg-brain-700/60 text-brain-200 text-[11px] flex items-center justify-center mt-0.5">{i + 1}</span>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-brain-200 leading-relaxed">{t(step.textKey)}</p>
                          {step.code && <CodeBlock code={step.code} />}
                        </div>
                      </div>
                    ))}
                    {sec.noteKey && (
                      <p className="text-[11px] text-amber-300/80 pl-7">{t(sec.noteKey)}</p>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
