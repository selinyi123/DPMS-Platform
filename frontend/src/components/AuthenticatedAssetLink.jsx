import { useState } from 'react';

import { fetchAuthenticatedBlob } from '../api';

async function openBlobInNewWindow(path) {
  const popup = window.open('about:blank', '_blank');
  if (popup) popup.opener = null;
  try {
    const blob = await fetchAuthenticatedBlob(path);
    const objectUrl = URL.createObjectURL(blob);
    if (popup && !popup.closed) {
      popup.location.replace(objectUrl);
    } else {
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = 'dpms-evidence.png';
      link.rel = 'noreferrer';
      document.body.appendChild(link);
      link.click();
      link.remove();
    }
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  } catch (error) {
    popup?.close();
    throw error;
  }
}

export default function AuthenticatedAssetLink({
  path,
  children,
  className = '',
  onError,
}) {
  const [busy, setBusy] = useState(false);
  const open = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await openBlobInNewWindow(path);
    } catch (error) {
      onError?.(error);
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      className={`${className} evidence-link-button`.trim()}
      type="button"
      disabled={busy}
      onClick={open}
    >
      {children}
    </button>
  );
}
