export type Language = 'spanish' | 'english';
export type Rating = 'again' | 'hard' | 'good' | 'easy';

export type TodaySummary = {
  language: Language;
  deck: string;
  new: number;
  learning: number;
  review: number;
  total: number;
};

export type ReviewQuestion = {
  kind: string;
  prompt_html: string;
  instruction: string;
  input_required: boolean;
  audio_urls: string[];
  tts_locale: string | null;
  tense?: string;
  person?: string;
};

export type ReviewCard = {
  token: string;
  expires_in_seconds: number;
  language: Language;
  card_id: number;
  note_id: number;
  question: ReviewQuestion;
};

export type CheckResponse = {
  token: string;
  correct: boolean | null;
  diff_html: string | null;
  back: { fields: Record<string, string>; audio_urls: string[] };
};

export type AnswerResponse = {
  answered: true;
  card_id: number;
  rating: Rating;
  next: ReviewCard | null;
};
