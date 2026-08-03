function translated(t, key, fallbackKey) {
  const value = t(key);
  if (value !== key) return value;
  return t(fallbackKey);
}

/** Localize backend learning-gap codes without rendering backend English copy. */
export function knowledgeGapPresentation(item, t) {
  const code = typeof item?.code === 'string' && /^[a-z0-9_]{1,64}$/u.test(item.code)
    ? item.code
    : 'unknown';
  const base = `knowledge.gaps.${code}`;
  return {
    label: translated(t, `${base}.label`, 'knowledge.gaps.unknown.label'),
    title: translated(t, `${base}.title`, 'knowledge.gaps.unknown.title'),
    detail: translated(t, `${base}.detail`, 'knowledge.gaps.unknown.detail'),
  };
}

