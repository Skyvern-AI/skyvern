"""Static guard: `sdk_equivalent` snippets must never interpolate a value into hand-written quotes.

Hand-quoted caller text emits code that fails to parse or silently means something else; `!r`
quotes correctly. #13532 fixed the 31 existing sites; this encodes the rule so site #32 cannot
drift in: each sink is rendered into the lines it can emit (values replaced by MARKER) and each
line is tokenized -- a marker inside a STRING token was hand-quoted.

Fails CLOSED: a discovered sink that cannot be fully rendered or tokenized is reported, never
skipped. Only the four shapes real sinks use are modelled (f-string, plain string, local name,
conditional); anything else -- concatenation, `+=` onto the sink, `.format()`, helper calls -- is
unverifiable by policy. Deliberately not discovered (covered elsewhere or unused): values composed
inside another function (`do_find` has a runtime hostile-value test in
test_mcp_semantic_locators.py) and non-literal sink keys.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

from skyvern.cli import mcp_tools

SINK_KEY = "sdk_equivalent"
# Local names that hold the snippet before it reaches the dict. Exact, not a prefix: an
# `sdk_equivalent_note` is not a sink. Alias resolution covers fragments, so this does not need
# to grow for every new `*_str` fragment.
SLOT_NAMES = ("sdk_equivalent", "sdk_eq")
# Stands in for an interpolated value. A valid identifier, so a rendered line still tokenizes.
MARKER = "_SKYVERN_INTERPOLATED_VALUE_"
MCP_TOOLS_DIR = Path(mcp_tools.__file__).parent

# Floor, not a pin. Set above the 24 dict-literal sinks so that losing a whole discovery arm
# (Assign, AnnAssign/AugAssign, Call keyword, or alias resolution) fails loudly rather than
# merely dipping. 57 sinks today.
MIN_EXPECTED_SINKS = 50

# Snippets allowed to interpolate into hand-written quotes, keyed on (file, source text).
# `dx`/`dy` are ints from a literal direction map (browser.py), so they cannot carry a quote or
# a backslash into the JS string. Scoped to one file, and
# `test_each_exemption_matches_exactly_one_site` requires exactly one match, so neither a stale
# entry nor a second copy can quietly inherit this justification.
EXEMPT_SNIPPETS = frozenset(
    {
        ("browser.py", "f'await page.evaluate(\"window.scrollBy({dx}, {dy})\")'"),
    }
)


@dataclass(frozen=True)
class Finding:
    filename: str
    lineno: int
    snippet: str
    unverifiable: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.filename, self.snippet)

    def __str__(self) -> str:
        problem = (
            "is built in a shape this guard cannot verify; build it from an f-string"
            if self.unverifiable
            else "interpolates a value into hand-written quotes; use {x!r}"
        )
        return f"{self.filename}:{self.lineno}: {problem} -> {self.snippet}"


def _is_sink_target(node: ast.expr) -> bool:
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == SINK_KEY
    return isinstance(node, ast.Name) and node.id in SLOT_NAMES


def _sink_values(tree: ast.AST) -> list[ast.expr]:
    """Every expression assigned into an `sdk_equivalent` slot -- only the expression paired
    with the key, not sibling dict entries."""
    values: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            values.extend(
                value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == SINK_KEY
            )
        elif isinstance(node, ast.Call):
            values.extend(kw.value for kw in node.keywords if kw.arg == SINK_KEY)
        elif isinstance(node, ast.Assign) and any(_is_sink_target(t) for t in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if _is_sink_target(node.target):
                values.append(node.value)
        elif isinstance(node, ast.AugAssign) and node.value is not None:
            if _is_sink_target(node.target):
                # += pieces are only meaningful joined: a bare-Name fragment checked in
                # isolation can pair its sibling's quotes invisibly. Model the join itself,
                # which no template shape understands, so the sink reports unverifiable.
                values.append(ast.copy_location(ast.BinOp(left=node.target, op=ast.Add(), right=node.value), node))
    return values


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes belonging to this scope, not descending into nested functions or classes.

    A nested def has its own bindings; folding them in both invents false positives and can
    shadow the outer binding that actually reaches the sink (`workflow.py:1600-1604`).
    """
    out: list[ast.AST] = []
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, _NESTED_SCOPES):
            continue
        out.append(child)
        out.extend(_own_nodes(child))
    return out


