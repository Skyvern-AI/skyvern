import * as React from "react";

import { cn } from "@/util/utils";

type HeadingLevel = "h1" | "h2" | "h3" | "h4" | "h5" | "h6";

interface TitleDescriptionProps extends React.HTMLAttributes<HTMLDivElement> {
  title: string;
  // Empty string or undefined suppresses the `<p>` so screen readers don't see a phantom paragraph.
  description?: string;
  // Defaults to `h2`; use `h3` for nested sections.
  as?: HeadingLevel;
}

/** Two-slot title + description compound for section headers. */
function TitleDescription({
  title,
  description,
  as: Heading = "h2",
  className,
  ...props
}: TitleDescriptionProps) {
  const showDescription = description !== undefined && description !== "";
  return (
    <div className={cn("flex flex-col gap-1", className)} {...props}>
      <Heading className="font-semibold tracking-tight text-foreground">
        {title}
      </Heading>
      {showDescription ? (
        <p className="text-sm text-muted-foreground">{description}</p>
      ) : null}
    </div>
  );
}

export { TitleDescription };
export type { TitleDescriptionProps };
