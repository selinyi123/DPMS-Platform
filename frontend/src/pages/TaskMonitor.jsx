import { useEffect, useRef, useState } from 'react';

import { apiPath } from '../api';
import { useUi } from '../uiContext';

export default function TaskMonitor() {
  const { notify, t } = useUi();
  const [logs, setLogs] = useState([]);
  const [connection, setConnection] = useState('connecting');
  const bottomRef = useRef(null);
  const lastErrorRef = useRef('');

  useEffect(() => {
    const source = new EventSource(apiPath('/metrics/stream'));
    source.onopen = () => {
      setConnection('connected');
      lastErrorRef.current = '';
    };
    source.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        setLogs(prev => [...prev.slice(-200), data]);
      } catch {
        if (lastErrorRef.current !== 'bad_sse_payload') {
          notify(t('tasks.badPayload'), 'error');
          lastErrorRef.current = 'bad_sse_payload';
        }
        setLogs(prev => [...prev.slice(-200), { level: 'error', event: 'bad_sse_payload', ts: Date.now() }]);
      }
    };
    source.onerror = () => {
      setConnection('disconnected');
      if (lastErrorRef.current !== 'sse_disconnected') {
        notify(t('tasks.streamDisconnected'), 'error');
        lastErrorRef.current = 'sse_disconnected';
      }
      setLogs(prev => [...prev.slice(-200), { level: 'error', event: 'sse_disconnected', ts: Date.now() }]);
    };
    return () => source.close();
  }, [notify, t]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [logs]);

  const connectionClass = connection === 'connected' ? 'badge-ready' : connection === 'connecting' ? 'badge-warn' : 'badge-danger';

  return (
    <section className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('tasks.eyebrow')}</p>
          <h1>{t('tasks.title')}</h1>
        </div>
        <span className={`badge ${connectionClass}`}>{t(`tasks.${connection}`)}</span>
      </header>
      <div className="panel">
        <div className="log-panel">
          {logs.map((log, i) => (
            <div key={i} className={`log-line log-${log.level || 'info'}`}>
              <span className="log-time">{new Date(log.ts).toLocaleTimeString()}</span>
              <span>[{log.event}]</span>
              {log.task_id && <span className="mono">{t('tasks.task')} {log.task_id.slice(0, 8)}</span>}
              {log.phase && <span>{t('tasks.phase')} {log.phase}</span>}
              {log.account_id && <span>{t('tasks.account')} A{log.account_id}</span>}
            </div>
          ))}
          {!logs.length && <div className="empty-cell">{t('tasks.waiting')}</div>}
          <div ref={bottomRef} />
        </div>
      </div>
    </section>
  );
}
