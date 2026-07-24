type Props = {
  className?: string;
};

function PencilSolidIcon({ className }: Props) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
    >
      <path d="M21.731 2.269a2.625 2.625 0 0 0-3.712 0l-1.157 1.157 3.712 3.712 1.157-1.157a2.625 2.625 0 0 0 0-3.712ZM19.513 8.199l-3.712-3.712-12.15 12.15a5.25 5.25 0 0 0-1.32 2.214l-1.072 3.605a.563.563 0 0 0 .7.7l3.605-1.072a5.25 5.25 0 0 0 2.214-1.32L19.513 8.2Z" />
    </svg>
  );
}

export { PencilSolidIcon };
