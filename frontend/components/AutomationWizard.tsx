'use client'

/**
 * AutomationWizard — нативное создание автоматизации без техножаргона.
 *
 * Аудит показал: старая форма требовала знать action_type, cron,
 * interval_seconds, писать JSON руками. Здесь пользователь отвечает на
 * три человеческих вопроса: ЧТО делаем → КОГДА → КУДА, и видит итог
 * обычным предложением. Техника (cron/action_type/расписание) собирается
 * под капотом. Расширенная форма остаётся за «Тонкая настройка».
 *
 * Шаг «Куда» — выбор канала (Telegram / Почта / Slack), а не только TG.
 * Telegram: контакт из интеграций, ручной ввод (личный chat_id, @username
 * или ID группы «-100…») или ПУСТО = чат по умолчанию из интеграций.
 */
import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { authFetch } from '@/lib/authFetch'
import {
  X, ChevronRight, ChevronLeft, Check, AlertCircle, Loader2,
  CalendarClock, Send, FileText, Sparkles, Bell, ListChecks, Sun,
} from 'lucide-react'

interface Props {
  userId?: string
  onClose: () => void
  onCreated: () => void
  onAdvanced: () => void   // открыть старую форму «Тонкая настройка»
}

type Channel = 'telegram' | 'email' | 'slack'

type Translator = ReturnType<typeof useTranslations>

// Сценарий = понятная задача. Под капотом мапится в шаблон или простое
// действие бэка. `channels` — какие каналы доставки доступны (первый —
// по умолчанию). `templateId` — если это готовый workflow.
type Scenario = {
  key: string
  icon: React.ReactNode
  title: string
  what: string            // что произойдёт (превью результата)
  defaultSchedule: 'once' | 'daily' | 'weekdays' | 'weekly'
  fixedSchedule?: boolean // расписание жёсткое (напр. «после каждой встречи»)
  scheduleNote?: string
  needsMessage?: boolean  // произвольный текст (для напоминаний)
  channels?: Channel[]
  templateId?: string     // POST /from-template
  actionType?: string     // POST /automations (простое действие)
}

function buildScenarios(t: Translator): Scenario[] {
  return [
    {
      key: 'reminder',
      icon: <Bell className="w-5 h-5" />,
      title: t('scenario_reminder_title'),
      what: t('scenario_reminder_what'),
      defaultSchedule: 'once',
      needsMessage: true,
      channels: ['telegram', 'email', 'slack'],
      actionType: 'send_telegram',
    },
    {
      key: 'daily_digest',
      icon: <CalendarClock className="w-5 h-5" />,
      title: t('scenario_daily_digest_title'),
      what: t('scenario_daily_digest_what'),
      defaultSchedule: 'daily',
      channels: ['telegram', 'email', 'slack'],
      templateId: 'daily_meetings_digest',
    },
    {
      key: 'morning',
      icon: <Sun className="w-5 h-5" />,
      title: t('scenario_morning_title'),
      what: t('scenario_morning_what'),
      defaultSchedule: 'weekdays',
      channels: ['telegram', 'email', 'slack'],
      templateId: 'morning_briefing',
    },
    {
      key: 'weekly',
      icon: <ListChecks className="w-5 h-5" />,
      title: t('scenario_weekly_title'),
      what: t('scenario_weekly_what'),
      defaultSchedule: 'weekly',
      channels: ['telegram', 'email', 'slack'],
      templateId: 'weekly_meetings_summary',
    },
    {
      key: 'report_email',
      icon: <FileText className="w-5 h-5" />,
      title: t('scenario_report_email_title'),
      what: t('scenario_report_email_what'),
      defaultSchedule: 'once',
      fixedSchedule: true,
      scheduleNote: t('scenario_report_email_note'),
      channels: ['email'],
      templateId: 'meeting_report_email',
    },
    {
      key: 'agent',
      icon: <Sparkles className="w-5 h-5" />,
      title: t('scenario_agent_title'),
      what: t('scenario_agent_what'),
      defaultSchedule: 'daily',
      // особый: ведёт на панель агентных автоматизаций
    },
  ]
}

