# Schema Patterns for Extraction

## Minimal list schema

```json
{
  "type": "object",
  "properties": {
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": {"type": "string"},
          "price": {"type": "string"}
        },
        "required": ["name"]
      }
    }
  },
  "required": ["items"]
}
```

## Practical guidance

- Keep required fields to truly required business data.
- Use strings first for prices/dates unless typed values are guaranteed.
- Add numeric typing only after site formatting is known to be consistent.
- Do not request every visible field in the first pass.
