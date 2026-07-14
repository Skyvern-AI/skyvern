import { useMemo } from "react";
import type { SetURLSearchParams } from "react-router-dom";

import {
  parseTagFilter,
  serializeTagFilter,
  type TagFilterTerm,
} from "../types/tagTypes";

function useRunTagFilterParam(
  searchParams: URLSearchParams,
  setSearchParams: SetURLSearchParams,
) {
  const tagTerms = useMemo(
    () => parseTagFilter(searchParams.getAll("tags").join(",")),
    [searchParams],
  );
  const tagsParam = useMemo(
    () => serializeTagFilter(tagTerms) || undefined,
    [tagTerms],
  );

  function writeTagsParam(terms: Array<TagFilterTerm>) {
    const params = new URLSearchParams(searchParams);
    if (terms.length === 0) {
      params.delete("tags");
    } else {
      params.set("tags", serializeTagFilter(terms));
    }
    params.set("page", "1");
    setSearchParams(params, { replace: true });
  }

  return { tagTerms, tagsParam, writeTagsParam };
}

export { useRunTagFilterParam };
