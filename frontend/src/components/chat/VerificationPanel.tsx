import React, { useState } from 'react';
import type { VerificationReport, ClaimStatus } from '../../types';

interface VerificationPanelProps {
  report: VerificationReport;
}

const CLAIM_STATUS_CONFIG: Record<ClaimStatus, { icon: string; color: string; label: string }> = {
  SUPPORTED:           { icon: '✅', color: 'var(--color-accent-green)',  label: 'SUPPORTED' },
  PARTIALLY_SUPPORTED: { icon: '⚠️',  color: 'var(--color-accent-amber)',  label: 'PARTIALLY SUPPORTED' },
  UNSUPPORTED:         { icon: '❌', color: 'var(--color-accent-red)',    label: 'UNSUPPORTED' },
  CONTRADICTED:        { icon: '🚫', color: '#f97316',                    label: 'CONTRADICTED' },
};

const VerificationPanel: React.FC<VerificationPanelProps> = ({ report }) => {
  const [expandedClaim, setExpandedClaim] = useState<string | null>(null);

  return (
    <div className="glass-card anim-slide-up" style={{ padding: '20px', marginTop: '12px' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h4 style={{ fontSize: '0.875rem', fontWeight: 700, color: 'var(--color-text-primary)' }}>
          🔍 Hallucination Verification Report
        </h4>
        <span
          style={{
            fontSize: '0.85rem',
            fontWeight: 700,
            color: report.overallConfidence >= 0.85 ? 'var(--color-accent-green)' : 'var(--color-accent-amber)',
          }}
        >
          Confidence: {(report.overallConfidence * 100).toFixed(0)}%
        </span>
      </div>

      {/* Summary stats */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(4, 1fr)',
        gap: '8px',
        marginBottom: '16px',
        padding: '12px',
        background: 'rgba(255,255,255,0.03)',
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--color-border)',
      }}>
        {[
          { label: '✅ Supported',    value: report.supported,           color: 'var(--color-accent-green)' },
          { label: '⚠️ Partial',       value: report.partiallySupported,  color: 'var(--color-accent-amber)' },
          { label: '❌ Unsupported',  value: report.unsupported,         color: 'var(--color-accent-red)' },
          { label: '🚫 Contradicted', value: report.contradicted,        color: '#f97316' },
        ].map(item => (
          <div key={item.label} style={{ textAlign: 'center' }}>
            <div style={{ fontSize: '1.25rem', fontWeight: 700, color: item.color }}>{item.value}</div>
            <div style={{ fontSize: '0.65rem', color: 'var(--color-text-muted)', marginTop: '2px' }}>{item.label}</div>
          </div>
        ))}
      </div>

      {/* Coverage metrics */}
      <div style={{ display: 'flex', gap: '16px', marginBottom: '16px', flexWrap: 'wrap' }}>
        <div style={{ fontSize: '0.78rem', color: 'var(--color-text-secondary)' }}>
          <span style={{ color: 'var(--color-accent-cyan)' }}>Evidence Coverage:</span>{' '}
          {(report.evidenceCoverage * 100).toFixed(0)}%
        </div>
        <div style={{ fontSize: '0.78rem', color: 'var(--color-text-secondary)' }}>
          <span style={{ color: 'var(--color-accent-cyan)' }}>Citation Accuracy:</span>{' '}
          {(report.citationAccuracy * 100).toFixed(0)}%
        </div>
        <div style={{ fontSize: '0.78rem', color: 'var(--color-text-secondary)' }}>
          <span style={{ color: 'var(--color-accent-cyan)' }}>Claims Extracted:</span>{' '}
          {report.claimsExtracted}
        </div>
      </div>

      <div className="divider" />

      {/* Individual claims */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {report.claims.map((claim, idx) => {
          const cfg = CLAIM_STATUS_CONFIG[claim.status];
          const isExpanded = expandedClaim === claim.id;

          return (
            <div
              key={claim.id}
              style={{
                background: 'rgba(255,255,255,0.03)',
                border: `1px solid ${isExpanded ? cfg.color + '40' : 'var(--color-border)'}`,
                borderRadius: 'var(--radius-md)',
                overflow: 'hidden',
                transition: 'border-color var(--transition-fast)',
              }}
            >
              <button
                onClick={() => setExpandedClaim(isExpanded ? null : claim.id)}
                style={{
                  width: '100%',
                  background: 'transparent',
                  border: 'none',
                  padding: '10px 12px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: '8px',
                  textAlign: 'left',
                }}
                aria-expanded={isExpanded}
                aria-controls={`claim-detail-${claim.id}`}
              >
                <span style={{ fontSize: '0.95rem', flexShrink: 0 }}>{cfg.icon}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '3px' }}>
                    <span style={{ fontSize: '0.7rem', fontWeight: 700, color: cfg.color, letterSpacing: '0.05em' }}>
                      CLAIM {idx + 1} — {cfg.label}
                    </span>
                    <span style={{ fontSize: '0.7rem', color: 'var(--color-text-muted)', marginLeft: 'auto' }}>
                      conf: {(claim.confidence * 100).toFixed(0)}%
                    </span>
                    <span style={{ fontSize: '0.7rem', color: 'var(--color-text-muted)' }}>
                      {isExpanded ? '▲' : '▼'}
                    </span>
                  </div>
                  <p style={{ fontSize: '0.8rem', color: 'var(--color-text-secondary)', margin: 0, lineHeight: 1.5 }}>
                    "{claim.text}"
                  </p>
                </div>
              </button>

              {isExpanded && claim.evidence && (
                <div
                  id={`claim-detail-${claim.id}`}
                  style={{
                    padding: '10px 12px 12px 40px',
                    borderTop: `1px solid var(--color-border)`,
                    background: 'rgba(255,255,255,0.02)',
                  }}
                >
                  <p style={{ fontSize: '0.78rem', color: 'var(--color-accent-cyan)', marginBottom: '4px', fontWeight: 600 }}>
                    Supporting Evidence:
                  </p>
                  <p style={{
                    fontSize: '0.78rem',
                    color: 'var(--color-text-secondary)',
                    fontStyle: 'italic',
                    marginBottom: '6px',
                    lineHeight: 1.5,
                  }}>
                    "{claim.evidence}"
                  </p>
                  {claim.sourceRef && (
                    <p style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)' }}>
                      — {claim.sourceRef}
                    </p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default VerificationPanel;
