// Общая обёртка юридических страниц (оферта / соглашение). Серверный
// компонент: единый header с возвратом, контейнер, стили в brain-теме.
import Link from 'next/link'
import { ArrowLeft } from 'lucide-react'
import { LEGAL } from '@/lib/legal'

export default function LegalLayout({
  locale,
  title,
  subtitle,
  children,
}: {
  locale: string
  title: string
  subtitle?: string
  children: React.ReactNode
}) {
  return (
    <div className="min-h-screen">
      <header className="border-b border-brain-800/60 bg-brain-950/70 backdrop-blur sticky top-0 z-10">
        <div className="max-w-3xl mx-auto px-5 py-4 flex items-center justify-between gap-4">
          <Link href={`/${locale}`} className="inline-flex items-center gap-2 text-brain-300 hover:text-white text-sm">
            <ArrowLeft className="w-4 h-4" /> {LEGAL.product}
          </Link>
          <div className="flex items-center gap-3 text-xs text-brain-500">
            <Link href={`/${locale}/offer`} className="hover:text-brain-200">Оферта</Link>
            <span className="opacity-40">·</span>
            <Link href={`/${locale}/terms`} className="hover:text-brain-200">Соглашение</Link>
          </div>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-5 py-10">
        <h1 className="text-2xl md:text-3xl font-semibold text-white text-balance">{title}</h1>
        {subtitle && <p className="mt-2 text-sm text-brain-400">{subtitle}</p>}
        <p className="mt-1 text-xs text-brain-600">Редакция от {LEGAL.updated}</p>

        <article className="mt-8 space-y-8 text-[15px] leading-relaxed text-brain-200">
          {children}
        </article>

        <footer className="mt-14 pt-6 border-t border-brain-800/60 text-xs text-brain-500 space-y-1">
          <p>{LEGAL.seller.name} · {LEGAL.seller.address}</p>
          <p>ИНН/TIN {LEGAL.seller.inn} · <a href={`mailto:${LEGAL.seller.email}`} className="hover:text-brain-300">{LEGAL.seller.email}</a></p>
          <p className="pt-2">© {LEGAL.product}. Все права защищены.</p>
        </footer>
      </main>
    </div>
  )
}

// Мелкие типографские хелперы, чтобы страницы читались единообразно.
export function Section({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-white">
        <span className="text-brain-500 tabular-nums mr-2">{n}.</span>{title}
      </h2>
      <div className="space-y-2 text-brain-300">{children}</div>
    </section>
  )
}

export function P({ children }: { children: React.ReactNode }) {
  return <p>{children}</p>
}
