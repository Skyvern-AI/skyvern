type Props = {
  className?: string;
};

function SearchIcon({ className }: Props) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      className={className}
    >
      <path d="m0 0h24v24h-24z" fill="none" />
      <path
        d={`m15.5 14h-.79l-.28-.27c1.2-1.4 1.82-3.31 1.48-5.34-.47-2.78-2.79-5-5.59-5.34-4.23-.52-7.79 
          3.04-7.27 7.27.34 2.8 2.56 5.12 5.34 5.59 2.03.34 3.94-.28 5.34-1.48l.27.28v.79l4.25 
          4.25c.41.41 1.08.41 1.49 0s.41-1.08 0-1.49zm-6 0c-2.49 0-4.5-2.01-4.5-4.5s2.01-4.5 
          4.5-4.5 4.5 2.01 4.5 4.5-2.01 4.5-4.5 4.5z`}
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export { SearchIcon };
