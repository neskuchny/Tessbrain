/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './banana/**/*.{js,ts,jsx,tsx,mdx}',
    './sima/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        // Tessbrain цветовая схема
        brain: {
          50: '#f0f7ff',
          100: '#e0effe',
          200: '#b9dffd',
          300: '#7cc5fb',
          400: '#36a7f6',
          500: '#0c8ce7',
          600: '#006fc5',
          700: '#0159a0',
          800: '#064b84',
          900: '#0a3f6e',
          950: '#072849',
        },
        // SIMA цветовая схема
        // Палитра через CSS-переменные (app/globals.css): тёмная по
        // умолчанию, светлая (из прототипа Sima Remix) через
        // html[data-sima-theme="light"]. RGB-триплеты — чтобы работали
        // модификаторы прозрачности (bg-sima-primary/10).
        sima: {
          bg: 'rgb(var(--sima-bg) / <alpha-value>)',
          surface: 'rgb(var(--sima-surface) / <alpha-value>)',
          surfaceLight: 'rgb(var(--sima-surface-light) / <alpha-value>)',
          border: 'rgb(var(--sima-border) / <alpha-value>)',
          borderLight: 'rgb(var(--sima-border-light) / <alpha-value>)',
          primary: 'rgb(var(--sima-primary) / <alpha-value>)',
          primaryHover: 'rgb(var(--sima-primary-hover) / <alpha-value>)',
          accent: 'rgb(var(--sima-accent) / <alpha-value>)',
          accentGreen: 'rgb(var(--sima-accent-green) / <alpha-value>)',
          accentYellow: 'rgb(var(--sima-accent-yellow) / <alpha-value>)',
          accentRed: 'rgb(var(--sima-accent-red) / <alpha-value>)',
          accentPurple: 'rgb(var(--sima-accent-purple) / <alpha-value>)',
          text: 'rgb(var(--sima-text) / <alpha-value>)',
          textMuted: 'rgb(var(--sima-text-muted) / <alpha-value>)',
          textDim: 'rgb(var(--sima-text-dim) / <alpha-value>)',
        },
        // Акцентные цвета для типов сущностей
        entity: {
          person: '#4CAF50',
          project: '#2196F3',
          meeting: '#FF9800',
          task: '#9C27B0',
          decision: '#F44336',
        }
      },
      fontFamily: {
        sans: ['JetBrains Mono', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'glow': 'glow 2s ease-in-out infinite alternate',
      },
      keyframes: {
        glow: {
          '0%': { boxShadow: '0 0 5px rgba(12, 140, 231, 0.5)' },
          '100%': { boxShadow: '0 0 20px rgba(12, 140, 231, 0.8)' },
        }
      }
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
}


