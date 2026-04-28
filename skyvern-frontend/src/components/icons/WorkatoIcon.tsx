type Props = {
  className?: string;
};

// Placeholder icon - replace with official asset before setting status to live.
function WorkatoIcon({ className }: Props) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <rect width="24" height="24" rx="5" fill="#F05A28" />
      <circle cx="9" cy="12" r="3" fill="white" />
      <circle cx="15" cy="12" r="3" fill="white" fillOpacity="0.8" />
    </svg>
  );
}

export { WorkatoIcon };
