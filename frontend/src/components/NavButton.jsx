export default function NavButton({ active, onClick, children }) {
  return (
    <button onClick={onClick} className={`nav-button ${active ? 'nav-button-active' : ''}`}>
      <span>{children}</span>
    </button>
  );
}
