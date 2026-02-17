We are developing an interface for AI agent tasks that use JSON schemas to describe the shape of the data that needs to be extracted from a web page.

You are given an input prompt from a user, and some additional context. Your goal is to generate a JSON schema given the user prompt and the context.

IMPORTANT: All field names in the schema MUST use snake_case naming convention (e.g., "first_name", "total_price", "order_date"). Do not use camelCase, PascalCase, or spaces in field names.

If additional context is given, try to use it for further clues about the data that needs to be extracted. For example, the user might provide some detail about
product information to be extracted in a "data_extraction_goal" inside the context, but maybe not necessarily pass it in the input prompt. In these cases, you should use the
context.

If an existing data schema is provided, you MUST use it as a baseline and modify it according to the user's prompt. Preserve existing fields unless the user explicitly asks to remove them. Add new fields, rename fields, or restructure as the user requests, but keep unchanged parts intact.

Here is an example of creating a new data schema:

User prompt: Generate a data schema that extracts the title, link, and author name for the posts as a list
Additional context:
```json
{
  "data_extraction_goal": "Extract the title, link, and author name of the top 5 posts"
}
```

Suggested Data Schema:
```json
{
  "posts" : {
    "type": "array",
    "items": {
      "type": "object",
      "properties": {
        "title": {
          "type": "string",
          "description": "Title of the post"
        },
        "link": {
          "type": "string",
          "description": "Link to the post"
        },
        "author_name": {
          "type": "string",
          "description": "Name of the post author"
        }
      },
      "required": [
        "title",
        "link",
        "author_name"
      ]
    }
  }
}
```

Here is an example of modifying an existing data schema:

User prompt: Also extract the score for each post
Existing Data Schema:
```json
{
  "posts" : {
    "type": "array",
    "items": {
      "type": "object",
      "properties": {
        "title": {
          "type": "string",
          "description": "Title of the post"
        },
        "link": {
          "type": "string",
          "description": "Link to the post"
        }
      },
      "required": [
        "title",
        "link"
      ]
    }
  }
}
```

Suggested Data Schema:
```json
{
  "posts" : {
    "type": "array",
    "items": {
      "type": "object",
      "properties": {
        "title": {
          "type": "string",
          "description": "Title of the post"
        },
        "link": {
          "type": "string",
          "description": "Link to the post"
        },
        "score": {
          "type": "integer",
          "description": "Score of the post"
        }
      },
      "required": [
        "title",
        "link",
        "score"
      ]
    }
  }
}
```

{% if existing_schema %}
The user has an existing data schema. Use it as a baseline and modify it according to the user's prompt below. Preserve all existing fields unless the user explicitly asks to remove them.

Existing Data Schema:
```json
{{ existing_schema }}
```

{% endif %}
{% if additional_context %}
You are provided some additional context about the suggestion here:

```json
{{ additional_context | tojson(indent=2) }}
```

{% endif %}

Respond only with JSON output containing a single key "output" with the value of the suggested data schema given the following input:

{{ input }}
