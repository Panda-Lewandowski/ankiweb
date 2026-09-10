import DOMPurify from 'dompurify';
import type { ComponentType } from 'react';
import type { ReviewQuestion } from '../../api/types';

function HtmlPrompt({ value, className = 'question__prompt' }: { value: string; className?: string }) {
  return value ? (
    <div className={className} dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(value) }} />
  ) : null;
}

function VocabularyProduction({ question }: { question: ReviewQuestion }) {
  return <div className="question-content"><p className="question__cue">Как сказать:</p><HtmlPrompt value={question.prompt_html} /></div>;
}

function VocabularyRecognition({ question }: { question: ReviewQuestion }) {
  return <div className="question-content question-content--recognition"><HtmlPrompt value={question.prompt_html} /></div>;
}

function GrammarCloze({ question }: { question: ReviewQuestion }) {
  return <div className="question-content question-content--grammar"><p className="question__cue">Вставьте правильную форму</p><HtmlPrompt value={question.prompt_html} /></div>;
}

function PersonalError({ question }: { question: ReviewQuestion }) {
  return <div className="question-content question-content--error"><p className="question__cue">Исправьте фразу</p><HtmlPrompt value={question.prompt_html} /></div>;
}

function SpanishConjugation({ question }: { question: ReviewQuestion }) {
  return (
    <div className="question-content question-content--conjugation">
      <HtmlPrompt value={question.prompt_html} className="question__verb" />
      <div className="question__conjugation-context">
        {question.tense ? <span>{question.tense}</span> : null}
        {question.person ? <strong>{question.person}</strong> : null}
      </div>
    </div>
  );
}

function PhraseRetrieval({ question }: { question: ReviewQuestion }) {
  return <div className="question-content question-content--phrase"><p className="question__cue">Как сказать естественно:</p><HtmlPrompt value={question.prompt_html} /></div>;
}

function UnknownQuestion({ question }: { question: ReviewQuestion }) {
  return <div className="question-content"><HtmlPrompt value={question.prompt_html} /></div>;
}

const RENDERERS: Record<string, ComponentType<{ question: ReviewQuestion }>> = {
  vocabulary_production: VocabularyProduction,
  vocabulary_recognition: VocabularyRecognition,
  grammar_cloze: GrammarCloze,
  personal_error: PersonalError,
  spanish_conjugation: SpanishConjugation,
  phrase_retrieval: PhraseRetrieval,
};

export function QuestionContent({ question }: { question: ReviewQuestion }) {
  const Renderer = RENDERERS[question.kind] ?? UnknownQuestion;
  return <Renderer question={question} />;
}
