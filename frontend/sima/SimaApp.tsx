'use client'

import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import { useSimaStore } from '@/sima/lib/store'
import { simaFetch } from '@/sima/lib/api'
import type { ProjectData } from '@/sima/types'
import ProjectSelector from '@/sima/components/Layout/ProjectSelector'
import Workspace from '@/sima/components/Layout/Workspace'

interface SimaAppProps {
  userId: string | null
}

export default function SimaApp({ userId }: SimaAppProps) {
  const t = useTranslations('sima')
  const { project, setProject, setAllBlocks, setAllConnections, setMessages, resetProjectState } = useSimaStore()
  const [projects, setProjects] = useState<ProjectData[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (userId) {
      loadProjects()
    }
  }, [userId])

  // Тема SIMA (Sima Remix) применяется на ВЕСЬ раздел, а не только на Workspace:
  // раньше атрибут вешался внутри Workspace, а внешние обёртки SimaApp были
  // жёстко тёмными (bg-brain-950) — из-за этого «светлая тема» была не видна
  // (тёмный фон не реагировал). Дефолт — светлая; тёмная — только по явному
  // сохранённому выбору. Снятие атрибута при выходе из SIMA — здесь (владелец).
  useEffect(() => {
    const dark = typeof window !== 'undefined' &&
      localStorage.getItem('sima_theme') === 'dark'
    if (dark) document.documentElement.removeAttribute('data-sima-theme')
    else document.documentElement.setAttribute('data-sima-theme', 'light')
    return () => document.documentElement.removeAttribute('data-sima-theme')
  }, [])

  async function loadProjects() {
    try {
      const res = await simaFetch('/projects')
      if (res.ok) {
        const data = await res.json()
        setProjects(data)
      }
    } catch (e) {
      console.error('SIMA: Failed to load projects:', e)
    } finally {
      setLoading(false)
    }
  }

  async function selectProject(id: string) {
    try {
      resetProjectState()
      const res = await simaFetch(`/projects/${id}`)
      if (res.ok) {
        const data = await res.json()
        setProject(data)
        setAllBlocks(data.blocks || [])
        setAllConnections(data.connections || [])
        setMessages(data.messages || [])
      }
    } catch (e) {
      console.error('SIMA: Failed to load project:', e)
    }
  }

  async function createProject(name: string, template?: string, taskKind?: string) {
    try {
      resetProjectState()
      const res = await simaFetch('/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, template, taskKind }),
      })
      if (res.ok) {
        const data = await res.json()
        setProject(data)
        setAllBlocks(data.blocks || [])
        setMessages(data.messages || [])
        setProjects(prev => [data, ...prev])
      }
    } catch (e) {
      console.error('SIMA: Failed to create project:', e)
    }
  }

  function goBack() {
    resetProjectState()
    setProject(null)
    loadProjects()
  }

  if (!userId) {
    return (
      <div className="h-full flex items-center justify-center bg-sima-bg">
        <div className="text-center">
          <p className="text-brain-400">{t('auth_required')}</p>
        </div>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center bg-sima-bg">
        <div className="text-center">
          <div className="w-16 h-16 border-4 border-purple-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
          <p className="text-brain-400">{t('loading')}</p>
        </div>
      </div>
    )
  }

  if (!project) {
    return (
      <div className="h-full overflow-auto bg-sima-bg">
        <ProjectSelector
          projects={projects}
          onSelect={selectProject}
          onCreate={createProject}
        />
      </div>
    )
  }

  return (
    <div className="h-full overflow-hidden bg-sima-bg">
      <Workspace onBack={goBack} userId={userId}
        projects={projects} onSwitchProject={selectProject} />
    </div>
  )
}
