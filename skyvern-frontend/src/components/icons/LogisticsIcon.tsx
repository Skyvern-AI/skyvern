type Props = {
  className?: string;
};

function LogisticsIcon({ className }: Props) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      className={className}
    >
      {/* Truck Body */}
      <path
        d="M1 8h15v10H1z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Truck Cabin */}
      <path
        d="M16 8h4l3 3v7h-7V8z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Front Wheel */}
      <circle
        cx="5.5"
        cy="18.5"
        r="2.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Rear Wheel */}
      <circle
        cx="18.5"
        cy="18.5"
        r="2.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export { LogisticsIcon };
