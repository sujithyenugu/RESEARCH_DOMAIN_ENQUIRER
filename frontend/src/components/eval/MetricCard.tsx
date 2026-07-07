import React from 'react';
import type { MetricValue } from '../../types';

interface MetricCardProps {
  metric: MetricValue;
  index?: number;
}

const MetricCard: React.FC<MetricCardProps> = ({ metric, index = 0 }) => {
  const passFail = metric.pass ? 'PASS' : 'FAIL';
  const passColor = metric.pass ? 'var(--color-accent-green)' : 'var(--color-accent-red)';
  const deltaColor = metric.delta7d > 0
    ? 'var(--color-accent-green)'
    : metric.delta7d < 0
      ? 'var(--color-accent-red)'
      : 'var(--color-text-muted)';
  const deltaArrow = metric.delta7d > 0 ? '▲' : metric.delta7d < 0 ? '▼' : '─';
  const pctComplete = (metric.value / 1) * 100;
  const targetPct = (metric.target / 1) * 100;

  return (
    <div
      className="glass-card anim-fade-in"
      style={{
        padding: '18px 20px',
        animationDelay: `${index * 80}ms`,
        border: `1px solid ${metric.pass ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)'}`,
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      {/* Background glow */}
      <div style={{
        position: 'absolute',
        top: 0, right: 0,
        width: '80px', height: '80px',
        borderRadius: '50%',
        background: passColor,
        opacity: 0.04,
        transform: 'translate(25%, -25%)',
        pointerEvents: 'none',
      }} />

      {/* Name + badge */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '10px' }}>
        <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--color-text-muted)', letterSpacing: '0.03em' }}>
          {metric.name}
        </span>
        <span
          className="badge"
          style={{
            background: `${passColor}18`,
            color: passColor,
            border: `1px solid ${passColor}35`,
            fontSize: '0.65rem',
          }}
        >
          {metric.pass ? '✅' : '❌'} {passFail}
        </span>
      </div>

      {/* Value */}
      <div style={{
        fontSize: '2rem',
        fontWeight: 800,
        color: 'var(--color-text-primary)',
        lineHeight: 1,
        marginBottom: '8px',
        fontVariantNumeric: 'tabular-nums',
      }}>
        {metric.value.toFixed(2)}
      </div>

      {/* Progress bar */}
      <div style={{
        height: '4px',
        background: 'var(--color-bg-tertiary)',
        borderRadius: 'var(--radius-full)',
        marginBottom: '10px',
        position: 'relative',
        overflow: 'visible',
      }}>
        <div style={{
          height: '100%',
          width: `${pctComplete}%`,
          background: `linear-gradient(90deg, ${passColor}80, ${passColor})`,
          borderRadius: 'var(--radius-full)',
          transition: 'width 1s ease',
        }} />
        {/* Target marker */}
        <div style={{
          position: 'absolute',
          top: '-3px',
          left: `${targetPct}%`,
          width: '2px',
          height: '10px',
          background: 'var(--color-text-muted)',
          borderRadius: '1px',
        }} />
      </div>

      {/* Delta + target */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: '0.75rem', color: deltaColor, fontWeight: 600 }}>
          {deltaArrow} {metric.delta7d > 0 ? '+' : ''}{metric.delta7d.toFixed(2)} (7d)
        </span>
        <span style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)' }}>
          Target: <strong style={{ color: 'var(--color-text-secondary)' }}>{metric.target.toFixed(2)}</strong>
        </span>
      </div>
    </div>
  );
};

export default MetricCard;
