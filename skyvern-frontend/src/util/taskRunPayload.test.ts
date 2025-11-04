import { describe, expect, it } from "vitest";
import type { CreateTaskRequest } from "../api/types";
import { buildTaskRunPayload } from "./taskRunPayload";

describe("buildTaskRunPayload", () => {
  it("maps v1 task fields into the Runs API payload shape", () => {
    const request: CreateTaskRequest = {
      title: "  Test Task ",
      url: " https://example.com/task ",
      navigation_goal: "Navigate somewhere",
      data_extraction_goal: "Collect some data",
      navigation_payload: { name: "John", age: 30 },
      webhook_callback_url: " https://callback.example.com ",
      proxy_location: "RESIDENTIAL",
      extracted_information_schema: { foo: "bar" },
      error_code_mapping: { ERR42: "Meaningful message" },
      extra_http_headers: { "X-Trace-Id": "abc123" },
      totp_identifier: "  identifier  ",
      browser_address: "  chrome:1234  ",
      include_action_history_in_verification: true,
      max_screenshot_scrolls: 7,
    };

    const payload = buildTaskRunPayload(request);

    expect(payload).toEqual({
      prompt:
        'Navigate somewhere\n\nCollect some data\n\n{"name":"John","age":30}',
      url: "https://example.com/task",
      proxy_location: "RESIDENTIAL",
      data_extraction_schema: { foo: "bar" },
      error_code_mapping: { ERR42: "Meaningful message" },
      extra_http_headers: { "X-Trace-Id": "abc123" },
      webhook_url: "https://callback.example.com",
      totp_identifier: "identifier",
      browser_address: "chrome:1234",
      include_action_history_in_verification: true,
      max_screenshot_scrolls: 7,
      title: "Test Task",
    });
  });

  it("applies prompt fallbacks and drops invalid optional objects", () => {
    const request: CreateTaskRequest = {
      title: "   ",
      url: "  https://fallback.example.com ",
      navigation_goal: "",
      data_extraction_goal: null,
      navigation_payload: null,
      webhook_callback_url: "  ",
      proxy_location: null,
      extracted_information_schema: null,
      extra_http_headers: "not an object" as unknown as Record<string, string>,
      error_code_mapping: [] as unknown as Record<string, string>,
      totp_identifier: null,
      browser_address: " ",
      include_action_history_in_verification: null,
    };

    const payload = buildTaskRunPayload(request);

    expect(payload).toEqual({
      prompt: "https://fallback.example.com",
      url: "https://fallback.example.com",
      proxy_location: null,
      data_extraction_schema: null,
      webhook_url: undefined,
      totp_identifier: undefined,
      browser_address: undefined,
      include_action_history_in_verification: null,
      max_screenshot_scrolls: undefined,
      title: undefined,
      extra_http_headers: undefined,
      error_code_mapping: undefined,
    });
  });

  it("includes navigation_payload as string in prompt", () => {
    const request: CreateTaskRequest = {
      url: "https://example.com",
      navigation_goal: "Fill form",
      navigation_payload: '{"email": "test@example.com"}',
    };

    const payload = buildTaskRunPayload(request);

    expect(payload.prompt).toBe('Fill form\n\n{"email": "test@example.com"}');
  });

  it("formats navigation_payload object as JSON in prompt", () => {
    const request: CreateTaskRequest = {
      url: "https://example.com",
      navigation_goal: "Fill form",
      navigation_payload: { email: "test@example.com", name: "Test" },
    };

    const payload = buildTaskRunPayload(request);

    expect(payload.prompt).toBe(
      'Fill form\n\n{"email":"test@example.com","name":"Test"}',
    );
  });
});
