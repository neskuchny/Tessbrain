'use client';

// W44: dropdown переключения языка интерфейса.
// Использует next-intl router + Cookie для persist выбранного locale.

import { Globe } from 'lucide-react';
import { useLocale, useTranslations } from 'next-intl';
import { usePathname, useRouter } from 'next/navigation';
import { useState, useTransition } from 'react';
import {
  LOCALE_FLAGS,
  LOCALE_NAMES,
  LOCALES,
  type Locale,
} from '../i18n/config';

export function LanguageSwitcher() {
  const t = useTranslations('language_switcher');
  const router = useRouter();
  const pathname = usePathname();
  const currentLocale = useLocale() as Locale;
  const [isPending, startTransition] = useTransition();
  const [isOpen, setIsOpen] = useState(false);

  function handleSwitch(next: Locale) {
    if (next === currentLocale) {
      setIsOpen(false);
      return;
    }
    // Replace current locale segment в URL: /ru/foo → /en/foo.
    const segments = pathname.split('/').filter(Boolean);
    if (segments.length > 0 && (LOCALES as readonly string[]).includes(segments[0])) {
      segments[0] = next;
    } else {
      segments.unshift(next);
    }
    const newPath = '/' + segments.join('/');

    // Cookie сохраняет выбор на 365 дней.
    document.cookie = `NEXT_LOCALE=${next};path=/;max-age=${60 * 60 * 24 * 365};SameSite=Lax`;

    startTransition(() => {
      router.replace(newPath);
      setIsOpen(false);
    });
  }

  return (
    <div className="relative inline-block">
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        disabled={isPending}
        aria-label={t('label')}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        title={LOCALE_NAMES[currentLocale]}
        className="inline-flex items-center gap-1 px-2 py-1.5 rounded-md
                   bg-brain-800/40 hover:bg-brain-700/60 border border-brain-700/50
                   text-xs font-mono text-brain-100 transition-colors
                   disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <Globe size={14} aria-hidden />
        <span>{LOCALE_FLAGS[currentLocale]}</span>
      </button>

      {isOpen && (
        <div
          role="listbox"
          aria-label={t('label')}
          className="absolute right-0 mt-1 min-w-[160px] py-1
                     bg-brain-900 border border-brain-700 rounded-md shadow-xl z-50"
        >
          {LOCALES.map((loc) => (
            <button
              key={loc}
              role="option"
              aria-selected={loc === currentLocale}
              onClick={() => handleSwitch(loc)}
              disabled={isPending}
              className={`w-full text-left px-3 py-1.5 text-xs font-mono
                          flex items-center gap-2 transition-colors
                          ${
                            loc === currentLocale
                              ? 'bg-brain-700/40 text-white'
                              : 'text-brain-200 hover:bg-brain-800/60'
                          }`}
            >
              <span>{LOCALE_FLAGS[loc]}</span>
              <span>{LOCALE_NAMES[loc]}</span>
              {loc === currentLocale && (
                <span className="ml-auto text-brain-400">✓</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
