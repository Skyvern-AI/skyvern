"""Turning Python calls into ``Runtime.callFunctionOn`` and remote objects back into Python.

Playwright's contract is that ``evaluate`` takes a function source and one optional argument, and
returns a JSON-serializable value; ``evaluate_handle`` returns a live handle instead. Element handles
passed as arguments must arrive in the page as real DOM nodes. This module is the single place that
translation happens, so every higher layer can stay in Python terms.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from skyvern.webeye.skycdp.errors import CdpError, CdpScriptCompileError, CdpTargetClosedError

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.connection import CdpSession

# Chrome's callFunctionOn needs a function *declaration*. Callers pass three shapes -- a real
# function, a bare expression, and an immediately-invoked function expression -- so the shape is
# detected rather than declared. Only a real function may be forwarded as-is; the other two are
# wrapped. An IIFE is the case that punishes a loose check: it contains an arrow, but that arrow is
# the inner function's signature, not the IIFE's, and forwarding it makes Chrome reject the payload
# with a syntax error naming a token. Skyvern injects its DOM utilities in exactly that shape.


def _skip_balanced_parens(source: str, start: int) -> int:
    """Index just past the parenthesis group opening at ``start``, or -1 if it never closes."""
    depth = 0
    for index in range(start, len(source)):
        character = source[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def looks_like_function(source: str) -> bool:
    """Whether ``source`` is a function Chrome can accept as a declaration."""
    stripped = source.strip()
    if stripped.startswith(("function", "async function")):
        return True
    if stripped.startswith("async "):
        stripped = stripped[len("async ") :].lstrip()
        if stripped.startswith("function"):
            return True

    if stripped.startswith("("):
        # An arrow function's parameter list is the FIRST parenthesis group and the arrow follows it
        # immediately. Anything else after that group -- a call, a property access -- means this is an
        # expression that merely contains a function.
        after_params = _skip_balanced_parens(stripped, 0)
        if after_params == -1:
            return False
        return stripped[after_params:].lstrip().startswith("=>")

    # A single unparenthesised parameter: `element => ...`
    head, arrow, _ = stripped.partition("=>")
    return bool(arrow) and head.strip().isidentifier()


def wrap_as_function(source: str) -> str:
    """Turn any evaluatable source into something Chrome will accept as a function declaration.

    The newlines are load-bearing. A wrapped expression that begins or ends with a ``//`` comment --
    which is exactly how an injected script file looks -- would otherwise have the opening or closing
    parenthesis swallowed by that comment, and Chrome rejects the result with a syntax error naming a
    token from deep inside the script.
    """
    if looks_like_function(source):
        return source
    return f"() => (\n{source}\n)"


def wrap_as_function_body(source: str) -> str:
    """Wrap source that is a sequence of statements rather than a single expression.

    An injected script file ends in ``})();`` -- a statement. Placed in the expression form above,
    that trailing semicolon sits inside a parenthesised expression and Chrome rejects it. Statements
    need a function body instead, which returns undefined; that is correct for a script evaluated
    for its side effects, which is what such files are.
    """
    return f"() => {{\n{source}\n}}"


class RemoteHandle:
    """A live reference to an object inside the page."""

    def __init__(self, session: CdpSession, remote_object: dict[str, Any]) -> None:
        self._session = session
        self._remote = remote_object
        self._disposed = False

    @property
    def object_id(self) -> str | None:
        return self._remote.get("objectId")

    @property
    def subtype(self) -> str | None:
        return self._remote.get("subtype")

    @property
    def session(self) -> CdpSession:
        return self._session

    async def json_value(self) -> Any:
        if self.object_id is None:
            return deserialize(self._remote)
        result = await self._session.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": "function() { return this; }",
                "objectId": self.object_id,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        return deserialize(result.get("result", {}))

    async def dispose(self) -> None:
        if self._disposed or self.object_id is None:
            return
        self._disposed = True
        try:
            await self._session.send("Runtime.releaseObject", {"objectId": self.object_id})
        except (CdpTargetClosedError, CdpError):
            return


def deserialize(remote_object: dict[str, Any]) -> Any:
    """Convert a by-value ``RemoteObject`` into the Python value Playwright would return."""
    kind = remote_object.get("type")
    if kind == "undefined":
        return None
    if "unserializableValue" in remote_object:
        raw = remote_object["unserializableValue"]
        return {"NaN": float("nan"), "Infinity": float("inf"), "-Infinity": float("-inf"), "-0": -0.0}.get(raw, raw)
    if "value" in remote_object:
        return remote_object["value"]
    if kind == "object" and remote_object.get("subtype") == "null":
        return None
    return None


def as_remote_handle(value: Any) -> RemoteHandle | None:
    """The RemoteHandle behind a value, whether it is one or merely wraps one.

    Callers hold the public facade types (ElementHandle, JSHandle), not the internal handle, and
    passing one straight back into evaluate is the documented JSHandle argument protocol -- the DOM
    layer relies on it. Unwrapping here rather than type-checking at each call site keeps the public
    and internal representations from diverging.
    """
    if isinstance(value, RemoteHandle):
        return value
    inner = getattr(value, "_handle", None)
    return inner if isinstance(inner, RemoteHandle) else None


# Rebuilds an argument whose element handles were lifted out and passed alongside it. The markers are
# substituted back in the page, so the caller's function receives the structure it wrote.
_REVIVE_ARGUMENT = """
function(spec, ...handles) {
  const revive = (value) => {
    if (value === null || typeof value !== 'object') return value;
    if (value.__skycdpHandle__ !== undefined) return handles[value.__skycdpHandle__];
    if (Array.isArray(value)) return value.map(revive);
    const out = {};
    for (const key of Object.keys(value)) out[key] = revive(value[key]);
    return out;
  };
  return (__USER_FUNCTION__).call(this, revive(spec));
}
"""


def lift_handles(value: Any, handles: list[Any]) -> Any:
    """Replace every element handle nested in ``value`` with a positional marker.

    Production passes handles inside containers -- `get_select_options` calls
    `evaluate("([element]) => ...", arg=[element])` -- and a bare `json.dumps` on that raises, which
    made every `<select>` on every page unreachable. Chrome takes handles only as top-level call
    arguments, so they are lifted out here and put back by `_REVIVE_ARGUMENT` in the page.
    """
    handle = as_remote_handle(value)
    if handle is not None:
        if handle.object_id is None:
            raise CdpError("cannot pass a disposed or value-typed handle as an argument")
        handles.append(handle)
        return {"__skycdpHandle__": len(handles) - 1}
    if isinstance(value, (list, tuple)):
        return [lift_handles(item, handles) for item in value]
    if isinstance(value, dict):
        return {key: lift_handles(item, handles) for key, item in value.items()}
    return value


def serialize_argument(value: Any) -> dict[str, Any]:
    """Encode one Python value as a ``Runtime.CallArgument``."""
    handle = as_remote_handle(value)
    if handle is not None:
        if handle.object_id is None:
            raise CdpError("cannot pass a disposed or value-typed handle as an argument")
        return {"objectId": handle.object_id}
    if value is None:
        return {"value": None}
    if isinstance(value, float):
        if value != value:
            return {"unserializableValue": "NaN"}
        if value == float("inf"):
            return {"unserializableValue": "Infinity"}
        if value == float("-inf"):
            return {"unserializableValue": "-Infinity"}
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise CdpError(f"argument of type {type(value).__name__} is not serializable to the page") from exc
    return {"value": value}


def raise_for_exception(result: dict[str, Any], source: str) -> None:
    details = result.get("exceptionDetails")
    if not details:
        return
    exception = details.get("exception") or {}
    message = exception.get("description") or details.get("text") or "evaluation failed"
    text = f"{message}\n  while evaluating: {source.strip()[:200]}"
    if _is_compile_failure(details):
        raise CdpScriptCompileError(text)
    raise CdpError(text)


def _is_compile_failure(details: dict[str, Any]) -> bool:
    """Whether the source failed to compile, as opposed to running and throwing.

    Measured against Chrome rather than inferred. A wrapper that will not parse reports
    className=SyntaxError with NO stackTrace, because no frame ever existed. A script that ran and
    threw reports a stackTrace -- including `JSON.parse` on bad input, whose error className is
    *also* literally SyntaxError.

    That distinction is the whole point. The retry that disambiguates an expression from a statement
    body used to fire on the substring "SyntaxError" appearing anywhere in the formatted message --
    which included up to 200 characters of the caller's own source. So a script containing the word
    in a comment, or any JSON.parse failure, re-executed in full: every DOM write, click and request
    it had already made before throwing, done twice.
    """
    exception = details.get("exception") or {}
    return exception.get("className") == "SyntaxError" and not details.get("stackTrace")


async def _call(
    session: CdpSession,
    declaration: str,
    params: dict[str, Any],
    source: str,
    by_value: bool,
    timeout: float | None,
) -> Any:
    result = await session.send(
        "Runtime.callFunctionOn", {**params, "functionDeclaration": declaration}, timeout=timeout
    )
    raise_for_exception(result, source)
    remote = result.get("result", {})
    return deserialize(remote) if by_value else RemoteHandle(session, remote)


async def evaluate(
    session: CdpSession,
    source: str,
    arg: Any = None,
    *,
    context_id: int | None = None,
    object_id: str | None = None,
    by_value: bool = True,
    timeout: float | None = None,
) -> Any:
    """Call ``source`` in the page, optionally bound to ``object_id`` as ``this``."""
    # A handle passed on its own stays a plain argument; one nested inside a list or dict is lifted
    # out and revived in the page, because Chrome accepts handles only as top-level arguments.
    nested_handles: list[Any] = []
    lifted = None if arg is None else lift_handles(arg, nested_handles)
    revive_nested = bool(nested_handles) and as_remote_handle(arg) is None

    if arg is None:
        arguments: list[dict[str, Any]] = []
    elif revive_nested:
        arguments = [serialize_argument(lifted)] + [{"objectId": handle.object_id} for handle in nested_handles]
    else:
        arguments = [serialize_argument(arg)]

    params: dict[str, Any] = {
        "arguments": arguments,
        "returnByValue": by_value,
        "awaitPromise": True,
        "userGesture": True,
    }
    if object_id is not None:
        params["objectId"] = object_id
    elif context_id is not None:
        params["executionContextId"] = context_id
    else:
        raise CdpError("evaluate needs either an execution context or an object to bind to")

    already_a_function = looks_like_function(source)
    if revive_nested:
        # The revive wrapper must receive a function, so an expression is wrapped first.
        inner = source if already_a_function else wrap_as_function(source)
        return await _call(
            session,
            _REVIVE_ARGUMENT.replace("__USER_FUNCTION__", inner),
            params,
            source,
            by_value,
            timeout,
        )

    declarations = [wrap_as_function(source)]
    if not already_a_function:
        # An expression and a statement sequence cannot be told apart reliably by inspection -- a
        # trailing semicolon is legal on both -- and guessing wrong on an expression would silently
        # return undefined instead of its value. So the expression form is tried first, keeping value
        # semantics for the common case, and only a syntax error falls back to the body form.
        declarations.append(wrap_as_function_body(source))

    last_error: CdpError | None = None
    for declaration in declarations:
        result = await session.send(
            "Runtime.callFunctionOn", {**params, "functionDeclaration": declaration}, timeout=timeout
        )
        try:
            raise_for_exception(result, source)
        except CdpScriptCompileError as exc:
            # Safe to retry precisely because nothing ran.
            last_error = exc
            continue
        remote = result.get("result", {})
        return deserialize(remote) if by_value else RemoteHandle(session, remote)

    raise last_error or CdpError(f"evaluation failed: {source.strip()[:200]}")
