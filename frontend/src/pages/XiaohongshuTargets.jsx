import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { fetchJSON, postJSON, putJSON } from '../api';
import { formatText } from '../i18n/format';
import { useUi } from '../uiContext';
import {
  XHS_OFFLINE_MAX_BYTES,
  XHS_DECISION_REASON_MAX_LENGTH,
  XHS_KEYWORD_MAX_LENGTH,
  buildCandidateDecisionPayload,
  buildCandidateIngestPayload,
  buildXiaohongshuScanPayload,
  candidateDecisionCanSubmit,
  candidateItemsFromResponse,
  normalizeXiaohongshuCandidate,
  parseOfflineSearchResult,
} from '../xiaohongshuTargetPursuit';

const CANDIDATE_LIMIT = 100;
const SCAN_TIMEOUT_MS = 120_000;
const SOURCE_FILTERS = ['', 'keyword', 'author_profile', 'offline_search_result'];
const STATUS_FILTERS = ['', 'pending', 'accepted', 'needs_review', 'skipped'];
const DECISION_BUTTON_ORDER = ['accepted', 'needs_review', 'skipped'];

function localizedError(error, t) {
  const code = String(error?.code || error?.message || '').trim();
  if (code) {
    const exactKey = `xhsTargets.errors.${code}`;
    const exact = t(exactKey);
    if (exact !== exactKey) return exact;
    const knownCode = code.match(/(?:xhs|xiaohongshu)_target_[a-z0-9_]+/i)?.[0];
    if (knownCode) {
      const key = `xhsTargets.errors.${knownCode}`;
      const translated = t(key);
      if (translated !== key) return translated;
    }
  }
  return error?.message || t('xhsTargets.errors.unknown');
}

