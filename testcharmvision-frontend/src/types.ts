export type SubmitEvent = Event & {
  submitter: Element;
};

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | { [key: string]: JsonValue }
  | JsonValue[];

export type JsonObject = { [key: string]: JsonValue };

/**
 * Use when you know a part of the JSON object, but there may be more bits that
 * you don't know or care about (yet).
 */
export type JsonObjectExtendable<T extends { [key: string]: JsonValue }> =
  JsonObject & T;
