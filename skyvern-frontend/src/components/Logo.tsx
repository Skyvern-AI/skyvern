function Logo() {
  const src = "/logo.png";
  return (
    <img
      src={src}
      alt="Logo"
      className="hue-rotate-180 invert dark:hue-rotate-0 dark:invert-0"
    />
  );
}

export { Logo };
