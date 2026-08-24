'use client'

import { useEffect, useRef, useState } from 'react'
import { LineChart, Line, ResponsiveContainer } from 'recharts'

/**
 * AnimatedNumber — count-up анимация при изменении value.
 *
 * Wow-screen урок (OSIRIS / Understand-Anything): «движение = жизнь».
 * Без tweening числа дёргаются мгновенно (142 → 143), пользователь не
 * замечает изменения. С tweening — глаз ловит «считается на лету».
 *
 * Сделано без framer-motion (хотя он установлен) — pure JS через
 * requestAnimationFrame с ease-out cubic. ~30 строк, никаких deps,
 * легко тестировать.
 *
 * Особенности:
 *   - Первый рендер: моментально показывает значение (без анимации от 0)
 *   - Последующие изменения: плавная анимация duration=600ms
 *   - locale-aware форматирование через toLocaleString
 *   - decimals=0 для int, decimals=2 для cost
 */

interface AnimatedNumberProps {
  value: number
  duration?: number
  decimals?: number
  prefix?: string
  suffix?: string
  className?: string
  /** Локаль для форматирования. Default — 'ru-RU' (пробелы между разрядами). */
  locale?: string
}

export function AnimatedNumber({
  value,
  duration = 600,
  decimals = 0,
  prefix = '',
  suffix = '',
  className = '',
  locale = 'ru-RU',
}: AnimatedNumberProps) {
  const [display, setDisplay] = useState(value)
  const fromRef = useRef(value)
  const startedAtRef = useRef<number | null>(null)
  const rafRef = useRef<number | null>(null)
  const firstRenderRef = useRef(true)

  useEffect(() => {
    // Первый рендер — без анимации (избегаем «0 → реальное число»)
    if (firstRenderRef.current) {
      firstRenderRef.current = false
      setDisplay(value)
      fromRef.current = value
      return
    }

    if (value === fromRef.current) return

    const from = fromRef.current
    const to = value
    fromRef.current = value
    startedAtRef.current = performance.now()

    const tick = (now: number) => {
      const elapsed = now - (startedAtRef.current ?? now)
      const t = Math.min(1, elapsed / duration)
      // ease-out cubic: быстро в начале, плавно в конце
      const eased = 1 - Math.pow(1 - t, 3)
      const current = from + (to - from) * eased
      setDisplay(current)
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick)
      } else {
        setDisplay(to)
        rafRef.current = null
      }
    }
    rafRef.current = requestAnimationFrame(tick)

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
    }
  }, [value, duration])

  const formatted =
    decimals === 0
      ? Math.round(display).toLocaleString(locale)
      : display.toLocaleString(locale, {
          minimumFractionDigits: decimals,
          maximumFractionDigits: decimals,
        })

  return (
    <span className={className}>
      {prefix}
      {formatted}
      {suffix}
    </span>
  )
}

/**
 * MiniSparkline — компактный график тренда для hero card / big numbers.
 *
 * Recharts ResponsiveContainer + Line. ~50×30px типичный размер,
 * без осей, без tooltip — чистый visual hint.
 *
 * Использование:
 *   <MiniSparkline data={[1.2, 0.8, 1.5, 2.1, 1.9]} color="#36a7f6" />
 */
interface MiniSparklineProps {
  data: number[]
  color?: string
  height?: number
  width?: number
}

export function MiniSparkline({
  data,
  color = '#36a7f6',
  height = 32,
  width = 80,
}: MiniSparklineProps) {
  if (!data || data.length < 2) {
    return <div style={{ width, height }} />
  }
  const chartData = data.map((v, i) => ({ v, i }))
  return (
    <div style={{ width, height }} className="inline-block">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
          <Line
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/**
 * TrendIndicator — стрелка + процент изменения.
 *
 * Сейчас показывает разницу (current - previous). В будущем можно
 * расширить под «vs прошлая неделя» / «vs среднее за месяц».
 */
interface TrendIndicatorProps {
  current: number
  previous: number
  /** «обратная» метрика: для cost — меньше = лучше → зелёное. */
  invert?: boolean
}

export function TrendIndicator({
  current,
  previous,
  invert = false,
}: TrendIndicatorProps) {
  if (previous === 0) return null
  const delta = current - previous
  const pct = (delta / previous) * 100
  if (Math.abs(pct) < 1) return null  // не шумим на <1% изменениях

  const goodDirection = invert ? delta < 0 : delta > 0
  const color = goodDirection ? 'text-green-400' : 'text-orange-400'
  const arrow = delta > 0 ? '↑' : '↓'
  return (
    <span className={`text-xs ${color} ml-1`}>
      {arrow}
      {Math.abs(pct).toFixed(0)}%
    </span>
  )
}
