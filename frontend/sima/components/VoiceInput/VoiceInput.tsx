'use client'

import { useState, useRef, useCallback } from 'react'
import { useTranslations } from 'next-intl'
import { Mic, MicOff, Square } from 'lucide-react'
import { cn } from '@/sima/lib/utils'

interface VoiceInputProps {
  onResult: (text: string) => void
}

export default function VoiceInput({ onResult }: VoiceInputProps) {
  const t = useTranslations('sima_voice')
  const [isRecording, setIsRecording] = useState(false)
  const [isSupported, setIsSupported] = useState(true)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const recognitionRef = useRef<any>(null)

  const startRecording = useCallback(() => {
    if (typeof window === 'undefined') return

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const SpeechRecognitionAPI = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition

    if (!SpeechRecognitionAPI) {
      setIsSupported(false)
      return
    }

    const recognition = new SpeechRecognitionAPI()
    recognition.lang = 'ru-RU'
    recognition.continuous = true
    recognition.interimResults = true

    let finalTranscript = ''

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    recognition.onresult = (event: any) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript
        if (event.results[i].isFinal) {
          finalTranscript += transcript + ' '
        }
      }
    }

    recognition.onend = () => {
      setIsRecording(false)
      if (finalTranscript.trim()) {
        onResult(finalTranscript.trim())
      }
      recognitionRef.current = null
    }

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    recognition.onerror = (event: any) => {
      console.error('Speech recognition error:', event.error)
      setIsRecording(false)
      recognitionRef.current = null
    }

    recognitionRef.current = recognition
    recognition.start()
    setIsRecording(true)
  }, [onResult])

  const stopRecording = useCallback(() => {
    if (recognitionRef.current) {
      recognitionRef.current.stop()
    }
    setIsRecording(false)
  }, [])

  if (!isSupported) {
    return (
      <button
        disabled
        className="p-2.5 bg-sima-surfaceLight rounded-xl text-sima-textDim opacity-50"
        title={t("not_supported_title")}
      >
        <MicOff className="w-4 h-4" />
      </button>
    )
  }

  return (
    <div className="relative">
      {isRecording && (
        <div className="absolute inset-0 rounded-xl bg-red-500/20 voice-pulse pointer-events-none" />
      )}
      <button
        onClick={isRecording ? stopRecording : startRecording}
        className={cn(
          'p-2.5 rounded-xl transition-all duration-200 relative shrink-0',
          isRecording
            ? 'bg-red-500/20 text-red-400 hover:bg-red-500/30 ring-2 ring-red-500/50'
            : 'bg-sima-surfaceLight text-sima-textMuted hover:text-sima-text hover:bg-sima-border'
        )}
        title={isRecording ? t('stop_recording_title') : t('start_recording_title')}
      >
        {isRecording ? <Square className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
      </button>
    </div>
  )
}
