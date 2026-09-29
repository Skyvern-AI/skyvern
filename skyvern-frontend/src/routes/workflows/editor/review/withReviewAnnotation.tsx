import type { ComponentType } from "react";
import type { NodeProps } from "@xyflow/react";

import { cn } from "@/util/utils";

import {
  REVIEW_STATUS_META,
  ReviewAnnotationContext,
  reviewStatusOf,
  type NodeReviewAnnotation,
} from "./reviewAnnotation";
import { ReviewFoldMarker } from "./ReviewParts";

// Only review canvases set `data.review`; every other canvas renders the node
// untouched. The outline wraps the whole node (a loop's children included) and
// the header and change list read the annotation from context.
export function withReviewAnnotation<P extends NodeProps>(
  Component: ComponentType<P>,
): ComponentType<P> {
  function Reviewed(props: P) {
    const review = (props.data as { review?: NodeReviewAnnotation }).review;
    if (!review) {
      return <Component {...props} />;
    }
    if (review.kind === "fold") {
      return <ReviewFoldMarker count={review.count} onShow={review.onShow} />;
    }
    const status = reviewStatusOf(review);
    return (
      <ReviewAnnotationContext.Provider value={review}>
        <div
          data-review-status={status}
          className={cn(
            "rounded-lg",
            review.showStatus && REVIEW_STATUS_META[status].outline,
          )}
        >
          <Component {...props} />
        </div>
      </ReviewAnnotationContext.Provider>
    );
  }
  Reviewed.displayName = `withReviewAnnotation(${
    Component.displayName ?? Component.name ?? "Component"
  })`;
  return Reviewed;
}
