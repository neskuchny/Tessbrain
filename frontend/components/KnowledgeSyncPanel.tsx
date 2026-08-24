'use client';

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslations, useLocale } from 'next-intl';

// VERSION: 2025-12-14-v3 (Force Reload)

interface Subscription {
  id: string;
  subscription_type: 'project' | 'folder';
  project_id?: string;
  folder_id?: string;
  auto_process_new_meetings: boolean;
  include_subfolders: boolean;
  status: string;
  meetings_processed: number;
  last_sync_at?: string;
  created_at: string;
}

interface Project {
  id?: string;
  project_id?: string;
  name: string;
}

interface Folder {
  id?: string;
  folder_id?: string;
  name: string;
  project_id: string;
}

interface SyncStatus {
  total_subscriptions: number;
  active_subscriptions: number;
  total_meetings_processed: number;
}

type SyncRunStatus = 'running' | 'completed' | 'error';

interface SyncRunInfo {
  run_id: string;
  subscription_id: string;
  status: SyncRunStatus;
  started_at?: string | null;
  finished_at?: string | null;
  stats?: {
    processed?: number;
    skipped?: number;
    errors?: number;
    entities_created?: number;
    relationships_created?: number;
  } | null;
  progress?: {
    total?: number;
    done?: number;
    processed?: number;
    skipped?: number;
    errors?: number;
    remaining?: number;
    current_title?: string;
    phase?: string;
    updated_at?: string;
  } | null;
  error?: string | null;
}

interface KnowledgeSyncPanelProps {
  userId?: string;
}

