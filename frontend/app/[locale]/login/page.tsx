'use client'

import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations, useLocale } from 'next-intl'
import {
  Brain, Mail, Lock, User, Eye, EyeOff, Loader2,
  ArrowRight, Check, Zap, TrendingUp, Shield,
  Clock, FileText, Search, Users, Play, Menu, X,
  ChevronRight, Database, MessageSquare, BarChart3,
  Globe, Laptop, Smartphone
} from 'lucide-react'
import NeuralCanvas from '@/components/landing/NeuralCanvas'
import Reveal from '@/components/landing/Reveal'

// --- Types ---

type AuthMode = 'login' | 'register'

// --- Components ---

function AuthForm({ onBack }: { onBack: () => void }) {
  const t = useTranslations('login_page')
  const router = useRouter()
  const [mode, setMode] = useState<AuthMode>('login')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  
  // Form fields
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')

  // Check if already logged in
  useEffect(() => {
    const token = localStorage.getItem('tessent_access_token')
    if (token) {
      // Verify token
      fetch('/api/v1/auth/verify', {
        headers: { 'Authorization': `Bearer ${token}` }
      })
      .then(res => res.json())
      .then(data => {
        if (data.valid) {
          router.push('/')
        }
      })
      .catch(() => {
        // Token invalid, stay on login page
        localStorage.removeItem('tessent_access_token')
        localStorage.removeItem('tessent_refresh_token')
        localStorage.removeItem('tessent_user')
      })
    }
  }, [router])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setLoading(true)

    try {
      const endpoint = mode === 'login' ? '/api/v1/auth/login' : '/api/v1/auth/register'
      const body = mode === 'login' 
        ? { email, password }
        : { name, email, password }

      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })

      const data = await res.json()

      if (!res.ok) {
        throw new Error(data.detail || data.error || 'Authentication failed')
      }

      if (data.success && data.access_token) {
        // Смена аккаунта: чистим ВСЁ состояние прошлого юзера, иначе стейлный
        // tessent_user_id уезжал в запросы с новым токеном — часть эндпоинтов
        // отдавала данные другого аккаунта (мульти-тенант утечка)
        const prevId = localStorage.getItem('tessent_user_id')
        if (prevId && prevId !== data.user.id) {
          Object.keys(localStorage)
            .filter((k) => k.startsWith('tessent_'))
            .forEach((k) => localStorage.removeItem(k))
        }
        // Save tokens and user info
        localStorage.setItem('tessent_access_token', data.access_token)
        localStorage.setItem('tessent_refresh_token', data.refresh_token || '')
        localStorage.setItem('tessent_user', JSON.stringify(data.user))
        localStorage.setItem('tessent_user_id', data.user.id)
        
        // Redirect to main page
        router.push('/')
      } else if (data.success && !data.access_token) {
        // Registration requires email confirmation
        setError(t('registration_success'))
        setMode('login')
      } else {
        throw new Error(data.message || 'Authentication failed')
      }
    } catch (err: any) {
      setError(err.message || t('generic_error'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen neural-grid flex items-center justify-center p-4 relative z-50">
      <div className="absolute top-4 left-4">
        <button 
          onClick={onBack}
          className="flex items-center gap-2 text-brain-400 hover:text-white transition-colors"
        >
          <ArrowRight className="w-5 h-5 rotate-180" />
          {t('back_button')}
        </button>
      </div>
      
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center mb-4">
            <div className="relative">
              <Brain className="w-16 h-16 text-brain-400 brain-pulse" />
              <div className="absolute inset-0 bg-brain-500/20 blur-2xl rounded-full" />
            </div>
          </div>
          <h1 className="text-3xl font-bold text-gradient mb-2">Tessent Brain</h1>
          <p className="text-brain-400">{t('tagline')}</p>
        </div>

        {/* Auth Card */}
        <div className="card bg-brain-950/80 backdrop-blur-xl border border-brain-700/30">
          {/* Tabs */}
          <div className="flex mb-6">
            <button
              onClick={() => { setMode('login'); setError(null) }}
              className={`flex-1 py-3 text-center font-medium transition-colors rounded-l-lg ${
                mode === 'login'
                  ? 'bg-brain-600 text-white'
                  : 'bg-brain-900/50 text-brain-400 hover:text-brain-300'
              }`}
            >
              {t('tab_login')}
            </button>
            <button
              onClick={() => { setMode('register'); setError(null) }}
              className={`flex-1 py-3 text-center font-medium transition-colors rounded-r-lg ${
                mode === 'register'
                  ? 'bg-brain-600 text-white'
                  : 'bg-brain-900/50 text-brain-400 hover:text-brain-300'
              }`}
            >
              {t('tab_register')}
            </button>
          </div>

          {/* Error Message */}
          {error && (
            <div className={`mb-4 p-3 rounded-lg text-sm ${
              error.includes('successful') || error.includes('успешна') 
                ? 'bg-green-600/20 text-green-400 border border-green-600/30'
                : 'bg-red-600/20 text-red-400 border border-red-600/30'
            }`}>
              {error}
            </div>
          )}

          {/* Form */}
          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Name (only for register) */}
            {mode === 'register' && (
              <div>
                <label className="block text-sm font-medium text-brain-300 mb-1.5">
                  {t('field_name')}
                </label>
                <div className="relative">
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-brain-500" />
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder={t("name_placeholder")}
                    required={mode === 'register'}
                    className="w-full pl-11 pr-4 py-3 bg-brain-900 border border-brain-700/50 rounded-lg text-brain-200 placeholder-brain-500 focus:outline-none focus:border-brain-500 transition-colors"
                  />
                </div>
              </div>
            )}

            {/* Email */}
            <div>
              <label className="block text-sm font-medium text-brain-300 mb-1.5">
                Email
              </label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-brain-500" />
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="your@email.com"
                  required
                  className="w-full pl-11 pr-4 py-3 bg-brain-900 border border-brain-700/50 rounded-lg text-brain-200 placeholder-brain-500 focus:outline-none focus:border-brain-500 transition-colors"
                />
              </div>
            </div>

            {/* Password */}
            <div>
              <label className="block text-sm font-medium text-brain-300 mb-1.5">
                {t('field_password')}
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-brain-500" />
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                  minLength={6}
                  className="w-full pl-11 pr-12 py-3 bg-brain-900 border border-brain-700/50 rounded-lg text-brain-200 placeholder-brain-500 focus:outline-none focus:border-brain-500 transition-colors"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-brain-500 hover:text-brain-300 transition-colors"
                >
                  {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                </button>
              </div>
              {mode === 'register' && (
                <p className="mt-1 text-xs text-brain-500">{t("password_min_hint")}</p>
              )}
            </div>

            {/* Submit Button */}
            <button
              type="submit"
              disabled={loading}
              className="w-full py-3 bg-brain-600 hover:bg-brain-500 disabled:bg-brain-700 disabled:cursor-not-allowed text-white font-medium rounded-lg transition-colors flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <Loader2 className="w-5 h-5 animate-spin" />
                  {mode === 'login' ? t('login_loading') : t('register_loading')}
                </>
              ) : (
                mode === 'login' ? t('login_submit') : t('register_submit')
              )}
            </button>
          </form>

          {/* Divider */}
          <div className="my-6 flex items-center gap-4">
            <div className="flex-1 h-px bg-brain-700/50" />
            <span className="text-sm text-brain-500">{t('or')}</span>
            <div className="flex-1 h-px bg-brain-700/50" />
          </div>

          {/* Alternative login hint */}
          <p className="text-center text-sm text-brain-400">
            {t('use_credentials_from')}{' '}
            <span className="text-brain-300 font-medium">MeetFlow</span>
          </p>
        </div>

        {/* Footer */}
        <p className="text-center text-xs text-brain-500 mt-6">
          {t('copyright')}
        </p>
      </div>
    </div>
  )
}

