import DOMPurify from 'dompurify';
import type { ComponentType } from 'react';

type Fields = Record<string, string>;

function Field({ label, value, primary = false, quote = false }: {
  label: string; value?: string; primary?: boolean; quote?: boolean;
}) {
  if (!value?.trim()) return null;
  return (
    <div className={`answer-field${primary ? ' answer-field--primary' : ''}${quote ? ' answer-field--quote' : ''}`}>
      <span>{label}</span>
      <div dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(value) }} />
    </div>
  );
}

function VocabularyProduction({ fields }: { fields: Fields }) {
  return <><Field label="Ответ" value={fields.Answer} primary /><Field label="Пример" value={fields.Example} quote /><Field label="Перевод" value={fields.Translation} /></>;
}

function VocabularyRecognition({ fields }: { fields: Fields }) {
  return <><Field label="Перевод" value={fields.Translation} primary /><Field label="Пример" value={fields.Example} quote /></>;
}

function GrammarCloze({ fields }: { fields: Fields }) {
  return <><Field label="Ответ" value={fields.Answer} primary /><Field label="Фраза целиком" value={fields.Text} quote /><Field label="Пояснение" value={fields.BackExtra} /></>;
}

function PersonalError({ fields }: { fields: Fields }) {
  return <><Field label="Правильный ответ" value={fields.Answer} primary /><Field label="Почему" value={fields.Explanation} /></>;
}

function SpanishConjugation({ fields }: { fields: Fields }) {
  return <><Field label="Форма" value={fields.Answer} primary /><Field label="Пример" value={fields.Example} quote /></>;
}

function Listening({ fields }: { fields: Fields }) {
  return <><Field label="Что прозвучало" value={fields.Sentence} primary /><Field label="Перевод" value={fields.Translation} /><Field label="Заметка" value={fields.Note} /></>;
}

function PhraseRetrieval({ fields }: { fields: Fields }) {
  return <><Field label="Естественная фраза" value={fields.Answer} primary /><Field label="В контексте" value={fields.Example} quote /></>;
}

function UnknownAnswer({ fields }: { fields: Fields }) {
  return <>{Object.entries(fields).filter(([name]) => name !== 'OriginalError').map(([name, value], index) => <Field key={name} label={name === 'answer_html' ? 'Ответ' : name} value={value} primary={index === 0} />)}</>;
}

const RENDERERS: Record<string, ComponentType<{ fields: Fields }>> = {
  vocabulary_production: VocabularyProduction,
  vocabulary_recognition: VocabularyRecognition,
  grammar_cloze: GrammarCloze,
  personal_error: PersonalError,
  spanish_conjugation: SpanishConjugation,
  listening_dictation: Listening,
  listening_comprehension: Listening,
  phrase_retrieval: PhraseRetrieval,
};

export function AnswerFields({ kind, fields }: { kind: string; fields: Fields }) {
  const Renderer = RENDERERS[kind] ?? UnknownAnswer;
  return <Renderer fields={fields} />;
}