def _local_aliases(scope: ast.AST) -> dict[str, list[ast.expr]]:
    """Every value bound to each local name -- all bindings, not the last, so a rebound
    branch cannot hide a regression."""
    aliases: dict[str, list[ast.expr]] = defaultdict(list)
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id].append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            if isinstance(node.target, ast.Name):
                aliases[node.target.id].append(node.value)
    return aliases


Rendered = tuple[list[str], bool]
"""(possible emitted lines, whether every reachable alternative was understood)."""


def _templates(node: ast.expr, aliases: dict[str, list[ast.expr]], seen: frozenset[str] = frozenset()) -> Rendered:
    """Render every line this expression could emit, values replaced by MARKER.

    Only the four shapes the tools use are modelled; anything else renders to nothing and is
    reported unverifiable -- guessing which operand is text fails silently, and one unrenderable
    branch must not be hidden by good siblings (`complete` travels separately from the lines).
    """
    if isinstance(node, ast.Constant):
        return ([node.value], True) if isinstance(node.value, str) else ([MARKER], True)

    if isinstance(node, ast.Name):
        if node.id not in aliases:
            # A parameter or import: a runtime value, not text this guard needs to know.
            return [MARKER], True
        if node.id in seen:
            return [], False
        lines, complete = [], True
        for bound in aliases[node.id]:
            sub, sub_ok = _templates(bound, aliases, seen | {node.id})
            lines.extend(sub)
            complete = complete and sub_ok and bool(sub)
        return lines, complete

    if isinstance(node, ast.JoinedStr):
        lines = [""]
        for part in node.values:
            if isinstance(part, ast.Constant):
                suffixes = [str(part.value)]
            elif part.conversion == -1 and isinstance(part.value, ast.Name) and part.value.id in aliases:
                # A local that renders to known text is a fragment: it contributes its own text,
                # quotes and all (`folder_str`). One that does not is a computed value like
                # `parsed_params` -- opaque, but a value, so a marker is exactly right.
                sub, sub_ok = _templates(part.value, aliases, seen)
                # Keep the rendered alternatives even when one binding is unmodelled: a
                # hand-quoted sibling binding must still be checked. The MARKER stands in
                # for the alternative(s) this cannot render.
                suffixes = sub if (sub and sub_ok) else sub + [MARKER]
            else:
                # Any other `{...}` is a value; `!r` is quoted correctly by Python.
                suffixes = [MARKER]
            lines = [prefix + suffix for prefix in lines for suffix in suffixes]
        return lines, True

    if isinstance(node, ast.IfExp):
        body, body_ok = _templates(node.body, aliases, seen)
        orelse, orelse_ok = _templates(node.orelse, aliases, seen)
        return body + orelse, body_ok and orelse_ok and bool(body) and bool(orelse)

    return [], False


def _marker_inside_string_literal(line: str) -> tuple[bool, bool]:
    """(marker sits inside a string literal, line was tokenizable).

    Tokenizing is what makes escapes, raw strings, triple quotes and comments correct without a
    special case each: a marker inside a STRING token was hand-quoted, one inside a COMMENT
    token is inert, and one standing alone is a properly passed value.
    """
    # Python treats a bare \r as a line break when compiling, but tokenize on 3.11 swallows it
    # into a comment -- so `# note\r<code>` would look inert there and not on 3.13. Normalise
    # first, so every supported interpreter sees the line the compiler would.
    line = line.replace("\r\n", "\n").replace("\r", "\n")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return False, False
    if any(t.type == tokenize.ERRORTOKEN and t.string.strip() for t in tokens):
        # 3.11 reports an unterminated literal as an ERRORTOKEN rather than raising, so
        # accepting it would read a fragment as a clean line.
        return False, False
    string_types = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    return any(t.type in string_types and MARKER in t.string for t in tokens), True


