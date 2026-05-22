# Parameter Design

## Rules

- Keep parameter names explicit (`customer_email`, not `value1`).
- Set required vs optional parameters intentionally.
- Pass parameters only to blocks that need them.
- Avoid leaking secrets into descriptions or run logs.

## Example parameter set

```json
[
  {"parameter_type":"workflow","key":"portal_url","workflow_parameter_type":"string"},
  {"parameter_type":"workflow","key":"username","workflow_parameter_type":"string"},
  {"parameter_type":"workflow","key":"password","workflow_parameter_type":"string"}
]
```

## Variable usage

Use `{{parameter_key}}` in block text fields.

Example:
`"Open {{portal_url}} and complete login with the provided credential values."`

## Run-time checklist

- Validate parameter JSON before invoking runs.
- Include defaults only when behavior is predictable.
- Record sample payloads in `examples/`.
