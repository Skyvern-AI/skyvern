function AuthenticatorAppLogo({ src }: { src: string }) {
  return (
    <img
      src={src}
      alt=""
      className="size-6 rounded-[6px] object-cover"
      draggable={false}
    />
  );
}

export { AuthenticatorAppLogo };
