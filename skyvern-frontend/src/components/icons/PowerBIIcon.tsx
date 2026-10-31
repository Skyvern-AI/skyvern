type Props = {
  className?: string;
};

function PowerBIIcon({ className }: Props) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-hidden="true"
    >
      <path d="M3 13.5h4.5V21H3v-7.5Z" fill="#F2C811" />
      <path d="M8.25 8.25h4.5V21h-4.5V8.25Z" fill="#E6B90C" />
      <path d="M13.5 3h4.5v18h-4.5V3Z" fill="#F2C811" />
      <path d="M18.75 10.5H21V21h-2.25V10.5Z" fill="#D4A900" />
    </svg>
  );
}

export { PowerBIIcon };
