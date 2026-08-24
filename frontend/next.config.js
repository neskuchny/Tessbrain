// W44: next-intl plugin enabled — поддержка [locale] routing + getMessages.
const createNextIntlPlugin = require('next-intl/plugin');
const withNextIntl = createNextIntlPlugin('./i18n/request.ts');

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // standalone output → самодостаточный .next/standalone/server.js + .next/static
  // для лёгкого Docker-образа (без полного ~750M node_modules).
  output: 'standalone',
  // Proxy API requests to backend
  async rewrites() {
    // Сервер-сайд прокси /api → backend. В Docker backend в той же сети как
    // http://api:8000; локально — http://localhost:8000. Семантика afterFiles
    // (возврат массива) → собственные роуты app/api/* (banana) матчатся раньше.
    const backend = process.env.BACKEND_URL || 'http://localhost:8000';
    return [
      {
        source: '/api/:path*',
        destination: `${backend}/api/:path*`,
      },
    ]
  },
  // Увеличиваем timeout для API запросов (по умолчанию ~60 сек)
  // Это важно для длительных запросов к LLM
  experimental: {
    // 4 минуты — под глубокие multi-query / deep-dive запросы к LLM.
    // Должно быть >= client AbortController в ChatPanel (240с), иначе прокси
    // рвёт соединение раньше клиента и пользователь видит немой сбой.
    proxyTimeout: 240000,
  },
}

module.exports = withNextIntl(nextConfig);
