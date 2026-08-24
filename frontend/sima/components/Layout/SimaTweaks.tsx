'use client'

/**
 * SimaTweaks — плавающая панель настроек внешнего вида (Sima Remix).
 *
 * Акцент (4 цвета → CSS-переменная --sima-primary) + тема (тёмная/светлая).
 * Persist в localStorage; применяется к document.documentElement. Плотность
 * прототипа пока не переносим: SIMA-размеры rem-based (Tailwind), честный
 * density потребует правки многих компонентов — отдельная задача, не no-op.
 */
import { useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { X } from 'lucide-react'

export type SimaAccent = 'indigo' | 'brown' | 'olive' | 'amber'
export type SimaTheme = 'dark' | 'light'

// RGB-триплеты в формате --sima-primary («R G B») + hover (чуть темнее).
const ACCENTS: Record<SimaAccent, { primary: string; hover: string; swatch: string }> = {
  indigo: { primary: '99 102 241', hover: '129 140 248', swatch: '#6366F1' },
  brown:  { primary: '166 103 58', hover: '138 85 48',   swatch: '#A6673A' },
  olive:  { primary: '107 122 61', hover: '90 103 50',   swatch: '#6B7A3D' },
  amber:  { primary: '180 122 31', hover: '150 100 24',  swatch: '#B47A1F' },
}

export function applyAccent(accent: SimaAccent) {
  const a = ACCENTS[accent] || ACCENTS.indigo
  document.documentElement.style.setProperty('--sima-primary', a.primary)
  document.documentElement.style.setProperty('--sima-primary-hover', a.hover)
}

export function readAccent(): SimaAccent {
  if (typeof window === 'undefined') return 'indigo'
  const v = localStorage.getItem('sima_accent') as SimaAccent | null
  return v && v in ACCENTS ? v : 'indigo'
}

export default function SimaTweaks({
  open, onClose, accent, setAccent, theme, setTheme,
}: {
  open: boolean
  onClose: () => void
  accent: SimaAccent
  setAccent: (a: SimaAccent) => void
  theme: SimaTheme
  setTheme: (t: SimaTheme) => void
}) {
  const t = useTranslations('sima_tweaks')

  // Применяем акцент при монтировании/смене (тема живёт в Workspace).
  useEffect(() => { applyAccent(accent) }, [accent])

  if (!open) return null

  return (
    <div className="fixed right-4 bottom-4 z-[60] w-64 rounded-2xl border border-sima-border bg-sima-surface/95 backdrop-blur shadow-2xl">
      <div className="flex items-center justify-between px-3.5 py-2.5 border-b border-sima-border">
        <span className="text-xs font-semibold text-sima-text">{t('title')}</span>
        <button onClick={onClose} className="p-1 rounded hover:bg-sima-surfaceLight text-sima-textDim">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <div className="p-3.5 flex flex-col gap-3">
        {/* Акцент */}
        <div>
          <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-1.5">{t('accent')}</div>
          <div className="flex gap-2">
            {(Object.keys(ACCENTS) as SimaAccent[]).map((a) => (
              <button
                key={a}
                onClick={() => setAccent(a)}
                title={t(`accent_${a}`)}
                className={
                  'w-7 h-7 rounded-full border-2 transition-transform ' +
                  (accent === a ? 'border-sima-text scale-110' : 'border-transparent hover:scale-105')
                }
                style={{ backgroundColor: ACCENTS[a].swatch }}
              />
            ))}
          </div>
        </div>

        {/* Тема */}
        <div>
          <div className="text-[10px] uppercase tracking-wide text-sima-textDim mb-1.5">{t('theme')}</div>
          <div className="flex gap-1.5">
            {(['dark', 'light'] as SimaTheme[]).map((th) => (
              <button
                key={th}
                onClick={() => setTheme(th)}
                className={
                  'flex-1 px-2 py-1.5 rounded-lg text-xs border transition-colors ' +
                  (theme === th
                    ? 'border-sima-primary bg-sima-primary/15 text-sima-primary font-medium'
                    : 'border-sima-border text-sima-textDim hover:bg-sima-surfaceLight')
                }
              >
                {t(`theme_${th}`)}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