function buildChannelMeta(t: Translator): Record<Channel, { label: string; provider: string }> {
  return {
    telegram: { label: '📱 Telegram', provider: 'telegram' },
    email: { label: t('channel_email_label'), provider: 'gmail' },
    slack: { label: '💬 Slack', provider: 'slack' },
  }
}

function buildScheduleChips(t: Translator): { key: Scenario['defaultSchedule']; label: string; hint: string }[] {
  return [
    { key: 'once', label: t('schedule_once_label'), hint: t('schedule_once_hint') },
    { key: 'daily', label: t('schedule_daily_label'), hint: t('schedule_daily_hint') },
    { key: 'weekdays', label: t('schedule_weekdays_label'), hint: t('schedule_weekdays_hint') },
    { key: 'weekly', label: t('schedule_weekly_label'), hint: t('schedule_weekly_hint') },
  ]
}

function scheduleToPayload(sched: Scenario['defaultSchedule'], time: string, onceWhen: string) {
  // time = "HH:MM", onceWhen = живой текст («через 2 часа», «завтра в 9:00»)
  const [h, m] = (time || '09:00').split(':')
  const hh = String(parseInt(h || '9', 10))
  const mm = String(parseInt(m || '0', 10))
  switch (sched) {
    case 'daily':    return { schedule_type: 'recurring', cron_expression: `${mm} ${hh} * * *` }
    case 'weekdays': return { schedule_type: 'recurring', cron_expression: `${mm} ${hh} * * 1-5` }
    case 'weekly':   return { schedule_type: 'recurring', cron_expression: `${mm} ${hh} * * 1` }
    case 'once':
    default:         return { schedule_type: 'once', execute_at: onceWhen }
  }
}

function humanSchedule(t: Translator, sched: Scenario['defaultSchedule'], time: string, onceWhen: string) {
  switch (sched) {
    case 'daily':    return t('human_schedule_daily', { time })
    case 'weekdays': return t('human_schedule_weekdays', { time })
    case 'weekly':   return t('human_schedule_weekly', { time })
    case 'once':     return onceWhen ? `${onceWhen}` : t('human_schedule_once_unset')
  }
}

