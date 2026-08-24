'use client';

import { useState, useEffect, useCallback } from 'react';
import { useTranslations } from 'next-intl';
import {
  Settings, Key, Plus, Trash2, Eye, EyeOff, Check, X,
  ChevronDown, ChevronRight, Users, Search, AlertCircle,
  MessageCircle, Mail, Calendar, CheckSquare, FileText,
  BarChart3, Palette, Wand2, Zap, Globe, Brain, HelpCircle, Send
} from 'lucide-react';
import SetupGuide from './SetupGuide';

interface IntegrationKey {
  id: string;
  key_name: string;
  masked_value: string;
  is_set: boolean;
  updated_at: string;
}

interface Contact {
  id: string;
  type: string;
  value: string;
  display_name: string;
}

interface Integration {
  provider: string;
  name: string;
  icon: string;
  category: string;
  configured: boolean;
  keys: Record<string, IntegrationKey>;
  contacts: Contact[];
}

interface ProviderConfig {
  id: string;
  name: string;
  icon: string;
  category: string;
  keys: Array<{
    key_name: string;
    label: string;
    placeholder: string;
    required: boolean;
    multiline?: boolean;
  }>;
  contacts_support?: boolean;
  description: string;
}

interface Category {
  name: string;
  icon: string;
  providers: ProviderConfig[];
}

const CATEGORY_ICONS: Record<string, React.ReactNode> = {
  llm: <Brain className="w-4 h-4" />,
  search: <Search className="w-4 h-4" />,
  messaging: <MessageCircle className="w-4 h-4" />,
  email: <Mail className="w-4 h-4" />,
  calendar: <Calendar className="w-4 h-4" />,
  tasks: <CheckSquare className="w-4 h-4" />,
  knowledge: <FileText className="w-4 h-4" />,
  crm: <Users className="w-4 h-4" />,
  analytics: <BarChart3 className="w-4 h-4" />,
  design: <Palette className="w-4 h-4" />,
  ai_generation: <Wand2 className="w-4 h-4" />,
  automation: <Zap className="w-4 h-4" />,
};

interface IntegrationsPanelProps {
  userId: string | null;
  isOpen: boolean;
  onClose: () => void;
}

import ExtractionRoutePanel from './ExtractionRoutePanel';
import ModelSettingsPanel from './ModelSettingsPanel';
import McpConnectPanel from './McpConnectPanel';

