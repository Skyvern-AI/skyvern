type Props = {
  className?: string;
};

// Placeholder icon - replace with official asset before setting status to live.
function MakeIcon({ className }: Props) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <rect width="24" height="24" rx="5" fill="#6D00CC" />
      <path d="M7 6L9 18H7L5 6H7Z" fill="white" />
      <path d="M11 6L13 18H11L9 6H11Z" fill="white" fillOpacity="0.85" />
      <path
        d="M16.5 6L19 12L16.5 18H14.5L17 12L14.5 6H16.5Z"
        fill="white"
        fillOpacity="0.7"
      />
    </svg>
  );
}

export { MakeIcon };