function displayValue(value) {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function compactValue(value) {
  const rendered = displayValue(value);
  return rendered.length > 240 ? `${rendered.slice(0, 240)}…` : rendered;
}

function statusBadgeClass(status) {
  if (status === 'accepted') return 'badge-ready';
  if (status === 'needs_review') return 'badge-warn';
  if (status === 'skipped') return 'badge-muted';
  return 'badge-info';
}

function decisionButtonClass(status) {
  if (status === 'accepted') return 'btn-primary';
  if (status === 'skipped') return 'btn-ghost';
  return 'btn-ghost xhs-targets-review-button';
}

function VerificationItem({ label, value, detail, t }) {
  const state = value === true ? 'yes' : value === false ? 'no' : 'unknown';
  const className = value === true
    ? 'badge-ready'
    : value === false
      ? 'badge-danger'
      : 'badge-muted';
  return (
    <div className="xhs-targets-verification-item">
      <div>
        <span className="xhs-targets-field-label">{label}</span>
        <span className={`badge ${className}`}>
          {t(`xhsTargets.verification.${state}`)}
        </span>
      </div>
      {detail && (
        <details>
          <summary>{t('xhsTargets.viewEvidence')}</summary>
          <pre>{displayValue(detail)}</pre>
        </details>
      )}
    </div>
  );
}

function Snapshot({ title, value, t }) {
  const exists = value !== null
    && value !== undefined
    && value !== ''
    && (typeof value !== 'object' || value.present !== false);
  return (
    <details className={`xhs-targets-snapshot ${exists ? 'has-evidence' : ''}`}>
      <summary>
        <span>{title}</span>
        <span className={`badge ${exists ? 'badge-ready' : 'badge-muted'}`}>
          {exists ? t('xhsTargets.captured') : t('xhsTargets.notCaptured')}
        </span>
      </summary>
      <pre>{exists ? displayValue(value) : t('xhsTargets.noSnapshot')}</pre>
    </details>
  );
}

function ValuePanel({ label, value, emptyLabel }) {
  const rendered = displayValue(value);
  return (
    <div className="xhs-targets-value-panel">
      <span>{label}</span>
      <pre>{rendered || emptyLabel}</pre>
    </div>
  );
}

function ChipList({ values, emptyLabel }) {
  if (!values?.length) return <span className="muted-text small-text">{emptyLabel}</span>;
  return (
    <div className="xhs-targets-chip-list">
      {values.map((value, index) => (
        <span className="badge badge-muted" key={`${compactValue(value)}-${index}`}>
          {compactValue(value)}
        </span>
      ))}
    </div>
  );
}

function SourceHitList({ hits, t }) {
  if (!hits.length) return <span className="muted-text small-text">{t('xhsTargets.noSourceHits')}</span>;
  return (
    <div className="xhs-targets-source-hits">
      {hits.map((hit, index) => (
        <div
          className="xhs-targets-source-hit"
          key={hit.id || `${hit.source_type}-${hit.source_value}-${index}`}
        >
          <span className="badge badge-info">
            {t(`xhsTargets.sourceTypes.${hit.source_type}`)}
          </span>
          <span>{hit.source_value || '-'}</span>
          <span className="muted-text small-text">
            {formatText(t('xhsTargets.hitCount'), { count: hit.hit_count ?? 1 })}
          </span>
        </div>
      ))}
    </div>
  );
}

function CandidateCard({
  candidate,
  busy,
  reason,
  setReason,
  decide,
  t,
}) {
  const targetUrl = candidate.canonicalUrl || candidate.rawUrl;
  const title = candidate.title || formatText(t('xhsTargets.untitledCandidate'), {
    id: candidate.id,
  });
  return (
    <article className="panel xhs-targets-candidate-card">
      <header className="xhs-targets-candidate-header">
        <div className="xhs-targets-candidate-title">
          <div className="toolbar">
            <span className={`badge ${statusBadgeClass(candidate.decisionStatus)}`}>
              {t(`xhsTargets.statuses.${candidate.decisionStatus}`)}
            </span>
            {candidate.acceptedLotteryId && (
              <span className="badge badge-ready">
                {formatText(t('xhsTargets.acceptedLottery'), {
                  id: candidate.acceptedLotteryId,
                })}
              </span>
            )}
            {candidate.analysis.initialDecision && (
              <span className="badge badge-muted">
                {formatText(t('xhsTargets.initialDecision'), {
                  value: compactValue(candidate.analysis.initialDecision),
                })}
              </span>
            )}
          </div>
          <h2>{title}</h2>
          <div className="xhs-targets-candidate-meta">
            <span className="mono">#{candidate.id}</span>
            <span>{formatText(t('xhsTargets.version'), { version: candidate.version })}</span>
            {candidate.lastSeenAt && (
              <span>{formatText(t('xhsTargets.lastSeen'), { value: candidate.lastSeenAt })}</span>
            )}
          </div>
        </div>
        {targetUrl ? (
          <a
            className="btn-ghost xhs-targets-open-link"
            href={targetUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('xhsTargets.openTarget')}
          </a>
        ) : (
          <span className="badge badge-danger">{t('xhsTargets.invalidTarget')}</span>
        )}
      </header>

      <div className="xhs-targets-verification-grid">
        <VerificationItem
          label={t('xhsTargets.collectionVerification')}
          value={candidate.verification.collection}
          detail={candidate.verificationDetails.collection}
          t={t}
        />
        <VerificationItem
          label={t('xhsTargets.originalPostVerification')}
          value={candidate.verification.originalPost}
          detail={candidate.verificationDetails.originalPost}
          t={t}
        />
        <VerificationItem
          label={t('xhsTargets.authorVerification')}
          value={candidate.verification.author}
          detail={candidate.verificationDetails.author}
          t={t}
        />
      </div>

      <div className="xhs-targets-snapshot-grid">
        <Snapshot title={t('xhsTargets.bodySnapshot')} value={candidate.bodySnapshot} t={t} />
        <Snapshot
          title={t('xhsTargets.expandedSnapshot')}
          value={candidate.expandedSnapshot}
          t={t}
        />
        <Snapshot
          title={t('xhsTargets.pinnedCommentSnapshot')}
          value={candidate.pinnedCommentSnapshot}
          t={t}
        />
      </div>

      <div className="xhs-targets-facts-grid">
        <ValuePanel
          label={t('xhsTargets.timing')}
          value={candidate.timing}
          emptyLabel={t('xhsTargets.unknown')}
        />
        <ValuePanel
          label={t('xhsTargets.prize')}
          value={candidate.prize}
          emptyLabel={t('xhsTargets.unknown')}
        />
        <div className="xhs-targets-value-panel">
          <span>{t('xhsTargets.actions')}</span>
          <ChipList values={candidate.actions} emptyLabel={t('xhsTargets.noneDetected')} />
        </div>
        <div className="xhs-targets-value-panel">
          <span>{t('xhsTargets.complexConditions')}</span>
          <ChipList
            values={candidate.complexConditions}
            emptyLabel={t('xhsTargets.noneDetected')}
          />
        </div>
      </div>

      {(candidate.analysis.confidence !== null || candidate.analysis.reasonCodes.length > 0) && (
        <div className="xhs-targets-analysis">
          {candidate.analysis.confidence !== null && (
            <span className="badge badge-info">
              {formatText(t('xhsTargets.confidence'), {
                value: compactValue(candidate.analysis.confidence),
              })}
            </span>
          )}
          <ChipList
            values={candidate.analysis.reasonCodes}
            emptyLabel={t('xhsTargets.noneDetected')}
          />
        </div>
      )}

      <div className="xhs-targets-source-section">
        <span className="xhs-targets-field-label">{t('xhsTargets.sourceHits')}</span>
        <SourceHitList hits={candidate.sourceHits} t={t} />
      </div>

      <details className="xhs-targets-raw-evidence">
        <summary>{t('xhsTargets.rawEvidence')}</summary>
        <div>
          <ValuePanel
            label={t('xhsTargets.evidence')}
            value={candidate.evidence}
            emptyLabel={t('xhsTargets.unknown')}
          />
          <ValuePanel
            label={t('xhsTargets.rule')}
            value={candidate.rule}
            emptyLabel={t('xhsTargets.unknown')}
          />
          <ValuePanel
            label={t('xhsTargets.classification')}
            value={candidate.classification}
            emptyLabel={t('xhsTargets.unknown')}
          />
        </div>
      </details>

      <div className="xhs-targets-decision">
        <label>
          <span>{t('xhsTargets.decisionReason')}</span>
          <input
            className="input"
            value={reason}
            maxLength={XHS_DECISION_REASON_MAX_LENGTH}
            onChange={event => setReason(event.target.value)}
            placeholder={t('xhsTargets.decisionReasonPlaceholder')}
          />
        </label>
        <div className="toolbar">
          {DECISION_BUTTON_ORDER.map(status => (
            <button
              type="button"
              className={decisionButtonClass(status)}
              disabled={busy || !candidateDecisionCanSubmit(candidate, status)}
              onClick={() => decide(candidate, status)}
              key={status}
            >
              {busy
                ? t('xhsTargets.saving')
                : t(`xhsTargets.decisions.${status}`)}
            </button>
          ))}
        </div>
        {candidate.decisionReason && (
          <p className="muted-text small-text">
            {formatText(t('xhsTargets.currentReason'), {
              value: candidate.decisionReason,
            })}
          </p>
        )}
      </div>
    </article>
  );
}

export default function XiaohongshuTargets() {
  const { notify, t } = useUi();
  const fileInputRef = useRef(null);
  const [keyword, setKeyword] = useState('抽奖');
  const [authorProfile, setAuthorProfile] = useState('');
  const [offlinePreview, setOfflinePreview] = useState(null);
  const [sourceBusy, setSourceBusy] = useState('');
  const [decisionBusy, setDecisionBusy] = useState(null);
  const [decisionReasons, setDecisionReasons] = useState({});
  const [filterStatus, setFilterStatus] = useState('');
  const [filterSource, setFilterSource] = useState('');
  const [candidates, setCandidates] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const loadCandidates = useCallback(async ({ signal } = {}) => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: String(CANDIDATE_LIMIT) });
      if (filterStatus) params.set('decision_status', filterStatus);
      if (filterSource) params.set('source_type', filterSource);
      const response = await fetchJSON(
        `/xiaohongshu-targets/candidates?${params.toString()}`,
        { signal },
      );
      const items = candidateItemsFromResponse(response)
        .map(normalizeXiaohongshuCandidate);
      setCandidates(items);
      setTotal(Number(response?.total ?? items.length));
      setError('');
    } catch (loadError) {
      if (loadError?.name !== 'AbortError') {
        setError(localizedError(loadError, t));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [filterSource, filterStatus, t]);

  useEffect(() => {
    const controller = new AbortController();
    void loadCandidates({ signal: controller.signal });
    return () => controller.abort();
  }, [loadCandidates]);

  const runScan = async (sourceType, sourceValue) => {
    setSourceBusy(sourceType);
    setError('');
    try {
      const payload = buildXiaohongshuScanPayload(sourceType, sourceValue);
      const response = await postJSON('/xiaohongshu-targets/scan', payload, {
        timeoutMs: SCAN_TIMEOUT_MS,
      });
      notify(formatText(t('xhsTargets.scanComplete'), {
        received: response?.received ?? response?.received_count ?? 0,
        created: response?.created_count ?? 0,
        updated: response?.updated_count ?? 0,
        invalid: response?.invalid_count ?? 0,
      }), 'success');
      await loadCandidates();
    } catch (scanError) {
      const message = localizedError(scanError, t);
      setError(message);
      notify(message, 'error');
    } finally {
      setSourceBusy('');
    }
  };

  const selectOfflineFile = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setError('');
    setOfflinePreview(null);
    try {
      if (file.size > XHS_OFFLINE_MAX_BYTES) {
        const tooLarge = new Error('xhs_target_offline_too_large');
        tooLarge.code = 'xhs_target_offline_too_large';
        throw tooLarge;
      }
      const result = parseOfflineSearchResult(file.name, await file.text());
      setOfflinePreview({ fileName: file.name, ...result });
      notify(formatText(t('xhsTargets.offlineReady'), {
        count: result.targetCount,
        discarded: result.discardedSensitiveFields,
        skipped: result.discardedRows,
      }), 'success');
    } catch (fileError) {
      const message = localizedError(fileError, t);
      setError(message);
      notify(message, 'error');
      event.target.value = '';
    }
  };

  const ingestOffline = async () => {
    if (!offlinePreview) return;
    setSourceBusy('offline_search_result');
    setError('');
    try {
      const payload = buildCandidateIngestPayload(
        {
          source_type: 'offline_search_result',
          source_value: offlinePreview.fileName,
        },
        offlinePreview.candidates,
      );
      const response = await postJSON('/xiaohongshu-targets/candidates/ingest', payload);
      notify(formatText(t('xhsTargets.ingestComplete'), {
        received: response?.received ?? payload.candidates.length,
        created: response?.created_count ?? 0,
        updated: response?.updated_count ?? 0,
        invalid: response?.invalid_count ?? 0,
      }), 'success');
      setOfflinePreview(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      await loadCandidates();
    } catch (ingestError) {
      const message = localizedError(ingestError, t);
      setError(message);
      notify(message, 'error');
    } finally {
      setSourceBusy('');
    }
  };

  const decide = async (candidate, status) => {
    if (!candidateDecisionCanSubmit(candidate, status)) return;
    setDecisionBusy(candidate.id);
    setError('');
    try {
      const payload = buildCandidateDecisionPayload(
        status,
        decisionReasons[candidate.id],
        candidate.version,
      );
      const response = await putJSON(
        `/xiaohongshu-targets/candidates/${encodeURIComponent(candidate.id)}/decision`,
        payload,
      );
      const responseItem = response?.candidate || response?.item || (
        response?.id ? response : null
      );
      if (responseItem) {
        const updated = normalizeXiaohongshuCandidate(responseItem);
        setCandidates(current => current.map(item => (
          item.id === candidate.id ? updated : item
        )));
      } else {
        await loadCandidates();
      }
      setDecisionReasons(current => ({ ...current, [candidate.id]: '' }));
      notify(t(`xhsTargets.decisionSaved.${status}`), 'success');
    } catch (decisionError) {
      const message = localizedError(decisionError, t);
      setError(message);
      notify(message, 'error');
      if (String(decisionError?.message || '').startsWith('409:')) {
        await loadCandidates();
      }
    } finally {
      setDecisionBusy(null);
    }
  };

  const previewCandidates = useMemo(
    () => offlinePreview?.candidates?.slice(0, 3) || [],
    [offlinePreview],
  );

  return (
    <section className="page-stack xhs-targets-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t('xhsTargets.eyebrow')}</p>
          <h1>{t('xhsTargets.title')}</h1>
        </div>
        <button
          type="button"
          className="btn-ghost"
          disabled={loading}
          onClick={() => loadCandidates()}
        >
          {loading ? t('xhsTargets.loading') : t('common.refresh')}
        </button>
      </header>

      <p className="muted-text small-text">{t('xhsTargets.subtitle')}</p>
      <div className="notice xhs-targets-guardrail">
        <strong>{t('xhsTargets.guardrailTitle')}</strong>
        <span>{t('xhsTargets.guardrail')}</span>
      </div>
      {error && <div className="alert-danger" role="alert">{error}</div>}

      <div className="xhs-targets-source-grid">
        <section className="panel xhs-targets-source-card">
          <div>
            <span className="badge badge-info">{t('xhsTargets.sourceTypes.keyword')}</span>
            <h2>{t('xhsTargets.keywordTitle')}</h2>
            <p>{t('xhsTargets.keywordHint')}</p>
          </div>
          <label>
            <span>{t('xhsTargets.keyword')}</span>
            <input
              className="input"
              value={keyword}
              maxLength={XHS_KEYWORD_MAX_LENGTH}
              onChange={event => setKeyword(event.target.value)}
              placeholder={t('xhsTargets.keywordPlaceholder')}
            />
          </label>
          <button
            type="button"
            className="btn-primary"
            disabled={Boolean(sourceBusy) || !keyword.trim()}
            onClick={() => runScan('keyword', keyword)}
          >
            {sourceBusy === 'keyword' ? t('xhsTargets.scanning') : t('xhsTargets.scan')}
          </button>
        </section>

        <section className="panel xhs-targets-source-card">
          <div>
            <span className="badge badge-info">
              {t('xhsTargets.sourceTypes.author_profile')}
            </span>
            <h2>{t('xhsTargets.authorTitle')}</h2>
            <p>{t('xhsTargets.authorHint')}</p>
          </div>
          <label>
            <span>{t('xhsTargets.authorProfile')}</span>
            <input
              className="input"
              type="url"
              value={authorProfile}
              maxLength={256}
              onChange={event => setAuthorProfile(event.target.value)}
              placeholder={t('xhsTargets.authorPlaceholder')}
            />
          </label>
          <button
            type="button"
            className="btn-primary"
            disabled={Boolean(sourceBusy) || !authorProfile.trim()}
            onClick={() => runScan('author_profile', authorProfile)}
          >
            {sourceBusy === 'author_profile'
              ? t('xhsTargets.scanning')
              : t('xhsTargets.scan')}
          </button>
        </section>

        <section className="panel xhs-targets-source-card">
          <div>
            <span className="badge badge-info">
              {t('xhsTargets.sourceTypes.offline_search_result')}
            </span>
            <h2>{t('xhsTargets.offlineTitle')}</h2>
            <p>{t('xhsTargets.offlineHint')}</p>
          </div>
          <label className="target-file-button file-button">
            {t('xhsTargets.selectFile')}
            <input
              ref={fileInputRef}
              hidden
              type="file"
              accept=".json,.jsonl,.csv,application/json,text/csv"
              onChange={selectOfflineFile}
            />
          </label>
          {offlinePreview ? (
            <div className="xhs-targets-offline-preview">
              <strong>{offlinePreview.fileName}</strong>
              <span>
                {formatText(t('xhsTargets.offlineSummary'), {
                  count: offlinePreview.targetCount,
                  discarded: offlinePreview.discardedSensitiveFields,
                  skipped: offlinePreview.discardedRows,
                })}
              </span>
              <ul>
                {previewCandidates.map(candidate => (
                  <li key={candidate.raw_url}>
                    {candidate.title || candidate.raw_url}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="muted-text small-text">{t('xhsTargets.noFile')}</p>
          )}
          <button
            type="button"
            className="btn-primary"
            disabled={Boolean(sourceBusy) || !offlinePreview}
            onClick={ingestOffline}
          >
            {sourceBusy === 'offline_search_result'
              ? t('xhsTargets.ingesting')
              : t('xhsTargets.ingest')}
          </button>
        </section>
      </div>

      <section className="panel xhs-targets-queue-panel">
        <div className="xhs-targets-queue-header">
          <div>
            <div className="panel-title">{t('xhsTargets.candidateQueue')}</div>
            <p className="muted-text small-text">
              {formatText(t('xhsTargets.queueCount'), {
                visible: candidates.length,
                total,
              })}
            </p>
          </div>
          <div className="xhs-targets-filters">
            <label>
              <span>{t('xhsTargets.statusFilter')}</span>
              <select
                className="input"
                value={filterStatus}
                onChange={event => setFilterStatus(event.target.value)}
              >
                {STATUS_FILTERS.map(status => (
                  <option value={status} key={status || 'all'}>
                    {status
                      ? t(`xhsTargets.statuses.${status}`)
                      : t('xhsTargets.allStatuses')}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>{t('xhsTargets.sourceFilter')}</span>
              <select
                className="input"
                value={filterSource}
                onChange={event => setFilterSource(event.target.value)}
              >
                {SOURCE_FILTERS.map(source => (
                  <option value={source} key={source || 'all'}>
                    {source
                      ? t(`xhsTargets.sourceTypes.${source}`)
                      : t('xhsTargets.allSources')}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
      </section>

      <div className="xhs-targets-candidate-list" aria-busy={loading}>
        {candidates.map(candidate => (
          <CandidateCard
            key={candidate.id}
            candidate={candidate}
            busy={decisionBusy === candidate.id}
            reason={decisionReasons[candidate.id] || ''}
            setReason={value => setDecisionReasons(current => ({
              ...current,
              [candidate.id]: value,
            }))}
            decide={decide}
            t={t}
          />
        ))}
        {!loading && !candidates.length && (
          <div className="panel empty-cell">{t('xhsTargets.noCandidates')}</div>
        )}
        {loading && !candidates.length && (
          <div className="panel empty-cell">{t('xhsTargets.loading')}</div>
        )}
      </div>
    </section>
  );
}

export {
  CANDIDATE_LIMIT,
  DECISION_BUTTON_ORDER,
  SOURCE_FILTERS,
  STATUS_FILTERS,
};