export default function IntegrationsPanel({ userId, isOpen, onClose }: IntegrationsPanelProps) {
  const t = useTranslations('integrations');
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [categories, setCategories] = useState<Record<string, Category>>({});
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set(['llm', 'messaging']));
  const [selectedProvider, setSelectedProvider] = useState<ProviderConfig | null>(null);
  const [editingKey, setEditingKey] = useState<{provider: string; key_name: string; value: string} | null>(null);
  const [showSecrets, setShowSecrets] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
  // Adding contact state
  const [addingContact, setAddingContact] = useState<{provider: string; type: string} | null>(null);
  const [newContactValue, setNewContactValue] = useState('');
  const [newContactName, setNewContactName] = useState('');

  // «Подключить Telegram» в один клик: backend выдаёт одноразовый токен и
  // deep-link t.me/<бот>?start=<токен>; после /start бот привязывает chat_id
  // к аккаунту (messenger_links) — автоматизации шлют туда без настройки.
  const [tgLinking, setTgLinking] = useState(false);
  const [tgLinkNote, setTgLinkNote] = useState<string>('');
  const [tgDeeplink, setTgDeeplink] = useState<string>('');
  const [showGuide, setShowGuide] = useState(false);
  const [tgTesting, setTgTesting] = useState(false);
  const [tgTestNote, setTgTestNote] = useState<{ ok: boolean; msg: string } | null>(null);

  // Реальная проба доставки: жмём — бэкенд шлёт тестовое сообщение и возвращает
  // ТОЧНУЮ причину провала (нет токена / нет chat_id / ответ Telegram).
  const handleTestTelegram = useCallback(async () => {
    if (tgTesting) return;
    setTgTesting(true);
    setTgTestNote(null);
    try {
      const userParam = userId ? `?user_id=${userId}` : '';
      const res = await fetch(`/api/v1/integrations/telegram/test${userParam}`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      setTgTestNote({ ok: !!data?.ok, msg: data?.message || (data?.ok ? 'OK' : t('tg_test_failed')) });
    } catch {
      setTgTestNote({ ok: false, msg: t('tg_test_failed') });
    } finally {
      setTgTesting(false);
    }
  }, [tgTesting, userId, t]);

  // preOpened — вкладка, открытая СИНХРОННО в обработчике клика (пока есть
  // «жест пользователя»). Раньше window.open звался ПОСЛЕ await fetch — жест
  // уже истёк, и popup-блокировщик молча резал вкладку, а UI всё равно писал
  // «Открыли бота». Теперь: заранее открытую вкладку перенаправляем на deep-link,
  // а если её всё же зарезали — показываем кликабельную ссылку (100% работает).
  const handleConnectTelegram = useCallback(async (preOpened?: Window | null) => {
    if (tgLinking) return;
    setTgLinking(true);
    setTgLinkNote('');
    setTgDeeplink('');
    try {
      const res = await fetch('/api/v1/messengers/telegram/onboarding/start', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        preOpened?.close();
        setTgLinkNote(data?.detail || t('tg_connect_failed'));
        return;
      }
      if (data.deeplink) {
        setTgDeeplink(data.deeplink);        // кликабельный фолбэк рендерится ниже
        if (preOpened && !preOpened.closed) {
          try {
            preOpened.location.href = data.deeplink;
            try { preOpened.opener = null; } catch { /* после навигации бывает throw */ }
            setTgLinkNote(t('tg_connect_opened'));
          } catch {
            setTgLinkNote(t('tg_connect_click_link'));
          }
        } else {
          // вкладку зарезал блокировщик — не врём про «открыли», даём ссылку
          setTgLinkNote(t('tg_connect_click_link'));
        }
      } else {
        preOpened?.close();
        // deeplink нет ⇒ на бэкенде не задан TELEGRAM_BOT_TOKEN/USERNAME
        setTgLinkNote(data?.reason || t('tg_connect_no_bot'));
      }
    } catch {
      preOpened?.close();
      setTgLinkNote(t('tg_connect_failed'));
    } finally {
      setTgLinking(false);
    }
  }, [tgLinking, t]);

  const fetchIntegrations = useCallback(async () => {
    if (!userId) return;
    
    setLoading(true);
    try {
      // Fetch providers config
      const providersRes = await fetch('/api/v1/integrations/providers');
      const providersData = await providersRes.json();
      setCategories(providersData.categories || {});
      
      // Fetch user integrations
      const integrationsRes = await fetch(`/api/v1/integrations/?user_id=${userId}`);
      const integrationsData = await integrationsRes.json();
      setIntegrations(integrationsData.integrations || []);
      
    } catch (err) {
      console.error('Failed to fetch integrations:', err);
      setError(t('err_loading'));
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    if (isOpen && userId) {
      setSearchQuery(''); // Reset search on open
      fetchIntegrations();
    }
  }, [isOpen, userId, fetchIntegrations]);

  const handleSaveKey = async () => {
    if (!editingKey || !userId) return;
    
    setSaving(true);
    setError(null);
    
    try {
      const res = await fetch(`/api/v1/integrations/keys?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: editingKey.provider,
          key_name: editingKey.key_name,
          secret_value: editingKey.value,
          meta: {}
        })
      });
      
      const data = await res.json();
      
      if (data.success) {
        setEditingKey(null);
        fetchIntegrations();
      } else {
        setError(data.error || t('err_saving_key'));
      }
    } catch (err) {
      setError(t('err_saving'));
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteKey = async (integrationId: string) => {
    if (!userId || !confirm(t('confirm_delete_key'))) return;
    
    try {
      const res = await fetch(`/api/v1/integrations/keys/${integrationId}?user_id=${userId}`, {
        method: 'DELETE'
      });
      
      const data = await res.json();
      if (data.success) {
        fetchIntegrations();
      }
    } catch (err) {
      setError(t('err_deleting'));
    }
  };

  const handleAddContact = async () => {
    if (!addingContact || !userId || !newContactValue) return;
    
    setSaving(true);
    try {
      const res = await fetch(`/api/v1/integrations/contacts?user_id=${userId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: addingContact.provider,
          contact_type: addingContact.type,
          contact_value: newContactValue,
          display_name: newContactName,
          meta: {}
        })
      });
      
      const data = await res.json();
      if (data.success) {
        setAddingContact(null);
        setNewContactValue('');
        setNewContactName('');
        fetchIntegrations();
      }
    } catch (err) {
      setError(t('err_adding_contact'));
    } finally {
      setSaving(false);
    }
  };

  const toggleCategory = (category: string) => {
    const newExpanded = new Set(expandedCategories);
    if (newExpanded.has(category)) {
      newExpanded.delete(category);
    } else {
      newExpanded.add(category);
    }
    setExpandedCategories(newExpanded);
  };

  const getIntegrationByProvider = (providerId: string): Integration | undefined => {
    return integrations.find(i => i.provider === providerId);
  };

  const filteredCategories = Object.entries(categories).filter(([_, cat]) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return cat.providers.some(p => 
      p.name.toLowerCase().includes(query) || 
      p.description.toLowerCase().includes(query)
    );
  });

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-brain-900 border border-brain-700 rounded-2xl w-full max-w-4xl max-h-[85vh] overflow-hidden shadow-2xl flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-brain-700/50 flex-shrink-0">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-purple-500/20 rounded-xl">
                <Settings className="w-6 h-6 text-purple-400" />
              </div>
              <div>
                <h2 className="text-xl font-semibold text-white">{t('title')}</h2>
                <p className="text-sm text-brain-400">{t('description')}</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowGuide(true)}
                title={t('setup_guide_title')}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brain-700 text-brain-300 hover:bg-brain-800 text-sm transition-colors"
              >
                <HelpCircle className="w-4 h-4" />
                {t('setup_guide_button')}
              </button>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-brain-800 text-brain-400 hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>
          {showGuide && <SetupGuide initialSection="telegram" onClose={() => setShowGuide(false)} />}
          
          {/* Search */}
          <div className="mt-4 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-brain-500" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t('search_placeholder')}
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck="false"
              data-form-type="other"
              name="integration-search-field"
              className="w-full bg-brain-800 border border-brain-700 rounded-lg pl-10 pr-4 py-2 text-sm text-white placeholder-brain-500 focus:outline-none focus:border-purple-500"
            />
          </div>
        </div>

        {/* Error */}
        {error && (
          <div className="mx-6 mt-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-center gap-2 text-red-400 text-sm">
            <AlertCircle className="w-4 h-4" />
            {error}
            <button onClick={() => setError(null)} className="ml-auto">
              <X className="w-4 h-4" />
            </button>
          </div>
        )}

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 custom-scrollbar">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin w-8 h-8 border-2 border-purple-500 border-t-transparent rounded-full" />
            </div>
          ) : (
            <div className="space-y-4">
              {/* Выбор модели обработки встреч (роутер провайдер×уровень) */}
              {!searchQuery && userId && <ExtractionRoutePanel userId={userId} />}
              {/* Свои модели для уровней Стандарт/Премиум (tier_overrides) */}
              {!searchQuery && userId && <ModelSettingsPanel />}
              {/* Токен подключения для Claude/Cursor — раньше выпускался только
                  командой на сервере, теперь кнопкой здесь */}
              {!searchQuery && userId && <McpConnectPanel />}
              {filteredCategories.map(([categoryId, category]) => (
                <div key={categoryId} className="border border-brain-700/50 rounded-xl overflow-hidden">
                  {/* Category Header */}
                  <button
                    onClick={() => toggleCategory(categoryId)}
                    className="w-full flex items-center gap-3 p-4 bg-brain-800/50 hover:bg-brain-800 transition-colors"
                  >
                    <div className="p-1.5 bg-brain-700 rounded-lg text-brain-300">
                      {CATEGORY_ICONS[categoryId] || <Globe className="w-4 h-4" />}
                    </div>
                    <span className="font-medium text-white">{category.name}</span>
                    <span className="text-xs text-brain-500 ml-1">
                      ({category.providers.length})
                    </span>
                    <div className="ml-auto">
                      {expandedCategories.has(categoryId) ? (
                        <ChevronDown className="w-4 h-4 text-brain-400" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-brain-400" />
                      )}
                    </div>
                  </button>
                  
                  {/* Providers */}
                  {expandedCategories.has(categoryId) && (
                    <div className="p-4 space-y-3">
                      {category.providers
                        .filter(p => !searchQuery || p.name.toLowerCase().includes(searchQuery.toLowerCase()))
                        .map((provider) => {
                          const integration = getIntegrationByProvider(provider.id);
                          const isConfigured = integration?.configured || false;
                          
                          return (
                            <div
                              key={provider.id}
                              className={`p-4 rounded-xl border transition-all ${
                                isConfigured 
                                  ? 'bg-green-500/5 border-green-500/30' 
                                  : 'bg-brain-800/30 border-brain-700/50 hover:border-brain-600'
                              }`}
                            >
                              <div className="flex items-start gap-3">
                                <span className="text-2xl">{provider.icon}</span>
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2">
                                    <h4 className="font-medium text-white">{provider.name}</h4>
                                    {isConfigured && (
                                      <span className="px-2 py-0.5 bg-green-500/20 text-green-400 text-xs rounded-full flex items-center gap-1">
                                        <Check className="w-3 h-3" />
                                        {t('connected_badge')}
                                      </span>
                                    )}
                                  </div>
                                  <p className="text-sm text-brain-400 mt-0.5">{provider.description}</p>
                                  
                                  {/* Keys */}
                                  <div className="mt-3 space-y-2">
                                    {provider.keys.map((keyConfig) => {
                                      const existingKey = integration?.keys[keyConfig.key_name];
                                      const isEditing = editingKey?.provider === provider.id && editingKey?.key_name === keyConfig.key_name;
                                      
                                      return (
                                        <div key={keyConfig.key_name} className="flex items-center gap-2">
                                          <Key className="w-3 h-3 text-brain-500 flex-shrink-0" />
                                          <span className="text-xs text-brain-400 w-28 flex-shrink-0">{keyConfig.label}:</span>
                                          
                                          {isEditing ? (
                                            <div className="flex-1 flex items-center gap-2">
                                              <input
                                                type="text"
                                                value={editingKey.value}
                                                onChange={(e) => setEditingKey({...editingKey, value: e.target.value})}
                                                placeholder={keyConfig.placeholder}
                                                autoComplete="new-password"
                                                autoCorrect="off"
                                                data-form-type="other"
                                                data-lpignore="true"
                                                data-1p-ignore="true"
                                                style={!showSecrets.has(`${provider.id}-${keyConfig.key_name}`) ? {
                                                  WebkitTextSecurity: 'disc',
                                                  textSecurity: 'disc'
                                                } as React.CSSProperties : undefined}
                                                className="flex-1 bg-brain-900 border border-brain-600 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-purple-500"
                                              />
                                              <button
                                                type="button"
                                                onClick={() => {
                                                  const key = `${provider.id}-${keyConfig.key_name}`;
                                                  const newShow = new Set(showSecrets);
                                                  newShow.has(key) ? newShow.delete(key) : newShow.add(key);
                                                  setShowSecrets(newShow);
                                                }}
                                                className="p-1 text-brain-400 hover:text-white"
                                              >
                                                {showSecrets.has(`${provider.id}-${keyConfig.key_name}`) ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                                              </button>
                                              <button
                                                type="button"
                                                onClick={handleSaveKey}
                                                disabled={saving}
                                                className="p-1 text-green-400 hover:text-green-300"
                                              >
                                                <Check className="w-3 h-3" />
                                              </button>
                                              <button
                                                type="button"
                                                onClick={() => setEditingKey(null)}
                                                className="p-1 text-red-400 hover:text-red-300"
                                              >
                                                <X className="w-3 h-3" />
                                              </button>
                                            </div>
                                          ) : existingKey?.is_set ? (
                                            <div className="flex-1 flex items-center gap-2">
                                              <code className="text-xs text-brain-300 bg-brain-800 px-2 py-0.5 rounded">
                                                {existingKey.masked_value}
                                              </code>
                                              <button
                                                type="button"
                                                onClick={(e) => {
                                                  e.preventDefault();
                                                  e.stopPropagation();
                                                  setEditingKey({provider: provider.id, key_name: keyConfig.key_name, value: ''});
                                                }}
                                                className="p-1 text-brain-400 hover:text-white"
                                                title={t('edit_title')}
                                              >
                                                <Settings className="w-3 h-3" />
                                              </button>
                                              <button
                                                type="button"
                                                onClick={(e) => {
                                                  e.preventDefault();
                                                  e.stopPropagation();
                                                  handleDeleteKey(existingKey.id);
                                                }}
                                                className="p-1 text-red-400 hover:text-red-300"
                                                title={t('delete_title')}
                                              >
                                                <Trash2 className="w-3 h-3" />
                                              </button>
                                            </div>
                                          ) : (
                                            <button
                                              type="button"
                                              onClick={(e) => {
                                                e.preventDefault();
                                                e.stopPropagation();
                                                setEditingKey({provider: provider.id, key_name: keyConfig.key_name, value: ''});
                                              }}
                                              className="flex items-center gap-1 text-xs text-purple-400 hover:text-purple-300"
                                            >
                                              <Plus className="w-3 h-3" />
                                              {t('add_button')}
                                            </button>
                                          )}
                                        </div>
                                      );
                                    })}
                                  </div>
                                  
                                  {/* Contacts (for providers that support it) */}
                                  {provider.contacts_support && (
                                    <div className="mt-3 pt-3 border-t border-brain-700/50">
                                      <div className="flex items-center gap-2 mb-2">
                                        <Users className="w-3 h-3 text-brain-500" />
                                        <span className="text-xs text-brain-400">{t('contacts_label')}</span>
                                      </div>

                                      {/* One-click Telegram link via platform bot */}
                                      {provider.id === 'telegram' && (
                                        <div className="mb-2">
                                          <div className="flex items-center gap-2 flex-wrap">
                                          <button
                                            type="button"
                                            disabled={tgLinking}
                                            onClick={(e) => {
                                              e.preventDefault();
                                              e.stopPropagation();
                                              // Открываем вкладку СИНХРОННО (жест ещё активен) — иначе
                                              // popup-блокировщик зарежет открытие после await fetch.
                                              const pre = typeof window !== 'undefined'
                                                ? window.open('about:blank', '_blank') : null;
                                              handleConnectTelegram(pre);
                                            }}
                                            className="flex items-center gap-1.5 text-xs px-2 py-1 rounded bg-sky-600/20 border border-sky-500/40 text-sky-300 hover:bg-sky-600/30 disabled:opacity-50"
                                          >
                                            {tgLinking ? t('tg_connect_loading') : t('tg_connect_button')}
                                          </button>
                                          <button
                                            type="button"
                                            disabled={tgTesting}
                                            onClick={(e) => { e.preventDefault(); e.stopPropagation(); handleTestTelegram(); }}
                                            title={t('tg_test_title')}
                                            className="flex items-center gap-1.5 text-xs px-2 py-1 rounded bg-emerald-600/20 border border-emerald-500/40 text-emerald-300 hover:bg-emerald-600/30 disabled:opacity-50"
                                          >
                                            <Send className="w-3 h-3" />
                                            {tgTesting ? t('tg_test_loading') : t('tg_test_button')}
                                          </button>
                                          </div>
                                          {tgTestNote && (
                                            <div className={`text-[11px] mt-1 ${tgTestNote.ok ? 'text-emerald-400' : 'text-amber-400'}`}>
                                              {tgTestNote.ok ? '✅ ' : '⚠️ '}{tgTestNote.msg}
                                            </div>
                                          )}
                                          {tgLinkNote && (
                                            <div className="text-[11px] text-brain-400 mt-1">{tgLinkNote}</div>
                                          )}
                                          {tgDeeplink && (
                                            <a
                                              href={tgDeeplink}
                                              target="_blank"
                                              rel="noopener noreferrer"
                                              className="inline-flex items-center gap-1.5 text-[11px] mt-1 px-2 py-1 rounded bg-sky-600/30 border border-sky-500/50 text-sky-200 hover:bg-sky-600/40"
                                            >
                                              {t('tg_open_bot_link')}
                                            </a>
                                          )}
                                        </div>
                                      )}

                                      {/* Existing contacts */}
                                      {integration?.contacts.map((contact, idx) => (
                                        <div key={idx} className="flex items-center gap-2 text-xs mb-1">
                                          <span className="text-brain-300">{contact.display_name || contact.value}</span>
                                          <span className="text-brain-500">({contact.type})</span>
                                          <button
                                            type="button"
                                            onClick={(e) => {
                                              e.preventDefault();
                                              e.stopPropagation();
                                              handleDeleteKey(contact.id);
                                            }}
                                            className="p-0.5 text-red-400 hover:text-red-300 ml-auto"
                                          >
                                            <Trash2 className="w-3 h-3" />
                                          </button>
                                        </div>
                                      ))}
                                      
                                      {/* Add contact form */}
                                      {addingContact?.provider === provider.id ? (
                                        <div className="flex items-center gap-2 mt-2">
                                          <select
                                            value={addingContact.type}
                                            onChange={(e) => setAddingContact({...addingContact, type: e.target.value})}
                                            className="bg-brain-900 border border-brain-600 rounded px-2 py-1 text-xs text-white"
                                          >
                                            {provider.id === 'telegram' && (
                                              <>
                                                <option value="user_id">User ID</option>
                                                <option value="username">Username</option>
                                                <option value="chat_id">Chat ID</option>
                                              </>
                                            )}
                                            {provider.id === 'gmail' && <option value="email">Email</option>}
                                            {provider.id === 'slack' && (
                                              <>
                                                <option value="channel">Channel</option>
                                                <option value="user">User</option>
                                              </>
                                            )}
                                          </select>
                                          <input
                                            type="text"
                                            value={newContactValue}
                                            onChange={(e) => setNewContactValue(e.target.value)}
                                            placeholder={t('value_placeholder')}
                                            autoComplete="off"
                                            className="flex-1 bg-brain-900 border border-brain-600 rounded px-2 py-1 text-xs text-white"
                                          />
                                          <input
                                            type="text"
                                            value={newContactName}
                                            onChange={(e) => setNewContactName(e.target.value)}
                                            placeholder={t('name_placeholder')}
                                            autoComplete="off"
                                            className="w-24 bg-brain-900 border border-brain-600 rounded px-2 py-1 text-xs text-white"
                                          />
                                          <button type="button" onClick={(e) => { e.preventDefault(); e.stopPropagation(); handleAddContact(); }} className="p-1 text-green-400"><Check className="w-3 h-3" /></button>
                                          <button type="button" onClick={(e) => { e.preventDefault(); e.stopPropagation(); setAddingContact(null); }} className="p-1 text-red-400"><X className="w-3 h-3" /></button>
                                        </div>
                                      ) : (
                                        <button
                                          type="button"
                                          onClick={(e) => {
                                            e.preventDefault();
                                            e.stopPropagation();
                                            setAddingContact({provider: provider.id, type: provider.id === 'telegram' ? 'user_id' : 'email'});
                                          }}
                                          className="flex items-center gap-1 text-xs text-purple-400 hover:text-purple-300 mt-1"
                                        >
                                          <Plus className="w-3 h-3" />
                                          {t('add_contact')}
                                        </button>
                                      )}
                                    </div>
                                  )}
                                </div>
                              </div>
                            </div>
                          );
                        })}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-4 border-t border-brain-700/50 bg-brain-900/50 flex-shrink-0">
          <div className="flex items-center justify-between text-xs text-brain-500">
            <span>
              {t('footer_progress', {
                configured: integrations.filter(i => i.configured).length,
                total: Object.keys(categories).reduce((acc, cat) => acc + (categories[cat]?.providers.length || 0), 0)
              })}
            </span>
            <span>{t('encrypted_note')}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

