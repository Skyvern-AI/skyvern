import { isDeeplyNestedDocument } from "@/routes/workflows/components/oversizedDocument";

type SchemaField = {
  name: string;
  type: string;
  optional: boolean;
  nullable: boolean;
  description: string | null;
  constraints: Array<string>;
  unsupported: Array<string>;
  children: Array<SchemaField>;
};

type SchemaOutline = {
  isList: boolean;
  description: string | null;
  constraints: Array<string>;
  unsupported: Array<string>;
  fields: Array<SchemaField>;
};

type SchemaView = "fields" | "json";

type JsonObject = Record<string, unknown>;

const ROOT_FALLBACK_KEYWORDS = [
  "anyOf",
  "oneOf",
  "allOf",
  "$ref",
  "$defs",
  "not",
  "if",
  "patternProperties",
];

const ROOT_KEYWORDS = new Set([
  "type",
  "properties",
  "required",
  "description",
  "title",
  "additionalProperties",
  "$schema",
]);

const FIELD_KEYWORDS = new Set([
  "type",
  "properties",
  "required",
  "items",
  "description",
  "title",
  "additionalProperties",
  "enum",
  "const",
  "pattern",
  "format",
  "minItems",
  "maxItems",
  "minLength",
  "maxLength",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "default",
  "examples",
]);

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function formatList(value: unknown): string {
  return Array.isArray(value)
    ? value.map(formatValue).join(", ")
    : formatValue(value);
}

function joinText(texts: Array<string | null>): string | null {
  const present = [
    ...new Set(texts.filter((text): text is string => text !== null)),
  ];
  return present.length === 0 ? null : present.join("\n");
}

function otherFieldsPhrase(schema: JsonObject): string | null {
  if (schema.additionalProperties === false) {
    return "no other fields";
  }
  if (schema.additionalProperties === true) {
    return "other fields allowed";
  }
  return null;
}

function isNullSchema(value: unknown): boolean {
  return isObject(value) && value.type === "null";
}

function hasConflictingKeyword(inner: JsonObject, rest: JsonObject): boolean {
  return Object.keys(inner).some(
    (key) =>
      key !== "title" &&
      key !== "description" &&
      key in rest &&
      JSON.stringify(inner[key]) !== JSON.stringify(rest[key]),
  );
}

function unwrapNullable(schema: JsonObject): {
  schema: JsonObject;
  nullable: boolean;
  description: string | null;
} {
  for (const keyword of ["anyOf", "oneOf"]) {
    const options = schema[keyword];
    if (
      Array.isArray(options) &&
      options.length === 2 &&
      options.some(isNullSchema)
    ) {
      const inner = options.find((option) => !isNullSchema(option));
      const rest = { ...schema };
      delete rest[keyword];
      if (isObject(inner) && !hasConflictingKeyword(inner, rest)) {
        return {
          schema: { ...inner, ...rest },
          nullable: true,
          description: joinText([
            formatDescription(inner),
            formatDescription(rest),
          ]),
        };
      }
    }
  }
  return { schema, nullable: false, description: formatDescription(schema) };
}

function describeType(schema: JsonObject): {
  type: string;
  nullable: boolean;
} {
  const raw = schema.type;
  const declared = (
    Array.isArray(raw) ? raw : raw === undefined ? [] : [raw]
  ).filter((type): type is string => typeof type === "string");
  const types = declared.filter((type) => type !== "null");
  const nullable = Array.isArray(raw) && raw.includes("null");
  if (types.length > 0) {
    return { type: types.join(" | "), nullable };
  }
  if (declared.length > 0) {
    return { type: "null", nullable };
  }
  if (isObject(schema.properties)) {
    return { type: "object", nullable };
  }
  if (schema.items !== undefined) {
    return { type: "array", nullable };
  }
  return { type: "", nullable };
}

function constraintPhrases(schema: JsonObject): Array<string> {
  const phrases: Array<string> = [];
  if (schema.enum !== undefined) {
    phrases.push(`one of: ${formatList(schema.enum)}`);
  }
  if (schema.const !== undefined) {
    phrases.push(`always ${formatValue(schema.const)}`);
  }
  if (schema.pattern !== undefined) {
    phrases.push(`matches ${formatValue(schema.pattern)}`);
  }
  if (schema.format !== undefined) {
    phrases.push(formatValue(schema.format));
  }
  if (schema.minItems !== undefined) {
    const noun = schema.minItems === 1 ? "item" : "items";
    phrases.push(`at least ${formatValue(schema.minItems)} ${noun}`);
  }
  if (schema.maxItems !== undefined) {
    phrases.push(`up to ${formatValue(schema.maxItems)}`);
  }
  if (schema.minLength !== undefined) {
    phrases.push(`at least ${formatValue(schema.minLength)} characters`);
  }
  if (schema.maxLength !== undefined) {
    phrases.push(`up to ${formatValue(schema.maxLength)} characters`);
  }
  if (schema.minimum !== undefined) {
    phrases.push(`at least ${formatValue(schema.minimum)}`);
  }
  if (schema.maximum !== undefined) {
    phrases.push(`at most ${formatValue(schema.maximum)}`);
  }
  if (schema.exclusiveMinimum !== undefined) {
    phrases.push(`more than ${formatValue(schema.exclusiveMinimum)}`);
  }
  if (schema.exclusiveMaximum !== undefined) {
    phrases.push(`less than ${formatValue(schema.exclusiveMaximum)}`);
  }
  if (schema.default !== undefined) {
    phrases.push(`default ${formatValue(schema.default)}`);
  }
  if (schema.examples !== undefined) {
    phrases.push(`e.g. ${formatList(schema.examples)}`);
  }
  const otherFields = otherFieldsPhrase(schema);
  if (otherFields !== null) {
    phrases.push(otherFields);
  }
  return phrases;
}

