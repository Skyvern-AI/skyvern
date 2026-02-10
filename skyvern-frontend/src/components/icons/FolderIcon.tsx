type Props = {
  className?: string;
};

function FolderIcon({ className }: Props) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="15"
      height="15"
      viewBox="0 0 15 15"
      fill="none"
      className={className}
    >
      <path
        d="M2 3.5C2 3.22386 2.22386 3 2.5 3H5.5C5.64184 3 5.77652 3.05996 5.86853 3.16438L6.86853 4.33562C6.96054 4.44004 7.09522 4.5 7.23706 4.5H12.5C12.7761 4.5 13 4.72386 13 5V11.5C13 11.7761 12.7761 12 12.5 12H2.5C2.22386 12 2 11.7761 2 11.5V3.5Z"
        fill="currentColor"
      />
    </svg>
  );
}

export { FolderIcon };
