import React from 'react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from 'recharts';
import type { LatencyMetrics } from '../../types';

interface LatencyChartProps {
  data: LatencyMetrics;
}

const LATENCY_COLORS = ['#10b981', '#3b82f6', '#f59e0b', '#ef4444'];

const LatencyChart: React.FC<LatencyChartProps> = ({ data }) => {
  const chartData = [
    { label: 'P50', value: data.p50 },
    { label: 'P90', value: data.p90 },
    { label: 'P95', value: data.p95 },
    { label: 'P99', value: data.p99 },
  ];

  return (
    <div className="glass-card anim-fade-in" style={{ padding: '18px' }}>
      <div className="section-label" style={{ marginBottom: '14px' }}>Latency Distribution (seconds)</div>
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={chartData} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
          <XAxis
            dataKey="label"
            tick={{ fill: 'var(--color-text-muted)', fontSize: 12 }}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            tick={{ fill: 'var(--color-text-muted)', fontSize: 11 }}
            axisLine={false}
            tickLine={false}
            tickFormatter={v => `${v}s`}
          />
          <Tooltip
            contentStyle={{
              background: 'var(--color-bg-tertiary)',
              border: '1px solid var(--color-border)',
              borderRadius: '8px',
              color: 'var(--color-text-primary)',
              fontSize: '0.8rem',
            }}
            formatter={(value: number) => [`${value}s`, 'Latency']}
            cursor={{ fill: 'rgba(255,255,255,0.04)' }}
          />
          <Bar dataKey="value" radius={[4, 4, 0, 0]}>
            {chartData.map((_, idx) => (
              <Cell key={idx} fill={LATENCY_COLORS[idx]} opacity={0.85} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      {/* Value annotations */}
      <div style={{ display: 'flex', justifyContent: 'space-around', marginTop: '8px' }}>
        {chartData.map((d, i) => (
          <div key={d.label} style={{ textAlign: 'center' }}>
            <div style={{ fontSize: '0.75rem', color: LATENCY_COLORS[i], fontWeight: 700 }}>{d.value}s</div>
            <div style={{ fontSize: '0.65rem', color: 'var(--color-text-muted)' }}>{d.label}</div>
          </div>
        ))}
      </div>
    </div>
  );
};

export default LatencyChart;
