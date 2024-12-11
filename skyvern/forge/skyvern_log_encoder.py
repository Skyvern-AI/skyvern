from typing import Any, Dict, List
from datetime import datetime
import json

from structlog.dev import ConsoleRenderer
import structlog

LOG = structlog.get_logger()

class SkyvernLogEncoder:
    """Encodes Skyvern logs from JSON format to human-readable string format"""

    def __init__(self):
        self.renderer = ConsoleRenderer(
            pad_event=30,
            colors=False,
        )

    @staticmethod
    def _format_value(value: Any) -> str:
        """Format complex values into readable strings."""
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        return str(value)

    @staticmethod
    def _parse_json_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a JSON log entry into our standard format."""
        event = entry.get('message', entry.get('event', ''))

        clean_entry = {
            'timestamp': entry.get('timestamp', datetime.utcnow().isoformat() + "Z"),
            'level': entry.get('level', 'info').lower(),
            'event': event
        }

        for key, value in entry.items():
            if key not in ('timestamp', 'level', 'event', 'message'):
                clean_entry[key] = SkyvernLogEncoder._format_value(value)

        return clean_entry

    @classmethod
    def encode(cls, log_entries: List[Dict[str, Any]]) -> str:
        """
        Encode log entries into formatted string output using structlog's ConsoleRenderer.

        Args:
            log_entries: List of log entry dictionaries

        Returns:
            Formatted string with one log entry per line
        """
        encoder = cls()
        formatted_lines = []

        for entry in log_entries:
            try:
                if isinstance(entry, str):
                    try:
                        entry = json.loads(entry)
                    except json.JSONDecodeError:
                        entry = {'event': entry, 'level': 'info'}

                parsed_entry = cls._parse_json_entry(entry)

                formatted_line = encoder.renderer(None, None, parsed_entry)
                formatted_lines.append(formatted_line)

            except Exception as e:
                LOG.error(
                    "Failed to format log entry",
                    entry=entry,
                    error=str(e),
                    exc_info=True
                )
                # Add error line to output
                error_timestamp = datetime.utcnow().isoformat() + "Z"
                error_entry = {
                    'timestamp': error_timestamp,
                    'level': 'error',
                    'event': 'Failed to format log entry',
                    'entry': str(entry),
                    'error': str(e)
                }
                formatted_lines.append(encoder.renderer(None, None, error_entry))

        return "\n".join(formatted_lines)
