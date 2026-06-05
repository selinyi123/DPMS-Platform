export default function MetricsCard({ label, value, unit, color }) {
  return (
    <div className="metric-card" style={{ borderTopColor: color || '#2563eb' }}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value ?? '-'}</div>
      <div className="metric-unit">{unit}</div>
    </div>
  );
}
