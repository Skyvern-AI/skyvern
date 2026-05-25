function LogoMinimized() {
  const src = "/logo-small.png";
  return (
    <img
      src={src}
      alt="Minimized Logo"
      className="hue-rotate-180 invert dark:hue-rotate-0 dark:invert-0"
    />
  );
}

export { LogoMinimized };
