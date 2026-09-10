import { Headphones, LoaderCircle, Play, RotateCcw } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { ReviewCard } from '../api/types';
import { Button } from '../components/ui/button';
import { QuestionContent } from './renderers/QuestionRenderers';

type Props = {
  card: ReviewCard;
  value: string;
  onChange: (value: string) => void;
  onCheck: () => void;
  busy: boolean;
};

export function CardQuestion({ card, value, onChange, onCheck, busy }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const playerRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const [audioState, setAudioState] = useState<'idle' | 'loading' | 'playing' | 'error'>('idle');
  const storedAudio = card.question.audio_urls[0];
  const isListening = card.question.kind.startsWith('listening_');

  useEffect(() => {
    if (card.question.input_required) inputRef.current?.focus();
  }, [card.token, card.question.input_required]);

  useEffect(() => () => {
    playerRef.current?.pause();
    playerRef.current = null;
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    objectUrlRef.current = null;
  }, [card.token]);

  async function playAudio() {
    if (audioState === 'loading' || audioState === 'playing') return;
    setAudioState('loading');
    try {
      let source = storedAudio ?? objectUrlRef.current ?? undefined;
      if (!source) {
        const blob = await api.audio(card.token);
        source = URL.createObjectURL(blob);
        objectUrlRef.current = source;
      }
      const player = new Audio(source);
      playerRef.current = player;
      player.addEventListener('ended', () => setAudioState('idle'), { once: true });
      player.addEventListener('error', () => setAudioState('error'), { once: true });
      setAudioState('playing');
      await player.play();
    } catch {
      setAudioState('error');
    }
  }

  return (
    <div className={`question question--${card.question.kind}`} key={card.token}>
      {isListening ? (
        <div className="audio-block">
          <Headphones aria-hidden="true" size={22} />
          <Button type="button" size="icon" variant="quiet" onClick={playAudio}
            aria-label={audioState === 'playing' ? 'Аудио воспроизводится' : 'Прослушать аудио'}
            disabled={audioState === 'loading' || audioState === 'playing'}>
            {audioState === 'loading' ? <LoaderCircle className="spin" size={20} />
              : audioState === 'playing' ? <RotateCcw className="spin" size={20} />
                : <Play size={20} fill="currentColor" />}
          </Button>
          <p className={audioState === 'error' ? 'audio-unavailable audio-unavailable--error' : 'audio-unavailable'} role="status">
            {audioState === 'error' ? 'Не удалось воспроизвести аудио' : storedAudio ? 'Прослушать ещё раз' : 'Системный голос'}
          </p>
        </div>
      ) : null}

      {!isListening ? <QuestionContent question={card.question} /> : null}

      {card.question.instruction ? <p className="question__instruction">{card.question.instruction}</p> : null}

      {card.question.input_required ? (
        <input
          ref={inputRef}
          className="answer-input"
          aria-label={isListening ? 'Напечатайте услышанную фразу' : 'Ваш ответ'}
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !busy) {
              event.preventDefault();
              onCheck();
            }
          }}
        />
      ) : null}

      <Button className="check-button" type="button" disabled={busy} onClick={onCheck}>
        {busy ? 'Проверяем…' : card.question.input_required ? 'Проверить' : 'Показать ответ'}
      </Button>
    </div>
  );
}