def _scan_source(source: str, filename: str = "<corpus>") -> tuple[int, list[Finding]]:
    """Returns (sinks discovered, findings). Exemptions are NOT applied."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def scope_of(node: ast.AST) -> ast.AST:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = parents.get(current)
        return tree

    findings: list[Finding] = []
    sinks = _sink_values(tree)
    for value in sinks:
        lines, complete = _templates(value, _local_aliases(scope_of(value)))
        tokenizable = all(_marker_inside_string_literal(line)[1] for line in lines)
        if not lines or not complete or not tokenizable:
            findings.append(Finding(filename, value.lineno, ast.unparse(value), unverifiable=True))
            continue
        if any(_marker_inside_string_literal(line)[0] for line in lines):
            findings.append(Finding(filename, value.lineno, ast.unparse(value)))
    return len(sinks), findings


def _scan_mcp_tools() -> tuple[int, list[Finding]]:
    sinks, findings = 0, []
    for path in sorted(MCP_TOOLS_DIR.glob("**/*.py")):
        count, found = _scan_source(path.read_text(), path.name)
        sinks += count
        findings.extend(found)
    return sinks, findings


# ═══════════════════════════════════════════════════
# The rule, against the real tree
# ═══════════════════════════════════════════════════


def test_no_sdk_equivalent_interpolates_into_hand_written_quotes() -> None:
    sinks, findings = _scan_mcp_tools()
    assert sinks >= MIN_EXPECTED_SINKS, f"only {sinks} sinks found — the scanner is broken, not the source"

    flagged = [f for f in findings if f.key not in EXEMPT_SNIPPETS]
    assert not flagged, "sdk_equivalent snippets must be verifiable and never hand-quoted:\n" + "\n".join(
        f"  {f}" for f in flagged
    )


def test_marker_never_occurs_in_scanned_source() -> None:
    """The marker stands for an interpolated value, so real source containing it would read as one."""
    for path in sorted(MCP_TOOLS_DIR.glob("**/*.py")):
        assert MARKER not in path.read_text(), f"{path.name} contains {MARKER}; choose a different MARKER"


def test_each_exemption_matches_exactly_one_site() -> None:
    """An exemption licenses one reviewed site, not a string of text.

    Exactly one, in both directions: zero means the entry is stale; two means a second site
    quietly inherited the first one's justification.
    """
    _, findings = _scan_mcp_tools()
    matches = Counter(f.key for f in findings)
    for entry in sorted(EXEMPT_SNIPPETS):
        assert matches[entry] == 1, (
            f"expected exactly 1 site for exemption {entry}, found {matches[entry]}; delete or re-justify"
        )


# ═══════════════════════════════════════════════════
# Non-vacuity: the guard fires on known-bad source
# ═══════════════════════════════════════════════════

BAD_CORPUS = {
    "quoted whole argument": """
async def tool(intent: str) -> dict:
    return {"sdk_equivalent": f'await page.click("{intent}")'}
""",
    "quoted prefix": """
async def tool(intent: str) -> dict:
    return {"sdk_equivalent": f'await page.click("prefix {intent}")'}
""",
    "adjacent placeholders": """
async def tool(first: str, last: str) -> dict:
    return {"sdk_equivalent": f'await page.click("{first}{last}")'}
""",
    "quotes around a repr conversion": """
async def tool(intent: str) -> dict:
    return {"sdk_equivalent": f'await page.click("{intent!r}")'}
""",
    "fragment alias concatenated into the sink": """
async def tool(folder_id: str) -> dict:
    data = {}
    folder_str = f', folder_id="{folder_id}"'
    data["sdk_equivalent"] = f"await skyvern.create_workflow(definition{folder_str})"
    return data
""",
    "local sdk_eq assign": """
async def tool(key: str) -> dict:
    sdk_eq = f'await page.keyboard.press("{key}")'
    return {"sdk_equivalent": sdk_eq}
""",
    "selector built from a quoted value": """
async def tool(by: str, value: str) -> dict:
    return {"sdk_equivalent": f'page.{by}("{value}")'}
""",
    "fragment rebound on a second branch": """
