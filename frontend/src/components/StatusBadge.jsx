import { useUi } from '../uiContext';

const map = {
  ready: 'badge-ready',
  gray: 'badge-info',
  login_required: 'badge-warn',
  warming: 'badge-info',
  cooling: 'badge-cooling',
  frozen: 'badge-frozen',
  executing: 'badge-executing',
  pending: 'badge-warn',
  claimed: 'badge-info',
  running: 'badge-executing',
  participated: 'badge-info',
  won: 'badge-ready',
  lost: 'badge-muted',
  succeeded: 'badge-ready',
  failed: 'badge-danger',
  queued: 'badge-warn',
  opening: 'badge-info',
  waiting_scan: 'badge-warn',
  scanned: 'badge-info',
  confirmed: 'badge-ready',
  expired: 'badge-muted',
};

export default function StatusBadge({ status }) {
  const { t } = useUi();
  const className = map[status] || 'badge-muted';
  const label = status ? t(`status.${status}`) : t('status.unknown');
  return <span className={`badge ${className}`}>{label}</span>;
}