function unsupportedKeywords(
  schema: JsonObject,
  known: Set<string>,
): Array<string> {
  const keywords = Object.keys(schema).filter((key) => !known.has(key));
  if (
    schema.additionalProperties !== undefined &&
    typeof schema.additionalProperties !== "boolean"
  ) {
    keywords.push("additionalProperties");
  }
  return keywords;
}

function requiredNames(schema: JsonObject): Set<string> {
  return new Set(
    Array.isArray(schema.required)
      ? schema.required.filter(
          (name): name is string => typeof name === "string",
        )
      : [],
  );
}

function describeProperties(schema: JsonObject): Array<SchemaField> {
  if (!isObject(schema.properties)) {
    return [];
  }
  const required = requiredNames(schema);
  return Object.entries(schema.properties).map(([name, value]) =>
    describeField(name, value, !required.has(name)),
  );
}

function describeField(
  name: string,
  value: unknown,
  notListedAsRequired: boolean,
): SchemaField {
  if (!isObject(value)) {
    return {
      name,
      type: "",
      optional: notListedAsRequired,
      nullable: false,
      description: null,
      constraints: [],
      unsupported: [formatValue(value)],
      children: [],
    };
  }
  const unwrapped = unwrapNullable(value);
  const schema = unwrapped.schema;
  const described = describeType(schema);
  const field: SchemaField = {
    name,
    type: described.type,
    optional: notListedAsRequired && schema.required !== true,
    nullable:
      (unwrapped.nullable || described.nullable) && described.type !== "null",
    description: unwrapped.description,
    constraints: constraintPhrases(schema),
    unsupported: unsupportedKeywords(schema, FIELD_KEYWORDS),
    children: describeProperties(schema),
  };
  if (described.type === "array" && isObject(schema.items)) {
    const item = describeField("", schema.items, false);
    field.type = item.type === "" ? "array" : `${item.type}[]`;
    field.description = joinText([field.description, item.description]);
    const itemPhrase = scopedPhrase("each item", item);
    if (itemPhrase !== null && (item.constraints.length > 0 || item.nullable)) {
      field.constraints.push(itemPhrase);
    }
    field.unsupported = [
      ...new Set([...field.unsupported, ...item.unsupported]),
    ];
    field.children.push(...item.children);
  } else if (schema.items !== undefined) {
    field.unsupported.push("items");
  }
  const entry =
    isObject(schema.additionalProperties) && !isObject(schema.properties)
      ? describeField("", schema.additionalProperties, false)
      : null;
  const valuePhrase = entry === null ? null : scopedPhrase("each value", entry);
  if (entry !== null && valuePhrase !== null) {
    field.type = field.type === "" ? "object" : field.type;
    field.description = joinText([field.description, entry.description]);
    field.constraints.push(valuePhrase);
    field.unsupported = [
      ...new Set([
        ...field.unsupported.filter(
          (keyword) => keyword !== "additionalProperties",
        ),
        ...entry.unsupported,
      ]),
    ];
    field.children.push(...entry.children);
  }
  return field;
}

function scopedPhrase(label: string, value: SchemaField): string | null {
  const parts = [value.type, ...value.constraints].filter(Boolean);
  if (parts.length === 0) {
    return null;
  }
  if (value.nullable && value.type !== "") {
    parts[0] = `${value.type} or null`;
  } else if (value.nullable) {
    parts.push("or null");
  }
  return `${label}: ${parts.join(", ")}`;
}

function formatDescription(schema: JsonObject): string | null {
  return joinText(
    [schema.title, schema.description].map((text) =>
      text === undefined ? null : formatValue(text),
    ),
  );
}

function hasProperties(schema: JsonObject): boolean {
  return (
    isObject(schema.properties) && Object.keys(schema.properties).length > 0
  );
}

function describeSchema(text: string): SchemaOutline | null {
  if (isDeeplyNestedDocument(text)) {
    return null;
  }
  let root: unknown;
  try {
    root = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isObject(root)) {
    return null;
  }
  let isList = false;
  let body = root;
  if (root.type === "array" && isObject(root.items)) {
    isList = true;
    body = root.items;
  }
  if (
    !hasProperties(body) ||
    ROOT_FALLBACK_KEYWORDS.some((keyword) => keyword in body || keyword in root)
  ) {
    return null;
  }
  const unsupported = unsupportedKeywords(body, ROOT_KEYWORDS);
  if (isList) {
    unsupported.push(
      ...unsupportedKeywords(root, new Set([...ROOT_KEYWORDS, "items"])),
    );
  }
  return {
    isList,
    description: joinText([formatDescription(root), formatDescription(body)]),
    constraints: [
      ...new Set(
        [otherFieldsPhrase(root), otherFieldsPhrase(body)].filter(
          (phrase): phrase is string => phrase !== null,
        ),
      ),
    ],
    unsupported,
    fields: describeProperties(body),
  };
}

export {
  describeSchema,
  type SchemaField,
  type SchemaOutline,
  type SchemaView,
};
