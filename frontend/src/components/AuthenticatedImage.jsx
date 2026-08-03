import { useEffect, useRef, useState } from 'react';

import { fetchAuthenticatedBlob } from '../api';

export default function AuthenticatedImage({
  path,
  alt,
  onError,
  onLoad,
  ...props
}) {
  const [objectUrl, setObjectUrl] = useState('');
  const onErrorRef = useRef(onError);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    const controller = new AbortController();
    let currentUrl = '';
    setObjectUrl('');
    fetchAuthenticatedBlob(path, { signal: controller.signal })
      .then((blob) => {
        if (controller.signal.aborted) return;
        currentUrl = URL.createObjectURL(blob);
        setObjectUrl(currentUrl);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        onErrorRef.current?.(error);
      });
    return () => {
      controller.abort();
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [path]);

  if (!objectUrl) return null;
  return (
    <img
      {...props}
      alt={alt}
      src={objectUrl}
      onLoad={onLoad}
      onError={onError}
    />
  );
}