async def tool(folder_id: str, legacy: bool) -> dict:
    data = {}
    if legacy:
        folder_str = f', folder_id="{folder_id}"'
    else:
        folder_str = f", folder_id={folder_id!r}"
    data["sdk_equivalent"] = f"await skyvern.create_workflow(definition{folder_str})"
    return data
""",
    "single-quoted region": """
async def tool(text: str) -> dict:
    return {"sdk_equivalent": f"await page.fill('{text}')"}
""",
    "conditional branch quoted": """
async def tool(selector: str, intent: str) -> dict:
    return {
        "sdk_equivalent": (
            f"await page.locator({selector!r}).click()" if selector else f'await page.click("{intent}")'
        )
    }
""",
    "explicit concatenation around an f-string": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click("' + f"{selector}" + '")'
    return data
""",
    "direct parameter concatenation": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click("' + selector + '")'
    return data
""",
    "implicit adjacent literal concatenation": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click("' f'{selector}' '")'
    return data
""",
    "str.format": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click("{}")'.format(selector)
    return data
""",
    "percent formatting": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click("%s")' % selector
    return data
""",
    "augmented assignment fragment": """
async def tool(wpid: str, folder_id: str) -> dict:
    sdk_eq = f"await skyvern.run_workflow({wpid!r}"
    if folder_id:
        sdk_eq += f', folder_id="{folder_id}"'
    sdk_eq += ")"
    return {"sdk_equivalent": sdk_eq}
""",
    "unverifiable: split augmented assignment (f-string fragment)": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click('
    data["sdk_equivalent"] += f'"{selector}"'
    data["sdk_equivalent"] += ')'
    return data
""",
    "comment line followed by real code": """
async def tool(intent: str) -> dict:
    data = {}
    data["sdk_equivalent"] = f'# requires an authenticated session\\nawait page.click("{intent}")'
    return data
""",
    "carriage return comment break": """
async def tool(intent: str) -> dict:
    data = {}
    data["sdk_equivalent"] = f'# requires an authenticated session\\rawait page.click("{intent}")'
    return data
""",
    "escaped quotes around the placeholder": """
async def tool(selector: str) -> dict:
    return {"sdk_equivalent": f'await page.evaluate("document.querySelector(\\\\"{selector}\\\\")")'}
""",
    "backslash immediately before the placeholder": """
async def tool(value: str) -> dict:
    return {"sdk_equivalent": f'await page.click("\\\\{value}")'}
""",
    "joined fragment list": """
async def tool(selector: str) -> dict:
    parts = [f'await page.click("{selector}")']
    return {"sdk_equivalent": "\\n".join(parts)}
""",
    "conditional join alternative": """
async def tool(selector: str, legacy: bool) -> dict:
    first = f'await page.click("{selector}")' if legacy else f"await page.click({selector!r})"
    return {"sdk_equivalent": "\\n".join([first])}
""",
    "keyword-form sink": """
async def tool(intent: str) -> dict:
    data = {}
    data.update(sdk_equivalent=f'await page.click("{intent}")')
    return data
""",
    "dict() keyword sink": """
async def tool(intent: str) -> dict:
    return dict(sdk_equivalent=f'await page.click("{intent}")')
""",
    "unverifiable: same-file helper": """
def _snippet(value: str) -> str:
    return f'await page.click("{value}")'

async def tool(intent: str) -> dict:
    return {"sdk_equivalent": _snippet(intent)}
""",
    "unverifiable: concatenation, even with repr": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = "await page.click(" + f"{selector!r}" + ")"
    return data
""",
    "unverifiable: format_map": """
async def tool(intent: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click("{intent}")'.format_map({"intent": intent})
    return data
""",
    "unverifiable: helper on one branch only": """
def _snippet(value: str) -> str:
    return f'await page.click("{value}")'

async def tool(intent: str, legacy: bool) -> dict:
    return {"sdk_equivalent": _snippet(intent) if legacy else f"await page.click({intent!r})"}
""",
    "unverifiable: split augmented assignment with a bare name": """
async def tool(selector: str) -> dict:
    data = {}
    data["sdk_equivalent"] = 'await page.click("'
    data["sdk_equivalent"] += selector
    data["sdk_equivalent"] += '")'
    return data
""",
    "multi-binding fragment alias masks a hand-quoted sibling": """
def _helper(value: str) -> str:
    return value

async def tool(selector: str, legacy: bool) -> dict:
    frag = f'await page.click("{selector}")'
    if legacy:
        frag = _helper(selector)
    return {"sdk_equivalent": f"{frag}"}
""",
}


