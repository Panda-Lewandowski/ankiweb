import DOMPurify from 'dompurify';
import { Headphones, Play, RotateCcw } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ReviewCard } from '../api/types';
import { Button } from '../components/ui/button';

type Props = {
  card: ReviewCard;
  value: string;
  onChange: (value: string) => void;
  onCheck: () => void;
  busy: boolean;
};

export function CardQuestion({ card, value, onChange, onCheck, busy }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [playing, setPlaying] = useState(false);
  const audio = card.question.audio_urls[0];

  useEffect(() => {
    if (card.question.input_required) inputRef.current?.focus();
  }, [card.token, card.question.input_required]);

  async function playAudio() {
    if (!audio || playing) return;
    const player = new Audio(audio);
    setPlaying(true);
    player.addEventListener('ended', () => setPlaying(false), { once: true });
    player.addEventListener('error', () => setPlaying(false), { once: true });
    await player.play().catch(() => setPlaying(false));
  }

  const safePrompt = DOMPurify.sanitize(card.question.prompt_html);
  const isListening = card.question.kind.startsWith('listening_');

  return (
    <div className="question" key={card.token}>
      {isListening ? (
        <div className="audio-block">
          <Headphones aria-hidden="true" size={22} />
          {audio ? (
            <Button type="button" size="icon" variant="quiet" onClick={playAudio}
              aria-label={playing ? 'Аудио воспроизводится' : 'Прослушать аудио'} disabled={playing}>
              {playing ? <RotateCcw className="spin" size={20} /> : <Play size={20} fill="currentColor" />}
            </Button>
          ) : (
            <p className="audio-unavailable" role="status">Для этой карточки пока нет сохранённого аудио</p>
          )}
        </div>
      ) : null}

      {safePrompt ? (
        <div className="question__prompt" dangerouslySetInnerHTML={{ __html: safePrompt }} />
      ) : null}

      {card.question.tense || card.question.person ? (
        <p className="question__context">
          {[card.question.tense, card.question.person].filter(Boolean).join(' · ')}
        </p>
      ) : null}

      {card.question.instruction ? <p className="question__instruction">{card.question.instruction}</p> : null}

      {card.question.input_required ? (
        <input
          ref={inputRef}
          className="answer-input"
          aria-label="Ваш ответ"
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
