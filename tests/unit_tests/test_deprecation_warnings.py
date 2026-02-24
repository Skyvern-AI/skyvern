"""Tests that verify no deprecation warnings are raised by Skyvern's Pydantic models,
FastAPI routes, and websockets integration.

See: https://linear.app/starstruck/issue/SKY-52
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest


class TestPydanticModelValidatorDeprecations:
    """Verify @model_validator(mode='after') methods use instance method style.

    Pydantic V2.12 deprecated classmethod-style @model_validator(mode='after').
    The correct pattern uses 'self' with no second positional parameter.
    """

    @pytest.mark.parametrize(
        "import_path,class_name,method_name",
        [
            ("skyvern.schemas.runs", "WorkflowRunRequest", "validate_browser_reference"),
            ("skyvern.schemas.workflows", "BranchConditionYAML", "validate_condition"),
            ("skyvern.schemas.workflows", "ConditionalBlockYAML", "validate_branches"),
            ("skyvern.schemas.workflows", "WorkflowDefinitionYAML", "validate_unique_block_labels"),
            ("skyvern.forge.sdk.workflow.models.block", "BranchCondition", "validate_condition"),
            ("skyvern.forge.sdk.workflow.models.block", "ConditionalBlock", "validate_branches"),
            ("skyvern.forge.sdk.workflow.models.workflow", "WorkflowRequestBody", "validate_browser_reference"),
        ],
    )
    def test_model_validator_uses_instance_method_style(self, import_path: str, class_name: str, method_name: str):
        """Model validator should use 'self' parameter, not old cls+values classmethod pattern."""
        module = importlib.import_module(import_path)
        model_cls = getattr(module, class_name)

        # Read the source code directly and parse AST to check the method signature,
        # because Pydantic wraps the validator at class creation time.
        source_file = inspect.getfile(model_cls)
        tree = ast.parse(Path(source_file).read_text())

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != class_name:
                continue
            for item in node.body:
                if not isinstance(item, ast.FunctionDef) or item.name != method_name:
                    continue
                args = item.args
                param_names = [a.arg for a in args.args]

                assert len(param_names) == 1, (
                    f"{class_name}.{method_name} has {len(param_names)} parameters {param_names}, "
                    f"expected 1 (just 'self'). "
                    f"Two-parameter signature triggers PydanticDeprecatedSince212 warning."
                )
                assert param_names[0] == "self", (
                    f"{class_name}.{method_name} first parameter is '{param_names[0]}', expected 'self'. "
                    f"Using 'cls' triggers PydanticDeprecatedSince212 warning."
                )
                return

        pytest.fail(f"Could not find {class_name}.{method_name} in source AST")

    @pytest.mark.parametrize(
        "import_path,class_name,method_name",
        [
            ("skyvern.schemas.runs", "WorkflowRunRequest", "validate_browser_reference"),
            ("skyvern.schemas.workflows", "BranchConditionYAML", "validate_condition"),
            ("skyvern.schemas.workflows", "ConditionalBlockYAML", "validate_branches"),
            ("skyvern.schemas.workflows", "WorkflowDefinitionYAML", "validate_unique_block_labels"),
            ("skyvern.forge.sdk.workflow.models.block", "BranchCondition", "validate_condition"),
            ("skyvern.forge.sdk.workflow.models.block", "ConditionalBlock", "validate_branches"),
            ("skyvern.forge.sdk.workflow.models.workflow", "WorkflowRequestBody", "validate_browser_reference"),
        ],
    )
    def test_no_pydantic_deprecation_warning_on_import(self, import_path: str, class_name: str, method_name: str):
        """Importing modules should not raise PydanticDeprecatedSince212 warnings."""
        # Note: warnings are raised at class definition time (first import).
        # After initial import, re-importing doesn't re-trigger.
        # This test verifies via subprocess to get a clean import.
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-W",
                "error::DeprecationWarning",
                "-c",
                f"from {import_path} import {class_name}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Importing {import_path}.{class_name} raised a DeprecationWarning:\n{result.stderr}"
        )


class TestPydanticConfigDeprecations:
    """Verify Pydantic models use model_config = ConfigDict(...) instead of inner class Config."""

    @pytest.mark.parametrize(
        "import_path,class_name",
        [
            ("skyvern.services.browser_recording.types", "CdpEventFrame"),
            ("skyvern.schemas.folders", "Folder"),
        ],
    )
    def test_model_uses_config_dict_not_inner_class(self, import_path: str, class_name: str):
        """Models should use model_config = ConfigDict(...) instead of class Config."""
        module = importlib.import_module(import_path)
        model_cls = getattr(module, class_name)

        # Check via AST that the class doesn't have an inner Config class
        source_file = inspect.getfile(model_cls)
        tree = ast.parse(Path(source_file).read_text())

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != class_name:
                continue
            inner_class_names = [item.name for item in node.body if isinstance(item, ast.ClassDef)]
            assert "Config" not in inner_class_names, (
                f"{class_name} uses inner 'class Config' which is deprecated in Pydantic V2.0. "
                f"Use 'model_config = ConfigDict(...)' instead."
            )
            return

        pytest.fail(f"Could not find class {class_name} in source AST")


class TestFastAPIExampleDeprecations:
    """Verify FastAPI routes use examples= instead of deprecated example= parameter."""

    def test_credentials_routes_use_examples_not_example(self):
        """Body() definitions in credentials routes should use examples= not example=."""
        credentials_path = Path("skyvern/forge/sdk/routes/credentials.py")
        source = credentials_path.read_text()
        tree = ast.parse(source)

        deprecated_usages = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Check for Body(..., example=...) or Query(..., example=...) calls
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name in ("Body", "Query", "Path", "Header"):
                for keyword in node.keywords:
                    if keyword.arg == "example":
                        deprecated_usages.append(
                            f"{func_name}() at line {node.lineno} uses deprecated 'example=' parameter"
                        )

        assert not deprecated_usages, (
            "Found deprecated 'example=' usage in credentials routes:\n"
            + "\n".join(f"  - {u}" for u in deprecated_usages)
            + "\nUse 'examples=[...]' (list format) instead."
        )


class TestWebsocketsDeprecations:
    """Verify websockets legacy API is not used (deprecated in v14.0)."""

    def test_uvicorn_websockets_no_legacy_import(self):
        """Importing uvicorn's websockets implementation should not trigger legacy warnings."""
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-W",
                "error::DeprecationWarning",
                "-c",
                "from uvicorn.protocols.websockets import websockets_impl",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Importing uvicorn websockets raised a DeprecationWarning:\n{result.stderr}"