@pytest.mark.parametrize("case", sorted(BAD_CORPUS))
def test_known_bad_source_is_flagged(case: str) -> None:
    sinks, findings = _scan_source(BAD_CORPUS[case])
    assert sinks, f"no sink discovered in {case!r} — the corpus proves nothing"
    assert findings, f"guard did not flag known-bad case: {case}"


# ═══════════════════════════════════════════════════
# The guard stays quiet on known-good source
# ═══════════════════════════════════════════════════

GOOD_CORPUS = {
    "repr conversion, unquoted": """
async def tool(selector: str, intent: str) -> dict:
    return {"sdk_equivalent": f"await page.click({selector!r}, prompt={intent!r})"}
""",
    "bare int, unquoted": """
async def tool(index: int, time_ms: int) -> dict:
    return {"sdk_equivalent": f"await page.select_option(index={index}, timeout={time_ms})"}
""",
    "code fragment interpolation": """
async def tool(result) -> dict:
    return {"sdk_equivalent": f"page.{result.selector}"}
""",
    "hand-quoted error prose is not a sink": """
async def tool(step, url: str) -> dict:
    if step is None:
        raise ValueError(f"Unknown tool '{step.tool}' -- check the name")
    return {"sdk_equivalent": f"await page.goto({url!r})"}
""",
    "comment-valued sink": """
async def tool(workflow_id: str, version) -> dict:
    version_str = f", version={version}" if version is not None else ""
    return {"sdk_equivalent": f"# No SDK method yet -- GET /api/v1/workflows/{workflow_id}{version_str}"}
""",
    "apostrophe in a trailing comment": """
async def tool(run_id: str) -> dict:
    return {"sdk_equivalent": f"await skyvern.get_run({run_id!r})  # don't use the legacy route"}
""",
    "constant snippet, no placeholders": """
async def tool() -> dict:
    return {"sdk_equivalent": "await page.screenshot(path='screenshot.png')"}
""",
    "repr fragment alias": """
async def tool(folder_id: str) -> dict:
    data = {}
    folder_str = f", folder_id={folder_id!r}" if folder_id is not None else ""
    data["sdk_equivalent"] = f"await skyvern.create_workflow(definition{folder_str})"
    return data
""",
    "computed value interpolated, not a text fragment": """
import json

async def tool(workflow_id: str, params: str, wait: bool) -> dict:
    data = {}
    parsed_params = json.loads(params)
    params_str = f", parameters={parsed_params}" if parsed_params else ""
    wait_str = ", wait_for_completion=True" if wait else ""
    data["sdk_equivalent"] = f"await skyvern.run_workflow(workflow_id={workflow_id!r}{params_str}{wait_str})"
    return data
""",
    "sdk_equivalent_note is not a sink": """
async def tool(intent: str, url: str) -> dict:
    sdk_equivalent_note = f'the operator typed "{intent}" here'
    return {"note": sdk_equivalent_note, "sdk_equivalent": f"await page.goto({url!r})"}
""",
    "nested-scope binding does not reach the outer sink": """
async def tool(folder_id: str) -> dict:
    def _label(name: str) -> str:
        folder_str = f'label("{name}")'
        return folder_str

    folder_str = f", folder_id={folder_id!r}"
    return {"sdk_equivalent": f"await skyvern.create_workflow(definition{folder_str})", "l": _label(folder_id)}
""",
}


@pytest.mark.parametrize("case", sorted(GOOD_CORPUS))
def test_known_good_source_is_not_flagged(case: str) -> None:
    sinks, findings = _scan_source(GOOD_CORPUS[case])
    assert sinks, f"no sink discovered in {case!r} — the control proves nothing"
    assert not findings, f"false positive on known-good case {case}:\n" + "\n".join(f"  {f}" for f in findings)
