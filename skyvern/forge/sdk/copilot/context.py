"""Structured context for copilot cross-turn memory."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field


class UrlVisit(BaseModel):
    url: str
    summary: str = ""


class FieldFilled(BaseModel):
    selector: str = ""
    label: str = ""
    value: str = ""


class CredentialCheck(BaseModel):
    credential_name: str = ""
    credential_id: str | None = None
    found: bool = False


class StructuredContext(BaseModel):
    user_goal: str = ""
    urls_visited: list[UrlVisit] = Field(default_factory=list)
    fields_filled: list[FieldFilled] = Field(default_factory=list)
    credentials_checked: list[CredentialCheck] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    workflow_state: str = ""

    def to_json_str(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json_str(cls, raw: str | None) -> StructuredContext:
        if not raw:
            return cls()
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                return cls.model_validate_json(raw)
            except Exception:
                return cls(user_goal=raw)
        return cls(user_goal=raw)

    def merge_turn_summary(self, tool_activity: list[dict]) -> None:
        for entry in tool_activity:
            tool = entry.get("tool", "")
            summary = entry.get("summary", "")

            if tool == "navigate_browser":
                url = summary.removeprefix("Navigated to ").strip()
                if url and not any(v.url == url for v in self.urls_visited):
                    self.urls_visited.append(UrlVisit(url=url, summary=""))

            elif tool == "list_credentials":
                match = re.search(r"Found (\d+)", summary)
                found = int(match.group(1)) > 0 if match else False
                self.credentials_checked.append(CredentialCheck(credential_name=summary, found=found))

            elif tool == "type_text":
                parts = summary.split("into ")
                selector = parts[-1].strip("'\"") if len(parts) > 1 else ""
                # Intentionally omit value: typed text may contain PII / credentials.
                self.fields_filled.append(FieldFilled(selector=selector, label=selector))

            elif tool == "update_workflow":
                self.workflow_state = summary

            elif tool in ("click", "evaluate", "run_blocks_and_collect_debug", "get_run_results"):
                self.decisions_made.append(f"{tool}: {summary}")

            elif tool == "get_browser_screenshot":
                if "(" in summary and ")" in summary:
                    url = summary.split("(", 1)[1].rsplit(")", 1)[0]
                    if url and not any(v.url == url for v in self.urls_visited):
                        self.urls_visited.append(UrlVisit(url=url, summary="screenshot"))

            output = entry.get("output_preview")
            if output and tool in ("run_blocks_and_collect_debug", "get_run_results"):
                preview = output[:300] if len(output) > 300 else output
                self.decisions_made.append(f"  output: {preview}")

        if len(self.decisions_made) > 20:
            self.decisions_made = self.decisions_made[-15:]
        if len(self.urls_visited) > 50:
            self.urls_visited = self.urls_visited[-40:]
        if len(self.fields_filled) > 50:
            self.fields_filled = self.fields_filled[-40:]
        if len(self.credentials_checked) > 50:
            self.credentials_checked = self.credentials_checked[-40:]
