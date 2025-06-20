# If Block Documentation

The If Block is a conditional workflow block that allows you to create branching logic in your workflows based on the current state of the browser.

## Overview

The If Block evaluates a condition based on the browser's current content and executes different sets of blocks depending on whether the condition is true or false.

## Block Structure

```yaml
- block_type: if
  label: unique_label_for_block
  condition: "Your condition to evaluate based on browser content"
  true_blocks:
    # Blocks to execute if condition is true
    - block_type: ...
  false_blocks:
    # Blocks to execute if condition is false
    - block_type: ...
  parameter_keys: # Optional: Parameters used in the condition
    - parameter_name
```

## Fields

- **block_type**: Must be `"if"`
- **label**: A unique identifier for the block within the workflow
- **condition**: A text description of the condition to evaluate
- **true_blocks**: Array of blocks to execute if the condition evaluates to true
- **false_blocks**: Array of blocks to execute if the condition evaluates to false
- **parameter_keys**: Optional array of parameter keys that can be used in the condition

## How It Works

1. The If Block captures the current browser state (screenshot and HTML)
2. An LLM evaluates the condition based on the visible content
3. Depending on the evaluation result, either `true_blocks` or `false_blocks` are executed
4. The block output includes the condition result and which branch was executed

## Examples

### Example 1: Check Login Status

```yaml
- block_type: if
  label: check_if_logged_in
  condition: "The page shows that the user is logged in (e.g., displays username or logout button)"
  true_blocks:
    - block_type: extraction
      label: extract_user_data
      data_extraction_goal: "Extract user profile information"
  false_blocks:
    - block_type: login
      label: perform_login
      navigation_goal: "Log in to the website"
```

### Example 2: Check for Errors

```yaml
- block_type: if
  label: check_for_errors
  condition: "The page displays an error message or alert"
  true_blocks:
    - block_type: text_prompt
      label: analyze_error
      prompt: "Analyze the error message and suggest a solution"
  false_blocks:
    - block_type: navigation
      label: continue_to_next_step
      navigation_goal: "Proceed to the next page"
```

### Example 3: Nested If Blocks

```yaml
- block_type: if
  label: check_cart_status
  condition: "The shopping cart has items"
  true_blocks:
    - block_type: if
      label: check_minimum_order
      condition: "The total order value is above $50"
      true_blocks:
        - block_type: action
          label: apply_discount
          navigation_goal: "Apply the free shipping discount code"
      false_blocks:
        - block_type: text_prompt
          label: suggest_more_items
          prompt: "Suggest items to add to reach $50 for free shipping"
  false_blocks:
    - block_type: navigation
      label: go_shopping
      navigation_goal: "Navigate to the products page"
```

## Condition Writing Guidelines

1. **Be Specific**: Write clear, specific conditions that can be evaluated based on visible page content
2. **Use Visual Cues**: Reference visible elements like buttons, text, images, or page structure
3. **Avoid Ambiguity**: Make conditions that have clear true/false outcomes

### Good Conditions:
- "The page contains a login form"
- "There is a 'Submit' button visible on the page"
- "The page title contains the word 'Success'"
- "An error message in red text is displayed"
- "The shopping cart icon shows a number greater than 0"

### Poor Conditions:
- "The user wants to continue" (not based on page content)
- "The process is complete" (too vague)
- "Everything looks good" (subjective)

## Output Format

The If Block outputs:
```json
{
  "condition": "The original condition text",
  "condition_result": true/false,
  "branch_executed": "true" or "false",
  "block_results": [
    // Results from executed branch blocks
  ]
}
```

## Best Practices

1. **Test Both Branches**: Ensure both true and false branches handle their scenarios properly
2. **Use Descriptive Labels**: Give your If blocks meaningful labels that describe what they're checking
3. **Keep Conditions Simple**: Complex conditions are harder to evaluate accurately
4. **Consider Edge Cases**: What happens if neither branch perfectly matches the scenario?
5. **Use Parameters**: Leverage workflow parameters to make conditions dynamic

## Limitations

- Conditions are evaluated based on visible page content only
- The evaluation depends on LLM interpretation, so be clear and specific
- Cannot access browser state like cookies or local storage directly
- Cannot evaluate JavaScript state or hidden elements