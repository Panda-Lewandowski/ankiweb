import type { ReactNode } from 'react';
import { cn } from '../lib/utils';

type GlassPanelProps = {
  children: ReactNode;
  className?: string;
  title?: string;
};

export function GlassPanel({ children, className, title }: GlassPanelProps) {
  return (
    <section className={cn('glass-panel', className)}>
      {title ? <h2 className="glass-panel__title">{title}</h2> : null}
      <div className="glass-panel__body">{children}</div>
    </section>
  );
}
