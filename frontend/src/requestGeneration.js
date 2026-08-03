export function createLatestRequestGate() {
  let generation = 0;

  return {
    begin() {
      generation += 1;
      return generation;
    },
    invalidate() {
      generation += 1;
    },
    isCurrent(token) {
      return token === generation;
    },
  };
}

export function createLatestAbortableRequestGate() {
  const generationGate = createLatestRequestGate();
  let controller = null;

  return {
    begin() {
      controller?.abort();
      controller = new AbortController();
      return {
        token: generationGate.begin(),
        signal: controller.signal,
      };
    },
    invalidate() {
      controller?.abort();
      controller = null;
      generationGate.invalidate();
    },
    isCurrent(request) {
      return Boolean(
        request
        && !request.signal.aborted
        && generationGate.isCurrent(request.token),
      );
    },
  };
}
