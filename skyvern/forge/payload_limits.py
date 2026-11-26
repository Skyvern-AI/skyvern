MAX_FILE_UPLOAD_WS_JSON_OVERHEAD_BYTES = 1 * 1024 * 1024
MIN_SHARED_WS_CONTENT_BYTES = 32 * 1024 * 1024


def max_file_upload_ws_message_size(max_file_size_bytes: int) -> int:
    # Base64 expands content to 4/3 its size; leave 1 MiB for the JSON envelope.
    # The shared WebSocket limit never drops below 32 MiB of content.
    content_bytes = max(max_file_size_bytes, MIN_SHARED_WS_CONTENT_BYTES)
    return ((content_bytes + 2) // 3) * 4 + MAX_FILE_UPLOAD_WS_JSON_OVERHEAD_BYTES
