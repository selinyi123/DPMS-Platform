export function settleRequestSlicesIndependently(slices = []) {
  return slices.map(async ({ key, request, onFulfilled }) => {
    try {
      const value = await request;
      if (onFulfilled) onFulfilled(value);
      return Object.freeze({ key, status: 'fulfilled', value });
    } catch (error) {
      return Object.freeze({ key, status: 'rejected', error });
    }
  });
}

export function reloadApplicationModuleGraph(location = globalThis.location) {
  if (!location || typeof location.reload !== 'function') {
    throw new Error('module_graph_reload_unavailable');
  }
  location.reload();
}