function LandingPage({ onLogin }: { onLogin: () => void }) {
  const t = useTranslations('login_page')
  const locale = useLocale()
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  const scrollTo = (id: string) => {
    const el = document.getElementById(id)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth' })
      setMobileMenuOpen(false)
    }
  }

  return (
    <div className="min-h-screen bg-[#12121f] text-white overflow-x-hidden font-sans">
      {/* Wow-слой: живой нейро-мозг за контентом + кино-фактура (зерно, виньетка) */}
      <NeuralCanvas className="fixed inset-0 w-full h-full z-0 pointer-events-none" />
      <div className="landing-vignette" aria-hidden="true" />
      <div className="landing-grain" aria-hidden="true" />

      {/* Header */}
      <header className="fixed top-0 left-0 right-0 z-40 bg-[#1a1a2e]/90 backdrop-blur-md border-b border-white/5">
        <div className="container mx-auto px-4 md:px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="relative">
              <Brain className="w-8 h-8 text-[#4dc1c7]" />
              <div className="absolute inset-0 bg-[#4dc1c7]/40 blur-lg rounded-full animate-pulse" />
            </div>
            <span className="text-xl font-bold tracking-tight">TESSENT</span>
          </div>

          {/* Desktop Nav */}
          <nav className="hidden md:flex items-center gap-8">
            <button onClick={() => scrollTo('problem')} className="text-sm text-gray-300 hover:text-[#4dc1c7] transition-colors">{t("nav_problem")}</button>
            <button onClick={() => scrollTo('solution')} className="text-sm text-gray-300 hover:text-[#4dc1c7] transition-colors">{t("nav_solution")}</button>
            <button onClick={() => scrollTo('features')} className="text-sm text-gray-300 hover:text-[#4dc1c7] transition-colors">{t("nav_features")}</button>
            <button 
              onClick={onLogin}
              className="px-5 py-2 rounded-full border border-[#4dc1c7]/30 text-[#4dc1c7] hover:bg-[#4dc1c7]/10 transition-colors text-sm font-medium"
            >
              {t('nav_login')}
            </button>
            <button 
              onClick={() => scrollTo('cta')}
              className="px-5 py-2 rounded-full bg-[#4dc1c7] text-[#1a1a2e] hover:bg-[#3daeb3] transition-colors text-sm font-bold"
            >
              {t('nav_request_demo')}
            </button>
          </nav>

          {/* Mobile Menu Button */}
          <button 
            className="md:hidden p-2 text-gray-300"
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
          >
            {mobileMenuOpen ? <X /> : <Menu />}
          </button>
        </div>

        {/* Mobile Nav */}
        {mobileMenuOpen && (
          <div className="md:hidden absolute top-full left-0 right-0 bg-[#1a1a2e] border-b border-white/5 p-4 flex flex-col gap-4">
            <button onClick={() => scrollTo('problem')} className="text-left text-gray-300 py-2">{t("nav_problem")}</button>
            <button onClick={() => scrollTo('solution')} className="text-left text-gray-300 py-2">{t("nav_solution")}</button>
            <button onClick={() => scrollTo('features')} className="text-left text-gray-300 py-2">{t("nav_features")}</button>
            <div className="flex flex-col gap-3 mt-2">
              <button 
                onClick={onLogin}
                className="w-full py-3 rounded-lg border border-[#4dc1c7]/30 text-[#4dc1c7] font-medium"
              >
                {t('nav_login')}
              </button>
              <button 
                onClick={() => scrollTo('cta')}
                className="w-full py-3 rounded-lg bg-[#4dc1c7] text-[#1a1a2e] font-bold"
              >
                {t('nav_request_demo')}
              </button>
            </div>
          </div>
        )}
      </header>

      {/* Hero Section — стеклянная панель поверх живого мозга */}
      <section className="pt-32 pb-20 md:pt-40 md:pb-32 px-4 relative z-10 overflow-hidden min-h-[92vh] flex items-center">
        <div className="container mx-auto md:flex items-center gap-12 relative z-10">
          <div className="md:w-1/2 mb-12 md:mb-0">
            <div className="landing-hero-glass rounded-3xl p-8 md:p-10">
              <Reveal>
                <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-[#4dc1c7]/10 text-[#4dc1c7] text-xs font-medium mb-6 border border-[#4dc1c7]/20">
                  <span className="w-2 h-2 rounded-full bg-[#4dc1c7] animate-pulse" />
                  {t('hero_eyebrow')}
                </div>
              </Reveal>
              <Reveal delay={120}>
                <h1 className="text-4xl md:text-6xl font-bold leading-tight mb-6">
                  {t("hero_title_1")} <br/>
                  <span className="landing-title-gradient text-transparent bg-clip-text bg-gradient-to-r from-[#4dc1c7] via-[#a6a4ff] to-[#e94560]">{t("hero_title_2")}</span><br/>
                  {t("hero_title_3")}
                </h1>
              </Reveal>
              <Reveal delay={240}>
                <p className="text-xl text-gray-400 mb-8 leading-relaxed max-w-xl">
                  {t('hero_subtitle')}
                </p>
              </Reveal>
              <Reveal delay={360}>
                <div className="flex flex-col sm:flex-row gap-4">
                  <button
                    onClick={() => scrollTo('cta')}
                    className="px-8 py-4 rounded-lg bg-[#4dc1c7] text-[#1a1a2e] font-bold hover:bg-[#3daeb3] hover:shadow-[0_0_36px_rgba(77,193,199,0.45)] transition-all flex items-center justify-center gap-2 group"
                  >
                    {t('nav_request_demo')}
                    <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
                  </button>
                  <button
                    onClick={() => scrollTo('demo')}
                    className="px-8 py-4 rounded-lg border border-white/10 hover:bg-white/5 transition-all text-white font-medium flex items-center justify-center gap-2"
                  >
                    <Play className="w-5 h-5 fill-current" />
                    {t('hero_cta_how')}
                  </button>
                </div>
              </Reveal>
            </div>
          </div>

          {/* Visual: Abstract Split Screen Representation */}
          <div className="md:w-1/2 relative">
            <div className="relative rounded-2xl overflow-hidden border border-white/10 bg-[#0f0f1a] shadow-2xl shadow-[#4dc1c7]/10">
              <div className="aspect-video relative grid grid-cols-2">
                {/* Left: Office (Symbolic) */}
                <div className="bg-[#151525] p-6 flex flex-col justify-between border-r border-white/5 opacity-50 grayscale transition-all hover:grayscale-0">
                  <div className="flex items-center gap-2 mb-4">
                    <Clock className="w-5 h-5 text-gray-500" />
                    <span className="text-xs text-gray-500">18:00</span>
                  </div>
                  <div className="space-y-3">
                    <div className="h-2 w-3/4 bg-white/10 rounded" />
                    <div className="h-2 w-1/2 bg-white/10 rounded" />
                    <div className="h-2 w-5/6 bg-white/10 rounded" />
                  </div>
                  <div className="mt-auto pt-8 text-xs text-gray-500 text-center">
                    {t('office_empty')}
                  </div>
                </div>
                
                {/* Right: Tessent Interface */}
                <div className="bg-[#0c2e4e]/20 p-6 flex flex-col justify-between relative overflow-hidden group">
                  <div className="absolute inset-0 bg-[#4dc1c7]/5 group-hover:bg-[#4dc1c7]/10 transition-colors" />
                  <div className="relative z-10">
                    <div className="flex items-center justify-between mb-4">
                      <div className="flex items-center gap-2">
                        <Brain className="w-5 h-5 text-[#4dc1c7]" />
                        <span className="text-xs text-[#4dc1c7]">TESSENT Active</span>
                      </div>
                      <div className="w-2 h-2 rounded-full bg-[#4dc1c7] animate-pulse" />
                    </div>
                    <div className="space-y-3">
                      <div className="flex items-center gap-2 text-xs text-[#4dc1c7]">
                        <Check className="w-3 h-3" />
                        <span>{t('analysis_complete')}</span>
                      </div>
                      <div className="flex items-center gap-2 text-xs text-[#4dc1c7]">
                        <Check className="w-3 h-3" />
                        <span>{t('tasks_distributed')}</span>
                      </div>
                      <div className="flex items-center gap-2 text-xs text-[#4dc1c7]">
                        <Zap className="w-3 h-3" />
                        <span>{t('report_built')}</span>
                      </div>
                    </div>
                  </div>
                  <div className="relative z-10 mt-auto pt-8">
                    <div className="h-1 w-full bg-[#4dc1c7]/20 rounded-full overflow-hidden">
                      <div className="h-full bg-[#4dc1c7] w-2/3 animate-[shimmer_2s_infinite]" />
                    </div>
                  </div>
                </div>
              </div>
            </div>
            
            {/* Decorative elements */}
            <div className="absolute -top-10 -right-10 w-40 h-40 bg-[#4dc1c7]/20 blur-[60px] rounded-full" />
            <div className="absolute -bottom-10 -left-10 w-40 h-40 bg-[#e94560]/20 blur-[60px] rounded-full" />
          </div>
        </div>
      </section>

      {/* Problem Section */}
      <section id="problem" className="py-20 bg-[#151525] relative z-10">
        <div className="container mx-auto px-4">
          <div className="text-center mb-16">
            <h2 className="text-3xl md:text-5xl font-bold mb-6">
              {t("problem_title_1")} <br/>
              <span className="text-[#e94560]">{t("problem_title_2")}</span>
            </h2>
            <p className="text-xl text-gray-400 max-w-2xl mx-auto mt-6">
              {t('problem_subtitle')}
            </p>
            <div className="max-w-3xl mx-auto grid grid-cols-2 md:grid-cols-4 gap-4 mt-12">
              <div className="p-4 rounded-xl bg-white/5 border border-white/5 text-center flex flex-col justify-center h-full">
                <div className="text-2xl font-bold text-gray-400 mb-2">2015</div>
                <div className="text-sm text-gray-300 font-medium">{t("label_18_months")}</div>
                <div className="text-xs text-gray-500 mt-1">{t("label_idea_to_launch")}</div>
              </div>
              <div className="p-4 rounded-xl bg-white/5 border border-white/5 text-center flex flex-col justify-center h-full">
                <div className="text-2xl font-bold text-gray-400 mb-2">2020</div>
                <div className="text-sm text-gray-300 font-medium">{t("label_9_months")}</div>
                <div className="text-xs text-gray-500 mt-1">{t("label_idea_to_launch")}</div>
              </div>
              <div className="p-4 rounded-xl bg-white/5 border border-white/5 text-center relative overflow-hidden flex flex-col justify-center h-full">
                <div className="absolute inset-0 bg-[#e94560]/10" />
                <div className="text-2xl font-bold text-[#e94560] mb-2 relative z-10">2025</div>
                <div className="text-sm text-[#e94560] font-medium relative z-10">{t("label_6_weeks")}</div>
                <div className="text-xs text-[#e94560]/70 mt-1 relative z-10">{t("label_idea_to_launch")}</div>
              </div>
              <div className="p-4 rounded-xl bg-white/5 border border-white/5 text-center opacity-50 flex flex-col justify-center h-full">
                <div className="text-2xl font-bold text-gray-600 mb-2">2027</div>
                <div className="text-sm text-gray-600 font-medium">{t("you_already_late")}</div>
              </div>
            </div>
          </div>

          <div className="grid md:grid-cols-2 gap-8 max-w-5xl mx-auto">
            {/* Without AI */}
            <div className="p-8 rounded-2xl bg-[#1a1a2e] border border-white/5 relative overflow-hidden group hover:border-[#e94560]/30 transition-colors">
              <div className="absolute top-0 left-0 w-1 h-full bg-gray-700 group-hover:bg-[#e94560] transition-colors" />
              <h3 className="text-xl font-bold mb-6 text-gray-400">{t("company_without_ai")}</h3>
              
              <div className="space-y-8 relative">
                {/* Timeline line */}
                <div className="absolute left-[7px] top-2 bottom-2 w-px bg-white/10" />
                
                <div className="relative pl-8">
                  <div className="absolute left-0 top-1 w-4 h-4 rounded-full bg-[#1a1a2e] border-2 border-gray-600" />
                  <div className="text-sm font-mono text-gray-500 mb-1">{t("monday_1047")}</div>
                  <p className="text-gray-300">{t("without_ai_morning")}</p>
                </div>
                
                <div className="relative pl-8">
                  <div className="absolute left-0 top-1 w-4 h-4 rounded-full bg-[#1a1a2e] border-2 border-gray-600" />
                  <div className="text-sm font-mono text-gray-500 mb-1">{t("wednesday_2300")}</div>
                  <p className="text-gray-300">{t("without_ai_evening")}</p>
                </div>
                
                <div className="relative pl-8 pt-4">
                  <div className="inline-block px-3 py-1 rounded bg-[#e94560]/20 text-[#e94560] font-bold text-sm">
                    {t('lost_label')}
                  </div>
                  <p className="text-sm text-gray-500 mt-2">{t("lost_hint")}</p>
                </div>
              </div>
            </div>

            {/* With Tessent */}
            <div className="p-8 rounded-2xl bg-[#1a1a2e] border border-[#4dc1c7]/20 relative overflow-hidden group hover:border-[#4dc1c7]/50 transition-colors shadow-2xl shadow-[#4dc1c7]/5">
              <div className="absolute top-0 left-0 w-1 h-full bg-[#4dc1c7]" />
              <div className="absolute top-0 right-0 p-4">
                 <Brain className="w-6 h-6 text-[#4dc1c7] opacity-50 group-hover:opacity-100 transition-opacity" />
              </div>
              <h3 className="text-xl font-bold mb-6 text-white">{t("company_with_tessent")}</h3>
              
              <div className="space-y-8 relative">
                {/* Timeline line */}
                <div className="absolute left-[7px] top-2 bottom-2 w-px bg-[#4dc1c7]/30" />
                
                <div className="relative pl-8">
                  <div className="absolute left-0 top-1 w-4 h-4 rounded-full bg-[#1a1a2e] border-2 border-[#4dc1c7]" />
                  <div className="text-sm font-mono text-[#4dc1c7] mb-1">{t("monday_1047")}</div>
                  <p className="text-gray-300">{t("with_tessent_morning")}</p>
                </div>
                
                <div className="relative pl-8">
                  <div className="absolute left-0 top-1 w-4 h-4 rounded-full bg-[#1a1a2e] border-2 border-[#4dc1c7]" />
                  <div className="text-sm font-mono text-[#4dc1c7] mb-1">{t("wednesday_2300")}</div>
                  <p className="text-gray-300">{t("with_tessent_evening")}</p>
                </div>
                
                <div className="relative pl-8 pt-4">
                  <div className="inline-block px-3 py-1 rounded bg-[#4dc1c7]/20 text-[#4dc1c7] font-bold text-sm">
                    {t('won_label')}
                  </div>
                  <p className="text-sm text-gray-500 mt-2">{t("won_hint")}</p>
                </div>
              </div>
            </div>
          </div>
          
          <div className="text-center mt-12 text-gray-400 italic">
            {t('comparison_quote')}
          </div>
        </div>
      </section>

      {/* Solution Section */}
      <section id="solution" className="py-20 relative z-10 overflow-hidden">
        {/* Abstract Brain Background */}
        <div className="absolute inset-0 flex items-center justify-center opacity-5 pointer-events-none">
          <Brain className="w-[800px] h-[800px]" />
        </div>
        
        <div className="container mx-auto px-4 relative z-10">
          <div className="text-center mb-16">
            <h2 className="text-3xl md:text-5xl font-bold mb-4">
              {t("solution_title_prefix")} <span className="text-[#4dc1c7]">{t("solution_title_highlight")}</span> {t("solution_title_suffix")}
            </h2>
            <p className="text-xl text-gray-400 max-w-2xl mx-auto">
              {t('solution_subtitle')}
            </p>
          </div>
          
          <div className="grid md:grid-cols-4 gap-6">
            {[
              { icon: Users, title: t("card_present_title"), desc: t("card_present_desc") },
              { icon: Database, title: t("card_remembers_title"), desc: t("card_remembers_desc") },
              { icon: MessageSquare, title: t("card_answers_title"), desc: t("card_answers_desc") },
              { icon: Check, title: t("card_acts_title"), desc: t("card_acts_desc") }
            ].map((item, i) => (
              <div key={i} className="p-6 rounded-xl bg-white/5 border border-white/5 hover:bg-white/10 hover:border-[#4dc1c7]/30 transition-all group">
                <div className="w-12 h-12 rounded-lg bg-[#4dc1c7]/10 flex items-center justify-center mb-4 group-hover:scale-110 transition-transform">
                  <item.icon className="w-6 h-6 text-[#4dc1c7]" />
                </div>
                <h3 className="text-lg font-bold mb-2">{item.title}</h3>
                <p className="text-gray-400 text-sm">{item.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Features Section */}
      <section id="features" className="py-20 bg-[#151525] relative z-10">
        <div className="container mx-auto px-4">
          <div className="text-center mb-16">
            <h2 className="text-3xl md:text-5xl font-bold mb-6">{t("features_title")}</h2>
          </div>
          
          <div className="space-y-8">
            {/* Feature 1 */}
            <div className="group p-1 rounded-2xl bg-gradient-to-r from-transparent via-white/5 to-transparent hover:via-[#4dc1c7]/20 transition-all">
              <div className="bg-[#1a1a2e] rounded-xl p-8 md:flex items-center gap-8">
                <div className="md:w-1/2 mb-6 md:mb-0">
                  <div className="flex items-center gap-3 mb-4">
                    <Brain className="w-8 h-8 text-[#4dc1c7]" />
                    <h3 className="text-2xl font-bold">Brain</h3>
                  </div>
                  <p className="text-gray-300 mb-6 text-lg">{t("feature_brain_desc")}</p>
                  <div className="bg-[#0f0f1a] p-4 rounded-lg border border-white/10 font-mono text-sm">
                    <div className="text-gray-500 mb-2">// User Query</div>
                    <div className="text-white">{t("feature_brain_example")}</div>
                    <div className="my-2 h-px bg-white/5" />
                    <div className="text-[#4dc1c7]">
                      Found 3 tasks:<br/>
                      1. Update landing page (Due: Friday)<br/>
                      2. Prepare Q3 report<br/>
                      3. Sync with design team
                    </div>
                  </div>
                </div>
                <div className="md:w-1/2 flex justify-center">
                  {/* Visual placeholder */}
                  <div className="w-full h-48 bg-[#4dc1c7]/5 rounded-lg border border-[#4dc1c7]/20 flex items-center justify-center relative overflow-hidden">
                    <div className="absolute inset-0 bg-grid-white/[0.02]" />
                    <Search className="w-16 h-16 text-[#4dc1c7]/40" />
                  </div>
                </div>
              </div>
            </div>

            {/* Feature 2 */}
            <div className="group p-1 rounded-2xl bg-gradient-to-r from-transparent via-white/5 to-transparent hover:via-[#4dc1c7]/20 transition-all">
              <div className="bg-[#1a1a2e] rounded-xl p-8 md:flex items-center gap-8 flex-row-reverse">
                <div className="md:w-1/2 mb-6 md:mb-0">
                  <div className="flex items-center gap-3 mb-4">
                    <Shield className="w-8 h-8 text-[#4dc1c7]" />
                    <h3 className="text-2xl font-bold">{t("feature_local_title")}</h3>
                  </div>
                  <p className="text-gray-300 mb-6 text-lg">{t("feature_local_desc")}</p>
                  <ul className="space-y-2">
                    <li className="flex items-center gap-2 text-gray-400">
                      <Check className="w-4 h-4 text-[#4dc1c7]" /> {t("feature_local_on_premise")}
                    </li>
                    <li className="flex items-center gap-2 text-gray-400">
                      <Check className="w-4 h-4 text-[#4dc1c7]" /> {t("feature_local_compliance")}
                    </li>
                    <li className="flex items-center gap-2 text-gray-400">
                      <Check className="w-4 h-4 text-[#4dc1c7]" /> {t("feature_local_processing")}
                    </li>
                  </ul>
                </div>
                <div className="md:w-1/2 flex justify-center">
                  <div className="w-full h-48 bg-[#4dc1c7]/5 rounded-lg border border-[#4dc1c7]/20 flex items-center justify-center relative overflow-hidden">
                    <Lock className="w-16 h-16 text-[#4dc1c7]/40" />
                  </div>
                </div>
              </div>
            </div>

            {/* Feature 3: Mark */}
            <div className="group p-1 rounded-2xl bg-gradient-to-r from-transparent via-white/5 to-transparent hover:via-[#e94560]/20 transition-all">
              <div className="bg-[#1a1a2e] rounded-xl p-8 md:flex items-center gap-8">
                <div className="md:w-1/2 mb-6 md:mb-0">
                  <div className="flex items-center gap-3 mb-4">
                    <TrendingUp className="w-8 h-8 text-[#e94560]" />
                    <h3 className="text-2xl font-bold">Mark</h3>
                  </div>
                  <p className="text-gray-300 mb-6 text-lg">{t("feature_mark_desc")}</p>
                  <div className="bg-[#0f0f1a] p-4 rounded-lg border border-white/10 font-mono text-sm relative overflow-hidden">
                    <div className="absolute top-0 left-0 w-1 h-full bg-[#e94560]" />
                    <div className="text-white">{t("feature_mark_example")}</div>
                    <div className="mt-3 flex items-center gap-2 text-[#e94560]">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      <span>{t('feature_mark_running')}</span>
                    </div>
                  </div>
                </div>
                <div className="md:w-1/2 flex justify-center">
                   <div className="w-full h-48 bg-[#e94560]/5 rounded-lg border border-[#e94560]/20 flex items-center justify-center relative overflow-hidden">
                    <BarChart3 className="w-16 h-16 text-[#e94560]/40" />
                  </div>
                </div>
              </div>
            </div>
            
             {/* Feature 4: Automation */}
            <div className="group p-1 rounded-2xl bg-gradient-to-r from-transparent via-white/5 to-transparent hover:via-[#4dc1c7]/20 transition-all">
              <div className="bg-[#1a1a2e] rounded-xl p-8 md:flex items-center gap-8 flex-row-reverse">
                <div className="md:w-1/2 mb-6 md:mb-0">
                  <div className="flex items-center gap-3 mb-4">
                    <Zap className="w-8 h-8 text-[#4dc1c7]" />
                    <h3 className="text-2xl font-bold">Automation</h3>
                  </div>
                  <p className="text-gray-300 mb-6 text-lg">{t("feature_automation_desc")}</p>
                  <div className="flex gap-4">
                    {['Notion', 'Telegram', 'Slack', 'Jira'].map(app => (
                      <div key={app} className="px-4 py-2 bg-white/5 rounded-lg border border-white/10 text-sm text-gray-400">
                        {app}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="md:w-1/2 flex justify-center">
                  <div className="w-full h-48 bg-[#4dc1c7]/5 rounded-lg border border-[#4dc1c7]/20 flex items-center justify-center relative overflow-hidden">
                    <div className="flex gap-4 opacity-40">
                      <Laptop className="w-12 h-12 text-[#4dc1c7]" />
                      <ArrowRight className="w-8 h-8 text-white mt-2" />
                      <Smartphone className="w-12 h-12 text-[#4dc1c7]" />
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Demo Section */}
      <section id="demo" className="py-20 relative z-10">
        <div className="container mx-auto px-4 text-center">
          <h2 className="text-3xl md:text-4xl font-bold mb-12">{t("demo_section_title")}</h2>
          <div className="max-w-4xl mx-auto bg-black rounded-xl overflow-hidden shadow-2xl border border-white/10 aspect-video relative">
            <iframe 
              src="https://rutube.ru/play/embed/d0cfaa6dbeb5a52ff1873f1026dc825f" 
              className="w-full h-full"
              frameBorder="0" 
              allow="clipboard-write; autoplay; fullscreen" 
              allowFullScreen
            ></iframe>
          </div>
        </div>
      </section>

      {/* CTA Section */}
      <section id="cta" className="py-20 bg-[#151525] relative z-10 overflow-hidden">
        <div className="container mx-auto px-4 relative z-10">
          <div className="grid md:grid-cols-2 gap-12 items-center">
            <div>
              <h2 className="text-3xl md:text-5xl font-bold mb-6">
                {t("cta_title_1")} <br/>
                <span className="text-[#4dc1c7]">{t("cta_title_2")}</span>
              </h2>
              <p className="text-xl text-gray-400 mb-8">
                {t('cta_subtitle')}
              </p>
              <div className="space-y-4">
                <div className="flex items-center gap-3 text-gray-300">
                  <Check className="w-5 h-5 text-[#4dc1c7]" />
                  <span>{t('cta_bullet_1')}</span>
                </div>
                <div className="flex items-center gap-3 text-gray-300">
                  <Check className="w-5 h-5 text-[#4dc1c7]" />
                  <span>{t('cta_bullet_2')}</span>
                </div>
                <div className="flex items-center gap-3 text-gray-300">
                  <Check className="w-5 h-5 text-[#4dc1c7]" />
                  <span>{t('cta_bullet_3')}</span>
                </div>
              </div>
            </div>
            
            <div className="bg-[#1a1a2e] p-8 rounded-2xl border border-white/10 shadow-xl">
              <form className="space-y-4" onSubmit={async (e) => {
                e.preventDefault();
                const form = e.target as HTMLFormElement;
                const formData = new FormData(form);
                const data = {
                  name: formData.get('name') as string,
                  company: formData.get('company') as string,
                  contact: formData.get('contact') as string,
                };
                
                try {
                    const res = await fetch('/api/v1/demo/request-demo', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    
                    if (res.ok) {
                        alert(t('demo_form_success'));
                        form.reset();
                    } else {
                        alert(t('demo_form_error'));
                    }
                } catch (err) {
                    console.error('Failed to submit demo request:', err);
                    alert(t('demo_form_network_error'));
                }
              }}>
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-1">{t("demo_name_label")}</label>
                  <input name="name" type="text" required className="w-full px-4 py-3 bg-[#0f0f1a] border border-white/10 rounded-lg text-white focus:border-[#4dc1c7] outline-none transition-colors" placeholder={t("demo_name_placeholder")} />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-1">{t("demo_company_label")}</label>
                  <input name="company" type="text" required className="w-full px-4 py-3 bg-[#0f0f1a] border border-white/10 rounded-lg text-white focus:border-[#4dc1c7] outline-none transition-colors" placeholder="SynLabs" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-1">{t("demo_contact_label")}</label>
                  <input name="contact" type="text" required className="w-full px-4 py-3 bg-[#0f0f1a] border border-white/10 rounded-lg text-white focus:border-[#4dc1c7] outline-none transition-colors" placeholder="+7 999 000 00 00" />
                </div>
                <button type="submit" className="w-full py-4 bg-[#4dc1c7] text-[#1a1a2e] font-bold rounded-lg hover:bg-[#3daeb3] transition-colors">
                  {t('nav_request_demo')}
                </button>
                <p className="text-xs text-gray-500 text-center mt-4">
                  {t('demo_consent')}
                </p>
              </form>
            </div>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="py-12 border-t border-white/5 text-sm text-gray-500 relative z-10">
        <div className="container mx-auto px-4">
          <div className="flex flex-col md:flex-row justify-between items-center gap-6">
            <div className="flex items-center gap-2">
              <Brain className="w-6 h-6 text-gray-600" />
              <span className="font-bold text-gray-300">TESSENT</span>
            </div>
            <div className="flex gap-8">
              <a href={`/${locale}/offer`} className="hover:text-[#4dc1c7] transition-colors">{t('footer_offer')}</a>
              <a href={`/${locale}/terms`} className="hover:text-[#4dc1c7] transition-colors">{t("footer_terms")}</a>
            </div>
            <div>
              {t('copyright')}
            </div>
          </div>
        </div>
      </footer>
    </div>
  )
}

export default function Page() {
  const [showAuth, setShowAuth] = useState(false)

  // Optional: Auto-redirect if token exists logic could be here,
  // but we leave it inside AuthForm for when user actively tries to log in.
  // Or we can verify silently. 
  
  if (showAuth) {
    return <AuthForm onBack={() => setShowAuth(false)} />
  }

  return <LandingPage onLogin={() => setShowAuth(true)} />
}
