'use client'

import { useState, useCallback, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { cn } from '@/sima/lib/utils'
import { simaFetch } from '@/sima/lib/api'
import {
  User,
  Users,
  Play,
  Loader2,
  Plus,
  Trash2,
  ChevronDown,
  ChevronRight,
  AlertTriangle,
  Zap,
  Download,
} from 'lucide-react'
import type { PersonaData, SimulationReport, SimulationStep, FrictionPoint } from '@/sima/types'

type ViewMode = 'personas' | 'report'

const buildLayerOptions = (t: (k: string) => string) => [
  { value: 'mvp', label: 'MVP' },
  { value: 'medium', label: t('filter_medium') },
  { value: 'ideal', label: t('filter_ideal') },
  { value: 'full', label: t('filter_full') },
]

const buildArchetypeLabels = (t: (k: string) => string): Record<string, string> => ({
  best_case: t('persona_best_case'),
  average: t('persona_average'),
  worst_case: t('persona_worst_case'),
  custom: t('persona_custom'),
})

const SEVERITY_COLORS: Record<string, string> = {
  minor: 'text-sima-textMuted',
  moderate: 'text-yellow-400',
  major: 'text-orange-400',
  wall: 'text-red-400',
}

export default function SimulatorPanel() {
  const t = useTranslations('sima_simulator')
  const LAYER_OPTIONS = buildLayerOptions(t)
  const ARCHETYPE_LABELS = buildArchetypeLabels(t)
  const {
    project,
    personas,
    setPersonas,
    addPersona,
    removePersona,
    simulationReport,
    setSimulationReport,
    simulationReports,
    setSimulationReports,
    simulatorLoading,
    setSimulatorLoading,
    selectedPersonaId,
    setSelectedPersonaId,
  } = useSimaStore()

  const [view, setView] = useState<ViewMode>('personas')
  const [personaText, setPersonaText] = useState('')
  const [selectedLayer, setSelectedLayer] = useState('mvp')
  const [expandedSteps, setExpandedSteps] = useState<Record<number, boolean>>({})
  const [generatingPersonas, setGeneratingPersonas] = useState(false)
  const [creatingPersona, setCreatingPersona] = useState(false)

  // Загрузить персоны при открытии
  useEffect(() => {
    if (project?.id) {
      loadPersonas()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id])

  const loadPersonas = useCallback(async () => {
    if (!project?.id) return
    try {
      const res = await simaFetch('/ai/simulator', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, action: 'get-personas' }),
      })
      if (res.ok) {
        const data = await res.json()
        setPersonas(data)
      }
    } catch (err) {
      console.error('Failed to load personas:', err)
    }
  }, [project?.id, setPersonas])

  const handleGeneratePersonas = useCallback(async () => {
    if (!project?.id) return
    setGeneratingPersonas(true)
    try {
      const res = await simaFetch('/ai/simulator', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectId: project.id, action: 'generate-personas' }),
      })
      const data = await res.json()
      if (!res.ok || data.error) {
        alert(data.error || t('err_generate_personas'))
      } else if (Array.isArray(data)) {
        setPersonas([...personas, ...data])
      } else {
        alert(t('err_unexpected_format'))
      }
    } catch (err) {
      console.error('Failed to generate personas:', err)
      alert(t('err_generate_personas'))
    } finally {
      setGeneratingPersonas(false)
    }
  }, [project?.id, personas, setPersonas])

  const handleCreatePersona = useCallback(async () => {
    if (!project?.id || !personaText.trim()) return
    setCreatingPersona(true)
    try {
      const res = await simaFetch('/ai/simulator', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'create-persona',
          text: personaText.trim(),
        }),
      })
      const data = await res.json()
      if (!res.ok || data.error) {
        alert(data.error || t('err_create_persona'))
      } else {
        addPersona(data)
        setPersonaText('')
      }
    } catch (err) {
      console.error('Failed to create persona:', err)
      alert(t('err_create_persona'))
    } finally {
      setCreatingPersona(false)
    }
  }, [project?.id, personaText, addPersona])

  const handleDeletePersona = useCallback(
    async (personaId: string) => {
      if (!project?.id) return
      try {
        await simaFetch('/ai/simulator', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            projectId: project.id,
            action: 'delete-persona',
            personaId,
          }),
        })
        removePersona(personaId)
      } catch (err) {
        console.error('Failed to delete persona:', err)
      }
    },
    [project?.id, removePersona]
  )

  const handleSimulate = useCallback(
    async (personaId: string) => {
      if (!project?.id) return
      setSimulatorLoading(true)
      setSelectedPersonaId(personaId)
      try {
        const res = await simaFetch('/ai/simulator', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            projectId: project.id,
            action: 'simulate',
            personaId,
            layerSlug: selectedLayer,
          }),
        })
        const data = await res.json()
        if (!res.ok || data.error) {
          alert(data.error || t('err_simulation'))
        } else {
          setSimulationReport(data as SimulationReport)
          setView('report')
          setExpandedSteps({})
        }
      } catch (err) {
        console.error('Failed to simulate:', err)
        alert(t('err_simulation'))
      } finally {
        setSimulatorLoading(false)
      }
    },
    [project?.id, selectedLayer, setSimulatorLoading, setSelectedPersonaId, setSimulationReport]
  )

  const handleSimulateAll = useCallback(async () => {
    if (!project?.id || personas.length === 0) return
    setSimulatorLoading(true)
    try {
      const res = await simaFetch('/ai/simulator', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          projectId: project.id,
          action: 'simulate-all',
          layerSlug: selectedLayer,
        }),
      })
      const data = await res.json()
      if (!res.ok || data.error) {
        alert(data.error || t('err_simulation'))
      } else if (Array.isArray(data)) {
        setSimulationReports(data as SimulationReport[])
        if (data.length > 0) {
          setSimulationReport(data[0])
        }
        setView('report')
        setExpandedSteps({})
      } else {
        alert(t('err_unexpected_format'))
      }
    } catch (err) {
      console.error('Failed to simulate all:', err)
      alert(t('err_simulation'))
    } finally {
      setSimulatorLoading(false)
    }
  }, [project?.id, personas, selectedLayer, setSimulatorLoading, setSimulationReports, setSimulationReport])

  const toggleStep = (stepNumber: number) => {
    setExpandedSteps((prev) => ({ ...prev, [stepNumber]: !prev[stepNumber] }))
  }

  if (!project) {
    return (
      <div className="p-4 text-sm text-sima-textDim">
        {t('no_project')}
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header with view toggle */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-sima-border shrink-0">
        <button
          onClick={() => setView('personas')}
          className={cn(
            'px-2.5 py-1 text-xs rounded transition-colors',
            view === 'personas'
              ? 'bg-sima-primary/20 text-sima-primary'
              : 'text-sima-textDim hover:text-sima-textMuted'
          )}
        >
          <Users className="w-3 h-3 inline mr-1" />
          {t('personas_heading', { count: personas.length })}
        </button>
        <button
          onClick={() => setView('report')}
          className={cn(
            'px-2.5 py-1 text-xs rounded transition-colors',
            view === 'report'
              ? 'bg-sima-primary/20 text-sima-primary'
              : 'text-sima-textDim hover:text-sima-textMuted'
          )}
        >
          <Play className="w-3 h-3 inline mr-1" />
          {t('result_tab')}
          {simulationReport && (
            <span className="ml-1">{simulationReport.summary.overallVerdict}</span>
          )}
        </button>

        <div className="flex-1" />

        {/* Layer selector */}
        <select
          value={selectedLayer}
          onChange={(e) => setSelectedLayer(e.target.value)}
          className="text-xs bg-sima-bg border border-sima-border rounded px-2 py-1 text-sima-text"
        >
          {LAYER_OPTIONS.map((l) => (
            <option key={l.value} value={l.value}>
              {l.label}
            </option>
          ))}
        </select>

        {/* Run all button */}
        {personas.length > 0 && (
          <button
            onClick={handleSimulateAll}
            disabled={simulatorLoading}
            className="flex items-center gap-1 px-2.5 py-1 text-xs bg-sima-primary/20 text-sima-primary rounded hover:bg-sima-primary/30 disabled:opacity-50 transition-colors"
          >
            {simulatorLoading ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <Zap className="w-3 h-3" />
            )}
            {t('all_personas')}
          </button>
        )}

        {simulationReport && (
          <button
            onClick={() => {
              const r = simulationReport
              const lines = [
                t('report_simulation_heading', { name: r.personaName }),
                `${t('report_verdict')}${r.summary.overallVerdict} ${r.summary.oneLiner}`,
                `**Completion rate:** ${r.summary.completionRate}%`,
                `**Time to value:** ${r.summary.timeToValue}`,
                '',
                t('report_steps_heading'),
                ...r.steps.map(s => `${s.stepNumber}. ${s.status} **${s.blockName}** — ${s.action} (drop-off: ${s.cumulativeDropoff}%)`),
                '',
                t('report_friction_heading'),
                ...r.frictionPoints.map(f => `- [${f.severity}] ${f.description} → ${f.recommendation.action}`),
                '',
                t('report_recommendations_heading'),
                ...r.recommendations.map(rec => `${rec.priority}. **${rec.action}** — ${rec.why}`),
              ]
              const blob = new Blob([lines.join('\n')], { type: 'text/markdown' })
              const url = URL.createObjectURL(blob)
              const a = document.createElement('a')
              a.href = url
              a.download = `simulation_${r.personaName}.md`
              a.click()
              URL.revokeObjectURL(url)
            }}
            className="flex items-center gap-1 px-2 py-1 text-[10px] text-sima-textDim hover:text-sima-textMuted transition-colors"
          >
            <Download className="w-3 h-3" />
          </button>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {view === 'personas' ? (
          <PersonasView
            personas={personas}
            personaText={personaText}
            setPersonaText={setPersonaText}
            onCreatePersona={handleCreatePersona}
            onGeneratePersonas={handleGeneratePersonas}
            onDeletePersona={handleDeletePersona}
            onSimulate={handleSimulate}
            selectedPersonaId={selectedPersonaId}
            simulatorLoading={simulatorLoading}
            generatingPersonas={generatingPersonas}
            creatingPersona={creatingPersona}
          />
        ) : (
          <ReportView
            report={simulationReport}
            allReports={simulationReports}
            onSelectReport={setSimulationReport}
            expandedSteps={expandedSteps}
            toggleStep={toggleStep}
            simulatorLoading={simulatorLoading}
          />
        )}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════
// PERSONAS VIEW
// ═══════════════════════════════════════════════════

function PersonasView({
  personas,
  personaText,
  setPersonaText,
  onCreatePersona,
  onGeneratePersonas,
  onDeletePersona,
  onSimulate,
  selectedPersonaId,
  simulatorLoading,
  generatingPersonas,
  creatingPersona,
}: {
  personas: PersonaData[]
  personaText: string
  setPersonaText: (t: string) => void
  onCreatePersona: () => void
  onGeneratePersonas: () => void
  onDeletePersona: (id: string) => void
  onSimulate: (id: string) => void
  selectedPersonaId: string | null
  simulatorLoading: boolean
  generatingPersonas: boolean
  creatingPersona: boolean
}) {
  const t = useTranslations('sima_simulator')
  return (
    <div className="p-4 space-y-4">
      {/* Persona cards */}
      {personas.length === 0 ? (
        <div className="text-center py-6">
          <User className="w-8 h-8 text-sima-textDim mx-auto mb-2" />
          <p className="text-sm text-sima-textDim mb-3">
            {t('no_personas_hint')}
          </p>
          <button
            onClick={onGeneratePersonas}
            disabled={generatingPersonas}
            className="px-4 py-2 text-xs bg-sima-primary text-white rounded-lg hover:bg-sima-primary/90 disabled:opacity-50 transition-colors"
          >
            {generatingPersonas ? (
              <>
                <Loader2 className="w-3 h-3 inline animate-spin mr-1" />
                {t('generating_3')}
              </>
            ) : (
              <>
                <Users className="w-3 h-3 inline mr-1" />
                {t('generate_3')}
              </>
            )}
          </button>
        </div>
      ) : (
        <div className="grid gap-3">
          {personas.map((persona) => (
            <PersonaCard
              key={persona.id}
              persona={persona}
              isSelected={selectedPersonaId === persona.id}
              isLoading={simulatorLoading && selectedPersonaId === persona.id}
              onSimulate={() => onSimulate(persona.id)}
              onDelete={() => onDeletePersona(persona.id)}
            />
          ))}
        </div>
      )}

      {/* Create persona form */}
      <div className="border border-sima-border rounded-lg p-3">
        <p className="text-xs text-sima-textMuted mb-2">{t('create_from_description')}</p>
        <div className="flex gap-2">
          <input
            type="text"
            value={personaText}
            onChange={(e) => setPersonaText(e.target.value)}
            placeholder={t("persona_placeholder")}
            className="flex-1 text-xs bg-sima-bg border border-sima-border rounded px-3 py-2 text-sima-text placeholder:text-sima-textDim focus:outline-none focus:border-sima-primary"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !creatingPersona) onCreatePersona()
            }}
          />
          <button
            onClick={onCreatePersona}
            disabled={creatingPersona || !personaText.trim()}
            className="px-3 py-2 text-xs bg-sima-primary/20 text-sima-primary rounded hover:bg-sima-primary/30 disabled:opacity-50 transition-colors shrink-0"
          >
            {creatingPersona ? (
              <Loader2 className="w-3 h-3 animate-spin" />
            ) : (
              <Plus className="w-3 h-3" />
            )}
          </button>
        </div>
      </div>

      {/* Generate more */}
      {personas.length > 0 && (
        <button
          onClick={onGeneratePersonas}
          disabled={generatingPersonas}
          className="w-full py-2 text-xs text-sima-textDim hover:text-sima-textMuted border border-dashed border-sima-border rounded-lg transition-colors"
        >
          {generatingPersonas ? (
            <>
              <Loader2 className="w-3 h-3 inline animate-spin mr-1" />
              {t('generating')}
            </>
          ) : (
            t('generate_3_more')
          )}
        </button>
      )}
    </div>
  )
}

