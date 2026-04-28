type Props = {
  className?: string;
};

// Placeholder icon - replace with official asset before setting status to live.
function N8nIcon({ className }: Props) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <rect width="24" height="24" rx="5" fill="#101330" />
      <circle cx="6.25" cy="12" r="1.75" fill="#EA4B71" />
      <circle cx="12" cy="7.5" r="1.75" fill="#EA4B71" />
      <circle cx="12" cy="16.5" r="1.75" fill="#EA4B71" />
      <circle cx="17.75" cy="12" r="1.75" fill="#EA4B71" />
      <path
        d="M6.25 12L12 7.5M6.25 12L12 16.5M12 7.5L17.75 12M12 16.5L17.75 12"
        stroke="#EA4B71"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
    </svg>
  );
}

export { N8nIcon };
