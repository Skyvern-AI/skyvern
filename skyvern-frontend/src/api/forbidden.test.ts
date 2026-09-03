import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, test } from "vitest";

import { isForbiddenError } from "./forbidden";

function axiosErrorWithStatus(status: number): AxiosError {
  const error = new AxiosError(`Request failed with status code ${status}`);
  error.response = {
    status,
    statusText: "",
    data: null,
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

describe("isForbiddenError", () => {
  test("is true for 401 and 403", () => {
    expect(isForbiddenError(axiosErrorWithStatus(401))).toBe(true);
    expect(isForbiddenError(axiosErrorWithStatus(403))).toBe(true);
  });

  test("is false for other statuses, including 402 and 404", () => {
    expect(isForbiddenError(axiosErrorWithStatus(402))).toBe(false);
    expect(isForbiddenError(axiosErrorWithStatus(404))).toBe(false);
    expect(isForbiddenError(axiosErrorWithStatus(500))).toBe(false);
  });

  test("is false for non-response and non-axios errors", () => {
    expect(isForbiddenError(new AxiosError("Network Error"))).toBe(false);
    expect(isForbiddenError(new Error("boom"))).toBe(false);
    expect(isForbiddenError(undefined)).toBe(false);
  });
});