function PersonaCard({
  persona,
  isSelected,
  isLoading,
  onSimulate,
  onDelete,
}: {
  persona: PersonaData
  isSelected: boolean
  isLoading: boolean
  onSimulate: () => void
  onDelete: () => void
}) {
  const t = useTranslations('sima_simulator')
  const ARCHETYPE_LABELS = buildArchetypeLabels(t)
  return (
    <div
      className={cn(
        'border rounded-lg p-3 transition-colors',
        isSelected
          ? 'border-sima-primary bg-sima-primary/5'
          : 'border-sima-border hover:border-sima-surfaceLight'
      )}
    >
      <div className="flex items-start justify-between mb-2">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-sima-text">{persona.name}</span>
            <span className="text-[10px] px-1.5 py-0.5 bg-sima-bg rounded text-sima-textDim">
              {ARCHETYPE_LABELS[persona.archetype] || persona.archetype}
            </span>
          </div>
          <p className="text-xs text-sima-textMuted mt-0.5">{persona.role}</p>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={onSimulate}
            disabled={isLoading}
            className="p-1.5 text-sima-primary hover:bg-sima-primary/10 rounded transition-colors disabled:opacity-50"
            title={t("run_simulation_title")}
          >
            {isLoading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Play className="w-3.5 h-3.5" />
            )}
          </button>
          <button
            onClick={onDelete}
            className="p-1.5 text-sima-textDim hover:text-red-400 hover:bg-red-400/10 rounded transition-colors"
            title={t("delete_persona_title")}
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-sima-textDim">
        <span>🎯 {persona.goal}</span>
        <span>⏱ {persona.timeBudget}</span>
        <span>💻 {persona.techLevel}</span>
        <span>{t('patience_label', { value: persona.patienceLevel })}</span>
      </div>

      {persona.traits.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {persona.traits.slice(0, 4).map((t, i) => (
            <span
              key={i}
              className="text-[9px] px-1.5 py-0.5 bg-sima-bg rounded text-sima-textMuted"
            >
              {t}
            </span>
          ))}
          {persona.traits.length > 4 && (
            <span className="text-[9px] text-sima-textDim">+{persona.traits.length - 4}</span>
          )}
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════
// REPORT VIEW
// ═══════════════════════════════════════════════════

function ReportView({
  report,
  allReports,
  onSelectReport,
  expandedSteps,
  toggleStep,
  simulatorLoading,
}: {
  report: SimulationReport | null
  allReports: SimulationReport[]
  onSelectReport: (r: SimulationReport) => void
  expandedSteps: Record<number, boolean>
  toggleStep: (n: number) => void
  simulatorLoading: boolean
}) {
  const t = useTranslations('sima_simulator')
  if (simulatorLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <Loader2 className="w-8 h-8 text-sima-primary animate-spin mx-auto mb-3" />
          <p className="text-sm text-sima-textMuted">{t('simulating')}</p>
          <p className="text-xs text-sima-textDim mt-1">{t('simulating_hint')}</p>
        </div>
      </div>
    )
  }

  if (!report) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <Play className="w-8 h-8 text-sima-textDim mx-auto mb-2" />
          <p className="text-sm text-sima-textDim">
            {t('select_persona_hint')}
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="p-4 space-y-4">
      {/* Persona tabs if multiple reports */}
      {allReports.length > 1 && (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {allReports.map((r) => (
            <button
              key={r.id}
              onClick={() => onSelectReport(r)}
              className={cn(
                'px-3 py-1.5 text-xs rounded-lg whitespace-nowrap transition-colors',
                r.id === report.id
                  ? 'bg-sima-primary/20 text-sima-primary'
                  : 'bg-sima-bg text-sima-textDim hover:text-sima-textMuted'
              )}
            >
              {r.summary.overallVerdict} {r.personaName} ({Math.round(r.summary.completionRate * 100)}%)
            </button>
          ))}
        </div>
      )}

      {/* Summary */}
      <div className="bg-sima-bg rounded-lg p-4">
        <div className="flex items-start gap-3">
          <span className="text-3xl">{report.summary.overallVerdict}</span>
          <div className="flex-1 min-w-0">
            <p className="text-sm text-sima-text font-medium">
              {report.personaName} → {report.scope.toUpperCase()}
            </p>
            <p className="text-xs text-sima-textMuted mt-1">{report.summary.oneLiner}</p>
            <div className="flex gap-4 mt-2 text-[10px] text-sima-textDim">
              <span>⏱ {report.summary.timeToValue}</span>
              <span>{t('completion_rate', { percent: Math.round(report.summary.completionRate * 100) })}</span>
              <span>{t('steps_count', { count: report.summary.totalSteps })}</span>
              <span>{t('analysis_time', { seconds: report.analysisTime })}</span>
            </div>
          </div>
        </div>

        {/* Completion bar */}
        <div className="mt-3">
          <div className="w-full h-2 bg-sima-surface rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: `${Math.round(report.summary.completionRate * 100)}%`,
                backgroundColor:
                  report.summary.completionRate > 0.7
                    ? '#34D399'
                    : report.summary.completionRate > 0.4
                    ? '#FBBF24'
                    : '#F87171',
              }}
            />
          </div>
          <div className="flex justify-between text-[9px] text-sima-textDim mt-1">
            <span>{t('start_label')}</span>
            <span>{t('value_label')}</span>
          </div>
        </div>
      </div>

      {/* Journey path visualization */}
      <div>
        <h4 className="text-xs font-medium text-sima-text mb-2">{t('user_path')}</h4>
        <div className="flex items-center gap-1 overflow-x-auto pb-2">
          {report.steps.map((step, i) => (
            <div key={i} className="flex items-center shrink-0">
              <button
                onClick={() => toggleStep(step.stepNumber)}
                className={cn(
                  'px-2 py-1 rounded text-[10px] font-medium transition-colors',
                  step.status === '🟢'
                    ? 'bg-green-500/10 text-green-400'
                    : step.status === '🟡'
                    ? 'bg-yellow-500/10 text-yellow-400'
                    : 'bg-red-500/10 text-red-400',
                  expandedSteps[step.stepNumber] && 'ring-1 ring-sima-primary'
                )}
              >
                {step.status} {step.blockName}
              </button>
              {i < report.steps.length - 1 && (
                <span className="text-sima-textDim mx-0.5">→</span>
              )}
            </div>
          ))}
        </div>
        {/* Dropoff under steps */}
        <div className="flex items-center gap-1 overflow-x-auto">
          {report.steps.map((step, i) => (
            <div key={i} className="flex items-center shrink-0">
              <span className="text-[9px] text-sima-textDim px-2 text-center" style={{ minWidth: 'fit-content' }}>
                {Math.round((1 - step.cumulativeDropoff) * 100)}%
              </span>
              {i < report.steps.length - 1 && (
                <span className="mx-0.5 opacity-0">→</span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Top friction points */}
      {report.frictionPoints.length > 0 && (
        <div>
          <h4 className="text-xs font-medium text-sima-text mb-2 flex items-center gap-1">
            <AlertTriangle className="w-3 h-3 text-orange-400" />
            {t('top_problems')}
          </h4>
          <div className="space-y-2">
            {report.frictionPoints.slice(0, 3).map((fp, i) => (
              <FrictionCard key={i} fp={fp} index={i + 1} />
            ))}
          </div>
        </div>
      )}

      {/* Expanded step details */}
      {report.steps.map((step) =>
        expandedSteps[step.stepNumber] ? (
          <StepDetail key={step.stepNumber} step={step} />
        ) : null
      )}

      {/* Recommendations */}
      {report.recommendations.length > 0 && (
        <div>
          <h4 className="text-xs font-medium text-sima-text mb-2">{t('recommendations_heading')}</h4>
          <div className="space-y-2">
            {report.recommendations.map((rec, i) => (
              <div
                key={i}
                className="bg-sima-bg rounded-lg p-3 border border-sima-border"
              >
                <div className="flex items-start gap-2">
                  <span className="text-xs font-bold text-sima-primary shrink-0">
                    #{rec.priority}
                  </span>
                  <div>
                    <p className="text-xs text-sima-text">{rec.action}</p>
                    <p className="text-[10px] text-sima-textDim mt-1">
                      <strong>{t('why_label')}</strong> {rec.why}
                    </p>
                    <p className="text-[10px] text-sima-textDim">
                      <strong>{t('impact_label')}</strong> {rec.impact}
                    </p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function FrictionCard({ fp, index }: { fp: FrictionPoint; index: number }) {
  const t = useTranslations('sima_simulator')
  return (
    <div className="bg-sima-bg rounded-lg p-3 border border-sima-border">
      <div className="flex items-start gap-2">
        <span className={cn('text-sm font-bold', SEVERITY_COLORS[fp.severity] || 'text-sima-textMuted')}>
          {fp.severity === 'wall' ? '🧱' : fp.severity === 'major' ? '🔴' : fp.severity === 'moderate' ? '🟡' : '⚪'}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-sima-text">
              {t('step_with_friction', { index, step: fp.stepNumber })}
            </span>
            <span className="text-[9px] px-1.5 py-0.5 bg-sima-surface rounded text-sima-textDim">
              {fp.type}
            </span>
            <span className={cn('text-[9px] font-medium', SEVERITY_COLORS[fp.severity])}>
              drop: {Math.round(fp.estimatedDropoff * 100)}%
            </span>
          </div>
          <p className="text-xs text-sima-textMuted mt-1">{fp.description}</p>
          {fp.userThinking && (
            <p className="text-[10px] text-sima-textDim mt-1 italic">
              💭 «{fp.userThinking}»
            </p>
          )}
          {fp.recommendation && (
            <div className="mt-2 pl-2 border-l-2 border-sima-primary/30">
              <p className="text-[10px] text-sima-primary">
                💡 {fp.recommendation.action}
              </p>
              <p className="text-[9px] text-sima-textDim">
                {t('effort_impact', { effort: fp.recommendation.effort, impact: fp.recommendation.impact })}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function StepDetail({ step }: { step: SimulationStep }) {
  const t = useTranslations('sima_simulator')
  return (
    <div className="bg-sima-surfaceLight rounded-lg p-3 border border-sima-primary/20">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-sm">{step.status}</span>
        <span className="text-xs font-medium text-sima-text">
          {t('step_block_label', { step: step.stepNumber, block: step.blockName })}
        </span>
        <span className="text-[9px] text-sima-textDim ml-auto">
          ⏱ {step.timeSpent} | drop: {Math.round(step.stepDropoff * 100)}%
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 text-[10px]">
        <div>
          <span className="text-sima-textDim font-medium">{t('what_does')}</span>
          <p className="text-sima-textMuted mt-0.5">{step.action}</p>
        </div>
        <div>
          <span className="text-sima-textDim font-medium">{t('what_sees')}</span>
          <p className="text-sima-textMuted mt-0.5">{step.userSees}</p>
        </div>
        <div>
          <span className="text-sima-textDim font-medium">{t('understands')}</span>
          <p className="text-sima-textMuted mt-0.5">{step.userUnderstands}</p>
        </div>
        <div>
          <span className="text-sima-textDim font-medium">{t('does_not_understand')}</span>
          <p className="text-sima-textMuted mt-0.5">{step.userDoesNotUnderstand}</p>
        </div>
        <div>
          <span className="text-sima-textDim font-medium">{t('action_label')}</span>
          <p className="text-sima-textMuted mt-0.5">{step.userDoes}</p>
        </div>
        <div>
          <span className="text-sima-textDim font-medium">{t('emotion_label')}</span>
          <p className="text-sima-textMuted mt-0.5">{step.userFeels}</p>
        </div>
      </div>
    </div>
  )
}
