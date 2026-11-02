MAX_FILE_UPLOAD_WS_JSON_OVERHEAD_BYTES = 1 * 1024 * 1024


def max_file_upload_ws_message_size(max_file_size_bytes: int) -> int:
    # Base64 expands the file to 4/3 its size; leave 1 MiB for the JSON envelope.
    return ((max_file_size_bytes + 2) // 3) * 4 + MAX_FILE_UPLOAD_WS_JSON_OVERHEAD_BYTES
