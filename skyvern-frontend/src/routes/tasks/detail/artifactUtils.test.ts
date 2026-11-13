import { describe, expect, test, vi } from "vitest";

import type { ArtifactApiResponse } from "@/api/types";
import { getImageURL } from "./artifactUtils";

vi.mock("@/util/env", () => ({ artifactApiBaseUrl: "http://artifacts.test" }));

const artifact = (uri: string) =>
  ({ uri, signed_url: null }) as unknown as ArtifactApiResponse;

describe("getImageURL", () => {
  test("hands a file:// URI to the artifact server whole, encoded once, so a %20 in a name survives", () => {
    const uri = "file:///artifacts/org/my%20shot.png";
    const url = new URL(getImageURL(artifact(uri)));
    expect(url.origin + url.pathname).toBe(
      "http://artifacts.test/artifact/image",
    );
    expect(url.searchParams.get("path")).toBe(uri);
  });

  test("hands a legacy unencoded file:// URI over whole as well; the server falls back to the raw path", () => {
    const uri = "file:///artifacts/org/100%/a#b.png";
    const url = new URL(getImageURL(artifact(uri)));
    expect(url.searchParams.get("path")).toBe(uri);
  });

  test("a signed https link is used as it is", () => {
    expect(getImageURL(artifact("https://bucket.test/a.png?sig=1"))).toBe(
      "https://bucket.test/a.png?sig=1",
    );
  });
});
