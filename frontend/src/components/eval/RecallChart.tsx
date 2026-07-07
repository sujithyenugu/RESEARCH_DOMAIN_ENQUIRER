import React from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts';
import type { RecallPoint } from '../../types';

interface RecallChartProps {
  data: RecallPoint[];
}

const RecallChart: React.FC<RecallChartProps> = ({ data }) => {
  return (
    <div className="glass-card anim-fade-in" style={{ padding: '18px' }}>
      <div className="section-label" style={{ marginBottom: '14px' }}>Recall@K Trend (14 days)</div>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data} margin={{ top: 4, right: 16, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
          <XAxis
            dataKey="date"
            tick={{ fill: 'var(--color-text-muted)', fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            interval={2}
          />
          <YAxis
            domain={[0.6, 1.0]}
            tick={{ fill: 'var(--color-text-muted)', fontSize: 11 }}
            axisLine={false}
            tickLine={false}
            tickFormatter={v => v.toFixed(1)}
          />
          <Tooltip
            contentStyle={{
              background: 'var(--color-bg-tertiary)',
              border: '1px solid var(--color-border)',
              borderRadius: '8px',
              color: 'var(--color-text-primary)',
              fontSize: '0.8rem',
            }}
            formatter={(value: number, name: string) => [value.toFixed(3), name]}
          />
          <Legend
            wrapperStyle={{ fontSize: '0.78rem', color: 'var(--color-text-muted)', paddingTop: '8px' }}
          />
          <Line
            type="monotone"
            dataKey="recall10"
            name="Recall@10"
            stroke="#3b82f6"
            strokeWidth={2}
            dot={{ r: 3, fill: '#3b82f6' }}
            activeDot={{ r: 5 }}
          />
          <Line
            type="monotone"
            dataKey="recall5"
            name="Recall@5"
            stroke="#06b6d4"
            strokeWidth={2}
            dot={{ r: 3, fill: '#06b6d4' }}
            activeDot={{ r: 5 }}
            strokeDasharray="5 3"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
};

export default RecallChart;
