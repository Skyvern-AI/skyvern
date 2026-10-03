import { isAxiosError } from "axios";
import { useState } from "react";

import {
  artifactIdFromContentUrl,
  freshArtifactUrl,
  mintSignedArtifactUrl,
} from "@/api/artifactUrls";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type HrefProps = Omit<React.ComponentPropsWithoutRef<"a">, "href"> & {
  href: string;
  refreshHref?: () => Promise<string>;
};

type ArtifactIdProps = Pick<
  React.ComponentPropsWithoutRef<"button">,
  "className" | "title" | "children"
> & { artifactId: string };

type Props =
  | (HrefProps & { artifactId?: never })
  | (ArtifactIdProps & { href?: never; refreshHref?: never });

/**
 * Anchor for artifact content URLs that mints a fresh short-lived URL at
 * click time (SKY-12541), so links keep working after the embedded URL
 * expires. Non-artifact hrefs navigate natively unless a caller can refresh
 * their storage-presigned URL.
 */
function ArtifactHrefLink({
  href,
  refreshHref,
  onClick,
  children,
  ...anchorProps
}: HrefProps) {
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
    const artifactId = artifactIdFromContentUrl(href);
    // Storage-presigned URLs can opt into a caller-supplied refresh on click;
    // other non-artifact links keep native anchor semantics.
    if (!refreshHref && !artifactId) {
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
    const resolveHref = refreshHref
      ? refreshHref
      : () => freshArtifactUrl(credentialGetter, href);
    void resolveHref()
      .catch(() => freshArtifactUrl(credentialGetter, href))
      .then((url) => {
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

// No href, so "open in new tab" cannot bypass the mint; every activation opens a new tab with a fresh signed URL.
function ArtifactIdButton({
  artifactId,
  className,
  title,
  children,
}: ArtifactIdProps) {
  const credentialGetter = useCredentialGetter();
  const [unavailable, setUnavailable] = useState(false);

  const open = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    const newTab = window.open("", "_blank");
    if (newTab) {
      newTab.opener = null;
    }
    void mintSignedArtifactUrl(credentialGetter, artifactId).then(
      ({ signed_url }) => {
        if (newTab) {
          newTab.location.href = signed_url;
        } else {
          window.location.assign(signed_url);
        }
      },
      (error: unknown) => {
        newTab?.close();
        const status = isAxiosError(error) ? error.response?.status : undefined;
        if (status === 404 || status === 410) {
          setUnavailable(true);
        }
      },
    );
  };

  if (unavailable) {
    return (
      <span className={className} title={title}>
        {children} (no longer available)
      </span>
    );
  }

  return (
    <button
      type="button"
      className={className}
      title={title}
      onClick={open}
      onAuxClick={(event) => {
        if (event.button === 1) {
          open(event);
        }
      }}
    >
      {children}
    </button>
  );
}

function ArtifactDownloadLink(props: Props) {
  if (props.artifactId !== undefined) {
    return <ArtifactIdButton {...props} />;
  }
  return <ArtifactHrefLink {...props} />;
}

export { ArtifactDownloadLink };
