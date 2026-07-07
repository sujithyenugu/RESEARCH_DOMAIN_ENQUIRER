import React from 'react';
import type { EvidenceChunk } from '../../types';

interface ChunkViewerProps {
  chunks: EvidenceChunk[];
}

const StarRating: React.FC<{ score: number }> = ({ score }) => {
  const stars = Math.round(score * 5);
  return (
    <span className="stars" aria-label={`Relevance: ${(score * 100).toFixed(0)}%`}>
      {Array.from({ length: 5 }, (_, i) => (
        <span key={i} className={i < stars ? 'star-filled' : 'star-empty'} aria-hidden="true">★</span>
      ))}
    </span>
  );
};

const ChunkViewer: React.FC<ChunkViewerProps> = ({ chunks }) => {
  return (
    <div className="chunk-viewer">
      <div className="section-label" style={{ marginBottom: '12px' }}>
        Evidence Chunks ({chunks.length})
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {chunks.map((chunk, idx) => (
          <div
            key={chunk.chunkId}
            className="glass-card chunk-card anim-fade-in"
            style={{ padding: '12px 14px', animationDelay: `${idx * 50}ms` }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span
                  className="badge badge-paper"
                  style={{ fontSize: '0.65rem', padding: '2px 6px' }}
                  title={chunk.paperId}
                >
                  {chunk.paperId}
                </span>
                <span style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)' }}>
                  {chunk.section}
                  {chunk.pageNumber && ` · p.${chunk.pageNumber}`}
                </span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                <StarRating score={chunk.relevanceScore} />
                <span style={{ fontSize: '0.7rem', color: 'var(--color-text-muted)', minWidth: '36px', textAlign: 'right' }}>
                  {(chunk.relevanceScore * 100).toFixed(0)}%
                </span>
              </div>
            </div>
            <p style={{
              fontSize: '0.8rem',
              color: 'var(--color-text-secondary)',
              lineHeight: 1.6,
              display: '-webkit-box',
              WebkitLineClamp: 3,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}>
              "{chunk.text}"
            </p>
          </div>
        ))}
      </div>
    </div>
  );
};

export default ChunkViewer;
