'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { useTranslations, useLocale } from 'next-intl';
import { Upload, FileText, Trash2, Search, Filter, Eye, Download, Plus, X, Loader2, RefreshCw, CheckCircle, AlertCircle, Clock, Folder } from 'lucide-react';

interface Document {
  id: string;
  title: string;
  doc_type: string;
  content?: string;
  content_preview?: string;
  file_name?: string;
  file_size?: number;
  created_at: string;
  updated_at?: string;
  metadata?: Record<string, any>;
  status?: string;
  sync_status?: 'not_synced' | 'syncing' | 'synced' | 'error';
  synced_at?: string;
  chunks_count?: number;
}

interface DocumentStats {
  total: number;
  by_type: Record<string, number>;
  by_sync_status: {
    not_synced: number;
    syncing: number;
    synced: number;
    error: number;
  };
  total_size: number;
  total_chunks: number;
}

interface DocumentsPanelProps {
  userId?: string;
  onDataUpdate?: () => void;
  focusRequest?: {
    documentId: string;
    requestId: number;
  } | null;
}

export default function DocumentsPanel({ userId, onDataUpdate, focusRequest }: DocumentsPanelProps) {
  const t = useTranslations('documents_panel');
  const locale = useLocale();
  const [documents, setDocuments] = useState<Document[]>([]);
  const [stats, setStats] = useState<DocumentStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedType, setSelectedType] = useState<string>('all');
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [selectedDocument, setSelectedDocument] = useState<Document | null>(null);

  // Upload form state
  const [uploadTitle, setUploadTitle] = useState('');
  const [uploadContent, setUploadContent] = useState('');
  const [uploadType, setUploadType] = useState('text');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadFolder, setUploadFolder] = useState('');

  // Папки документов (плоские ярлыки, как у чатов)
  const [folders, setFolders] = useState<string[]>([]);
  const [folderFilter, setFolderFilter] = useState<string>('all'); // all | none | <name>

  const getAuthHeader = useCallback((): Record<string, string> => {
    if (typeof window !== 'undefined') {
      const token = localStorage.getItem('tessent_access_token');
      if (token) {
        return { Authorization: `Bearer ${token}` };
      }
    }
    return {};
  }, []);

  const fetchFolders = useCallback(async () => {
    try {
      const userParam = userId ? `?user_id=${userId}` : '';
      const r = await fetch(`/api/v1/documents/folders${userParam}`, { headers: getAuthHeader() });
      const d = await r.json();
      if (Array.isArray(d?.folders)) setFolders(d.folders);
    } catch { /* ignore */ }
  }, [userId, getAuthHeader]);

  const createFolder = useCallback(async () => {
    const name = (typeof window !== 'undefined' ? window.prompt(t('folder_prompt')) : '')?.trim();
    if (!name) return;
    const userParam = userId ? `?user_id=${userId}` : '';
    try {
      const r = await fetch(`/api/v1/documents/folders${userParam}`, {
        method: 'POST', headers: { ...getAuthHeader(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const d = await r.json();
      if (Array.isArray(d?.folders)) { setFolders(d.folders); setFolderFilter(name); setUploadFolder(name); }
    } catch { /* ignore */ }
  }, [userId, getAuthHeader, t]);

  const deleteFolder = useCallback(async (name: string) => {
    if (typeof window !== 'undefined' && !window.confirm(t('delete_folder_confirm', { name }))) return;
    const userParam = userId ? `?user_id=${userId}` : '';
    try {
      await fetch(`/api/v1/documents/folders/delete${userParam}`, {
        method: 'POST', headers: { ...getAuthHeader(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (folderFilter === name) setFolderFilter('all');
      fetchFolders();
    } catch { /* ignore */ }
  }, [userId, getAuthHeader, folderFilter, fetchFolders, t]);

  const moveToFolder = useCallback(async (docId: string, folder: string) => {
    const userParam = userId ? `?user_id=${userId}` : '';
    setDocuments(prev => prev.map(d => d.id === docId
      ? { ...d, metadata: { ...(d.metadata || {}), folder: folder || undefined } } : d));
    try {
      await fetch(`/api/v1/documents/${docId}/folder${userParam}`, {
        method: 'POST', headers: { ...getAuthHeader(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder }),
      });
      fetchFolders();
    } catch { /* revert через рефетч */ }
  }, [userId, getAuthHeader, fetchFolders]);

  const fetchDocuments = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const headers = getAuthHeader();
      const userParam = userId ? `?user_id=${userId}` : '';
      
      const response = await fetch(`/api/v1/documents/${userParam}`, { headers });
      const data = await response.json();
      console.log('Fetched documents:', data.documents ? data.documents.length : 0);

      if (data.status === 'success') {
        setDocuments(data.documents || []);
        
        if (data.migration_required) {
          setError(t('err_table_missing'));
        }
      } else {
        setError(data.message || t('err_loading'));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('err_loading_short'));
    } finally {
      setLoading(false);
    }
  }, [userId, getAuthHeader, t]);

  const fetchStats = useCallback(async () => {
    try {
      const headers = getAuthHeader();
      const userParam = userId ? `?user_id=${userId}` : '';
      
      const response = await fetch(`/api/v1/documents/stats${userParam}`, { headers });
      const data = await response.json();

      if (data.status === 'success') {
        setStats(data.stats);
      }
    } catch (err) {
      console.error('Error fetching stats:', err);
    }
  }, [userId, getAuthHeader]);

  const fetchFullDocument = useCallback(async (docId: string) => {
    try {
      const headers = getAuthHeader();
      const userParam = userId ? `?user_id=${userId}` : '';
      const response = await fetch(`/api/v1/documents/${docId}${userParam}`, { headers });
      const data = await response.json();
      if (data.status === 'success') {
        setSelectedDocument(data.document);
      } else {
        setError(data.message || t('err_loading_doc'));
      }
    } catch (err) {
      console.error('Error fetching full document:', err);
      setError(t('err_loading_fulltext'));
    }
  }, [userId, getAuthHeader, t]);

  useEffect(() => {
    fetchDocuments();
    fetchStats();
    fetchFolders();
  }, [fetchDocuments, fetchStats, fetchFolders]);

  useEffect(() => {
    if (!focusRequest?.documentId) return;
    fetchFullDocument(focusRequest.documentId);
  }, [focusRequest?.requestId, focusRequest?.documentId, fetchFullDocument]);

  const handleUpload = async () => {
    if (!uploadTitle.trim()) {
      setError(t('err_name_required'));
      return;
    }

    if (!uploadContent.trim() && !uploadFile) {
      setError(t('err_content_required'));
      return;
    }

    setUploading(true);
    setError(null);

    try {
      const headers = {
        ...getAuthHeader(),
        'Content-Type': 'application/json'
      };

      let content = uploadContent;

      if (uploadFile) {
        // Читаем как DataURL (Base64) для сохранения бинарных данных и корректного парсинга на бэкенде
        content = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.onerror = reject;
          reader.readAsDataURL(uploadFile);
        });
      }

      const response = await fetch('/api/v1/documents/', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          folder: uploadFolder || undefined,
          title: uploadTitle,
          content: content,
          doc_type: uploadType,
          metadata: {
            user_id: userId,
            file_name: uploadFile?.name,
            file_size: uploadFile?.size
          }
        })
      });

      const data = await response.json();

      if (data.status === 'success') {
        setShowUploadModal(false);
        setUploadTitle('');
        setUploadContent('');
        setUploadType('text');
        setUploadFile(null);
        setUploadFolder('');
        setSuccess(t('success_uploaded'));
        fetchDocuments();
        fetchStats();
        if (onDataUpdate) onDataUpdate();
      } else {
        setError(data.message || t('err_upload'));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('err_loading_short'));
    } finally {
      setUploading(false);
    }
  };

  const handleSync = async (documentIds?: string[]) => {
    setSyncing(true);
    setError(null);

    try {
      const headers = {
        ...getAuthHeader(),
        'Content-Type': 'application/json'
      };

      const userParam = userId ? `?user_id=${userId}` : '';
      
      const response = await fetch(`/api/v1/documents/sync${userParam}`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          document_ids: documentIds || null
        })
      });

      const data = await response.json();

      if (data.status === 'success') {
        setSuccess(t('success_synced', { count: data.stats?.synced || 0 }));
        fetchDocuments();
        fetchStats();
        if (onDataUpdate) onDataUpdate();
      } else {
        setError(data.message || t('err_sync'));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('err_sync'));
    } finally {
      setSyncing(false);
    }
  };

  const handleDelete = async (documentId: string) => {
    if (!confirm(t('confirm_delete'))) return;

    try {
      const headers = getAuthHeader();
      const userParam = userId ? `?user_id=${userId}` : '';

      const response = await fetch(`/api/v1/documents/${documentId}${userParam}`, {
        method: 'DELETE',
        headers
      });

      const data = await response.json();

      if (data.status === 'success') {
        setSuccess(t('success_deleted'));
        fetchDocuments();
        fetchStats();
        if (onDataUpdate) onDataUpdate();
      } else {
        setError(data.message || t('err_delete'));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t('err_delete'));
    }
  };

  const filteredDocuments = documents.filter(doc => {
    if (selectedType !== 'all' && doc.doc_type !== selectedType) return false;
    if (searchQuery && !doc.title.toLowerCase().includes(searchQuery.toLowerCase())) return false;
    const f = String(doc.metadata?.folder || '').trim();
    if (folderFilter === 'none' && f) return false;
    if (folderFilter !== 'all' && folderFilter !== 'none' && f !== folderFilter) return false;
    return true;
  });

  const docTypes = ['all', 'text', 'markdown', 'code', 'transcript', 'report', 'pdf'];

  const getTypeIcon = (type: string) => {
    switch (type) {
      case 'code': return '💻';
      case 'markdown': return '📝';
      case 'transcript': return '🎙️';
      case 'report': return '📊';
      case 'pdf': return '📕';
      default: return '📄';
    }
  };

  const getSyncStatusIcon = (status?: string) => {
    switch (status) {
      case 'synced': return <CheckCircle className="w-4 h-4 text-green-400" />;
      case 'syncing': return <Loader2 className="w-4 h-4 text-blue-400 animate-spin" />;
      case 'error': return <AlertCircle className="w-4 h-4 text-red-400" />;
      default: return <Clock className="w-4 h-4 text-brain-500" />;
    }
  };

  const getSyncStatusText = (status?: string) => {
    switch (status) {
      case 'synced': return t('status_synced');
      case 'syncing': return t('status_syncing');
      case 'error': return t('status_error');
      default: return t('status_not_synced');
    }
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [success]);

  if (loading && documents.length === 0) {
    return (
      <div className="p-6 bg-brain-800 rounded-lg">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-brain-700 rounded w-1/3"></div>
          <div className="h-4 bg-brain-700 rounded w-2/3"></div>
          <div className="grid grid-cols-3 gap-4">
            {[1, 2, 3].map(i => (
              <div key={i} className="h-32 bg-brain-700 rounded"></div>
            ))}
          </div>
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
          <p className="text-sm text-brain-400">
            {t('description')}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => handleSync()}
            disabled={syncing}
            className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white rounded-lg transition-colors"
          >
            {syncing ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <RefreshCw className="w-4 h-4" />
            )}
            {t('sync_button')}
          </button>
          <button
            onClick={() => setShowUploadModal(true)}
            className="flex items-center gap-2 px-4 py-2 bg-brain-600 hover:bg-brain-500 text-white rounded-lg transition-colors"
          >
            <Plus className="w-4 h-4" />
            {t('upload_button')}
          </button>
        </div>
      </div>

      {/* Info banner */}
      <div className="p-4 bg-purple-900/20 border border-purple-700/30 rounded-lg text-purple-300 text-sm">
        <p>{t('how_to_use')}</p>
      </div>

      {/* Stats */}
      {stats && stats.total > 0 && (
        <div className="grid grid-cols-4 gap-4">
          <div className="p-3 bg-brain-700/50 rounded-lg">
            <div className="text-2xl font-bold text-brain-100">{stats.total}</div>
            <div className="text-sm text-brain-400">{t('stats_total')}</div>
          </div>
          <div className="p-3 bg-green-900/30 rounded-lg">
            <div className="text-2xl font-bold text-green-400">{stats.by_sync_status?.synced || 0}</div>
            <div className="text-sm text-brain-400">{t('stats_synced')}</div>
          </div>
          <div className="p-3 bg-yellow-900/30 rounded-lg">
            <div className="text-2xl font-bold text-yellow-400">{stats.by_sync_status?.not_synced || 0}</div>
            <div className="text-sm text-brain-400">{t('stats_pending')}</div>
          </div>
          <div className="p-3 bg-brain-700/50 rounded-lg">
            <div className="text-2xl font-bold text-brain-100">{stats.total_chunks || 0}</div>
            <div className="text-sm text-brain-400">{t('stats_chunks')}</div>
          </div>
        </div>
      )}

      {/* Success */}
      {success && (
        <div className="p-4 bg-green-900/30 border border-green-700 rounded-lg text-green-300 flex items-center justify-between">
          <span>✅ {success}</span>
          <button onClick={() => setSuccess(null)} className="text-green-400 hover:text-green-300">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="p-4 bg-red-900/30 border border-red-700 rounded-lg text-red-300 flex items-center justify-between">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-red-300">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Search & Filter */}
      <div className="flex gap-4">
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-brain-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t('search_placeholder')}
            className="w-full pl-10 pr-4 py-2 bg-brain-900 border border-brain-700 rounded-lg text-brain-100 placeholder-brain-500"
          />
        </div>
        <select
          value={selectedType}
          onChange={(e) => setSelectedType(e.target.value)}
          className="px-4 py-2 bg-brain-900 border border-brain-700 rounded-lg text-brain-100"
        >
          <option value="all">{t('filter_all_types')}</option>
          <option value="text">{t('filter_text')}</option>
          <option value="markdown">Markdown</option>
          <option value="code">{t('filter_code')}</option>
          <option value="transcript">{t('filter_transcript')}</option>
          <option value="report">{t('filter_report')}</option>
          <option value="pdf">PDF</option>
        </select>
      </div>

      {/* Папки документов (плоские ярлыки) */}
      <div className="flex items-center gap-1.5 flex-wrap">
        {[{ key: 'all', label: t('folder_all', { count: documents.length }) }, { key: 'none', label: t('folder_none') }].map((f) => (
          <button key={f.key} onClick={() => setFolderFilter(f.key)}
            className={`px-2.5 py-1 rounded-lg text-xs border transition-colors ${folderFilter === f.key ? 'bg-brain-600 border-brain-500 text-white' : 'border-brain-700 text-brain-400 hover:bg-brain-800/50'}`}>
            {f.label}
          </button>
        ))}
        {folders.map((name) => (
          <span key={name}
            className={`group inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs border transition-colors ${folderFilter === name ? 'bg-brain-600 border-brain-500 text-white' : 'border-brain-700 text-brain-300 hover:bg-brain-800/50'}`}>
            <button onClick={() => setFolderFilter(name)} className="flex items-center gap-1">
              <Folder className="w-3 h-3" />{name}
            </button>
            <button onClick={() => deleteFolder(name)} title={t('delete_folder_title')} className="opacity-0 group-hover:opacity-100 text-brain-500 hover:text-red-400">✕</button>
          </span>
        ))}
        <button onClick={createFolder}
          className="px-2.5 py-1 rounded-lg text-xs border border-dashed border-brain-600/50 text-brain-400 hover:bg-brain-800/50">
          {t('add_folder_button')}
        </button>
      </div>

      {/* Documents Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {filteredDocuments.length === 0 ? (
          <div className="col-span-full text-center py-12 text-brain-400">
            <FileText className="w-12 h-12 mx-auto mb-4 opacity-50" />
            <p>{t('empty_title')}</p>
            <p className="text-sm mt-1">{t('empty_hint')}</p>
          </div>
        ) : (
          filteredDocuments.map((doc) => (
            <div
              key={doc.id}
              className="p-4 bg-brain-700/30 rounded-lg hover:bg-brain-700/50 transition-colors"
            >
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-2">
                  <span className="text-xl">{getTypeIcon(doc.doc_type)}</span>
                  <span className="text-xs px-2 py-0.5 bg-brain-600 rounded text-brain-300">
                    {doc.doc_type}
                  </span>
                </div>
                <div className="flex items-center gap-1" title={getSyncStatusText(doc.sync_status)}>
                  {getSyncStatusIcon(doc.sync_status)}
                </div>
              </div>
              
              <h3 
                className="font-medium text-brain-100 mb-1 truncate cursor-pointer hover:text-brain-50"
                onClick={() => fetchFullDocument(doc.id)}
              >
                {doc.title}
              </h3>
              
              {doc.content_preview && (
                <p className="text-sm text-brain-400 line-clamp-2">{doc.content_preview}</p>
              )}
              
              <div className="flex items-center justify-between mt-3 text-xs text-brain-500">
                <span>{new Date(doc.created_at).toLocaleDateString(locale)}</span>
                <span className="flex items-center gap-1">
                  {doc.chunks_count ? t('chunks_count', { count: doc.chunks_count }) : getSyncStatusText(doc.sync_status)}
                </span>
              </div>

              {/* Папка документа */}
              <div className="flex items-center gap-1 mt-2 text-xs text-brain-500">
                <Folder className="w-3 h-3 flex-shrink-0" />
                <select
                  value={String(doc.metadata?.folder || '')}
                  onChange={(e) => moveToFolder(doc.id, e.target.value)}
                  className="flex-1 min-w-0 bg-brain-900 border border-brain-700 rounded px-1.5 py-1 text-xs text-brain-300"
                >
                  <option value="">{t('folder_none_option')}</option>
                  {folders.map((name) => (
                    <option key={name} value={name}>{name}</option>
                  ))}
                </select>
              </div>

              {/* Actions */}
              <div className="flex items-center gap-2 mt-3 pt-3 border-t border-brain-600">
                {doc.sync_status !== 'synced' && (
                  <button
                    onClick={() => handleSync([doc.id])}
                    disabled={syncing}
                    className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 bg-purple-600/30 hover:bg-purple-600/50 text-purple-300 rounded text-sm transition-colors"
                  >
                    <RefreshCw className="w-3 h-3" />
                    {t('sync_item')}
                  </button>
                )}
                {doc.sync_status === 'synced' && (
                  <span className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 text-green-400 text-sm">
                    <CheckCircle className="w-3 h-3" />
                    {t('available_in_chat')}
                  </span>
                )}
                <button
                  onClick={() => fetchFullDocument(doc.id)}
                  className="flex items-center justify-center gap-1 px-2 py-1.5 bg-brain-600/30 hover:bg-brain-600/50 text-brain-300 rounded text-sm transition-colors"
                >
                  <Eye className="w-3 h-3" />
                </button>
                <button
                  onClick={() => handleDelete(doc.id)}
                  className="flex items-center justify-center gap-1 px-2 py-1.5 bg-red-600/30 hover:bg-red-600/50 text-red-300 rounded text-sm transition-colors"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Upload Modal */}
      {showUploadModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-brain-800 rounded-lg p-6 w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-brain-100">{t('modal_title')}</h3>
              <button
                onClick={() => setShowUploadModal(false)}
                className="p-1 hover:bg-brain-700 rounded"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm text-brain-400 mb-1">{t('field_name')}</label>
                <input
                  type="text"
                  value={uploadTitle}
                  onChange={(e) => setUploadTitle(e.target.value)}
                  placeholder={t('field_name_placeholder')}
                  className="w-full px-3 py-2 bg-brain-900 border border-brain-700 rounded-lg text-brain-100 placeholder-brain-500"
                />
              </div>

              <div>
                <label className="block text-sm text-brain-400 mb-1">{t('field_doc_type')}</label>
                <select
                  value={uploadType}
                  onChange={(e) => setUploadType(e.target.value)}
                  className="w-full px-3 py-2 bg-brain-900 border border-brain-700 rounded-lg text-brain-100"
                >
                  <option value="text">{t('filter_text')}</option>
                  <option value="markdown">Markdown</option>
                  <option value="code">{t('filter_code')}</option>
                  <option value="transcript">{t('filter_transcript')}</option>
                  <option value="report">{t('filter_report')}</option>
                </select>
              </div>

              <div>
                <label className="block text-sm text-brain-400 mb-1 flex items-center gap-1">
                  <Folder className="w-3.5 h-3.5" /> {t('folder_label')}
                </label>
                <div className="flex gap-2">
                  <select
                    value={uploadFolder}
                    onChange={(e) => setUploadFolder(e.target.value)}
                    className="flex-1 px-3 py-2 bg-brain-900 border border-brain-700 rounded-lg text-brain-100"
                  >
                    <option value="">{t('folder_none_option')}</option>
                    {folders.map((name) => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={createFolder}
                    className="px-3 py-2 bg-brain-700 hover:bg-brain-600 text-brain-200 rounded-lg text-sm whitespace-nowrap"
                  >
                    {t('new_folder_button')}
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-sm text-brain-400 mb-1">{t('field_file_optional')}</label>
                <div className="border-2 border-dashed border-brain-700 rounded-lg p-4 text-center">
                  <input
                    type="file"
                    onChange={(e) => {
                      const file = e.target.files?.[0] || null;
                      setUploadFile(file);
                      if (file) {
                        // Автоматически определяем тип документа
                        const ext = file.name.split('.').pop()?.toLowerCase();
                        if (ext === 'pdf') setUploadType('pdf');
                        else if (ext === 'md') setUploadType('markdown');
                        else if (['py', 'js', 'ts', 'html', 'css', 'json'].includes(ext || '')) setUploadType('code');
                        else if (['pptx', 'ppt'].includes(ext || '')) setUploadType('presentation');
                        else if (['xlsx', 'xls', 'csv'].includes(ext || '')) setUploadType('spreadsheet');
                        else if (['docx', 'doc'].includes(ext || '')) setUploadType('text');
                        else setUploadType('text');
                      }
                    }}
                    accept=".txt,.md,.py,.js,.ts,.json,.csv,.html,.xml,.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls"
                    className="hidden"
                    id="file-upload"
                  />
                  <label
                    htmlFor="file-upload"
                    className="cursor-pointer flex flex-col items-center gap-2"
                  >
                    <Upload className="w-8 h-8 text-brain-500" />
                    {uploadFile ? (
                      <span className="text-brain-300">{uploadFile.name}</span>
                    ) : (
                      <span className="text-brain-500">{t('select_file_or_drag')}</span>
                    )}
                  </label>
                </div>
              </div>

              <div>
                <label className="block text-sm text-brain-400 mb-1">
                  {t('field_content', { hint: uploadFile ? t('field_content_replaced_by_file') : '' })}
                </label>
                <textarea
                  value={uploadContent}
                  onChange={(e) => setUploadContent(e.target.value)}
                  placeholder={t('content_placeholder')}
                  rows={8}
                  disabled={!!uploadFile}
                  className="w-full px-3 py-2 bg-brain-900 border border-brain-700 rounded-lg text-brain-100 placeholder-brain-500 disabled:opacity-50"
                />
              </div>

              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setShowUploadModal(false)}
                  className="px-4 py-2 bg-brain-700 hover:bg-brain-600 text-white rounded-lg transition-colors"
                >
                  {t('cancel_button')}
                </button>
                <button
                  onClick={handleUpload}
                  disabled={uploading}
                  className="flex items-center gap-2 px-4 py-2 bg-green-600 hover:bg-green-500 disabled:opacity-50 text-white rounded-lg transition-colors"
                >
                  {uploading ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      {t('upload_loading')}
                    </>
                  ) : (
                    <>
                      <Upload className="w-4 h-4" />
                      {t('upload_button')}
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Document Preview Modal */}
      {selectedDocument && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-brain-800 rounded-lg p-6 w-full max-w-3xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-3">
                <span className="text-2xl">{getTypeIcon(selectedDocument.doc_type)}</span>
                <div>
                  <h3 className="text-lg font-semibold text-brain-100">{selectedDocument.title}</h3>
                  <p className="text-sm text-brain-400">
                    {new Date(selectedDocument.created_at).toLocaleString(locale)}
                    {selectedDocument.sync_status === 'synced' && selectedDocument.chunks_count && (
                      <span className="ml-2 text-green-400">{t('selected_chunks_in_kb', { count: selectedDocument.chunks_count })}</span>
                    )}
                  </p>
                </div>
              </div>
              <button
                onClick={() => setSelectedDocument(null)}
                className="p-1 hover:bg-brain-700 rounded"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="bg-brain-900 rounded-lg p-4 max-h-96 overflow-y-auto">
              <pre className="text-sm text-brain-300 whitespace-pre-wrap">
                {selectedDocument.content || selectedDocument.content_preview || t('content_unavailable')}
              </pre>
            </div>

            <div className="flex justify-between gap-2 mt-4">
              <div className="flex gap-2">
                {selectedDocument.sync_status !== 'synced' && (
                  <button
                    onClick={() => {
                      handleSync([selectedDocument.id]);
                      setSelectedDocument(null);
                    }}
                    className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded-lg transition-colors"
                  >
                    <RefreshCw className="w-4 h-4" />
                    {t('sync_item')}
                  </button>
                )}
              </div>
              <button
                onClick={() => {
                  handleDelete(selectedDocument.id);
                  setSelectedDocument(null);
                }}
                className="flex items-center gap-2 px-4 py-2 bg-red-600/30 hover:bg-red-600/50 text-red-300 rounded-lg transition-colors"
              >
                <Trash2 className="w-4 h-4" />
                {t('delete')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Footer */}
      <div className="flex justify-between items-center pt-4 border-t border-brain-700">
        <span className="text-sm text-brain-400">
          {t('footer_count', { count: filteredDocuments.length })}
          {stats?.total_size ? ` • ${formatFileSize(stats.total_size)}` : ''}
        </span>
        <button
          onClick={() => {
            fetchDocuments();
            fetchStats();
          }}
          className="text-sm text-brain-400 hover:text-brain-300 transition-colors"
        >
          {t('refresh')}
        </button>
      </div>
    </div>
  );
}
