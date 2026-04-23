type Props = {
  className?: string;
};

function GoogleDocsIcon({ className }: Props) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <path
        d="M6 2C4.89543 2 4 2.89543 4 4V20C4 21.1046 4.89543 22 6 22H18C19.1046 22 20 21.1046 20 20V8L14 2H6Z"
        fill="#4285F4"
      />
      <path d="M14 2L20 8H14V2Z" fill="#A1C2FA" />
      <line
        x1="7"
        y1="11"
        x2="17"
        y2="11"
        stroke="white"
        strokeWidth="1"
        strokeLinecap="round"
      />
      <line
        x1="7"
        y1="14"
        x2="17"
        y2="14"
        stroke="white"
        strokeWidth="1"
        strokeLinecap="round"
      />
      <line
        x1="7"
        y1="17"
        x2="14"
        y2="17"
        stroke="white"
        strokeWidth="1"
        strokeLinecap="round"
      />
    </svg>
  );
}

export { GoogleDocsIcon };
