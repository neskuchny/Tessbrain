'use client';

// Единое меню аккаунта: сворачивает утилитарные иконки шапки (шина данных,
// интеграции, Daily Pulse, LLM-профили, компания), почту/статус, расход
// токенов и выход в один выпадающий список справа — чтобы верхняя панель
// не наезжала сама на себя.

import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
  Share2, Settings, Activity, Cpu, Building2, LogOut, ChevronDown, CreditCard, Landmark,
} from 'lucide-react';
import UsageIndicator from './UsageIndicator';
import IdentityLinkCard from './IdentityLinkCard';

interface AccountMenuProps {
  userEmail: string | null;
  online: boolean;
  onOpenDataBus: () => void;
  onOpenIntegrations: () => void;
  onOpenDailyPulse: () => void;
  onOpenLlmProfiles: () => void;
  onOpenOrg: () => void;
  onOpenBilling: () => void;
  onOpenHolding: () => void;
  onLogout: () => void;
}

export default function AccountMenu({
  userEmail,
  online,
  onOpenDataBus,
  onOpenIntegrations,
  onOpenDailyPulse,
  onOpenLlmProfiles,
  onOpenOrg,
  onOpenBilling,
  onOpenHolding,
  onLogout,
}: AccountMenuProps) {
  const t = useTranslations('main_page');
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Закрытие по клику вне и по Escape.
  useEffect(() => {
    if (!isOpen) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setIsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [isOpen]);

  const initial = (userEmail || '?').trim().charAt(0).toUpperCase();

  const tools: Array<{
    key: string; icon: React.ReactNode; label: string; hoverColor: string;
    onClick: () => void;
  }> = [
    { key: 'billing', icon: <CreditCard className="w-4 h-4" />, label: t('billing_menu_label'),
      hoverColor: 'group-hover:text-amber-400', onClick: onOpenBilling },
    { key: 'org', icon: <Building2 className="w-4 h-4" />, label: t('company_title_attr'),
      hoverColor: 'group-hover:text-purple-400', onClick: onOpenOrg },
    { key: 'holding', icon: <Landmark className="w-4 h-4" />, label: t('holding_console_menu_label'),
      hoverColor: 'group-hover:text-purple-400', onClick: onOpenHolding },
    { key: 'integrations', icon: <Settings className="w-4 h-4" />, label: t('integrations_title_attr'),
      hoverColor: 'group-hover:text-purple-400', onClick: onOpenIntegrations },
    { key: 'llm', icon: <Cpu className="w-4 h-4" />, label: t('llm_profiles_title_attr'),
      hoverColor: 'group-hover:text-purple-400', onClick: onOpenLlmProfiles },
    { key: 'databus', icon: <Share2 className="w-4 h-4" />, label: t('data_bus_menu_label'),
      hoverColor: 'group-hover:text-purple-400', onClick: onOpenDataBus },
    { key: 'pulse', icon: <Activity className="w-4 h-4" />, label: t('daily_pulse_admin_title_attr'),
      hoverColor: 'group-hover:text-cyan-400', onClick: onOpenDailyPulse },
  ];

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setIsOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        title={t('account_menu')}
        className={`flex items-center gap-1.5 pl-1 pr-1.5 py-1 rounded-full border transition-all ${
          isOpen
            ? 'bg-brain-800 border-brain-600'
            : 'bg-brain-900/50 border-brain-800 hover:bg-brain-800 hover:border-brain-600'
        }`}
      >
        <span className="relative flex items-center justify-center w-6 h-6 rounded-full bg-gradient-to-br from-brain-600 to-purple-600 text-white text-xs font-bold">
          {initial}
          <span
            className={`absolute -bottom-0.5 -right-0.5 w-2 h-2 rounded-full ring-2 ring-brain-950 ${
              online ? 'bg-green-500' : 'bg-yellow-500'
            }`}
          />
        </span>
        <ChevronDown className={`w-3.5 h-3.5 text-brain-400 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-2 w-72 bg-brain-900 border border-brain-700 rounded-xl shadow-2xl z-50 overflow-hidden animate-in fade-in zoom-in-95 duration-100"
        >
          {/* Профиль: почта + статус — теперь в одном месте */}
          <div className="p-3 border-b border-brain-700/50 bg-brain-800/30 flex items-center gap-3">
            <span className="relative flex items-center justify-center w-9 h-9 rounded-full bg-gradient-to-br from-brain-600 to-purple-600 text-white text-sm font-bold flex-shrink-0">
              {initial}
            </span>
            <div className="min-w-0">
              <div className="text-sm font-medium text-white truncate">
                {userEmail || t('account_menu')}
              </div>
              <div className="flex items-center gap-1.5 text-xs text-brain-400">
                <span className={`w-1.5 h-1.5 rounded-full ${online ? 'bg-green-500' : 'bg-yellow-500'}`} />
                {online ? t('status_online') : t('status_connecting')}
              </div>
            </div>
          </div>

          {/* Сшивка «это я»: аккаунт ↔ Person-сущность графа (CogniLayer Ф0) */}
          <IdentityLinkCard />

          {/* Расход токенов — встроенным вариантом, без своего поповера */}
          <div className="p-1 border-b border-brain-700/50">
            <UsageIndicator embedded />
          </div>

          {/* Инструменты — свёрнутые иконки шапки */}
          <div className="p-1">
            <div className="px-3 pt-1.5 pb-1 text-[10px] uppercase tracking-wide text-brain-500">
              {t('account_tools_section')}
            </div>
            {tools.map((tool) => (
              <button
                key={tool.key}
                role="menuitem"
                onClick={() => { tool.onClick(); setIsOpen(false); }}
                className="group w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-brain-200 hover:bg-brain-800/60 transition-colors"
              >
                <span className={`text-brain-400 ${tool.hoverColor} transition-colors`}>{tool.icon}</span>
                <span>{tool.label}</span>
              </button>
            ))}
          </div>

          {/* Выход */}
          <div className="p-1 border-t border-brain-700/50">
            <button
              role="menuitem"
              onClick={() => { setIsOpen(false); onLogout(); }}
              className="group w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-brain-300 hover:bg-red-500/10 hover:text-red-400 transition-colors"
            >
              <LogOut className="w-4 h-4" />
              <span>{t('logout_title_attr')}</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
