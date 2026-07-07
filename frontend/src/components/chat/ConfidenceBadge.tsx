import React from 'react';
import type { ConfidenceLevel, ActionType } from '../../types';

interface ConfidenceBadgeProps {
  confidence: number;
  level: ConfidenceLevel;
  action: ActionType;
}

const LEVEL_CONFIG: Record<ConfidenceLevel, { label: string; className: string; icon: string }> = {
  high:    { label: 'High Confidence',   className: 'badge-high',    icon: '✅' },
  medium:  { label: 'Medium Confidence', className: 'badge-medium',  icon: '⚠️' },
  low:     { label: 'Low Confidence',    className: 'badge-low',     icon: '🟠' },
  refused: { label: 'Refused',           className: 'badge-refused', icon: '🚫' },
};

const ACTION_CONFIG: Record<ActionType, { className: string }> = {
  PASS:                 { className: 'badge-pass' },
  PASS_WITH_DISCLAIMER: { className: 'badge-disclaimer' },
  WARN:                 { className: 'badge-warn' },
  REFUSE:               { className: 'badge-refuse' },
};

const ConfidenceBadge: React.FC<ConfidenceBadgeProps> = ({ confidence, level, action }) => {
  const lvl = LEVEL_CONFIG[level];
  const act = ACTION_CONFIG[action];

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
      <span className={`badge ${lvl.className}`} title={`Confidence score: ${confidence.toFixed(2)}`}>
        {lvl.icon} {lvl.label} ({(confidence * 100).toFixed(0)}%)
      </span>
      <span className={`badge ${act.className}`}>
        {action.replace(/_/g, ' ')}
      </span>
    </div>
  );
};

export default ConfidenceBadge;