export default function AutomationWizard({ userId, onClose, onCreated, onAdvanced }: Props) {
  const t = useTranslations('automation_wizard')
  const scenarios = buildScenarios(t)
  const channelMeta = buildChannelMeta(t)
  const scheduleChips = buildScheduleChips(t)
  const [step, setStep] = useState(0)                       // 0 сценарий, 1 когда, 2 куда/текст, 3 итог
  const [scenario, setScenario] = useState<Scenario | null>(null)
  const [sched, setSched] = useState<Scenario['defaultSchedule']>('once')
  const [time, setTime] = useState('09:00')
  const [onceWhen, setOnceWhen] = useState('')
  const [message, setMessage] = useState('')
  const [channel, setChannel] = useState<Channel>('telegram')
  // контакты по провайдерам: telegram/gmail из интеграций (slack — руками)
  const [contactsByProvider, setContactsByProvider] = useState<Record<string, Array<{ id: string; label: string; value: string }>>>({})
  const [pickedContact, setPickedContact] = useState<string>('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const contacts = contactsByProvider[channelMeta[channel].provider] || []

  // контакты для всех каналов (0 LLM, из уже подключённых интеграций)
  useEffect(() => {
    if (!scenario?.channels || !userId) return
    let alive = true
    ;(async () => {
      try {
        const res = await authFetch(`/api/v1/automations/integrations-list?user_id=${userId}`)
        const data = await res.json()
        const byProvider: Record<string, Array<{ id: string; label: string; value: string }>> = {}
        const groups = data.integrations || {}
        // integrations может быть массивом или объектом по провайдерам
        const entries: Array<[string, any]> = Array.isArray(groups)
          ? groups.map((g: any) => [g.provider, g])
          : Object.entries(groups)
        for (const [provider, group] of entries) {
          const list: Array<{ id: string; label: string; value: string }> = []
          for (const c of (group?.contacts || [])) {
            list.push({ id: c.id, label: c.display_name || c.display || c.value || c.type, value: c.value })
          }
          byProvider[provider] = list
        }
        if (alive) setContactsByProvider(byProvider)
      } catch { /* пусто — покажем поле вручную */ }
    })()
    return () => { alive = false }
  }, [scenario, userId])

  const pickScenario = (s: Scenario) => {
    setScenario(s)
    setSched(s.defaultSchedule)
    setChannel(s.channels?.[0] || 'telegram')
    setPickedContact('')
    setError(null)
    if (s.key === 'agent') { onAdvanced(); return }   // особый: агентная панель
    setStep(s.fixedSchedule ? 2 : 1)
  }

  const pickChannel = (c: Channel) => {
    setChannel(c)
    setPickedContact('')   // получатель у каждого канала свой
  }

  const canNext = () => {
    if (step === 1 && sched === 'once' && !onceWhen.trim()) return false
    if (step === 2) {
      if (scenario?.needsMessage && !message.trim()) return false
      // Telegram можно оставить пустым (чат по умолчанию из интеграций);
      // почте и Slack получатель обязателен
      if (scenario?.channels && channel !== 'telegram' && !pickedContact.trim()) return false
    }
    return true
  }

  // Человеческое «куда» для итога
  const humanDestination = (): string => {
    if (!scenario?.channels) return ''
    const label = contacts.find(c => c.value === pickedContact)?.label || pickedContact
    if (channel === 'email') return ` → ${t('dest_email', { label })}`
    if (channel === 'slack') return ` → ${t('dest_slack', { label })}`
    return pickedContact ? ` → ${t('dest_telegram', { label })}` : ` → ${t('dest_telegram_default')}`
  }

  const handleCreate = async () => {
    if (!scenario) return
    setError(null); setCreating(true)
    try {
      const sp = scheduleToPayload(sched, time, onceWhen)
      let res: Response
      if (scenario.templateId) {
        // Получатель по каналу: chat_id / to_email / slack_channel
        const recipientParams: Record<string, any> = { channel }
        if (channel === 'telegram') recipientParams.chat_id = pickedContact
        if (channel === 'email') recipientParams.to_email = pickedContact
        if (channel === 'slack') recipientParams.slack_channel = pickedContact
        res = await authFetch('/api/v1/automations/from-template', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            template_id: scenario.templateId,
            user_id: userId || 'default_user',
            parameters: recipientParams,
            cron_expression: (sp as any).cron_expression,
            execute_at: (sp as any).execute_at,
          }),
        })
      } else {
        // простое действие (напоминание) — action_type по каналу
        let actionType = 'send_telegram'
        let actionData: Record<string, any> = { message, chat_id: pickedContact }
        if (channel === 'email') {
          actionType = 'send_email'
          actionData = { subject: t('reminder_email_subject'), body: message, to: pickedContact }
        } else if (channel === 'slack') {
          actionType = 'send_slack'
          actionData = { message, channel: pickedContact }
        }
        res = await authFetch('/api/v1/automations', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: message.slice(0, 50) || scenario.title,
            action_type: actionType,
            action_data: actionData,
            user_id: userId || 'default_user',
            ...sp,
          }),
        })
      }
      if (res.ok) { onCreated(); onClose() }
      else {
        const d = await res.json().catch(() => ({}))
        setError(d?.detail || d?.message || t('create_failed_http', { status: res.status }))
      }
    } catch { setError(t('network_error')) }
    finally { setCreating(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-brain-900 rounded-2xl border border-brain-700/50 w-full max-w-xl max-h-[90vh] overflow-y-auto">
        {/* Шапка с объяснением, что это вообще такое */}
        <div className="p-5 border-b border-brain-700/30">
          <div className="flex items-start justify-between">
            <div>
              <h3 className="text-lg font-semibold text-brain-100">{t('wizard_title')}</h3>
              <p className="text-sm text-brain-400 mt-1">
                {t('wizard_subtitle')}
              </p>
            </div>
            <button onClick={onClose} className="p-2 rounded-lg hover:bg-brain-800 text-brain-400"><X className="w-4 h-4" /></button>
          </div>
          {/* прогресс шагов */}
          {scenario && scenario.key !== 'agent' && (
            <div className="flex items-center gap-2 mt-4 text-xs">
              {[t('step_what'), t('step_when'), t('step_where'), t('step_done')].map((lbl, i) => (
                <div key={lbl} className={`flex items-center gap-1 ${i <= step ? 'text-blue-400' : 'text-brain-600'}`}>
                  <span className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] border ${i < step ? 'bg-blue-500 border-blue-500 text-white' : i === step ? 'border-blue-400' : 'border-brain-700'}`}>
                    {i < step ? <Check className="w-3 h-3" /> : i + 1}
                  </span>
                  {lbl}{i < 3 && <ChevronRight className="w-3 h-3 text-brain-700" />}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="p-5">
          {/* ШАГ 0 — сценарий */}
          {step === 0 && (
            <div>
              <p className="text-sm text-brain-300 mb-3">{t('what_happens_question')}</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {scenarios.map((s) => (
                  <button key={s.key} onClick={() => pickScenario(s)}
                    className="text-left p-3 rounded-xl border border-brain-700/40 bg-brain-800/40 hover:border-blue-500/50 hover:bg-brain-800/70 transition-all group">
                    <div className="flex items-center gap-2 text-brain-100 font-medium">
                      <span className="text-blue-400 group-hover:scale-110 transition-transform">{s.icon}</span>
                      {s.title}
                    </div>
                    <p className="text-xs text-brain-400 mt-1.5 leading-snug">{s.what}</p>
                  </button>
                ))}
              </div>
              <button onClick={onAdvanced} className="mt-4 text-xs text-brain-500 hover:text-brain-300 underline underline-offset-2">
                {t('advanced_link')}
              </button>
            </div>
          )}

          {/* ШАГ 1 — когда */}
          {step === 1 && scenario && (
            <div>
              <p className="text-sm text-brain-300 mb-3">{t('when_to_run')}</p>
              <div className="flex flex-wrap gap-2 mb-4">
                {scheduleChips.map((c) => (
                  <button key={c.key} onClick={() => setSched(c.key)}
                    title={c.hint}
                    className={`px-3 py-1.5 rounded-full text-sm border transition-all ${sched === c.key ? 'bg-blue-600 border-blue-600 text-white' : 'border-brain-700/50 text-brain-300 hover:border-blue-500/40'}`}>
                    {c.label}
                  </button>
                ))}
              </div>
              {sched === 'once' ? (
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">{t('once_when_label')}</label>
                  <input type="text" value={onceWhen} onChange={(e) => setOnceWhen(e.target.value)}
                    placeholder={t('once_when_placeholder')}
                    className="w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-blue-500/50" />
                  <p className="text-xs text-brain-500 mt-1">{t('once_when_hint')}</p>
                </div>
              ) : (
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">{t('time_label')}</label>
                  <input type="time" value={time} onChange={(e) => setTime(e.target.value)}
                    className="bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-brain-100 focus:outline-none focus:border-blue-500/50" />
                </div>
              )}
            </div>
          )}

          {/* ШАГ 2 — куда/что */}
          {step === 2 && scenario && (
            <div className="space-y-4">
              {scenario.scheduleNote && (
                <div className="flex items-start gap-2 text-xs text-brain-400 bg-brain-800/40 rounded-lg px-3 py-2">
                  <CalendarClock className="w-4 h-4 mt-0.5 shrink-0 text-blue-400" />{scenario.scheduleNote}
                </div>
              )}
              {scenario.needsMessage && (
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">{t('message_label')}</label>
                  <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={3}
                    placeholder={t('message_placeholder')}
                    className="w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-blue-500/50 resize-none" />
                </div>
              )}
              {scenario.channels && (
                <div>
                  <label className="block text-sm text-brain-300 mb-1.5">{t('destination_label')}</label>
                  {/* выбор канала — не только Telegram */}
                  {scenario.channels.length > 1 && (
                    <div className="flex flex-wrap gap-2 mb-3">
                      {scenario.channels.map((c) => (
                        <button key={c} onClick={() => pickChannel(c)}
                          className={`px-3 py-1.5 rounded-lg text-sm border transition-all ${channel === c ? 'bg-blue-600 border-blue-600 text-white' : 'border-brain-700/50 text-brain-300 hover:border-blue-500/40'}`}>
                          {channelMeta[c].label}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* сохранённые контакты выбранного канала */}
                  {contacts.length > 0 && (
                    <div className="flex flex-wrap gap-2 mb-2">
                      {contacts.map((c) => (
                        <button key={c.id} onClick={() => setPickedContact(pickedContact === c.value ? '' : c.value)}
                          className={`px-3 py-1.5 rounded-lg text-sm border transition-all ${pickedContact === c.value ? 'bg-blue-600 border-blue-600 text-white' : 'border-brain-700/50 text-brain-300 hover:border-blue-500/40'}`}>
                          {c.label}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* ручной ввод получателя (для TG — опционально) */}
                  <input type="text" value={pickedContact} onChange={(e) => setPickedContact(e.target.value)}
                    placeholder={
                      channel === 'email' ? 'you@example.com'
                      : channel === 'slack' ? t('slack_placeholder')
                      : t('telegram_placeholder')
                    }
                    className="w-full bg-brain-800 border border-brain-700 rounded-lg px-3 py-2 text-sm text-brain-100 placeholder:text-brain-600 focus:outline-none focus:border-blue-500/50" />

                  {channel === 'telegram' && (
                    <p className="text-xs text-brain-500 mt-1.5">
                      {t.rich('telegram_hint', {
                        group: (chunks) => <span className="text-brain-300">{chunks}</span>,
                      })}
                    </p>
                  )}
                  {channel === 'email' && (
                    <p className="text-xs text-brain-500 mt-1.5">
                      {t('email_hint')}
                    </p>
                  )}
                  {channel === 'slack' && (
                    <p className="text-xs text-brain-500 mt-1.5">
                      {t('slack_hint')}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* ШАГ 3 — итог понятным предложением */}
          {step === 3 && scenario && (
            <div>
              <p className="text-sm text-brain-300 mb-3">{t('review_prompt')}</p>
              <div className="p-4 rounded-xl bg-blue-950/30 border border-blue-800/30 text-brain-100 text-sm leading-relaxed">
                <span className="text-blue-300 font-medium">{scenario.title}.</span>{' '}
                {scenario.needsMessage && <>{t('review_message_text', { message })}{' '}</>}
                {humanSchedule(t, sched, time, onceWhen).charAt(0).toUpperCase() + humanSchedule(t, sched, time, onceWhen).slice(1)}
                {humanDestination()}.
              </div>
              <p className="text-xs text-brain-500 mt-2">
                {t('after_create_note')}
              </p>
            </div>
          )}

          {error && (
            <div className="mt-4 flex items-start gap-2 text-sm text-red-300 bg-red-900/30 border border-red-700/40 rounded-lg px-3 py-2">
              <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />{error}
            </div>
          )}
        </div>

        {/* Навигация */}
        {scenario && scenario.key !== 'agent' && (
          <div className="p-4 border-t border-brain-700/30 flex items-center justify-between">
            <button
              onClick={() => step === 0 ? onClose() : setStep(step - 1)}
              className="flex items-center gap-1 px-3 py-2 rounded-lg text-sm text-brain-400 hover:bg-brain-800/50">
              <ChevronLeft className="w-4 h-4" />{t('back')}
            </button>
            {step < 3 ? (
              <button
                onClick={() => canNext() && setStep(step === 1 && scenario.fixedSchedule ? 3 : step + 1)}
                disabled={!canNext()}
                className="flex items-center gap-1 px-4 py-2 rounded-lg text-sm bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-40 disabled:cursor-not-allowed">
                {t('next')} <ChevronRight className="w-4 h-4" />
              </button>
            ) : (
              <button onClick={handleCreate} disabled={creating}
                className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
                {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                {t('create_automation')}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
