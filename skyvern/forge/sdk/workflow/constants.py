"""Workflow constants shared between block runtime and DB write paths.

Zero project-internal imports so block.py and db/utils.py can both consume without
creating a cycle.
"""

# Safety-net cap on serialized JSON size for a single jsonb-column value (SKY-9779).
OUTPUT_PARAMETER_MAX_VALUE_BYTES = 100 * 1024 * 1024
