type Props = {
  className?: string;
};

function OutlookIcon({ className }: Props) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 256 256"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
    >
      <path d="M96 48H224V208H96V48Z" fill="#0A6ED1" />
      <path d="M96 64H208V96H96V64Z" fill="#28A8EA" />
      <path d="M96 96H208V128H96V96Z" fill="#0078D4" />
      <path d="M96 128H208V192H96V128Z" fill="#0364B8" />
      <path d="M208 80L144 128L208 176V80Z" fill="#50D9FF" fillOpacity="0.7" />
      <path d="M80 64L16 76V180L80 192V64Z" fill="#0078D4" />
      <path d="M80 64H128V192H80V64Z" fill="#005A9E" />
      <path
        d="M47.5 153.5C38.2 153.5 31 146.2 31 128.1C31 109.8 38.5 102.5 48 102.5C57.3 102.5 64.5 109.8 64.5 127.9C64.5 146.2 57 153.5 47.5 153.5ZM47.8 140.2C51.1 140.2 53.5 136.7 53.5 128.1C53.5 119.1 51 115.8 47.7 115.8C44.4 115.8 42 119.3 42 127.9C42 136.9 44.5 140.2 47.8 140.2Z"
        fill="white"
      />
    </svg>
  );
}

export { OutlookIcon };
