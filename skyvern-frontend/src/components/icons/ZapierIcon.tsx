type Props = {
  className?: string;
};

// Placeholder icon - replace with official asset before setting status to live.
function ZapierIcon({ className }: Props) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <rect width="24" height="24" rx="5" fill="#FF4A00" />
      <path
        d="M12 5.5L13.05 9.4L17 8.4L14.85 11.9L17 15.4L13.05 14.4L12 18.3L10.95 14.4L7 15.4L9.15 11.9L7 8.4L10.95 9.4L12 5.5Z"
        fill="white"
      />
    </svg>
  );
}

export { ZapierIcon };