export default function KnowledgeSyncPanel({ userId }: KnowledgeSyncPanelProps) {
  const t = useTranslations('knowledge_sync');
  const locale = useLocale();
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncRunsBySubscriptionId, setSyncRunsBySubscriptionId] = useState<Record<string, SyncRunInfo>>({});

  const pollersRef = useRef<Record<string, number>>({});
  
  // Form state
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [subscriptionType, setSubscriptionType] = useState<'project' | 'folder'>('project');
  const [selectedProject, setSelectedProject] = useState<string>('');
  const [selectedFolder, setSelectedFolder] = useState<string>('');
  const [autoProcessNew, setAutoProcessNew] = useState(true);
  const [includeSubfolders, setIncludeSubfolders] = useState(true);
  
  // LLM Model level (affects quality/cost of LLM calls)
  type LLMLevel = 'standard' | 'premium';
  const [llmLevel, setLlmLevel] = useState<LLMLevel>('standard');
  
  // Аудит sync #2: селектор Tier 1-4 (processing_tier) убран — backend этот
  // параметр нигде не принимал, выбор не влиял ни на что (мёртвый UI).
  // Реальный выбор глубины — model_tier standard/premium выше.

  const getAuthHeader = useCallback((): Record<string, string> => {
    if (typeof window !== 'undefined') {
      // login сохраняет под 'tessent_access_token' (login/page.tsx:81); старый
      // ключ 'access_token' оставляем как fallback для совместимости с
      // legacy-сессиями. До фикса этот компонент читал ТОЛЬКО 'access_token',
      // не находил его → запрос /knowledge-sync/subscriptions летел без
      // Authorization → strict-auth блокировал создание подписки на папку.
      const token = localStorage.getItem('tessent_access_token')
        || localStorage.getItem('access_token');
      if (token) {
        return { Authorization: `Bearer ${token}` };
      }
    }
    return {};
  }, []);

  const parseJsonOrText = useCallback(async (response: Response) => {
    const contentType = response.headers.get('content-type') || '';
    const text = await response.text();
    if (contentType.includes('application/json')) {
      try {
        return JSON.parse(text);
      } catch {
        // fallthrough to generic parsing below
      }
    }
    try {
      return JSON.parse(text);
    } catch {
      return {
        status: 'error',
        message: text || `HTTP ${response.status} ${response.statusText}`
      };
    }
  }, []);

  const fetchData = useCallback(async () => {
    if (!userId) return;

    setLoading(true);
    setError(null);
    
    try {
      const headers = getAuthHeader();
      const userParam = `?user_id=${userId}`;
      
      // Fetch subscriptions
      const subsResponse = await fetch(`/api/v1/knowledge-sync/subscriptions${userParam}`, { headers });
      const subsData = await parseJsonOrText(subsResponse);
      if (subsData.status === 'success') {
        setSubscriptions(subsData.subscriptions || []);
      }
      
      // Fetch sync status
      const statusResponse = await fetch(`/api/v1/knowledge-sync/sync/status${userParam}`, { headers });
      const statusData = await parseJsonOrText(statusResponse);
      if (statusData.status === 'success') {
        setSyncStatus(statusData);
      }
      
      // Fetch projects
      const projectsResponse = await fetch(`/api/v1/meetflow/projects${userParam}`, { headers });
      const projectsData = await parseJsonOrText(projectsResponse);
      if (projectsData.status === 'success') {
        setProjects(projectsData.projects || []);
      }

      // Fetch all folders
      const foldersResponse = await fetch(`/api/v1/meetflow/folders${userParam}`, { headers });
      const foldersData = await parseJsonOrText(foldersResponse);
      if (foldersData.status === 'success') {
        setFolders(foldersData.folders || []);
      }
      
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  }, [userId, getAuthHeader, parseJsonOrText]);

  const stopPollingRun = useCallback((subscriptionId: string) => {
    const intervalId = pollersRef.current[subscriptionId];
    if (intervalId) {
      window.clearInterval(intervalId);
      delete pollersRef.current[subscriptionId];
    }
  }, []);

  const startPollingRun = useCallback((subscriptionId: string, runId: string) => {
    if (!userId) return;

    // Avoid duplicate pollers
    stopPollingRun(subscriptionId);

    const headers = getAuthHeader();
    const intervalId = window.setInterval(async () => {
      try {
        const resp = await fetch(`/api/v1/knowledge-sync/sync/run/${runId}?user_id=${userId}`, { headers });
        const data = await parseJsonOrText(resp);
        if (data.status !== 'success' || !data.run) {
          // Аудит sync #3: run хранится in-memory на backend — после рестарта
          // он пропадает, и раньше поллер крутился ВЕЧНО. Останавливаемся.
          if (typeof data.message === 'string' && data.message.includes('not found')) {
            stopPollingRun(subscriptionId);
            fetchData();
          }
          return;
        }
        const run: SyncRunInfo = data.run;
        setSyncRunsBySubscriptionId(prev => ({ ...prev, [subscriptionId]: run }));

        if (run.status === 'completed' || run.status === 'error') {
          stopPollingRun(subscriptionId);
          // Refresh main data once at the end
          fetchData();
        }
      } catch {
        // If polling fails transiently, keep trying
      }
    }, 2500);

    pollersRef.current[subscriptionId] = intervalId;
  }, [userId, getAuthHeader, parseJsonOrText, stopPollingRun, fetchData]);

  // Восстановление прогресса синка после возврата на страницу.
  // Прогресс жил только в React-state и пропадал при unmount'е. Теперь на
  // маунте читаем активные/последние run'ы с backend и возобновляем поллинг
  // для тех, что ещё 'running'.
  const restoreActiveRuns = useCallback(async () => {
    if (!userId) return;
    try {
      const headers = getAuthHeader();
      const resp = await fetch(`/api/v1/knowledge-sync/sync/runs?user_id=${userId}`, { headers });
      const data = await parseJsonOrText(resp);
      if (data.status !== 'success' || !Array.isArray(data.runs)) return;

      // Берём самый свежий run на подписку (бэкенд уже отсортировал desc).
      const latestBySub: Record<string, SyncRunInfo> = {};
      for (const run of data.runs as SyncRunInfo[]) {
        if (run.subscription_id && !latestBySub[run.subscription_id]) {
          latestBySub[run.subscription_id] = run;
        }
      }
      setSyncRunsBySubscriptionId(prev => ({ ...latestBySub, ...prev }));

      // Возобновляем поллинг для незавершённых.
      for (const run of Object.values(latestBySub)) {
        if (run.status === 'running' && !pollersRef.current[run.subscription_id]) {
          startPollingRun(run.subscription_id, run.run_id);
        }
      }
    } catch {
      // best-effort: не ломаем панель если runs недоступны
    }
  }, [userId, getAuthHeader, parseJsonOrText, startPollingRun]);

  useEffect(() => {
    restoreActiveRuns();
  }, [restoreActiveRuns]);

  // Cleanup pollers on unmount
  useEffect(() => {
    return () => {
      Object.values(pollersRef.current).forEach(id => window.clearInterval(id));
      pollersRef.current = {};
    };
  }, []);

  // Get folders filtered by selected project
  const filteredFolders = selectedProject 
    ? folders.filter(f => f.project_id === selectedProject)
    : folders;

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleCreateSubscription = async () => {
    // Validate based on subscription type
    if (subscriptionType === 'project' && !selectedProject) {
      setError(t('err_pick_project'));
      return;
    }
    if (subscriptionType === 'folder' && !selectedFolder) {
      setError(t('err_pick_folder'));
      return;
    }
    
    try {
      const headers = {
        ...getAuthHeader(),
        'Content-Type': 'application/json'
      };
      
      const body: Record<string, unknown> = {
        subscription_type: subscriptionType,
        auto_process_new_meetings: autoProcessNew,
        include_subfolders: includeSubfolders,
      };

      if (subscriptionType === 'project') {
        body.project_id = selectedProject;
        body.folder_id = null;
      } else {
        body.folder_id = selectedFolder;
        // For folder subscription, project_id can be set from the folder's project
        const folder = folders.find(f => (f.id || f.folder_id) === selectedFolder);
        if (folder) {
          body.project_id = folder.project_id;
        }
      }
      
      const response = await fetch(`/api/v1/knowledge-sync/subscriptions?user_id=${userId}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body)
      });
      
      const data = await parseJsonOrText(response);
      
      if (data.status === 'success') {
        setShowCreateForm(false);
        setSubscriptionType('project');
        setSelectedProject('');
        setSelectedFolder('');
        setAutoProcessNew(true);
        setIncludeSubfolders(true);
        fetchData();
      } else {
        setError(data.message || 'Failed to create subscription');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create subscription');
    }
  };

  const handleTriggerSync = async (
    subscriptionId: string,
    force: boolean = false,
    resumeForce: boolean = false,
  ) => {
    setSyncing(subscriptionId);
    setError(null);

    try {
      const headers = getAuthHeader();
      // resumeForce = «Продолжить прерванный force»: force, но пропустить встречи,
      // уже переобработанные СЕГОДНЯ (этой кампанией). cutoff = начало текущих
      // суток UTC (дата без времени — чтобы не кодировать «+00:00»).
      const forceParam = (force || resumeForce) ? '&force=true' : '';
      const resumeParam = resumeForce
        ? `&reprocess_older_than=${new Date().toISOString().slice(0, 10)}`
        : '';
      const response = await fetch(`/api/v1/knowledge-sync/sync/${subscriptionId}?user_id=${userId}&model_tier=${llmLevel}${forceParam}${resumeParam}`, {
        method: 'POST',
        headers
      });
      
      const data = await parseJsonOrText(response);
      
      if (data.status === 'success') {
        if (data.run_id) {
          const run: SyncRunInfo = {
            run_id: data.run_id,
            subscription_id: subscriptionId,
            status: 'running'
          };
          setSyncRunsBySubscriptionId(prev => ({ ...prev, [subscriptionId]: run }));
          startPollingRun(subscriptionId, data.run_id);
        }
        fetchData();
      } else {
        setError(data.message || 'Sync failed');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sync failed');
    } finally {
      setSyncing(null);
    }
  };

  const handleDeleteSubscription = async (subscriptionId: string) => {
    if (!confirm(t('confirm_delete'))) {
      return;
    }
    
    try {
      const headers = getAuthHeader();
      const response = await fetch(`/api/v1/knowledge-sync/subscriptions/${subscriptionId}?user_id=${userId}`, {
        method: 'DELETE',
        headers
      });
      
      const data = await parseJsonOrText(response);
      
      if (data.status === 'success') {
        fetchData();
      } else {
        setError(data.message || 'Failed to delete subscription');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete subscription');
    }
  };

  const handleTogglePause = async (subscriptionId: string, currentStatus: string) => {
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    
    try {
      const headers = {
        ...getAuthHeader(),
        'Content-Type': 'application/json'
      };
      
      const response = await fetch(`/api/v1/knowledge-sync/subscriptions/${subscriptionId}?user_id=${userId}`, {
        method: 'PATCH',
        headers,
        body: JSON.stringify({ status: newStatus })
      });
      
      const data = await parseJsonOrText(response);
      
      if (data.status === 'success') {
        fetchData();
      } else {
        setError(data.message || 'Failed to update subscription');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update subscription');
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'active': return 'text-green-400';
      case 'syncing': return 'text-yellow-400';
      case 'paused': return 'text-gray-400';
      case 'error': return 'text-red-400';
      default: return 'text-gray-400';
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'active': return '✓';
      case 'syncing': return '⟳';
      case 'paused': return '⏸';
      case 'error': return '✗';
      default: return '?';
    }
  };

  const getSubscriptionName = (sub: Subscription) => {
    if (sub.subscription_type === 'project') {
      const project = projects.find(p => (p.id || p.project_id) === sub.project_id);
      return project ? t('project_label_value', { name: project.name }) : t('project_not_found');
    } else {
      const folder = folders.find(f => (f.id || f.folder_id) === sub.folder_id);
      return folder ? t('folder_label_value', { name: folder.name }) : t('folder_not_found');
    }
  };

  if (!userId) {
    return (
      <div className="p-6 bg-brain-800 rounded-lg text-brain-300">
        <p>{t('login_required')}</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="p-6 bg-brain-800 rounded-lg">
        <div className="animate-pulse">
          <div className="h-6 bg-brain-700 rounded w-1/3 mb-4"></div>
          <div className="h-4 bg-brain-700 rounded w-2/3 mb-2"></div>
          <div className="h-4 bg-brain-700 rounded w-1/2"></div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 bg-brain-800 rounded-lg space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold text-brain-100">{t('title')}</h2>
          <p className="text-sm text-brain-400">{t('subtitle')}</p>
        </div>
        <button
          onClick={() => setShowCreateForm(true)}
          className="px-4 py-2 bg-brain-600 hover:bg-brain-500 text-white rounded-lg transition-colors"
        >
          {t('new_subscription_button')}
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="p-4 bg-red-900/30 border border-red-700 rounded-lg text-red-300">
          {error}
          <button onClick={() => setError(null)} className="ml-2 text-red-400 hover:text-red-300">×</button>
        </div>
      )}

      {/* Status Overview */}
      {syncStatus && (
        <div className="grid grid-cols-3 gap-4">
          <div className="p-4 bg-brain-700/50 rounded-lg">
            <div className="text-2xl font-bold text-brain-100">{syncStatus.total_subscriptions}</div>
            <div className="text-sm text-brain-400">{t('stat_total')}</div>
          </div>
          <div className="p-4 bg-brain-700/50 rounded-lg">
            <div className="text-2xl font-bold text-green-400">{syncStatus.active_subscriptions}</div>
            <div className="text-sm text-brain-400">{t('stat_active')}</div>
          </div>
          <div className="p-4 bg-brain-700/50 rounded-lg">
            <div className="text-2xl font-bold text-brain-100">{syncStatus.total_meetings_processed}</div>
            <div className="text-sm text-brain-400">{t('stat_processed')}</div>
          </div>
        </div>
      )}

      {/* Create Form */}
      {showCreateForm && (
        <div className="p-4 bg-brain-700/50 rounded-lg space-y-4">
          <h3 className="font-medium text-brain-100">{t('create_subscription_title')}</h3>
          
          <div>
            <label className="block text-sm text-brain-400 mb-1">{t('subscription_type_label')}</label>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="subscriptionType"
                  value="project"
                  checked={subscriptionType === 'project'}
                  onChange={() => {
                    setSubscriptionType('project');
                    setSelectedFolder('');
                  }}
                  className="text-brain-500"
                />
                <span className="text-brain-200">{t('subscription_project')}</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="subscriptionType"
                  value="folder"
                  checked={subscriptionType === 'folder'}
                  onChange={() => setSubscriptionType('folder')}
                  className="text-brain-500"
                />
                <span className="text-brain-200">{t('subscription_folder')}</span>
              </label>
            </div>
          </div>
          
          {subscriptionType === 'project' && (
            <div>
              <label className="block text-sm text-brain-400 mb-1">{t('project_required_label')}</label>
              <select
                value={selectedProject}
                onChange={(e) => setSelectedProject(e.target.value)}
                className="w-full px-3 py-2 bg-brain-800 border border-brain-600 rounded-lg text-brain-100"
              >
                <option value="">{t('select_project')}</option>
                {projects.filter(p => p.id || p.project_id).map((project) => {
                  const projectId = project.id || project.project_id;
                  return (
                    <option key={`project-${projectId}`} value={projectId}>{project.name}</option>
                  );
                })}
              </select>
            </div>
          )}

          {subscriptionType === 'folder' && (
            <>
              <div>
                <label className="block text-sm text-brain-400 mb-1">{t('project_filter_label')}</label>
                <select
                  value={selectedProject}
                  onChange={(e) => {
                    setSelectedProject(e.target.value);
                    setSelectedFolder('');
                  }}
                  className="w-full px-3 py-2 bg-brain-800 border border-brain-600 rounded-lg text-brain-100"
                >
                  <option value="">{t('all_projects')}</option>
                  {projects.filter(p => p.id || p.project_id).map((project) => {
                    const projectId = project.id || project.project_id;
                    return (
                      <option key={`project-filter-${projectId}`} value={projectId}>{project.name}</option>
                    );
                  })}
                </select>
              </div>
              
              <div>
                <label className="block text-sm text-brain-400 mb-1">{t('folder_required_label')}</label>
                <select
                  value={selectedFolder}
                  onChange={(e) => setSelectedFolder(e.target.value)}
                  className="w-full px-3 py-2 bg-brain-800 border border-brain-600 rounded-lg text-brain-100"
                >
                  <option value="">{t('select_folder')}</option>
                  {filteredFolders.filter(f => f.id || f.folder_id).map((folder) => {
                    const folderId = folder.id || folder.folder_id;
                    return (
                      <option key={`folder-${folderId}`} value={folderId}>{folder.name}</option>
                    );
                  })}
                </select>
              </div>
            </>
          )}
          
          <div className="flex flex-col gap-2">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={autoProcessNew}
                onChange={(e) => setAutoProcessNew(e.target.checked)}
                className="text-brain-500"
              />
              <span className="text-brain-200 text-sm">{t('auto_process_label')}</span>
            </label>
            
            {subscriptionType === 'folder' && (
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={includeSubfolders}
                  onChange={(e) => setIncludeSubfolders(e.target.checked)}
                  className="text-brain-500"
                />
                <span className="text-brain-200 text-sm">{t('include_subfolders_label')}</span>
              </label>
            )}
          </div>
          
          <div className="flex gap-2">
            <button
              onClick={handleCreateSubscription}
              className="px-4 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg transition-colors"
            >
              {t('submit_create')}
            </button>
            <button
              onClick={() => {
                setShowCreateForm(false);
                setError(null);
              }}
              className="px-4 py-2 bg-brain-600 hover:bg-brain-500 text-white rounded-lg transition-colors"
            >
              {t('cancel')}
            </button>
          </div>
        </div>
      )}

      {/* Subscriptions List */}
      <div className="space-y-3">
        {subscriptions.length === 0 ? (
          <div className="text-center py-8 text-brain-400">
            {t('empty_hint')}
          </div>
        ) : (
          subscriptions.map((sub) => {
            const project = projects.find(p => (p.id || p.project_id) === sub.project_id);
            const folder = folders.find(f => (f.id || f.folder_id) === sub.folder_id);
            const run = syncRunsBySubscriptionId[sub.id];
            const derivedStatus =
              run?.status === 'running' ? 'syncing' :
              run?.status === 'error' ? 'error' :
              sub.status;
            
            return (
              <div key={sub.id} className="p-4 bg-brain-700/30 rounded-lg">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className={`${getStatusColor(derivedStatus)}`}>{getStatusIcon(derivedStatus)}</span>
                      <span className="font-medium text-brain-100">{getSubscriptionName(sub)}</span>
                      <span className="text-xs px-2 py-0.5 bg-brain-600 rounded text-brain-300">
                        {sub.subscription_type === 'project' ? t('sub_project') : t('sub_folder')}
                      </span>
                      {run?.status === 'running' && (
                        <span className="text-xs px-2 py-0.5 bg-yellow-600/30 rounded text-yellow-300">
                          ⟳ run
                        </span>
                      )}
                    </div>
                    <div className="text-sm text-brain-400 mt-1 flex flex-wrap gap-2">
                      {project && sub.subscription_type === 'folder' && (
                        <span className="px-2 py-0.5 bg-blue-600/30 rounded text-blue-300 text-xs">
                          📁 {project.name}
                        </span>
                      )}
                      {folder && (
                        <span className="px-2 py-0.5 bg-purple-600/30 rounded text-purple-300 text-xs">
                          📂 {folder.name}
                        </span>
                      )}
                      {sub.auto_process_new_meetings && (
                        <span className="px-2 py-0.5 bg-green-600/30 rounded text-green-300 text-xs">
                          {t('sub_auto')}
                        </span>
                      )}
                      {sub.include_subfolders && sub.subscription_type === 'folder' && (
                        <span className="px-2 py-0.5 bg-yellow-600/30 rounded text-yellow-300 text-xs">
                          {t('sub_nested')}
                        </span>
                      )}
                    </div>
                    <div className="text-sm text-brain-400 mt-1">
                      {t('processed_meetings', { count: sub.meetings_processed })}
                      {sub.last_sync_at && (
                        <span>{t('last_sync_prefix', { value: new Date(sub.last_sync_at).toLocaleString(locale) })}</span>
                      )}
                    </div>
                    {run && (
                      <div className="text-xs text-brain-400 mt-1">
                        {run.status === 'running' && (
                          run.progress && (run.progress.total ?? 0) > 0 ? (
                            <div className="mt-1">
                              <div className="flex items-center justify-between mb-1">
                                <span className="text-brain-200 font-medium">
                                  {t('progress_meetings', { done: run.progress.done ?? 0, total: run.progress.total ?? 0 })}
                                  {(run.progress.remaining ?? 0) > 0 && (
                                    <span className="text-brain-500"> {t('progress_remaining', { n: run.progress.remaining ?? 0 })}</span>
                                  )}
                                </span>
                                <span className="text-brain-500 font-mono">
                                  {Math.round(((run.progress.done ?? 0) / (run.progress.total || 1)) * 100)}%
                                </span>
                              </div>
                              <div className="w-full h-1.5 bg-brain-800/60 rounded-full overflow-hidden">
                                <div
                                  className="h-full bg-gradient-to-r from-cyan-500 to-blue-500 rounded-full transition-all duration-500"
                                  style={{ width: `${Math.min(100, ((run.progress.done ?? 0) / (run.progress.total || 1)) * 100)}%` }}
                                />
                              </div>
                              <div className="flex items-center gap-3 mt-1 text-[11px]">
                                <span className="text-emerald-400">✓ {t('progress_processed', { n: run.progress.processed ?? 0 })}</span>
                                {(run.progress.skipped ?? 0) > 0 && (
                                  <span className="text-yellow-400">⤼ {t('progress_skipped', { n: run.progress.skipped ?? 0 })}</span>
                                )}
                                {(run.progress.errors ?? 0) > 0 && (
                                  <span className="text-red-400">⚠ {t('progress_errors', { n: run.progress.errors ?? 0 })}</span>
                                )}
                              </div>
                              {run.progress.current_title && (
                                <div className="text-[11px] text-brain-500 mt-0.5 truncate">
                                  {t('progress_current', { title: run.progress.current_title })}
                                </div>
                              )}
                            </div>
                          ) : (
                            <span>{t('sync_running', { id: run.run_id })}</span>
                          )
                        )}
                        {run.status === 'completed' && run.stats && (
                          <span>
                            {t('last_run_done', { count: run.stats.processed ?? 0 })}
                            {(run.stats.skipped ?? 0) > 0 && (
                              <span className="text-yellow-400">{t('skipped_suffix', { count: run.stats.skipped ?? 0 })}</span>
                            )}
                            {(run.stats.errors ?? 0) > 0 && (
                              <span className="text-red-400">{t('errors_suffix', { count: run.stats.errors ?? 0 })}</span>
                            )}
                            {run.stats.processed === 0 && (run.stats.skipped ?? 0) > 0 && (
                              <span className="text-brain-500 block mt-0.5">
                                {t('all_meetings_synced_hint')}
                              </span>
                            )}
                          </span>
                        )}
                        {run.status === 'error' && (
                          <span className="text-red-300">{t('sync_error', { error: run.error || 'unknown' })}</span>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => handleTogglePause(sub.id, sub.status)}
                      className="px-3 py-1.5 bg-brain-600 hover:bg-brain-500 text-white rounded transition-colors text-sm"
                      title={sub.status === 'active' ? t('pause_title') : t('resume_title')}
                    >
                      {sub.status === 'active' ? '⏸' : '▶'}
                    </button>
                    {/* LLM model level */}
                    <select
                      value={llmLevel}
                      onChange={(e) => setLlmLevel(e.target.value as LLMLevel)}
                      className="px-2 py-1.5 bg-slate-800 border border-slate-600 text-slate-200 rounded text-sm"
                      title={t("model_tier_title")}
                    >
                      <option value="standard">{t("model_standard")}</option>
                      <option value="premium">{t("model_premium")}</option>
                    </select>
                    <button
                      onClick={() => handleTriggerSync(sub.id)}
                      disabled={syncing === sub.id || sub.status === 'paused' || run?.status === 'running'}
                      className="px-3 py-1.5 bg-brain-600 hover:opacity-90 disabled:opacity-50 text-white rounded transition-colors text-sm"
                    >
                      {syncing === sub.id || run?.status === 'running' ? t('syncing') : t('sync_button')}
                    </button>
                    <button
                      onClick={() => handleTriggerSync(sub.id, true)}
                      disabled={syncing === sub.id || sub.status === 'paused' || run?.status === 'running'}
                      className="px-3 py-1.5 bg-orange-600/70 hover:bg-orange-600 disabled:opacity-50 text-white rounded transition-colors text-sm"
                      title={t("force_resync_title")}
                    >
                      🔁 Force
                    </button>
                    <button
                      onClick={() => handleTriggerSync(sub.id, false, true)}
                      disabled={syncing === sub.id || sub.status === 'paused' || run?.status === 'running'}
                      className="px-3 py-1.5 bg-amber-700/50 hover:bg-amber-700/70 disabled:opacity-50 text-white rounded transition-colors text-sm"
                      title={t('resume_force_title')}
                    >
                      {t('resume_force_button')}
                    </button>
                    <button
                      onClick={() => handleDeleteSubscription(sub.id)}
                      className="px-3 py-1.5 bg-red-600/30 hover:bg-red-600/50 text-red-300 rounded transition-colors text-sm"
                    >
                      {t('delete_button')}
                    </button>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Actions */}
      <div className="flex justify-end items-center pt-4 border-t border-brain-700">
        <button
          onClick={fetchData}
          className="text-sm text-brain-400 hover:text-brain-300 transition-colors"
        >
          {t('refresh_button')}
        </button>
      </div>
    </div>
  );
}
