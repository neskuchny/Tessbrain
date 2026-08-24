// W44: next-intl getRequestConfig — loads messages by locale.
import { getRequestConfig } from 'next-intl/server';
import { DEFAULT_LOCALE, isValidLocale } from './config';

export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale;
  const locale = isValidLocale(requested) ? requested : DEFAULT_LOCALE;

  // Static-import: bundler видит файлы, treeshake'ит лишнее.
  const messages = (await import(`../messages/${locale}.json`)).default;

  return {
    locale,
    messages,
    timeZone: 'UTC',
  };
});
