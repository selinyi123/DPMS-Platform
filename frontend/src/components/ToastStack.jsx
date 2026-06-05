import { useUi } from '../uiContext';

export default function ToastStack() {
  const { toasts } = useUi();
  return (
    <div className="toast-stack">
      {toasts.map(toast => (
        <div className={`toast toast-${toast.type}`} key={toast.id}>
          {toast.message}
        </div>
      ))}
    </div>
  );
}
