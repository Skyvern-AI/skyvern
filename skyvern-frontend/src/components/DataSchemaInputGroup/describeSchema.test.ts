import { describe, expect, test } from "vitest";

import { describeSchema, type SchemaField } from "./describeSchema";

function field(name: string, overrides: Partial<SchemaField>): SchemaField {
  return {
    name,
    type: "string",
    optional: false,
    nullable: false,
    description: null,
    constraints: [],
    unsupported: [],
    children: [],
    ...overrides,
  };
}

function objectSchema(
  properties: Record<string, unknown>,
  extra: Record<string, unknown> = {},
) {
  return JSON.stringify({
    type: "object",
    properties,
    required: Object.keys(properties),
    ...extra,
  });
}

const exampleSchema = JSON.stringify({
  type: "object",
  properties: {
    approved: { type: "boolean", description: "Whether it was approved" },
    note: { type: "string" },
    items: {
      type: "array",
      minItems: 1,
      maxItems: 3,
      items: {
        type: "object",
        properties: { code: { type: "string", pattern: "^[0-9]+$" } },
        required: ["code"],
      },
    },
  },
  required: ["approved", "items"],
  additionalProperties: false,
});

const deeplyNested = `${'{"a":'.repeat(250)}1${"}".repeat(250)}`;

describe("describeSchema", () => {
  test.each([
    ["invalid JSON", '{"type": "object",'],
    ["a Jinja string", "{{ schema }}"],
    ["a JSON string", '"return the total"'],
    ["a list root", '["a", "b"]'],
    ["null", "null"],
    ["the task block default", '{"type":"object"}'],
    ["empty properties", '{"type":"object","properties":{}}'],
    ["a root anyOf", '{"anyOf":[{"type":"string"}],"properties":{"a":{}}}'],
    ["a root $ref", '{"$ref":"#/x","properties":{"a":{}}}'],
    [
      "a template outside quotes",
      '{"type":"object","properties":{"a":{"type":"string"}},"required":{{ keys }}}',
    ],
    [
      "a list root with anyOf",
      JSON.stringify({
        type: "array",
        anyOf: [{ type: "array" }],
        items: { type: "object", properties: { a: { type: "string" } } },
      }),
    ],
    [
      "a list root with $ref",
      JSON.stringify({
        type: "array",
        $ref: "#/x",
        items: { type: "object", properties: { a: { type: "string" } } },
      }),
    ],
    ["deep nesting", deeplyNested],
  ])("falls back to JSON for %s", (_, text) => {
    expect(describeSchema(text)).toBeNull();
  });

  test.each<[string, string, Array<SchemaField>]>([
    [
      "the example shape",
      exampleSchema,
      [
        field("approved", {
          type: "boolean",
          description: "Whether it was approved",
        }),
        field("note", { optional: true }),
        field("items", {
          type: "object[]",
          constraints: ["at least 1 item", "up to 3"],
          children: [field("code", { constraints: ["matches ^[0-9]+$"] })],
        }),
      ],
    ],
    [
      "a nullable anyOf",
      objectSchema({
        total: {
          anyOf: [{ type: "number" }, { type: "null" }],
          description: "Total",
        },
      }),
      [
        field("total", {
          type: "number",
          nullable: true,
          description: "Total",
        }),
      ],
    ],
    [
      "a nullable oneOf with a matching sibling keyword",
      objectSchema({
        code: {
          oneOf: [{ type: "string", maxLength: 5 }, { type: "null" }],
          maxLength: 5,
        },
      }),
      [
        field("code", {
          nullable: true,
          constraints: ["up to 5 characters"],
        }),
      ],
    ],
    [
      "a nullable anyOf whose sibling keyword disagrees",
      objectSchema({
        code: {
          anyOf: [{ type: "string", maxLength: 5 }, { type: "null" }],
          maxLength: 10,
          description: "Code",
        },
      }),
      [
        field("code", {
          type: "",
          description: "Code",
          constraints: ["up to 10 characters"],
          unsupported: ["anyOf"],
        }),
      ],
    ],
    [
      "a type list with null",
      objectSchema({ total: { type: ["string", "null"] } }),
      [field("total", { nullable: true })],
    ],
    [
      "a null-only type",
      objectSchema({ nothing: { type: "null" } }),
      [field("nothing", { type: "null" })],
    ],
    [
      "an enum, a format and a list of strings",
      objectSchema({
        status: { type: "string", enum: ["a", "b"] },
        done: { type: "boolean", const: true },
        due: { type: "string", format: "date" },
        tags: { type: "array", items: { type: "string" } },
      }),
      [
        field("status", { constraints: ["one of: a, b"] }),
        field("done", { type: "boolean", constraints: ["always true"] }),
        field("due", { constraints: ["date"] }),
        field("tags", { type: "string[]" }),
      ],
    ],
    [
      "keywords the list cannot show",
      objectSchema({
        ref: { $ref: "#/x" },
        choice: { anyOf: [{ type: "string" }, { type: "number" }] },
        mixed: {
          type: "object",
          properties: { a: { type: "string" } },
          additionalProperties: { type: "string" },
        },
      }),
      [
        field("ref", { type: "", unsupported: ["$ref"] }),
        field("choice", { type: "", unsupported: ["anyOf"] }),
        field("mixed", {
          type: "object",
          optional: false,
          unsupported: ["additionalProperties"],
          children: [field("a", { optional: true })],
        }),
      ],
    ],
    [
      "maps keyed by name",
      objectSchema({
        counts: { type: "object", additionalProperties: { type: "integer" } },
        people: {
          type: "object",
          additionalProperties: {
            type: "object",
            properties: { name: { type: "string" } },
            required: ["name"],
          },
        },
      }),
      [
        field("counts", {
          type: "object",
          constraints: ["each value: integer"],
        }),
        field("people", {
          type: "object",
          constraints: ["each value: object"],
          children: [field("name", {})],
        }),
      ],
    ],
    [
      "map values with a description, null, nesting or no type",
      objectSchema({
        described: {
          type: "object",
          additionalProperties: { type: "string", description: "A label" },
        },
        nullable: {
          type: "object",
          additionalProperties: {
            anyOf: [{ type: "string" }, { type: "null" }],
          },
        },
        nested: {
          type: "object",
          additionalProperties: {
            type: "object",
            additionalProperties: { type: "integer", maximum: 9 },
          },
        },
        untyped: { type: "object", additionalProperties: { enum: ["a", "b"] } },
        untypedNullable: {
          additionalProperties: { anyOf: [{ minimum: 1 }, { type: "null" }] },
        },
      }),
      [
        field("described", {
          type: "object",
          description: "A label",
          constraints: ["each value: string"],
        }),
        field("nullable", {
          type: "object",
          constraints: ["each value: string or null"],
        }),
        field("nested", {
          type: "object",
          constraints: ["each value: object, each value: integer, at most 9"],
        }),
        field("untyped", {
          type: "object",
          constraints: ["each value: one of: a, b"],
        }),
        field("untypedNullable", {
          type: "object",
          constraints: ["each value: at least 1, or null"],
        }),
      ],
    ],
    [
      "map values the list cannot show",
      objectSchema({
        ref: { type: "object", additionalProperties: { $ref: "#/x" } },
        any: { type: "object", additionalProperties: {} },
        bare: {},
        odd: { type: "object", additionalProperties: "x" },
      }),
      [
        field("ref", { type: "object", unsupported: ["additionalProperties"] }),
        field("any", { type: "object", unsupported: ["additionalProperties"] }),
        field("bare", { type: "" }),
        field("odd", { type: "object", unsupported: ["additionalProperties"] }),
      ],
    ],
    [
      "both a title and a description",
      objectSchema({
        total: { type: "number", title: "Total", description: "In dollars" },
        nullable: {
          anyOf: [{ type: "string", description: "A name" }, { type: "null" }],
          description: "Optional",
        },
        codes: {
          type: "array",
          description: "Codes",
          items: { type: "string", description: "A code" },
        },
        labels: {
          type: "object",
          description: "Labels",
          additionalProperties: { type: "string", description: "A label" },
        },
      }),
      [
        field("total", { type: "number", description: "Total\nIn dollars" }),
        field("nullable", { nullable: true, description: "A name\nOptional" }),
        field("codes", { type: "string[]", description: "Codes\nA code" }),
        field("labels", {
          type: "object",
          description: "Labels\nA label",
          constraints: ["each value: string"],
        }),
      ],
    ],
    [
      "objects that allow or forbid other fields",
      objectSchema({
        closed: {
          type: "object",
          properties: { a: { type: "string" } },
          additionalProperties: false,
        },
        open: { type: "object", additionalProperties: true },
        rows: {
          type: "array",
          items: {
            type: "object",
            properties: { a: { type: "string" } },
            additionalProperties: false,
          },
        },
      }),
      [
        field("closed", {
          type: "object",
          constraints: ["no other fields"],
          children: [field("a", { optional: true })],
        }),
        field("open", {
          type: "object",
          constraints: ["other fields allowed"],
        }),
        field("rows", {
          type: "object[]",
          constraints: ["each item: object, no other fields"],
          children: [field("a", { optional: true })],
        }),
      ],
    ],
    [
      "a single enum or example value",
      objectSchema({ kind: { type: "string", enum: "a", examples: "b" } }),
      [field("kind", { constraints: ["one of: a", "e.g. b"] })],
    ],
    [
      "list items with constraints or null",
      objectSchema({
        codes: {
          type: "array",
          maxItems: 3,
          items: { type: "string", maxLength: 5, description: "A code" },
        },
        maybe: {
          type: "array",
          items: { anyOf: [{ type: "number" }, { type: "null" }] },
        },
        grid: {
          type: "array",
          items: { type: "array", maxItems: 2, items: { type: "integer" } },
        },
      }),
      [
        field("codes", {
          type: "string[]",
          description: "A code",
          constraints: ["up to 3", "each item: string, up to 5 characters"],
        }),
        field("maybe", {
          type: "number[]",
          constraints: ["each item: number or null"],
        }),
        field("grid", {
          type: "integer[][]",
          constraints: ["each item: integer[], up to 2"],
        }),
      ],
    ],
  ])("lists the fields of %s", (_, text, fields) => {
    expect(describeSchema(text)?.fields).toEqual(fields);
  });

  test("shows a template inside a description as written", () => {
    const outline = describeSchema(
      objectSchema({ a: { type: "string", description: "Use {{ name }}" } }),
    );
    expect(outline?.fields[0]?.description).toBe("Use {{ name }}");
    expect(JSON.stringify(outline)).not.toContain("<STUB>");
  });

  test("shows unknown root keywords and a list root in the header", () => {
    const text = JSON.stringify({
      type: "array",
      title: "Rows",
      items: {
        type: "object",
        properties: { a: { type: "string" } },
        required: ["a"],
        description: "One row",
        minProperties: 1,
        additionalProperties: false,
      },
    });
    expect(describeSchema(text)).toEqual({
      isList: true,
      description: "Rows\nOne row",
      constraints: ["no other fields"],
      unsupported: ["minProperties"],
      fields: [field("a", {})],
    });
  });
});
