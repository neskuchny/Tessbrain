// W44: tessent_brain/frontend i18n config.
// Расширяется добавлением кода в LOCALES + создание messages/<code>.json.

export const LOCALES = ['ru', 'en'] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = 'ru';

export const LOCALE_NAMES: Record<Locale, string> = {
  ru: 'Русский',
  en: 'English',
};

export const LOCALE_FLAGS: Record<Locale, string> = {
  ru: '🇷🇺',
  en: '🇬🇧',
};

export function isValidLocale(value: string | undefined): value is Locale {
  return value !== undefined && (LOCALES as readonly string[]).includes(value);
}
