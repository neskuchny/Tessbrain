// W44: redirect `/` → `/<locale>/...` based on cookie / Accept-Language.
import createMiddleware from 'next-intl/middleware';
import { DEFAULT_LOCALE, LOCALES } from './i18n/config';

export default createMiddleware({
  locales: LOCALES,
  defaultLocale: DEFAULT_LOCALE,
  localePrefix: 'always',
  localeDetection: true,
});

export const config = {
  // Skip:
  //  - /api/* (backend proxy)
  //  - /_next/* (build artifacts)
  //  - /favicon, manifest, etc
  matcher: ['/((?!api|_next|.*\\..*).*)'],
};
