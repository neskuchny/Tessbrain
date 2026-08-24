import { useState, useEffect, useRef } from 'react';
import { useTranslations } from 'next-intl';
import { TrendingUp, PieChart, Activity, RefreshCw, FileText, Zap, ChevronDown, Coins, Brain, MessageSquare, Layout } from 'lucide-react';

interface UsageStats {
  period: string;
  total: {
    tokens: number;
    cost: number;
    cost_formatted: string;
  };
  by_agent_mode: Record<string, {
    requests: number;
    tokens: number;
    cost: number;
    cost_formatted: string;
  }>;
  by_model: Record<string, {
    requests: number;
    tokens: number;
    cost: number;
    cost_formatted: string;
  }>;
}

export default function UsageIndicator({ embedded = false }: { embedded?: boolean } = {}) {
  const t = useTranslations('usage_indicator');
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const fetchStats = async () => {
    try {
      // БАГ был двойной: ключ 'tessent_token' не существует (реальный —
      // tessent_access_token) → Bearer не уходил; и user_id= слался пустым.
      const { authHeaders, getUserIdFromToken } = await import('@/lib/authFetch');
      const userId = getUserIdFromToken() || localStorage.getItem('tessent_user_id');
      if (!userId) return; // без юзера запрос бессмыслен

      const res = await fetch(
        `/api/v1/usage/by-operation?period=today&user_id=${encodeURIComponent(userId)}`,
        { headers: authHeaders() });
      if (res.ok) {
        const data = await res.json();
        
        // Умножаем только стоимость на 5 для отображения (токены остаются без изменений)
        const costMultiplier = 5;
        
        // Умножаем общую стоимость
        data.total.cost = data.total.cost * costMultiplier;
        data.total.cost_formatted = `$${data.total.cost.toFixed(4)}`;
        
        // Умножаем стоимость по категориям (agent_mode)
        for (const mode in data.by_agent_mode) {
          data.by_agent_mode[mode].cost = data.by_agent_mode[mode].cost * costMultiplier;
          data.by_agent_mode[mode].cost_formatted = `$${data.by_agent_mode[mode].cost.toFixed(4)}`;
        }
        
        // Умножаем стоимость по моделям
        if (data.by_model) {
          for (const model in data.by_model) {
            data.by_model[model].cost = data.by_model[model].cost * costMultiplier;
            data.by_model[model].cost_formatted = `$${data.by_model[model].cost.toFixed(4)}`;
          }
        }
        
        setStats(data);
      }
    } catch (error) {
      console.error('Failed to fetch usage stats:', error);
    }
  };

  // Загружаем статистику при монтировании и периодически
  useEffect(() => {
    fetchStats();
    
    const interval = setInterval(fetchStats, 30000); // Каждые 30 сек
    
    // Слушаем событие обновления статистики (если другие компоненты будут его диспатчить)
    const handleUpdate = () => fetchStats();
    window.addEventListener('usage_updated', handleUpdate);
    
    return () => {
      clearInterval(interval);
      window.removeEventListener('usage_updated', handleUpdate);
    };
  }, []);

  // Закрытие при клике вне
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const getAgentIcon = (mode: string) => {
    switch (mode) {
      case 'brain': return <Brain className="w-3 h-3 text-purple-400" />;
      case 'mark': return <TrendingUp className="w-3 h-3 text-orange-400" />;
      case 'sync': return <RefreshCw className="w-3 h-3 text-blue-400" />;
      case 'documents': return <FileText className="w-3 h-3 text-green-400" />;
      case 'templates': return <FileText className="w-3 h-3 text-indigo-400" />;
      case 'automation': return <Zap className="w-3 h-3 text-yellow-400" />;
      case 'chat': return <MessageSquare className="w-3 h-3 text-pink-400" />;
      case 'board': return <Layout className="w-3 h-3 text-teal-400" />;
      default: return <Activity className="w-3 h-3 text-gray-400" />;
    }
  };

  const getAgentLabel = (mode: string) => {
    const labels: Record<string, string> = {
      'brain': t('cat_brain'),
      'mark': t('cat_mark'),
      'sync': t('cat_sync'),
      'documents': t('cat_documents'),
      'templates': t('cat_templates'),
      'automation': t('cat_automation'),
      'chat': t('cat_chat'),
      'board': t('cat_board'),
      'knowledge_extraction': t('cat_knowledge_extraction'),
      'night_analysis': t('cat_night_analysis')
    };
    return labels[mode] || mode;
  };

  if (!stats) return null;

  // Разбивка по агент-режимам — общий кусок для обоих вариантов рендера.
  const breakdownRows = Object.keys(stats.by_agent_mode).length === 0 ? (
    <div className="text-center py-8 text-brain-500 text-sm">
      {t('no_usage_today')}
    </div>
  ) : (
    <div className="space-y-1">
      {Object.entries(stats.by_agent_mode)
        .sort(([, a], [, b]) => b.cost - a.cost)
        .map(([mode, data]) => (
          <div key={mode} className="flex items-center justify-between p-2 hover:bg-brain-800 rounded-lg group">
            <div className="flex items-center gap-2">
              <div className="p-1.5 bg-brain-800 rounded-md group-hover:bg-brain-700 transition-colors">
                {getAgentIcon(mode)}
              </div>
              <div>
                <div className="text-xs font-medium text-brain-200">{getAgentLabel(mode)}</div>
                <div className="text-[10px] text-brain-500">{t('requests_count', { count: data.requests })}</div>
              </div>
            </div>
            <div className="text-right">
              <div className="text-xs font-medium text-brain-300 font-mono">{data.cost_formatted}</div>
              <div className="text-[10px] text-brain-500">{data.tokens.toLocaleString()} tok</div>
            </div>
          </div>
        ))
      }
    </div>
  );

  // Встроенный вариант: строка внутри меню аккаунта (без своего absolute-поповера).
  if (embedded) {
    return (
      <div className="w-full">
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="w-full flex items-center justify-between px-3 py-2 rounded-lg hover:bg-brain-800/60 transition-colors"
          title={t('title_button_tooltip')}
        >
          <span className="flex items-center gap-2 text-sm text-brain-200">
            <Coins className={`w-4 h-4 ${stats.total.cost > 0.5 ? 'text-amber-400' : 'text-brain-400'}`} />
            {t('popover_title')}
          </span>
          <span className="flex items-center gap-1.5">
            <span className="text-sm font-mono font-semibold text-green-400">{stats.total.cost_formatted}</span>
            <ChevronDown className={`w-3.5 h-3.5 text-brain-500 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
          </span>
        </button>
        {isOpen && (
          <div className="mt-1 mb-1 max-h-[240px] overflow-y-auto custom-scrollbar px-1">
            <div className="flex items-center justify-between px-2 pb-1 text-[10px] text-brain-500">
              <span>{t('tokens_count', { count: stats.total.tokens.toLocaleString() })}</span>
              <span>{t('auto_updating')}</span>
            </div>
            {breakdownRows}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`flex items-center gap-2 px-3 py-1.5 rounded-lg transition-all border ${
          isOpen
            ? 'bg-brain-800 border-brain-600 text-white'
            : 'bg-brain-900/50 border-brain-800 text-brain-300 hover:bg-brain-800 hover:text-brain-200'
        }`}
        title={t('title_button_tooltip')}
      >
        <Coins className={`w-4 h-4 ${stats.total.cost > 0.5 ? 'text-amber-400' : 'text-brain-400'}`} />
        <span className="text-xs font-medium font-mono">{stats.total.cost_formatted}</span>
      </button>

      {isOpen && (
        <div className="absolute right-0 top-full mt-2 w-80 bg-brain-900 border border-brain-700 rounded-xl shadow-2xl z-50 overflow-hidden animate-in fade-in zoom-in-95 duration-100">
          <div className="p-4 border-b border-brain-700/50 bg-brain-800/30">
            <div className="flex items-center justify-between mb-1">
              <h3 className="text-sm font-semibold text-white">{t('popover_title')}</h3>
              <span className="text-xs text-brain-400 bg-brain-800 px-2 py-0.5 rounded-full">
                {t('tokens_count', { count: stats.total.tokens.toLocaleString() })}
              </span>
            </div>
            <div className="text-2xl font-bold text-green-400 font-mono">
              {stats.total.cost_formatted}
            </div>
          </div>

          <div className="max-h-[300px] overflow-y-auto custom-scrollbar p-2">
            {breakdownRows}
          </div>

          <div className="p-2 border-t border-brain-700/50 bg-brain-950/30 text-xs text-brain-500 text-center">
            {t('auto_updating')}
          </div>
        </div>
      )}
    </div>
  );
}
