import React, { useState, useRef, type FormEvent, type KeyboardEvent } from 'react';
import type { QueryFilters } from '../../types';

const CATEGORIES = ['cs.AI', 'cs.CL', 'cs.LG', 'cs.CV', 'cs.IR', 'cs.NE', 'stat.ML'];

interface QueryInputProps {
  onSubmit: (query: string, filters: QueryFilters) => void;
  disabled?: boolean;
  filters: QueryFilters;
  onFiltersChange: (f: Partial<QueryFilters>) => void;
}

const QueryInput: React.FC<QueryInputProps> = ({ onSubmit, disabled, filters, onFiltersChange }) => {
  const [query, setQuery] = useState('');
  const [showFilters, setShowFilters] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = query.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed, filters);
    setQuery('');
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const toggleCategory = (cat: string) => {
    const cats = filters.categories.includes(cat)
      ? filters.categories.filter(c => c !== cat)
      : [...filters.categories, cat];
    onFiltersChange({ categories: cats });
  };

  return (
    <div className="query-input-container">
      {/* Main search bar */}
      <form onSubmit={handleSubmit} style={{ position: 'relative' }}>
        <div className="glass-card query-bar" style={{ padding: '3px 3px 3px 16px' }}>
          <textarea
            ref={textareaRef}
            className="query-textarea"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask a research question… e.g. How does LoRA compare to full fine-tuning?"
            disabled={disabled}
            rows={1}
            aria-label="Research query input"
            style={{
              resize: 'none',
              background: 'transparent',
              border: 'none',
              outline: 'none',
              width: '100%',
              color: 'var(--color-text-primary)',
              fontSize: '0.9375rem',
              fontFamily: 'var(--font-sans)',
              lineHeight: 1.5,
              padding: '10px 0',
              verticalAlign: 'middle',
            }}
            onInput={e => {
              const t = e.currentTarget;
              t.style.height = 'auto';
              t.style.height = Math.min(t.scrollHeight, 120) + 'px';
            }}
          />
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: '6px', padding: '6px 6px' }}>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setShowFilters(v => !v)}
              aria-expanded={showFilters}
              title="Toggle filters"
              style={{ fontSize: '0.8rem' }}
            >
              ⚙️ Filters
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!query.trim() || disabled}
              aria-label="Submit query"
              style={{ padding: '8px 20px' }}
            >
              {disabled ? (
                <span className="anim-spin" style={{ display: 'inline-block' }}>⟳</span>
              ) : (
                '→ Search'
              )}
            </button>
          </div>
        </div>
      </form>

      {/* Filter panel */}
      {showFilters && (
        <div className="glass-card anim-slide-up" style={{ padding: '16px', marginTop: '8px' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: '16px', alignItems: 'end', flexWrap: 'wrap' }}>
            {/* Date range */}
            <div>
              <label className="section-label" style={{ display: 'block', marginBottom: '6px' }}>From Date</label>
              <input
                type="date"
                className="input"
                style={{ fontSize: '0.85rem', padding: '6px 10px' }}
                value={filters.dateFrom}
                onChange={e => onFiltersChange({ dateFrom: e.target.value })}
                aria-label="Start date filter"
              />
            </div>
            <div>
              <label className="section-label" style={{ display: 'block', marginBottom: '6px' }}>To Date</label>
              <input
                type="date"
                className="input"
                style={{ fontSize: '0.85rem', padding: '6px 10px' }}
                value={filters.dateTo}
                onChange={e => onFiltersChange({ dateTo: e.target.value })}
                aria-label="End date filter"
              />
            </div>
            {/* Top-K */}
            <div style={{ minWidth: '120px' }}>
              <label className="section-label" style={{ display: 'block', marginBottom: '6px' }}>
                Top-K Chunks: <strong style={{ color: 'var(--color-accent-blue)' }}>{filters.topK}</strong>
              </label>
              <input
                type="range"
                min={3}
                max={20}
                step={1}
                value={filters.topK}
                onChange={e => onFiltersChange({ topK: parseInt(e.target.value, 10) })}
                style={{ width: '100%' }}
                aria-label={`Top K: ${filters.topK}`}
              />
            </div>
          </div>

          {/* Categories */}
          <div style={{ marginTop: '12px' }}>
            <div className="section-label" style={{ marginBottom: '6px' }}>arXiv Categories</div>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
              {CATEGORIES.map(cat => (
                <button
                  key={cat}
                  className={`chip ${filters.categories.includes(cat) ? 'chip-active' : ''}`}
                  onClick={() => toggleCategory(cat)}
                  aria-pressed={filters.categories.includes(cat)}
                >
                  {cat}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default QueryInput;
