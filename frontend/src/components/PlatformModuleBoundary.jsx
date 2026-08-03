import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  loadPlatformModule,
  loadPlatformModulesIndependently,
  platformModuleLoadState,
  registeredPlatformIds,
} from '../platforms/index.js';
import { reloadApplicationModuleGraph } from '../asyncSlices.js';
import { DEFAULT_PLATFORM_MODULE_ID } from '../platformModuleDemand.js';

const REGISTERED_PLATFORM_IDS = Object.freeze(registeredPlatformIds());
const REGISTERED_PLATFORM_ID_SET = new Set(REGISTERED_PLATFORM_IDS);

function currentStates() {
  return Object.fromEntries(
    REGISTERED_PLATFORM_IDS.map(platformId => [
      platformId,
      platformModuleLoadState(platformId).status,
    ]),
  );
}

export default function PlatformModuleBoundary({ Component, language }) {
  const mountedRef = useRef(false);
  const [states, setStates] = useState(currentStates);
  const [requestedPlatformIds, setRequestedPlatformIds] = useState([]);

  const recordSettlement = useCallback((result) => {
    if (!mountedRef.current) return;
    setStates(previous => ({
      ...previous,
      [result.platformId]: result.status === 'fulfilled' ? 'ready' : 'failed',
    }));
  }, []);

  const startLoads = useCallback((platformIds, retry = false) => {
    const requested = [...new Set(
      (Array.isArray(platformIds) ? platformIds : [])
        .filter(platformId => REGISTERED_PLATFORM_ID_SET.has(platformId)),
    )];
    if (!requested.length) return;
    setRequestedPlatformIds((previous) => {
      const next = [...new Set([...previous, ...requested])];
      return next.length === previous.length ? previous : next;
    });
    const pending = requested.filter((platformId) => {
      const status = platformModuleLoadState(platformId).status;
      return retry ? status === 'failed' : status !== 'ready' && status !== 'failed';
    });
    if (!pending.length) return;
    setStates((previous) => {
      const next = { ...previous };
      pending.forEach((platformId) => {
        next[platformId] = 'loading';
      });
      return next;
    });
    loadPlatformModulesIndependently(pending, {
      loadModule: platformId => loadPlatformModule(platformId, { retry }),
      onSettled: recordSettlement,
    });
  }, [recordSettlement]);

  useEffect(() => {
    mountedRef.current = true;
    startLoads([DEFAULT_PLATFORM_MODULE_ID]);
    return () => {
      mountedRef.current = false;
    };
  }, [startLoads]);

  const failedPlatformIds = useMemo(
    () => requestedPlatformIds.filter(platformId => states[platformId] === 'failed'),
    [requestedPlatformIds, states],
  );

  return (
    <>
      {!!failedPlatformIds.length && (
        <div className="notice notice-warning" role="alert">
          <span>
            {language === 'en'
              ? `Platform modules unavailable: ${failedPlatformIds.join(', ')}`
              : `平台模块暂不可用：${failedPlatformIds.join('、')}`}
          </span>
          {' '}
          <button
            className="btn-ghost"
            type="button"
            onClick={() => reloadApplicationModuleGraph()}
          >
            {language === 'en' ? 'Reload' : '重新加载'}
          </button>
        </div>
      )}
      <Component
        platformModuleStates={states}
        requestPlatformModules={startLoads}
      />
    </>
  );
}
