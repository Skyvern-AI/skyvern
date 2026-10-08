import { createContext, useContext } from "react";

import type {
  InputReview,
  ReviewFieldChange,
  ReviewStatus,
} from "../panels/workflowReviewDiff";

export type BlockReviewAnnotation = {
  kind: "block";
  status: ReviewStatus;
  changes: Array<ReviewFieldChange>;
  // False for a brand-new workflow: one note says everything is new instead.
  showStatus: boolean;
};

export type StartReviewAnnotation = {
  kind: "start";
  inputs: Array<InputReview>;
  settings: Array<ReviewFieldChange>;
  showStatus: boolean;
};

export type FoldReviewAnnotation = {
  kind: "fold";
  count: number;
  onShow: () => void;
};

export type NodeReviewAnnotation =
  | BlockReviewAnnotation
  | StartReviewAnnotation
  | FoldReviewAnnotation;

export type CardReviewAnnotation =
  | BlockReviewAnnotation
  | StartReviewAnnotation;

export const ReviewAnnotationContext =
  createContext<CardReviewAnnotation | null>(null);

export function useReviewAnnotation(): CardReviewAnnotation | null {
  return useContext(ReviewAnnotationContext);
}

export function reviewStatusOf(review: CardReviewAnnotation): ReviewStatus {
  if (review.kind === "block") return review.status;
  return review.inputs.length > 0 || review.settings.length > 0
    ? "changed"
    : "unchanged";
}

// One vocabulary for every review surface: a glyph and a word with each color,
// so status never rests on color alone. Outline, tint, and glyph share one
// semantic token per status.
export const REVIEW_STATUS_META: Record<
  ReviewStatus,
  {
    glyph: string;
    word: string;
    chip: string;
    glyphClass: string;
    outline: string;
  }
> = {
  new: {
    glyph: "+",
    word: "New",
    chip: "bg-success/20 text-foreground",
    glyphClass: "text-success",
    outline: "outline outline-2 outline-offset-4 outline-success",
  },
  changed: {
    glyph: "~",
    word: "Changed",
    chip: "bg-warning/20 text-foreground",
    glyphClass: "text-warning",
    outline: "outline outline-2 outline-offset-4 outline-warning",
  },
  removed: {
    glyph: "−",
    word: "Removed",
    chip: "bg-destructive/20 text-foreground",
    glyphClass: "text-destructive",
    outline: "outline-dashed outline-2 outline-offset-4 outline-destructive",
  },
  unchanged: {
    glyph: "=",
    word: "Unchanged",
    chip: "bg-muted text-muted-foreground",
    glyphClass: "",
    outline: "",
  },
};
