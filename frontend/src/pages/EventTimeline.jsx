import { useEffect, useMemo, useState } from 'react';

import { fetchJSON } from '../api';
import { useUi } from '../uiContext';

const AGGREGATES = [
  '',
  'account',
  'lottery',
  'task',
  'risk',
  'browser',
  'worker',
  'notification_channel',
  'notification_log',
  'platform',
  'source',
  'lottery_import',
  'system',
];

export default function EventTimeline() {
  const { notify, t } = useUi();
  const [events, setEvents] = useState([]);
  const [filters, setFilters] = useState({ aggregate: '', event_type: '', correlation_id: '' });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: '120' });
    Object.entries(filters).forEach(([key, value]) => {
      if (value.trim()) params.set(key, value.trim());
    });
    return params.toString();
  }, [filters]);

  const load = async () => {
    setLoading(true);
    try {
      const rows = await fetchJSON(`/events/?${query}`);
      setEvents(rows);
      setError('');
    } catch (err) {
      setError(err.message);
      notify(err.message, 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 6000);
    return () => clearInterval(timer);
  }, [query]);

  const updateFilter = (key, value) => setFilters(prev => ({ ...prev, [key]: value }));
  const clearFilters = () => setFilters({ aggregate: '', event_type: '', correlation_id: '' });

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('events.eyebrow')}</p>
          <h1>{t('events.title')}</h1>
        </div>
        <div className="toolbar">
          <button className="btn-ghost" type="button" onClick={clearFilters}>{t('events.clearFilters')}</button>
          <button className="btn-ghost" type="button" onClick={load}>{loading ? t('events.loading') : t('common.refresh')}</button>
        </div>
      </header>

      {error && <div className="alert-danger">{error}</div>}

      <div className="panel">
        <div className="panel-title">{t('events.filters')}</div>
        <div className="form-grid event-filter-form">
          <label>
            <span>{t('events.aggregate')}</span>
            <select className="input" value={filters.aggregate} onChange={e => updateFilter('aggregate', e.target.value)}>
              {AGGREGATES.map(value => (
                <option key={value || 'all'} value={value}>
                  {value ? aggregateLabel(t, value) : t('events.allAggregates')}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('events.eventType')}</span>
            <input
              className="input"
              value={filters.event_type}
              onChange={e => updateFilter('event_type', e.target.value)}
              placeholder="TaskDispatched"
            />
          </label>
          <label>
            <span>{t('events.correlation')}</span>
            <input
              className="input"
              value={filters.correlation_id}
              onChange={e => updateFilter('correlation_id', e.target.value)}
              placeholder="task_id / import_id / log_id"
            />
          </label>
        </div>
      </div>

      <div className="panel">
        <div className="panel-title">{t('events.latest')}</div>
        <div className="table-wrap">
          <table className="data-table event-table">
            <thead>
              <tr>
                <th>{t('events.time')}</th>
                <th>{t('events.aggregate')}</th>
                <th>{t('events.eventType')}</th>
                <th>{t('events.actor')}</th>
                <th>{t('events.service')}</th>
                <th>{t('events.payload')}</th>
              </tr>
            </thead>
            <tbody>
              {events.map(event => (
                <tr key={event.id}>
                  <td className="small-text">{formatTime(event.occurred_at)}</td>
                  <td>
                    <div className="mono">{aggregateLabel(t, event.aggregate)}</div>
                    <div className="small-text muted-text">{event.aggregate_id}</div>
                  </td>
                  <td>
                    <span className="badge badge-info">{event.event_type}</span>
                    {event.correlation_id && <div className="small-text muted-text">{t('events.correlation')}: {event.correlation_id}</div>}
                  </td>
                  <td className="small-text">
                    <div>{event.actor_type || 'system'}</div>
                    <div className="muted-text">{event.actor_id || '-'}</div>
                  </td>
                  <td className="small-text">{event.source_service || '-'}</td>
                  <td><pre className="event-payload">{formatPayload(event.payload)}</pre></td>
                </tr>
              ))}
              {!events.length && <tr><td className="empty-cell" colSpan="6">{loading ? t('events.loading') : t('events.noEvents')}</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function formatPayload(payload) {
  if (payload === null || payload === undefined) return '{}';
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}

function formatTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function aggregateLabel(t, value) {
  const label = t(`events.aggregates.${value}`);
  return label === `events.aggregates.${value}` ? value : label;
}
