import { freshArtifactUrl } from "@/api/artifactUrls";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type Props = Omit<React.ComponentPropsWithoutRef<"a">, "href"> & {
  href: string;
};

/**
 * Anchor for artifact content URLs that mints a fresh short-lived URL at
 * click time (SKY-12541), so links keep working after the embedded URL
 * expires. Non-artifact hrefs (storage presigned URLs) navigate natively.
 */
function ArtifactDownloadLink({
  href,
  onClick,
  children,
  ...anchorProps
}: Props) {
  const credentialGetter = useCredentialGetter();

  const handleClick = (event: React.MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    // Modified clicks (new-tab, download-as) keep native anchor semantics.
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    // The tab must be opened synchronously inside the click gesture — opening
    // it after the awaited mint trips popup blockers (Safari/Firefox). The
    // minted URL is assigned to the already-open tab; if the open was still
    // blocked, fall back to same-tab navigation.
    const newTab =
      anchorProps.target === "_blank" ? window.open("", "_blank") : null;
    if (newTab) {
      newTab.opener = null;
    }
    void freshArtifactUrl(credentialGetter, href).then((url) => {
      if (newTab) {
        newTab.location.href = url;
      } else {
        window.location.assign(url);
      }
    });
  };

  return (
    <a href={href} onClick={handleClick} {...anchorProps}>
      {children}
    </a>
  );
}

export { ArtifactDownloadLink };
