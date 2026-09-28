type Props = {
  className?: string;
};

function SparklesIcon({ className }: Props) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path
        d="M9.94 14.06 7.5 21 5.06 14.06 0 12l5.06-2.06L7.5 3l2.44 6.94L15 12Z"
        transform="translate(3 0) scale(.82)"
      />
      <path d="M20 3v4" />
      <path d="M22 5h-4" />
    </svg>
  );
}

export { SparklesIcon };
