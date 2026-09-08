#!/usr/bin/env python3
"""End-to-end: run Skyvern's real FileParserBlock extraction with and without the filter.

    export ZQ_API_KEY=<key>
    python evaluation/vslm/repro_extraction.py

Requires whatever LLM provider your Skyvern install is already configured with
(the same one `skyvern run server` would use). No database and no browser.

This drives the real `FileParserBlock._extract_with_ai()` -- the same method a
production FileParser workflow block calls -- so the only difference between the
two runs is ENABLE_VSLM_TEXT_FILTER.

Expected result: with the filter on, `invoice_number` comes back null, because
the line carrying it (`Invoice #: 88213-B`) is classified irrelevant and removed
before the extraction prompt is built. No error is raised.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SAMPLE = Path(__file__).parent / "sample_remittance_advice.txt"

SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string", "description": "The invoice identifier"},
        "total_amount_due": {"type": "string", "description": "Total amount due on the invoice"},
        "purchase_order": {"type": "string", "description": "Referenced purchase order number"},
    },
}

EXPECTED = {
    "invoice_number": "88213-B",
    "total_amount_due": "48,215.60",
    "purchase_order": "PO-77213",
}


def _configure(enabled: bool) -> None:
    """Settings are read at import time, so set env before importing skyvern."""
    os.environ["ENABLE_VSLM_TEXT_FILTER"] = "true" if enabled else "false"


async def extract(document: str, *, filtered: bool) -> dict:
    from skyvern.config import settings

    settings.ENABLE_VSLM_TEXT_FILTER = filtered

    from skyvern.forge.sdk.workflow.models.block import FileParserBlock
    from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType

    now = datetime.now(timezone.utc)
    block = FileParserBlock(
        label="parse_remittance",
        output_parameter=OutputParameter(
            output_parameter_id="op_repro",
            key="parsed",
            workflow_id="w_repro",
            parameter_type=ParameterType.OUTPUT,
            created_at=now,
            modified_at=now,
        ),
        file_url=SAMPLE.as_uri(),
        json_schema=SCHEMA,
    )
    return await block._extract_with_ai(document, None)


def report(label: str, got: object) -> int:
    print(f"\n--- {label} ---")
    print(f"  raw: {json.dumps(got, default=str)}")
    missing = 0
    for field, expected in EXPECTED.items():
        actual = got.get(field) if isinstance(got, dict) else None
        ok = actual is not None and expected.replace(",", "") in str(actual).replace(",", "").replace("$", "")
        if not ok:
            missing += 1
        print(f"  {'OK  ' if ok else 'MISS'}  {field:<18} expected ~{expected!r}, got {actual!r}")
    return missing


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document", type=Path, default=SAMPLE)
    args = parser.parse_args()

    if not os.environ.get("ZQ_API_KEY"):
        sys.exit("set ZQ_API_KEY first")

    _configure(False)
    from skyvern.forge.forge_app_initializer import start_forge_app

    start_forge_app()

    document = args.document.read_text()
    print("=" * 74)
    print("Skyvern FileParserBlock extraction -- filter OFF vs ON")
    print("=" * 74)
    print(f"document: {args.document}  ({len(document.splitlines())} lines)")

    baseline_error = filtered_error = None
    try:
        baseline = await extract(document, filtered=False)
        missing_baseline = report("ENABLE_VSLM_TEXT_FILTER=false (baseline)", baseline)
    except ValueError as exc:
        baseline_error, missing_baseline = str(exc), len(EXPECTED)
        print("\n--- ENABLE_VSLM_TEXT_FILTER=false (baseline) ---")
        print(f"  block FAILED: {exc}")

    try:
        filtered = await extract(document, filtered=True)
        missing_filtered = report("ENABLE_VSLM_TEXT_FILTER=true", filtered)
    except ValueError as exc:
        # FileParserBlock validates the LLM response against json_schema and raises
        # after exhausting retries, so a filtered-out field fails the whole block.
        filtered_error, missing_filtered = str(exc), len(EXPECTED)
        print("\n--- ENABLE_VSLM_TEXT_FILTER=true ---")
        print(f"  block FAILED: {exc}")

    print("\n" + "=" * 74)
    if filtered_error and not baseline_error:
        print("REPRODUCED: the baseline extracted every field; with the filter on the")
        print("block failed schema validation because a required field came back null.")
    elif missing_filtered > missing_baseline:
        print("REPRODUCED: filtering removed at least one field the baseline extracted.")
        print("(Field was nullable in this schema, so it degraded silently rather than raising.)")
    else:
        print("not reproduced in this run.")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
