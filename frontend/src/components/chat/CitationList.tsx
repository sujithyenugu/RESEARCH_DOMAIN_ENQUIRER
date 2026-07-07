import React from 'react';
import type { Citation } from '../../types';
import { useNavigate } from 'react-router-dom';

interface CitationListProps {
  citations: Citation[];
}

const StarRating: React.FC<{ score: number }> = ({ score }) => {
  const stars = Math.round(score * 5);
  return (
    <span className="stars" aria-label={`Relevance: ${score.toFixed(2)}`}>
      {Array.from({ length: 5 }, (_, i) => (
        <span key={i} className={i < stars ? 'star-filled' : 'star-empty'} aria-hidden="true">
          ★
        </span>
      ))}
    </span>
  );
};

const CitationList: React.FC<CitationListProps> = ({ citations }) => {
  const navigate = useNavigate();

  return (
    <div className="citation-list">
      <div className="section-label" style={{ marginBottom: '12px' }}>
        Citations ({citations.length})
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
        {citations.map((citation, idx) => (
          <div key={citation.paperId} className="glass-card citation-card anim-fade-in" style={{ animationDelay: `${idx * 60}ms` }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '8px' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                  <span className="badge badge-paper" style={{ fontSize: '0.65rem' }}>
                    [{idx + 1}]
                  </span>
                  <span
                    className="font-mono text-xs"
                    style={{ color: 'var(--color-accent-blue)', cursor: 'pointer' }}
                    onClick={() => navigate(`/papers/${citation.paperId}`)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={e => e.key === 'Enter' && navigate(`/papers/${citation.paperId}`)}
                  >
                    {citation.paperId}
                  </span>
                </div>
                <p
                  className="citation-title"
                  style={{
                    fontSize: '0.825rem',
                    fontWeight: 600,
                    color: 'var(--color-text-primary)',
                    marginBottom: '3px',
                    lineHeight: 1.4,
                  }}
                >
                  {citation.title}
                </p>
                <p style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', marginBottom: '6px' }}>
                  {citation.authors.slice(0, 3).join(', ')}
                  {citation.authors.length > 3 && ' et al.'}
                  {' · '}{citation.year}
                </p>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <StarRating score={citation.relevanceScore} />
                  <a
                    href={citation.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn btn-ghost"
                    style={{ fontSize: '0.75rem', padding: '3px 8px' }}
                    aria-label={`View paper ${citation.paperId} on arXiv`}
                  >
                    📄 arXiv ↗
                  </a>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default CitationList;
