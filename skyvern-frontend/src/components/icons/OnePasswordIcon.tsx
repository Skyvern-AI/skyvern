type Props = {
  className?: string;
};

function OnePasswordIcon({ className }: Props) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="48 48 928 928"
      fill="none"
      className={className}
    >
      <circle cx="512" cy="512" r="464" fill="#FFFFFF" />
      <circle cx="512" cy="512" r="336" fill="#198CFF" />
      <circle cx="512" cy="512" r="264" fill="#F2F2F2" />
      <path
        fill="#FFFFFF"
        d="M468 720a36.1 36.1 0 0 1-36-36V503.3a36.3 36.3 0 0 1 8.4-23.2l10.6-12.5a4.2 4.2 0 0 0 0-5.2l-10.6-12.5a36.3 36.3 0 0 1-8.4-23.2V340a36.1 36.1 0 0 1 36-36h88a36.1 36.1 0 0 1 36 36v180.7a36.3 36.3 0 0 1-8.4 23.2L573 556.4a4.2 4.2 0 0 0 0 5.2l10.6 12.5a36.3 36.3 0 0 1 8.4 23.2V684a36.1 36.1 0 0 1-36 36z"
      />
      <path
        fill="#0A2B4C"
        d="M468 320h88a20.1 20.1 0 0 1 20 20v180.7a20.3 20.3 0 0 1-4.7 12.9l-10.5 12.5a20 20 0 0 0 0 25.8l10.5 12.5a20.3 20.3 0 0 1 4.7 12.9V684a20.1 20.1 0 0 1-20 20h-88a20.1 20.1 0 0 1-20-20V503.3a20.3 20.3 0 0 1 4.7-12.9l10.5-12.5a20 20 0 0 0 0-25.8l-10.5-12.5a20.3 20.3 0 0 1-4.7-12.9V340a20.1 20.1 0 0 1 20-20z"
      />
    </svg>
  );
}

export { OnePasswordIcon };
