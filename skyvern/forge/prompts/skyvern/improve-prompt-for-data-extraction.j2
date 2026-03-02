I am going to give you an original prompt for a data extraction task performed by an AI browser agent, and a field name.

Can you improve this original prompt using the following best practices for data extraction prompts:

Depending on the field, you will need to structure the prompt differently.

**data_extraction_goal**: Describes WHAT data the agent should extract from a web page. This is not about navigation or clicking — it is about reading, identifying, and pulling structured information from the visible content on a page.

**data_schema**: A JSON Schema that defines the expected shape of the extracted output.

Rules for improving a data extraction goal:
1. Be specific about what data to extract (e.g., "Extract the product name, price, and availability" not "Extract product info")
2. Mention whether the data is in a list/table or a single item
3. If the data spans multiple sections of a page, describe where to find each piece
4. Clarify how to handle missing data (e.g., "If price is not listed, output null")
5. If there are multiple similar items, clarify whether to extract all of them or just specific ones
6. Do NOT include navigation instructions — data extraction goals assume the agent is already on the correct page
7. If a data schema is provided in the context, ensure the extraction goal aligns with the schema fields

Rules for improving a data schema:
1. Use JSON Schema specification style
2. Include "type" and "description" for every field
3. Mark fields as required only when they are guaranteed to exist
4. For lists of items, use "type": "array" with an "items" schema
5. Use descriptive field names (e.g., "product_name" not "name")
6. Add a "description" to clarify ambiguous fields

Examples of good data extraction goals:

Example 1 (simple):
```
Extract the product name, price, and availability status from the product detail page.
```

Example 2 (list extraction):
```
Extract all job listings visible on the page. For each listing, capture the job title, company name, location, and salary range if available. If salary is not listed, output null for that field.
```

Example 3 (structured extraction with schema reference):
```
Extract the order summary information including: order number, order date, each line item (product name, quantity, unit price), subtotal, tax, and total amount. Output items as an array matching the provided schema.
```

Respond ONLY with valid JSON in this format with no additional text before or after it:
```json
{
 "improved_prompt": str, // The improved version of the prompt
}
```

Ensure that the "improved_prompt" contains liberal whitespace tokens for formatting, clarity, and legibility.

Here is the original prompt from the user:

```
{{ prompt }}
```

The field being improved is: {{ context.field | default("data_extraction_goal") }}
{% if context and context.data_schema %}

The expected data schema for the extraction output is:
```
{{ context.data_schema }}
```
Ensure the improved prompt aligns with the fields defined in this schema.
{% endif %}
{% if context and context.navigation_goal %}

Note: This data extraction happens after the following navigation goal is completed:
```
{{ context.navigation_goal }}
```
{% endif %}
