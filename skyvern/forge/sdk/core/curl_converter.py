from typing import Any

import curlparser
import structlog

LOG = structlog.get_logger()


def parse_curl_command(curl_command: str) -> dict[str, Any]:
    """
    Parse a curl command into HTTP request parameters using curlparser library.

    Args:
        curl_command: The curl command string to parse

    Returns:
        Dict containing:
        - method: HTTP method (GET, POST, etc.)
        - url: The URL to request
        - headers: Dict of headers
        - body: Request body as dict (parsed from JSON) or None

    Raises:
        ValueError: If the curl command cannot be parsed
    """
    try:
        # Parse the curl command
        parsed = curlparser.parse(curl_command)

        # Extract the components
        result = {
            "method": parsed.method.upper() if parsed.method else "GET",
            "url": parsed.url,
            "headers": {},
            "body": None,
        }

        # Process headers - curlparser returns headers as an OrderedDict
        if parsed.header:
            result["headers"] = {k: v.strip() for k, v in parsed.header.items()}

        # Process body/data
        if parsed.data:
            import json

            # Try to parse as JSON
            try:
                if isinstance(parsed.data, list):
                    # Join multiple data parts
                    data_str = "".join(parsed.data)
                else:
                    data_str = parsed.data

                result["body"] = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                # If not valid JSON, convert to dict with single "data" key
                LOG.warning(
                    "Curl data is not valid JSON, wrapping in data key",
                    data=parsed.data,
                )
                result["body"] = {"data": data_str}

        # Process JSON data if provided
        if hasattr(parsed, "json") and parsed.json:
            import json

            try:
                result["body"] = json.loads(parsed.json)
            except (json.JSONDecodeError, TypeError):
                LOG.warning("Curl json is not valid JSON", json=parsed.json)
                result["body"] = {"data": parsed.json}

        # Validate URL
        if not result["url"]:
            raise ValueError("No URL found in curl command")

        return result

    except Exception as e:
        LOG.error(
            "Failed to parse curl command",
            error=str(e),
            curl_command=curl_command[:100],  # Log first 100 chars for debugging
        )
        raise ValueError(f"Failed to parse curl command: {str(e)}")


def curl_to_http_request_block_params(curl_command: str) -> dict[str, Any]:
    """
    Convert a curl command to parameters suitable for HttpRequestBlock.

    This is a convenience function that can be used in API endpoints
    to convert curl commands before creating workflow blocks.

    Args:
        curl_command: The curl command string

    Returns:
        Dict with keys matching HttpRequestBlock parameters
    """
    parsed = parse_curl_command(curl_command)

    return {
        "method": parsed["method"],
        "url": parsed["url"],
        "headers": parsed["headers"] if parsed["headers"] else None,
        "body": parsed["body"],
        "timeout": 30,  # Default timeout
        "follow_redirects": True,  # Default follow redirects
    }
