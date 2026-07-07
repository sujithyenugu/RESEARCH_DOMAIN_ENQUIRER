import React, { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import MetricCard from '../components/eval/MetricCard';
import LatencyChart from '../components/eval/LatencyChart';
import RecallChart from '../components/eval/RecallChart';
import { getEvaluationLatest, getIngestionStatus, triggerEvaluation } from '../services/api';
import './EvaluationPage.css';

const CONFIDENCE_COLORS = ['#10b981', '#3b82f6', '#f97316', '#ef4444'];

const EvaluationPage: React.FC = () => {
  const [evalTriggered, setEvalTriggered] = useState(false);

  const { data: metrics, isLoading, refetch } = useQuery({
    queryKey: ['evaluation-latest'],
    queryFn: getEvaluationLatest,
    refetchInterval: 60_000,
  });

  const { data: ingestion } = useQuery({
    queryKey: ['ingestion-status'],
    queryFn: getIngestionStatus,
    refetchInterval: 30_000,
  });

  const triggerMutation = useMutation({
    mutationFn: triggerEvaluation,
    onSuccess: () => {
      setEvalTriggered(true);
      setTimeout(() => {
        setEvalTriggered(false);
        refetch();
      }, 3000);
    },
  });

  if (isLoading || !metrics) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', flexDirection: 'column', gap: '16px' }}>
        <div className="anim-spin" style={{ fontSize: '2rem' }}>⟳</div>
        <p style={{ color: 'var(--color-text-muted)' }}>Loading evaluation metrics…</p>
      </div>
    );
  }

  const confidenceData = [
    { name: 'High (≥0.85)', value: metrics.confidenceDistribution.high },
    { name: 'Medium (0.6-0.85)', value: metrics.confidenceDistribution.medium },
    { name: 'Low (<0.60)', value: metrics.confidenceDistribution.low },
    { name: 'Refused', value: metrics.confidenceDistribution.refused },
  ];

  const lastRunDate = new Date(metrics.timestamp).toLocaleString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZone: 'UTC', timeZoneName: 'short',
  });

  const status = ingestion?.status ?? 'healthy';
  const statusColor = status === 'healthy' ? 'var(--color-accent-green)' : status === 'degraded' ? 'var(--color-accent-amber)' : 'var(--color-accent-red)';

  return (
    <div className="eval-page scroll-panel">
      {/* Page header */}
      <div className="eval-header">
        <div>
          <h1 style={{ fontSize: '1.2rem', fontWeight: 700, marginBottom: '4px' }}>
            📊 Evaluation Dashboard
          </h1>
          <p style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>
            Last run: {lastRunDate}
          </p>
        </div>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
          {evalTriggered && (
            <span className="badge badge-pass anim-fade-in">✅ Evaluation queued!</span>
          )}
          <button
            className="btn btn-primary"
            onClick={() => triggerMutation.mutate()}
            disabled={triggerMutation.isPending || evalTriggered}
            aria-label="Trigger evaluation run"
          >
            {triggerMutation.isPending ? (
              <span className="anim-spin" style={{ display: 'inline-block' }}>⟳</span>
            ) : '▶'} Run Evaluation Now
          </button>
        </div>
      </div>

      <div className="eval-content">
        {/* Retrieval Metrics */}
        <section className="eval-section">
          <div className="section-label" style={{ marginBottom: '12px' }}>
            🔍 Retrieval Metrics
          </div>
          <div className="metrics-grid stagger">
            {Object.values(metrics.retrieval).map((m, i) => (
              <MetricCard key={m.name} metric={m} index={i} />
            ))}
          </div>
        </section>

        {/* Generation Metrics */}
        <section className="eval-section">
          <div className="section-label" style={{ marginBottom: '12px' }}>
            🧠 Generation Metrics
          </div>
          <div className="metrics-grid stagger">
            {Object.values(metrics.generation).map((m, i) => (
              <MetricCard key={m.name} metric={m} index={i} />
            ))}
          </div>
        </section>

        {/* Charts row */}
        <div className="charts-row">
          {/* Latency chart */}
          <LatencyChart data={metrics.latency} />

          {/* Confidence distribution */}
          <div className="glass-card anim-fade-in" style={{ padding: '18px' }}>
            <div className="section-label" style={{ marginBottom: '14px' }}>Confidence Distribution</div>
            <ResponsiveContainer width="100%" height={180}>
              <PieChart>
                <Pie
                  data={confidenceData}
                  cx="45%"
                  cy="50%"
                  innerRadius={45}
                  outerRadius={75}
                  paddingAngle={3}
                  dataKey="value"
                >
                  {confidenceData.map((_, index) => (
                    <Cell key={index} fill={CONFIDENCE_COLORS[index]} opacity={0.85} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{
                    background: 'var(--color-bg-tertiary)',
                    border: '1px solid var(--color-border)',
                    borderRadius: '8px',
                    fontSize: '0.8rem',
                  }}
                  formatter={(value: number) => [`${value}%`, '']}
                />
                <Legend
                  wrapperStyle={{ fontSize: '0.72rem', color: 'var(--color-text-muted)' }}
                  iconSize={8}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Recall trend */}
        <section className="eval-section">
          <RecallChart data={metrics.recallTrend} />
        </section>

        {/* Ingestion status */}
        <section className="eval-section">
          <div className="section-label" style={{ marginBottom: '12px' }}>
            🔄 Ingestion Pipeline Status
          </div>
          <div className="glass-card anim-fade-in" style={{ padding: '20px' }}>
            {!ingestion ? (
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <div className="skeleton" style={{ width: '100%', height: '60px', borderRadius: 'var(--radius-md)' }} />
              </div>
            ) : (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
                  <span style={{ width: 10, height: 10, borderRadius: '50%', background: statusColor, display: 'inline-block' }} />
                  <span style={{ fontSize: '0.85rem', fontWeight: 600, color: statusColor, textTransform: 'uppercase' }}>
                    {status}
                  </span>
                </div>
                <div className="ingestion-grid">
                  {[
                    { label: 'Papers Indexed', value: ingestion.papersIndexed.toLocaleString(), color: 'var(--color-accent-blue)' },
                    { label: 'New Today', value: ingestion.newToday.toString(), color: 'var(--color-accent-green)' },
                    { label: 'Failed Today', value: ingestion.failedToday.toString(), color: ingestion.failedToday > 0 ? 'var(--color-accent-red)' : 'var(--color-text-muted)' },
                    { label: 'Entities in Graph', value: ingestion.entitiesInGraph.toLocaleString(), color: 'var(--color-accent-purple)' },
                    { label: 'Graph Edges', value: ingestion.edgesInGraph.toLocaleString(), color: 'var(--color-accent-cyan)' },
                    { label: 'Paper Queue', value: ingestion.paperQueueDepth.toString(), color: 'var(--color-text-muted)' },
                    { label: 'DLQ Depth', value: ingestion.dlqDepth.toString(), color: ingestion.dlqDepth > 0 ? 'var(--color-accent-amber)' : 'var(--color-text-muted)' },
                    { label: 'Last Fetch', value: new Date(ingestion.lastFetchUtc).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC', color: 'var(--color-text-secondary)' },
                  ].map(item => (
                    <div key={item.label} className="ingestion-stat glass-card">
                      <div style={{ fontSize: '0.65rem', color: 'var(--color-text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                        {item.label}
                      </div>
                      <div style={{ fontSize: '1.1rem', fontWeight: 700, color: item.color }}>
                        {item.value}
                      </div>
                    </div>
                  ))}
                </div>

                <div style={{ display: 'flex', gap: '16px', marginTop: '14px', flexWrap: 'wrap', fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>
                  <span>Next fetch: <strong style={{ color: 'var(--color-text-secondary)' }}>
                    {new Date(ingestion.nextFetchUtc).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} UTC
                  </strong></span>
                  {ingestion.dlqDepth > 0 && (
                    <button className="btn btn-danger" style={{ fontSize: '0.75rem', padding: '3px 10px' }}>
                      🗑️ View DLQ Items ({ingestion.dlqDepth})
                    </button>
                  )}
                </div>
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  );
};

export default EvaluationPage;
