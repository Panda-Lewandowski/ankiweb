import { useState } from 'react';
import type { Language } from './api/types';
import { ReviewPage } from './pages/ReviewPage';
import { LessonImportsPage } from './pages/LessonImportsPage';
import { TodayPage } from './pages/TodayPage';

type Session = { language: Language; total: number } | null;
type View = 'today' | 'imports';

export default function App() {
  const [session, setSession] = useState<Session>(null);
  const [view, setView] = useState<View>('today');
  return (
    <div className="app-shell">
      <div className="app-background" aria-hidden="true" />
      <div className="app-overlay" aria-hidden="true" />
      <div className="app-content">
        {session ? (
          <ReviewPage language={session.language} initialTotal={session.total} onExit={() => setSession(null)} />
        ) : view === 'imports' ? (
          <LessonImportsPage onBack={() => setView('today')} />
        ) : (
          <TodayPage
            onImport={() => setView('imports')}
            onStart={(language, total) => setSession({ language, total })}
          />
        )}
      </div>
    </div>
  );
}
