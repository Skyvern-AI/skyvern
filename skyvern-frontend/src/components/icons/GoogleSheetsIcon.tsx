type Props = {
  className?: string;
};

function GoogleSheetsIcon({ className }: Props) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      {/* Document shape */}
      <path
        d="M6 2C4.89543 2 4 2.89543 4 4V20C4 21.1046 4.89543 22 6 22H18C19.1046 22 20 21.1046 20 20V8L14 2H6Z"
        fill="#0F9D58"
      />
      <path d="M14 2L20 8H14V2Z" fill="#87CEAC" />
      {/* Grid lines */}
      <rect x="7" y="10" width="10" height="9" rx="0.5" fill="white" />
      <line x1="7" y1="13" x2="17" y2="13" stroke="#0F9D58" strokeWidth="0.5" />
      <line x1="7" y1="16" x2="17" y2="16" stroke="#0F9D58" strokeWidth="0.5" />
      <line
        x1="11"
        y1="10"
        x2="11"
        y2="19"
        stroke="#0F9D58"
        strokeWidth="0.5"
      />
    </svg>
  );
}

export { GoogleSheetsIcon };
