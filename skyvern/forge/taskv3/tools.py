"""Raw-browser tools for the Task V3 native harness.

These drive the run's live Playwright page **directly** (raw DOM / CDP) — no calls into
the task/prompt ecosystem (no LLM-backed observe/act/extract). That is the whole point:
the agent perceives via a raw DOM snapshot and acts by selector, so the only LLM in the
loop is the agent's own persistent conversation.

`build_browser_tools(page_provider, ...)` returns `ToolSpec`s that resolve their page via
`page_provider` on every call (not a page bound once), ready to hand to `run_agent_tool_loop`
alongside `make_finish_tool()`.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import structlog
from PIL import Image, ImageDraw

from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX
from skyvern.core.script_generations.fuzzy_matcher import match_option_exact_or_stem
from skyvern.forge.sdk.core.skyvern_context import URL_IN_TEXT, canonical_url, opaque_url_echo_window
from skyvern.forge.taskv3.loop import (
    NAVIGATION_DEAD_END_STATUSES,
    PAGE_UNAVAILABLE_ERROR,
    ToolHandler,
    ToolResult,
    ToolSpec,
)
from skyvern.forge.taskv3.preflight import PREFLIGHT_TOOL_NAMES, preflight_tool_action

if TYPE_CHECKING:
    # opaque_refs imports auth_tools which imports this module, so it can only be referenced for
    # typing; the OpaqueUrlRefs instance is passed in at runtime, never imported here.
    from skyvern.forge.taskv3.opaque_refs import OpaqueUrlRefs

LOG = structlog.get_logger()

# Resolved fresh per tool call rather than a page bound once, so a click that opens a new
# tab/popup is followed on the next call instead of leaving the loop stuck on a stale page.
PageProvider = Callable[[], Awaitable[Any]]

# Cap on the page URL observe() echoes. Callers that register a secret URL for exact-match redaction
# must register this prefix too, or the truncated echo survives the scrub.
OBSERVE_URL_MAX_CHARS = 300

# The exact selector shapes our own enrichment mints: data-tv3 by observe(), data-tv3-menu by the
# click menu probe, data-tv3-act by act-by-mark (written transiently on the look-resolved element
# just before the action and cleared after). Each exists only where we set it, so one that matches
# nothing now cannot reappear without a fresh observe / menu-opening click / look.
_TV3_MARKER_SELECTOR_RE = re.compile(r'^\[data-tv3(?:-menu|-act)?="[^"\\]+"\]$')
# An opaque identifier (a uuid, or a run of 12+ hex digits) does not survive a model's copy: one
# transposed pair sends every later call to a selector that matches nothing. observe hands such a
# selector out under a short alias instead, resolved back before any handler sees it.
_OPAQUE_ID_RUN_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?=[0-9a-f]*[a-f])[0-9a-f]{12,}", re.I
)
# Lenient on purpose: the model may tag-qualify or unquote the handle; the number is what names it.
_ALIAS_SELECTOR_RE = re.compile(r'^\s*[a-z]*\[data-tv3-ref=["\']?(\d+)["\']?\]\s*$', re.I)
# Every identity attribute an emitted selector names (id, name, data-testid — the attributes
# observe's naturalSelector minds), wherever it sits in the compound: each one is masked out of
# results and markup, so the value that triggered the alias never reaches the transcript.
_SELECTOR_ID_COMPONENTS_RE = re.compile(
    r'\[(id|name|data-testid)="((?:[^"\\]|\\.)*)"\]|(#)([^\s#.>+~\[\]()=,:*|^$\'"\\]+)'
)
# Whitespace outside a quoted attribute value is a combinator: only hostAnchored composes selectors
# that way, while a natural `[name="first name"]` keeps its single round trip.
_TV3_QUOTED_VALUE_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_TV3_ANCHORED_SELECTOR_RE = re.compile(r"^\S+\s+\S.*$")


# Relies on observe emitting a combinator only from hostAnchored; every natural selector is one
# compound, with any whitespace inside a quoted value.
def _is_host_anchored_selector(selector: str) -> bool:
    return bool(_TV3_ANCHORED_SELECTOR_RE.match(_TV3_QUOTED_VALUE_RE.sub('""', selector.strip())))


# A plain bare `#<id>`: no combinator/pseudo/attribute part, and no char that would need escaping
# inside `[id="<id>"]` (quotes, backslash, and whitespace are excluded, so the rewrite is always safe).
_BARE_ID_SELECTOR_RE = re.compile(r"""^#([^\s#.>+~\[\]()=,:*|^$'"\\]+)$""")


def _bare_id_is_invalid_css(ident: str) -> bool:
    # Invalid as a bare `#id` when the first char can't start a CSS identifier: a digit, a hyphen
    # followed by a digit, or a lone hyphen. `--`-leading is valid and is deliberately not flagged.
    if not ident:
        return True
    if ident[0].isdigit():
        return True
    return ident[0] == "-" and (len(ident) == 1 or ident[1].isdigit())


def _normalize_selector(selector: str) -> str:
    """Rewrite a bare `#<id>` that is invalid as written (digit/UUID/hyphen-digit leading, common on ATS
    forms) into the equivalent `[id="<id>"]`. `#id` ≡ `[id="id"]` for every id, and a bare id that already
    parses is returned untouched, so a valid selector's target is never altered."""
    match = _BARE_ID_SELECTOR_RE.match(selector.strip())
    if match is None or not _bare_id_is_invalid_css(match.group(1)):
        return selector
    return f'[id="{match.group(1)}"]'


# patchright/playwright report an invalid CSS selector with one of these message markers; matching the
# message (not the exception type) survives the patchright/playwright fork boundary. Version-coupled
# strings: a unit test RED-proofs the exact wording so a library upgrade that reworded them fails loudly.
_INVALID_SELECTOR_MARKERS = ("is not a valid selector", "while parsing selector", "while parsing css selector")


def _invalid_selector_result(selector: Any, exc: Exception) -> ToolResult | None:
    """An actionable error when `exc` is an invalid-CSS-selector parse failure; otherwise None so the
    caller re-raises (timeouts, teardown, and unrelated failures must not be swallowed)."""
    if not any(marker in str(exc) for marker in _INVALID_SELECTOR_MARKERS):
        return None
    return ToolResult.error(
        f"{selector!r} is not a valid CSS selector. Use a selector from the latest observe(), or an "
        '[id="..."] / [name="..."] attribute form (ids that start with a digit are not valid as a bare #id).'
    )


# Every tool that acts on a model-supplied CSS selector. file_upload's naked query_selector was the one
# that crashed on an invalid selector; the guard is shared so all of these inherit the same behavior.
_SELECTOR_GUARD_TOOL_NAMES = frozenset(
    {
        "get_html",
        "click",
        "hover",
        "type",
        "select_option",
        "select_combobox",
        "press_key",
        "scroll",
        "wait",
        "file_upload",
    }
)


def _with_selector_guard(handler: ToolHandler) -> ToolHandler:
    """Shared seam for selector tools: normalize a bare invalid `#id` before the handler resolves it, and
    convert a residual invalid-selector crash into an actionable error instead of a batch-aborting raise."""

    async def wrapped(args: dict[str, Any]) -> ToolResult:
        selector = args.get("selector")
        if isinstance(selector, str):
            args = {**args, "selector": _normalize_selector(selector)}
        try:
            return await handler(args)
        except Exception as exc:
            guarded = _invalid_selector_result(args.get("selector"), exc)
            if guarded is not None:
                return guarded
            raise

    return wrapped


# The observable-state vocabulary a readback compares — the same fields observe reports per element.
# None means "not read"; the classifier treats absence as no-committable-state, never as a value.
_COMMIT_STATE_KEYS = ("value", "checked", "selected", "pressed")


class CommitStatus(str, Enum):
    OK = "ok"  # state moved in the committing direction, read off exactly one element
    DID_NOT_COMMIT = "did_not_commit"  # target readable, and it did NOT commit
    UNVERIFIED = "unverified"  # no readable committable state, or committed but re-resolved to n != 1


def _has_committable_state(state: dict[str, Any] | None) -> bool:
    return isinstance(state, dict) and any(state.get(k) is not None for k in _COMMIT_STATE_KEYS)


def _classify_commit(
    pre: dict[str, Any] | None, post_matches: int, post: dict[str, Any] | None, *, committed_value: bool | None = None
) -> CommitStatus:
    """Classify a value-must-change action from a before/after observable-state readback.

    Ranked fail-closed: a readable did-not-commit is reported whatever the target re-resolved to, because
    an error halts the rest of a batched turn only when it moved the page -- otherwise the field is
    reported unfilled and only its same-selector dependents and any later click or Enter are skipped
    (INV-1 guards the confident ok, not the refusal). A commit read off
    a target that re-resolved to n != 1 is `unverified` (INV-1); no readable committable state is
    `unverified` (INV-2). `committed_value` hands in a caller's own value-dimension truth in place of the
    generic any-field-changed rule.
    """
    if post is None or not _has_committable_state(post):
        return CommitStatus.UNVERIFIED
    if committed_value is None:
        if pre is None or not _has_committable_state(pre):
            return CommitStatus.UNVERIFIED
        committed_value = any(pre.get(k) != post.get(k) for k in _COMMIT_STATE_KEYS)
    if not committed_value:
        return CommitStatus.DID_NOT_COMMIT
    return CommitStatus.OK if post_matches == 1 else CommitStatus.UNVERIFIED


def _match_menu_option(value: str, options: list[dict[str, Any]]) -> int | None:
    """Pick the enumerated menu row (its data-tv3-menu index) whose label matches the wanted value.

    Deterministic and site-agnostic, precision-first. Exact/singular-plural-stem matching (apostrophe
    folding, unique-or-None) is delegated to the shared `match_option_exact_or_stem` so this is not a
    third copy of that logic. Failing that, a UNIQUE FORWARD token-prefix — the observed value is a whole-
    token prefix of a fuller option label ("Decline" → "Decline to self-identify") — is accepted. The
    REVERSE direction is deliberately NOT matched: committing a shorter, more-general option for a longer
    value ("New York" → "New") is a silent wrong success, and on a virtualised window the fuller row may
    simply be unrendered. A value that is only an incidental SUBSTRING of an option is never matched ("No"
    inside "Prefer not to answer"). Ambiguity or no match returns None so the caller hands the options
    back to the model. Uniqueness is only meaningful over the COMPLETE list — the caller must not pass a
    truncated slice.
    """
    rows = [(o.get("n"), str(o.get("text") or "")) for o in options if isinstance(o.get("n"), int)]
    if not value or not rows:
        return None

    # Collapse runs of whitespace before the exact/stem tier — the shared normalizer folds case and
    # apostrophes but not internal spacing.
    def _collapse(s: str) -> str:
        return " ".join(s.split())

    hit = match_option_exact_or_stem(_collapse(value), [_collapse(label) for _, label in rows])
    if hit is not None:
        return rows[hit][0]

    def toks(s: str) -> list[str]:
        # Fold commas and apostrophes so a short value token-prefix-matches a punctuated label ("Yes" →
        # "Yes, I consent"). A slash is left intact so a combined "Yes/No" option is not prefix-matched by
        # "Yes".
        return re.sub(r"[,'’]", " ", s).lower().split()

    want = toks(value)
    if not want:
        return None
    prefixed = [n for n, label in rows if (t := toks(label)) and len(want) < len(t) and t[: len(want)] == want]
    return prefixed[0] if len(prefixed) == 1 else None


# ARIA combobox signals — used by observe() only to add a hint that a field is a typeahead. This is a
# nudge for the model, not load-bearing: type() handles typeaheads behaviorally (see _FIND_SUGGESTION_JS),
# so a field with no ARIA (a plain <input> backed by a custom dropdown) is still handled correctly.
_IS_AUTOCOMPLETE_JS = r"""(el) => {
  if (!el || el.tagName !== 'INPUT') return false;
  const ac = el.getAttribute('aria-autocomplete');
  // Only definitive combobox semantics — NOT bare aria-controls, which a search/filter input pointing
  // at a results table also carries and would over-flag.
  return el.getAttribute('role') === 'combobox' || (ac && ac !== 'none') || el.getAttribute('aria-haspopup') === 'listbox';
}"""

# Function words to ignore when matching the typed value against a candidate's text — otherwise a stray
# "the"/"for"/"and" shared with some page chrome could score a hit. Only content words count. NOTE: not
# "new" — it is load-bearing in proper names ("New York" vs "York"), so it stays a matchable token.
_STOPWORDS_JS = (
    "new Set(['the','and','for','you','our','are','was','add','all','not','but','can','will',"
    "'one','get','job','your','this','that','with','from','has','have','may','use','any','per','via',"
    "'inc','llc','ltd','corp'])"
)

# The roles this engine treats as controls. observe enumerates exactly these (its `q` selector is
# this list expanded) and reports them on each record, so it is the single answer to "is this a
# control?" rather than each probe keeping its own.
_WIDGET_ROLES_JS = (
    "['button','checkbox','radio','combobox','option','menuitem',"
    "'menuitemcheckbox','menuitemradio','listbox','switch','spinbutton','tab']"
)

# The subset of those that can be a ROW in an opened menu. Derived rather than restated so the two
# cannot drift. Excluded: combobox/listbox/spinbutton, which are the control or its container and
# never one of its rows; and tab, which is navigational -- _FIND_SUGGESTION_JS refuses it for the
# same reason, and a probe that called a tab strip a menu of options would invite a wrong move.
_MENU_ROW_ROLES_JS = (
    "new Set(" + _WIDGET_ROLES_JS + ".filter((r) => ['combobox','listbox','spinbutton','tab'].indexOf(r) === -1))"
)

# Counts the VISIBLE menu-row descendants of a node, using the SAME row definition _FIND_MENU_JS
# reports on (its MENU_ROW_ROLES plus native <button>/<a>), so the growth signal and the finder cannot
# disagree about what a row is. Assumes an enclosing `vis(el)` helper. Shared by the two click probes.
_VIS_ROWS_JS = (
    r"""
  const MENU_ROW_ROLES = """
    + _MENU_ROW_ROLES_JS
    + r""";
  const _visRows = (el) => {
    let n = 0;
    try {
      for (const d of el.querySelectorAll('[role], button, a')) {
        const t = d.tagName;
        if ((MENU_ROW_ROLES.has(d.getAttribute('role')) || t === 'BUTTON' || t === 'A') && vis(d)) n++;
      }
    } catch (e) {}
    return n;
  };
"""
)


# Every open shadow root on the page, document first, then each root in depth-first order. Web-component libraries put the
# real input/button inside a shadow root, and `document.querySelector*` does not cross that boundary
# while Playwright's selector engine does — so any probe that must agree with what an action tool
# will resolve has to search these roots too, not just `document`.
_SHADOW_ROOTS_JS = r"""(from_root) => {
  const roots = [];
  const seen = new Set();
  // An explicit stack, not recursion: the traversal is unbounded in depth because Playwright's
  // selector engine is, and a root we stop short of is a root the callers' probes silently miss.
  const stack = [from_root];
  while (stack.length) {
    const root = stack.pop();
    roots.push(root);
    // Per root, not per walk: one root whose querySelectorAll throws would otherwise propagate out
    // of the whole traversal, and every caller reads that as "there are no shadow roots here".
    let all;
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    const kids = [];
    for (const el of all) {
      let sr = null;
      // A form's named getter can make el.shadowRoot a foreign element; nodeType 11 is what makes
      // this a real shadow root rather than an <input name="shadowRoot">.
      try { sr = el.shadowRoot; } catch (e) { continue; }
      if (!sr || sr.nodeType !== 11 || seen.has(sr)) continue;
      seen.add(sr);
      kids.push(sr);
    }
    // Reversed, so popping walks the children in document order and the list stays pre-order DFS.
    for (let k = kids.length - 1; k >= 0; k--) stack.push(kids[k]);
  }
  return roots;
}"""


# Document-plus-shadow equivalents of the DOM query APIs. Every reaction/commit probe below judges
# what a Playwright action just did, and Playwright's selector engine pierces open shadow roots — so
# a document-only probe reports "not an option" / "menu closed" / "did not commit" about elements
# that are visible and were acted on successfully, which is a fabricated answer rather than a gap.
_PIERCED_QUERY_JS = (
    r"""
  const _shadowRoots = """
    + _SHADOW_ROOTS_JS
    + r""";
  // Walked once per invocation: a probe calls these helpers several times, and the roots cannot
  // change between those calls.
  const _rootList = _shadowRoots(document);
  // A throw here propagates, exactly as document.querySelector did: an unparseable selector is not
  // the same fact as "no such element", and callers that gate on the result disarm themselves if the
  // two are conflated.
  const pQS = (sel) => {
    for (const root of _rootList) {
      const el = root.querySelector(sel);
      if (el) return el;
    }
    return null;
  };
  const pQSA = (sel) => {
    const acc = [];
    for (const root of _rootList) {
      for (const el of root.querySelectorAll(sel)) acc.push(el);
    }
    return acc;
  };
  // Node.contains walks the light tree only, so a host does not contain its own shadow content.
  // Every caller below compares elements drawn from the pierced scope, where a cross-tree pair is
  // ordinary. Same hop parentOf() uses: a ShadowRoot has no parentNode, so step to its host.
  const pContains = (a, b) => {
    if (!a || !b) return false;
    for (let n = b; n; n = n.parentNode || n.host || null) if (n === a) return true;
    return false;
  };
  // The pre-snapshot carries element identity across a click or a keystroke, so the carrier has to
  // survive whatever the page did in between -- and the two halves of the page need different ones.
  // In the light DOM an attribute is the only carrier that survives cloneNode/innerHTML, so a
  // container the page re-creates by cloning still reads as "existed before" rather than "appeared
  // in reaction". Inside a shadow root we write nothing at all: stamping there makes a component
  // watching its own root re-render, destroying the marks just made and leaving the finders reading
  // a static list as a reaction. The WeakSet is the best carrier that costs no mutation, at one
  // disclosed price -- a component that re-creates its own content by cloning reads as all-new.
  // Absent (a navigation cleared window) means "no snapshot", never "everything is new".
  const preMark = (el, inShadow) => {
    if (inShadow) window.__tv3_pre.add(el);
    else el.setAttribute('data-tv3-pre', '1');
  };
  // instanceof, not truthiness: a page that pre-defines __tv3_pre as an accessor keeps its own
  // object through preReset, and a `has: () => false` impostor would make every element read as a
  // reaction -- defeating the one distinction this guard exists to draw.
  const preReady = () => window.__tv3_pre instanceof WeakSet;
  const preHas = (el) => {
    try { if (el.hasAttribute('data-tv3-pre')) return true; } catch (e) { /* clobbered getter */ }
    return preReady() && window.__tv3_pre.has(el);
  };
  const preReset = () => {
    pQSA('[data-tv3-pre]').forEach((e) => e.removeAttribute('data-tv3-pre'));
    window.__tv3_pre = new WeakSet();
    focusReset();
  };
  // A third class beside pre-existing and typing-revealed: rows a list rendered in reaction to the
  // FOCUS click. They are options the widget offered, not a filter it applied to the typed value, so
  // a match among them is picked under the open->observe->pick contract, not the typeahead's.
  const focusMark = (el, inShadow) => {
    if (inShadow) { if (window.__tv3_focus instanceof WeakSet) window.__tv3_focus.add(el); }
    else el.setAttribute('data-tv3-focus', '1');
  };
  const focusHas = (el) => {
    try { if (el.hasAttribute('data-tv3-focus')) return true; } catch (e) { /* clobbered getter */ }
    return window.__tv3_focus instanceof WeakSet && window.__tv3_focus.has(el);
  };
  const focusReset = () => {
    pQSA('[data-tv3-focus]').forEach((e) => e.removeAttribute('data-tv3-focus'));
    window.__tv3_focus = new WeakSet();
    window.__tv3_focus_offered = null;
  };
  // 'body *' has no meaning inside a shadow root, whose own descendants are the equivalent scope.
  const pScopeEach = (fn) => {
    for (const root of _rootList) {
      const inShadow = root !== document;
      for (const el of root.querySelectorAll(inShadow ? '*' : 'body *')) fn(el, inShadow);
    }
  };
  const pScopeAll = () => {
    const acc = [];
    pScopeEach((el) => acc.push(el));
    return acc;
  };
"""
)

# Snapshot of everything visible BEFORE typing. The finder ignores anything marked here, so only DOM
# that appeared (or became visible) IN REACTION to typing can be treated as a suggestion — static page
# text that merely happens to share a word with the value (a nearby card, nav item, prior answer) is
# never eligible. This is what makes "detect by the page's reaction" rigorous rather than a claim.
_PRESNAPSHOT_JS = (
    r"""() => {"""
    + _PIERCED_QUERY_JS
    + r"""
  preReset();
  pScopeEach((el, inShadow) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) preMark(el, inShadow);
  });
}"""
)

# Behavioral, site-agnostic suggestion finder. After the caller types a value (with a pre-snapshot taken
# first), this looks for the suggestion list the typeahead rendered IN REACTION: a small, visible,
# leaf-ish row that did NOT exist/show before typing (not `data-tv3-pre`), sits in the dropdown region
# near the field, and shares a CONTENT word with the typed value. It keys off reaction + geometry + token
# overlap — NOT any site's CSS classes, ARIA, or field vocabulary — so a bespoke widget (plain <input> +
# custom dropdown) is handled like an ARIA combobox and it stays durable as sites restyle. Navigational
# controls (links/buttons) are excluded unless explicitly role=option. Among matches it picks the
# INNERMOST row — a candidate that contains another match is a container (its text is the union of all
# rows, so it ties/outranks any single row; clicking it would land on the wrong row), so it's dropped.
# Tags the winner with data-tv3-sugg and returns {text, score}, or null if nothing reacted.
# The ONE definition of "may this row be auto-clicked". Every finder that decides that embeds this
# snippet: two hand-copied predicates drifted once (menuitem listed as both option and nav, so
# `<a role=menuitem href>` read as an option and was clicked off the form). Semantics resolve from the
# closest declaring ANCESTOR, not the reduced leaf (`<a href><span>` reduces to the span).
_ROW_SEMANTICS_JS = r"""
  const OPT_SEL = '[role="option"],[role="menuitemradio"],[role="menuitemcheckbox"],[role="treeitem"],[role="radio"]';
  const NAV_SEL = 'a[href],button,[role="button"],[role="link"],[role="menuitem"],[role="tab"]';
  const isNavRow = (el) => { try { return !el.closest(OPT_SEL) && !!el.closest(NAV_SEL); } catch (e) { return true; } };
  // The nearest ancestor (across shadow boundaries, bounded) matching `sel`, or null. closest() stops
  // at a shadow root, so a row whose text lives inside a component would otherwise have no row.
  const LIST_SEL = '[role="listbox"],[role="menu"],[role="tree"],[role="grid"],[role="radiogroup"],datalist';
  const ancestorMatching = (el, sel) => {
    for (let n = el, hops = 0; n && hops < 32; hops++, n = n.parentNode || n.host || null) {
      if (n.nodeType !== 1) continue;
      let hit = false;
      try { hit = n.matches(sel); } catch (e) { hit = false; }
      if (hit) return n;
    }
    return null;
  };
  // Which of several lists is THIS field's menu: the one it declares (aria-controls/aria-owns,
  // resolved in the field's root and its ancestor roots), else one within the dropdown window under
  // or above the field, else the largest. Size alone would name a sibling list's options as its own.
  const fieldOwnList = (field, lists) => {
    if (!lists.length) return null;
    if (field) {
      const ids = [];
      for (const a of ['aria-controls', 'aria-owns']) {
        const v = field.getAttribute && field.getAttribute(a);
        if (v) for (const id of v.split(/\s+/)) if (id) ids.push(id);
      }
      if (ids.length) {
        for (let root = field.getRootNode(), hops = 0; root && hops < 8; hops++, root = root.host ? root.host.getRootNode() : null) {
          for (const id of ids) {
            let target = null;
            try { target = root.getElementById ? root.getElementById(id) : null; } catch (e) { target = null; }
            if (!target) continue;
            const hit = lists.find((l) => l === target || (target.contains && target.contains(l)) || pContains(target, l));
            if (hit) return hit;
          }
          if (!root.host) break;
        }
      }
      let fr = null;
      try { fr = field.getBoundingClientRect(); } catch (e) { fr = null; }
      if (fr) {
        const near = lists.filter((l) => {
          let r = null;
          try { r = l.getBoundingClientRect(); } catch (e) { return false; }
          if (!r || (r.width === 0 && r.height === 0)) return false;
          return r.top >= fr.top - 400 && r.top <= fr.bottom + 500 && r.right >= fr.left && r.left <= fr.right;
        });
        // A known field with no declared and no nearby list has no menu among these candidates;
        // saying so lets the caller fall back to what was recorded when the field opened.
        if (!near.length) return null;
        lists = near;
      }
    }
    return lists[0];
  };
"""

_FIND_SUGGESTION_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  const STOP = """
    + _STOPWORDS_JS
    + r""";
  const toks = (s) => new Set(String(s).toLowerCase().replace(/[\/,]/g, ' ').split(/\s+/).filter((w) => w.length >= 3 && !STOP.has(w)));
  const want = toks(args.value || '');
  const wantNorm = String(args.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  // A value with no >=3-char word ("No", "UK") has nothing to overlap; it matches a row only by exact text.
  const exact = want.size ? null : String(args.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  pQSA('[data-tv3-sugg]').forEach((e) => e.removeAttribute('data-tv3-sugg'));
  if ((!want.size && !exact) || !preReady()) return null;
  const field = pQS(args.field) || (args.el && args.el.isConnected ? args.el : null);
  // No field means no geometry gate, and without it the scan below is page-wide and will happily
  // tag -- and then click -- a row far from the control the caller typed into. Refuse instead:
  // "cannot judge" and "nothing reacted" are both safe, and a confident wrong click is not.
  if (!field) return null;
  const fr = field.getBoundingClientRect();
  const cands = [];
  for (const el of pScopeAll()) {
    if (preHas(el)) continue;                                         // existed/was visible before typing → not a reaction
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LABEL' || tag === 'FORM') continue;
    // never click something navigational (would leave the form) unless it's explicitly an option
    if (isNavRow(el)) continue;
    if (el.children.length > 8) continue;                             // a suggestion row, not a big container
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > 120) continue;  // visible, row-sized (allows a 2-line row)
    if (fr) {                                                          // in the dropdown region: below, or above if it flipped up
      if (r.top < fr.top - 400 || r.top > fr.bottom + 500) continue;
      if (r.right < fr.left || r.left > fr.right) continue;
    }
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 80) continue;
    let score = 0;
    const norm = txt.replace(/\s+/g, ' ').trim().toLowerCase();
    // A row whose whole text IS the value outranks every partial match ("New York" over "New York
    // City"; "No" over "No, I have not ..."), in both scoring modes.
    const isExact = norm === (exact !== null ? exact : wantNorm);
    if (exact !== null) {
      if (isExact) score = 2;
      else if (norm.split(/\s*[,;:(]\s*|\s+[-\u2013\u2014]\s+/)[0] === exact) score = 1;
    } else {
      const have = toks(txt);
      for (const w of want) if (have.has(w)) score++;
      if (isExact && score > 0) score += 100;
    }
    if (score > 0) cands.push({ el, score, h: r.height, exactRow: isExact });
  }
  if (!cands.length) return null;
  // Drop any candidate that CONTAINS another candidate (a dropdown container over its own rows), then
  // take the highest score, breaking ties toward the smallest (innermost) row.
  const leaves = cands.filter((c) => !cands.some((o) => o.el !== c.el && pContains(c.el, o.el)));
  const pool = leaves.length ? leaves : cands;
  pool.sort((a, b) => b.score - a.score || a.h - b.h);
  const best = pool[0];
  // Two leading-clause matches for a short value ("No, ..." and "No - ...") with no exact row are
  // ambiguous: geometry must not decide an answer, so refuse and let the caller report the options.
  if (exact !== null && !best.exactRow && pool.length > 1 && pool[1].score === best.score) return null;
  // Refuse to tag a multi-row CONTAINER even when it is the only match (its score came from different
  // rows' text combined, and clicking it would land on an arbitrary middle row). A real suggestion is a
  // single row: its visible child elements, if any, sit on one line (inline sub-parts), not stacked rows.
  const childRows = new Set();
  for (const ch of best.el.children) {
    const cr = ch.getBoundingClientRect();
    if (cr.width > 0 && cr.height > 0 && (ch.innerText || '').trim()) childRows.add(Math.round(cr.top));
  }
  if (childRows.size >= 2) return null;
  best.el.setAttribute('data-tv3-sugg', '1');
  // The values a row declares for itself (value/data-* attributes on the row or its option ancestor):
  // a widget that commits a code ("CA" for "California") commits one of these, and nothing else short.
  // Only attributes that NAME a value count; a positional or boolean attribute (data-index="1",
  // data-selected="true") is not a value the widget would commit.
  const declared = [];
  try {
    const VALUE_ATTR = /^(value|data-value|data-val|data-v|data-code|data-key|data-option-value|name|title)$/;
    // An explicit option value may legitimately be "1" or "true"; any other attribute must not
    // contribute one (a positional data-index="1" is not a value the widget commits).
    const EXPLICIT = /^(value|data-value)$/;
    // The option row may sit above a shadow boundary (a component's host); closest() stops there.
    let opt = null;
    for (let n = best.el, hops = 0; n && hops < 32; hops++, n = n.parentNode || n.host || null) {
      if (n.nodeType !== 1) continue;
      let hit = false;
      try { hit = n.matches(OPT_SEL); } catch (e) { hit = false; }
      if (hit) { opt = n; break; }
    }
    for (const node of new Set([best.el, opt || best.el])) {
      for (const a of node.attributes) {
        if (!VALUE_ATTR.test(a.name)) continue;
        const v = String(a.value).trim();
        if (!v || v.length > 40) continue;
        if (!EXPLICIT.test(a.name) && (/^\d+$/.test(v) || /^(true|false|null|undefined)$/i.test(v))) continue;
        declared.push(v);
      }
    }
  } catch (e) { /* attributes unreadable: no declared values */ }
  return { text: (best.el.innerText || '').trim(), score: best.score, fromFocus: focusHas(best.el), declared: declared.slice(0, 12) };
}"""
)

# Second pass after the focus click: everything now visible that is not a list row is marked as
# pre-existing, so focus-revealed help/validation text cannot read as a suggestion while a menu the
# focus opened keeps its rows eligible.
_FOCUS_SNAPSHOT_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  if (!preReady()) return;
  focusReset();
  let field = null;
  try { field = pQS(arg.sel); } catch (e) { field = null; }
  if (!field && arg.el && arg.el.isConnected) field = arg.el;
  // Menu/option semantics, not any list: a plain <ul> that focus revealed is page text, an ARIA
  // list or an option row is the widget's own menu. A list that CONTAINS the field is layout.
  const LIST = LIST_SEL;
  const inOptionList = (el) => {
    for (let n = el; n; n = n.parentNode || n.host || null) {
      if (n.nodeType !== 1) continue;
      let isList = false;
      try { isList = n.matches(LIST) || n.matches(OPT_SEL); } catch (e) { isList = false; }
      if (isList) return !(field && pContains(n, field));
    }
    return false;
  };
  pScopeEach((el, inShadow) => {
    if (preHas(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    if (inOptionList(el)) focusMark(el, inShadow); else preMark(el, inShadow);
  });
  // Record what the list offered NOW: a widget that filters by re-rendering unmounts the rows the
  // typed value does not match, so a later read would find nothing to name on an honest no-match.
  const byList = new Map();
  const seen = new Set();
  pScopeEach((el) => {
    if (!focusHas(el)) return;
    const row = ancestorMatching(el, OPT_SEL);
    if (!row || seen.has(row)) return;
    seen.add(row);
    const txt = (row.textContent || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 80) return;
    const key = ancestorMatching(row, LIST_SEL) || row.parentNode;
    if (!byList.has(key)) byList.set(key, []);
    byList.get(key).push(txt);
  });
  const lists = Array.from(byList.keys()).sort((a, b) => byList.get(b).length - byList.get(a).length);
  const own = fieldOwnList(field, lists);
  const best = own ? byList.get(own) : [];
  // Stamped with the field it was recorded for: a record left by an earlier field is never read
  // for this one, even when this call's own focus pass did not run.
  window.__tv3_focus_offered = { sel: arg.sel, total: best.length, labels: best.slice(0, 15) };
}"""
)

# The labels the widget OFFERED when the field opened — its focus-revealed rows (see _FOCUS_SNAPSHOT_JS),
# read even after the typed filter hid them, so an honest no-match can name the real choices instead of
# leaving the model to guess a label again. Reads only; tags nothing.
_FOCUS_OFFERED_LABELS_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  let field = null;
  try { field = pQS(arg.sel); } catch (e) { field = null; }
  if (!field && arg.el && arg.el.isConnected) field = arg.el;
  // Rows grouped by their list; only the list that offered the most rows is this field's own
  // menu -- a page-wide sweep would attribute another widget's options to this field.
  const byList = new Map();
  const seen = new Set();
  for (const el of pScopeAll()) {
    if (!focusHas(el)) continue;
    if (field && pContains(el, field)) continue;
    const row = ancestorMatching(el, OPT_SEL);
    if (!row || seen.has(row)) continue;
    seen.add(row);
    const txt = (row.textContent || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 80) continue;
    const list = ancestorMatching(row, LIST_SEL) || row.parentNode;
    if (!byList.has(list)) byList.set(list, []);
    byList.get(list).push(txt);
  }
  const lists = Array.from(byList.keys()).sort((a, b) => byList.get(b).length - byList.get(a).length);
  const own = fieldOwnList(field, lists);
  const best = own ? byList.get(own) : [];
  if (!best.length) {
    const rec = window.__tv3_focus_offered;
    if (rec && rec.sel === arg.sel && Array.isArray(rec.labels) && rec.labels.length) return { total: rec.total || rec.labels.length, labels: rec.labels.slice(0, 15) };
  }
  return { total: best.length, labels: best.slice(0, 15) };
}"""
)

# Classifies the currently-open list's rows as EXPANDABLE CATEGORIES rather than leaves — for the
# no-match error path only, when _FIND_SUGGESTION_JS found nothing (a drilldown menu's leaves are
# often hidden a level down until their category is clicked, so text-matching never sees them). Unlike
# the reaction-gated finders above, category rows commonly PRE-EXIST the typing, so this does not gate
# on preHas/preReady — only on geometry (the same field-rect window) and a positive expand signal:
# aria-haspopup, aria-expanded, or (for a clickable option/menuitem/treeitem row) >=2 nested option
# rows. Tags qualifying rows data-tv3-menu="1..N" so the model can click one via the menu-click channel;
# leaves data-tv3-menu untouched when it tags nothing, so it never clobbers a prior menu's tags.
_FIND_CATEGORIES_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const field = pQS(args.field);
  if (!field) return null;
  const fr = field.getBoundingClientRect();
  const ROW_ROLES = new Set(['option', 'menuitem', 'treeitem', 'row', 'group']);
  const CHILD_ROLES = new Set(['option', 'menuitem', 'treeitem']);
  const cats = [];
  for (const el of pScopeAll()) {
    if (el === field || cats.length >= 8) continue;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LABEL' || tag === 'FORM') continue;
    const role = el.getAttribute('role');
    if (!ROW_ROLES.has(role)) continue;
    // Never offer a navigational row: clicking an <a href>/<button> leaves the form (mirrors the same
    // exclusion in _FIND_SUGGESTION_JS). An already-open row (aria-expanded="true") would collapse on
    // click, not reveal, so it is not a category worth clicking either.
    if ((tag === 'A' && el.hasAttribute('href')) || tag === 'BUTTON') continue;
    if (el.getAttribute('aria-expanded') === 'true') continue;
    if (el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > 120) continue;
    if (r.top < fr.top - 400 || r.top > fr.bottom + 500) continue;
    if (r.right < fr.left || r.left > fr.right) continue;
    const hp = el.getAttribute('aria-haspopup');
    const hasPopup = hp !== null && hp !== 'false';
    // Only a COLLAPSED row is worth clicking to reveal options; aria-expanded="true" is already open,
    // so clicking it would toggle it closed.
    const hasExpanded = el.getAttribute('aria-expanded') === 'false';
    // A container role (group/row) is a static section wrapper unless it carries an explicit expand
    // affordance; only an option/menuitem/treeitem row may qualify on nested-option count alone, else
    // a grouped listbox (role=group over already-visible option leaves) is misread as a drilldown.
    let childCount = 0;
    if (CHILD_ROLES.has(role)) {
      for (const kid of el.querySelectorAll('[role]')) {
        if (CHILD_ROLES.has(kid.getAttribute('role'))) childCount++;
      }
    }
    if (!hasPopup && !hasExpanded && childCount < 2) continue;
    const label = el.getAttribute('aria-label') || (el.innerText || '').trim().split('\n')[0];
    const text = label.trim().slice(0, 80);
    if (!text) continue;
    cats.push({ el, text });
  }
  if (!cats.length) return null;
  pQSA('[data-tv3-menu]').forEach((e) => e.removeAttribute('data-tv3-menu'));
  cats.forEach((c, i) => c.el.setAttribute('data-tv3-menu', String(i + 1)));
  return { count: cats.length, categories: cats.map((c, i) => ({ n: i + 1, text: c.text })) };
}"""
)

# Read back whether the field committed a real selection CAUSED BY the suggestion click — not just that
# the field holds text (the caller typed into it before clicking, so a bare value check would call any
# no-op click a success). Committed iff the visible value (a) reflects the row we clicked (shares a word
# with the chosen suggestion, or the typed value) and (b) shows the click took effect — it changed from
# the raw typed text OR the suggestion list closed. Failing that, a hidden input in the nearest
# div/li/fieldset (never the whole <form>) whose value overlaps. Otherwise "" — nothing committed.
_VERIFY_COMMIT_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const toks = (s) => new Set(String(s).toLowerCase().replace(/[\/,]/g, ' ').split(/\s+/).filter((w) => w.length >= 3));
  const overlaps = (a, b) => { const B = toks(b); for (const w of toks(a)) if (B.has(w)) return true; return false; };
  // Token overlap drops words shorter than 3 chars, so a short committed label ("No", "MA") has no token
  // to overlap. Case/space-normalized EXACT equality rescues it — a hidden value that IS the chosen label
  // is a real commit, and an exact match cannot be an incidental partial overlap.
  const eqi = (a, b) => !!a && !!b && a.replace(/\s+/g, ' ').trim().toLowerCase() === b.replace(/\s+/g, ' ').trim().toLowerCase();
  const el = pQS(args.field) || (args.el && args.el.isConnected ? args.el : null);
  // null (not '') when there is nothing to read: the caller must tell "read it, no commit" from
  // "could not read it", and a later second probe would answer about a different instant.
  if (!el) return null;
  const typed = String(args.typed || '').trim();
  const chosen = String(args.chosen || '').trim() || typed;
  const cur = (el.value || '').trim();
  const tagged = pQS('[data-tv3-sugg]');
  // The open->observe->pick path tags no suggestion, so `listClosed` would be unconditionally true and
  // defeat the change check — the caller sets noSuggestionList so the el.value branch rests on an actual
  // change from the pre-click value (passed as `typed`), never leftover text the tool itself put there.
  const listClosed = args.noSuggestionList ? false : (!tagged || tagged.getBoundingClientRect().height === 0);
  // A short normalized value ("New York" -> "NY", "United States" -> "US") has no >=3-char token to
  // overlap, so accept it on causality alone (it changed / the list closed). Longer values must still
  // relate to the chosen suggestion so an unrelated change can't read as a successful commit.
  if (args.noSuggestionList) {
    // On the open->observe->pick path the CHOSEN label is known, so require the new value to BE it
    // (short: exact; long: token overlap), not merely "some short value changed" — a dead row that
    // resets the input to "N/A" changes cur but does not commit the chosen option.
    const declared = Array.isArray(args.chosenValues) ? args.chosenValues : [];
    if (cur && cur !== typed && (eqi(cur, chosen) || overlaps(cur, chosen) || declared.some((d) => eqi(d, cur)))) return cur;
  } else if (cur && (cur !== typed || listClosed) && (toks(cur).size === 0 || overlaps(cur, chosen) || overlaps(cur, typed))) {
    return cur;
  }
  const cont = el.closest('div,li,fieldset');
  if (cont) {
    // On the pick path the caller snapshots the hidden values BEFORE the click: a value that merely
    // shares a token with the chosen label ("People Operations" left over while "Sales Operations" was
    // clicked dead) is the stale state, not a commit — only an exact chosen label or a CHANGED value counts.
    const preHidden = new Set(Array.isArray(args.preHidden) ? args.preHidden : []);
    for (const h of cont.querySelectorAll('input[type=hidden]')) {
      const v = (h.value || '').trim();
      if (!v) continue;
      if (eqi(v, chosen)) return v;
      if (args.noSuggestionList) {
        const declaredHidden = Array.isArray(args.chosenValues) ? args.chosenValues : [];
        if ((overlaps(v, chosen) || declaredHidden.some((d) => eqi(d, v))) && !preHidden.has(v)) return v;
        continue;
      }
      if (overlaps(v, chosen) || eqi(v, typed) || overlaps(v, typed)) return v;
    }
  }
  // React-Select / styled combobox: on commit the value moves OUT of the filter input into a
  // single-value node or token beside it and the input is cleared, so the reads above miss it. Read
  // that committed surface — but only once the widget reports closed (aria-expanded=false), so a
  // still-open list reflecting the typed filter can't read as a commitment, and scoped to the nearest
  // ancestor holding exactly this one combobox trigger, so a sibling field showing the same label
  // can't pre-confirm this one. Mirrors v1's _CUSTOM_SELECT_COMMITTED_STATE_JS.
  const expandedEl = el.getAttribute('aria-expanded') != null ? el : el.closest('[aria-expanded]');
  const expanded = expandedEl ? expandedEl.getAttribute('aria-expanded') : null;
  if (expanded === 'false') {
    const TRIGGER = "[role=combobox],[aria-haspopup=listbox],[aria-haspopup=menu],button[aria-expanded],input[role=combobox],select";
    const SURFACE = "[class*='single-value'],[class*='singleValue'],[class*='multi-value__label'],[role=option][aria-selected=true],.chip,.pill,[class*='token']";
    let scope = null;
    for (let anc = el.parentElement, hops = 1; anc && hops <= 4; hops++, anc = anc.parentElement) {
      const trig = anc.querySelectorAll(TRIGGER);
      if (anc.querySelector(SURFACE) && trig.length === 1 && (trig[0] === el || el.contains(trig[0]))) {
        scope = anc;
        break;
      }
    }
    if (scope) {
      // EXACT normalized match, not token overlap (mirrors v1's matchesExpected): a stale single-value
      // or a leftover multi-select token that merely SHARES a word with the chosen label would read as a
      // false commit when the real selection silently failed. A committed surface normally holds exactly
      // the chosen label (or, for a multi-value chip, it among comma-separated parts).
      const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().toLowerCase();
      const want = norm(chosen) || norm(typed);
      const surfaceMatches = (raw) => {
        const n = norm(raw);
        if (!n || !want) return false;
        return n === want || n.split(',').map((p) => p.trim()).includes(want);
      };
      for (const s of scope.querySelectorAll(SURFACE)) {
        // textContent OR the accessible name (aria-label): a chip/single-value can carry the committed
        // label only in aria-label with no text node — v1 reads both, so this must too.
        const t = (s.textContent || '').trim();
        if (surfaceMatches(t)) return t;
        const al = (s.getAttribute('aria-label') || '').trim();
        if (surfaceMatches(al)) return al;
      }
    }
  }
  return '';
}"""
)

# The hidden-input values _VERIFY_COMMIT_JS would read for this field, in the same div/li/fieldset scope.
_HIDDEN_VALUES_JS = (
    "el => { const c = el.closest('div,li,fieldset'); if (!c) return []; "
    "return Array.from(c.querySelectorAll('input[type=hidden]')).map((h) => (h.value || '').trim()).filter(Boolean); }"
)

# Why a reaction probe's answer about this selector may not carry a claim. `unprobeable` -- in-page
# CSS cannot even parse it (Playwright syntax like `css=`, `>> nth=`, `:visible`, `text=`).
# `component` -- it lives inside a component. The probes DO pierce open shadow roots, so this no
# longer means they are blind to the element itself; it means the widget's own list may still render
# where a pierced query does not reach -- a portal mounted elsewhere in the page, or a closed root,
# which is undetectable from script. So a missing suggestion list there is still not proof the field
# is unfilled, and the softened reading is kept deliberately rather than for lack of reach.
# `` -- neither applies, so any failure to find the element is a fact about the page, not about us.
# Both readings happen in ONE evaluation: as two round trips, an ordinary re-render landing between
# them lets each describe a different moment.
_PROBE_REACH_JS = (
    r"""(arg) => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  try {
    if (document.querySelector(arg.sel)) return '';
  } catch (e) { return 'unprobeable'; }
  try {
    for (const root of _roots(document)) { if (root.querySelector(arg.sel)) return 'component'; }
  } catch (e) { return ''; }
  return (arg.el && arg.el.isConnected ? arg.el : null) ? 'component' : '';
}"""
)

# Whether a selector currently resolves. `true` on a broken selector: existence is only ever used to
# soften/enrich behavior, so an unparseable selector must take the normal (unenriched) path.
# Pierces open shadow roots because the caller compares against what `page.click` would resolve, and
# a document-only probe reports "gone" for every element a web component renders.
_SELECTOR_EXISTS_JS = (
    r"""(arg) => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  try {
    let found = null;
    for (const root of _roots(document)) { found = root.querySelector(arg.sel); if (found) break; }
    // The executor can resolve a selector no single root can match; that is existence too.
    if (!found) found = (arg.el && arg.el.isConnected ? arg.el : null);
    return !!found;
  } catch (e) { return true; }
}"""
)

# How many distinct elements a minted marker resolves to across open shadow roots: a re-render that
# CLONES the marked node copies the attribute, and a non-strict click would land on the first match.
# A root whose query throws is skipped without erasing duplicates already proven; with nothing proven
# it reads as 1, so an unparseable marker takes the normal path, like _SELECTOR_EXISTS_JS.
_MARKER_MATCH_COUNT_JS = (
    r"""(arg) => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  const matches = new Set();
  let unreadable = false;
  try {
    for (const root of _roots(document)) {
      try { for (const e of root.querySelectorAll(arg.sel)) matches.add(e); } catch (e) { unreadable = true; }
    }
    if (matches.size === 0 && arg.el && arg.el.isConnected) matches.add(arg.el);
  } catch (e) { unreadable = true; }
  if (matches.size > 1) return matches.size;
  return unreadable ? 1 : matches.size;
}"""
)

# The single discriminator observe and both act paths share: the element that visibly stands in for a
# control the page renders at zero size. Nothing rendered means a collapsed section, a closed modal or
# an inactive step. Kept as one fragment because three page.evaluate payloads cannot be kept in sync by
# hand, and the whole point is that perception and action agree on what counts as a styled proxy.
# The text the page still shows one control as in flight with, or null. Applied to an element handle
# the caller resolved, never to a selector -- resolution belongs to Playwright's engine, which is what
# the action tools act through.
PENDING_MARKER_JS = (
    "(el) => { let ctl = el;"
    " try { ctl = Element.prototype.closest.call(el,"
    "   'button,input[type=submit],input[type=button],input[type=image],[role=button]') || el }"
    " catch(e) {}"
    # A selector can name a wrapper (a <form>, or a clickable <div> the page put the handler on)
    # rather than the control. Descend when the element is not itself a control and holds exactly
    # one: a card holds several, or none.
    " if (ctl === el && el.tagName !== 'BUTTON' && el.tagName !== 'INPUT') {"
    "   let inners = []; try { inners = el.querySelectorAll("
    "     'button,input[type=submit],input[type=button],input[type=image],[role=button]') } catch(e) {}"
    "   if (inners.length === 1) ctl = inners[0]; }"
    " const isButtonInput = ctl.tagName === 'INPUT'"
    "   && /^(submit|button|image)$/i.test(ctl.getAttribute('type') || '');"
    # .value on a text input is the model's own typed text, not a label the page rendered.
    # The control's OWN label, not its whole subtree: innerText spans every descendant and `closest`
    # can climb to a card-sized [role=button], so a status row ("Processing - Order 4821 - $32.10")
    # would read as an in-flight submit. The subtree is a fallback only for a real <button>/<input>
    # simple enough to be one (a spinner plus a label) -- [role=button] is a claim the page makes,
    # and it is what cards are built from.
    " let own = '';"
    " for (const n of ctl.childNodes) { if (n.nodeType === 3) own += n.nodeValue; }"
    " const isElementControl = ctl.tagName === 'BUTTON' || ctl.tagName === 'INPUT';"
    " let inner = '';"
    " try { inner = isElementControl ? (ctl.innerText || '') : '' } catch(e) {}"
    " const t = String(own.trim() || inner || (isButtonInput ? ctl.value : '') || '').trim().slice(0, 60);"
    " if (!/^(submitting|processing|sending|uploading)\\b/i.test(t)) return null;"
    " const r = ctl.getBoundingClientRect();"
    " if (r.width < 8 || r.height < 8) return null;"
    " let cs; try { cs = getComputedStyle(ctl) } catch(e) { return null }"
    " if (cs.clip && cs.clip !== 'auto') return null;"
    " if (cs.clipPath && cs.clipPath !== 'none') return null;"
    " let shown;"
    " try { shown = ctl.checkVisibility({opacityProperty: true, visibilityProperty: true,"
    "   contentVisibilityAuto: true}) }"
    " catch(e) { shown = !(cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.05) }"
    " if (!shown) return null;"
    " return ctl.getAttribute('aria-busy') === 'true' ? t + ' (aria-busy)' : t; }"
)


async def pending_marker(page: Any, selector: str) -> str | None:
    """The text the page still shows `selector`'s control as in flight with, or None.

    Resolution goes through Playwright's engine — the one the action tools act through — so the probe
    judges the element the run acted on. A second, hand-rolled resolver would be a second source of
    truth: shadow-piercing CSS, host-anchored selectors straddling a shadow boundary, and the
    text=/xpath forms all resolve here and none of them resolve through an in-page querySelector walk.
    Fails open: an unresolvable control reports nothing, and nothing is not evidence of pending."""
    try:
        handle = await page.query_selector(selector)
    except Exception:
        LOG.warning("taskv3 pending-marker probe could not resolve the control", selector=selector, exc_info=True)
        return None
    if handle is None:
        # Not an error: the control being gone is the ordinary shape of a submission that landed.
        return None
    try:
        marker: str | None = await handle.evaluate(PENDING_MARKER_JS)
    except Exception:
        LOG.warning("taskv3 pending-marker probe failed on the control", selector=selector, exc_info=True)
        return None
    return marker


# Cap on marks a single look draws: more than this yields an unreadable set-of-marks image and a legend
# the model cannot map back. In DOM order (document first), so a truncated look still numbers the
# top-of-page controls the model most likely wants.
_LOOK_MAX_MARKS = 60

# Hard per-run ceiling on look() calls. look bills one image per call and is not an action step, so
# without a cap the only bound is max_turns (~1 image/turn) — a metered vision cost the operator's
# constraint forbids. A last-resort tool rarely needs more than a handful; past this it returns an
# error pointing back at the text tools rather than adding another image.
_LOOK_MAX_PER_RUN = 20

# Enumerate the same interactive controls observe() does, across open shadow roots, keeping only the
# ones with pixels on screen (a visible box intersecting the viewport). Tag each with a transient
# data-tv3-look index and return its CSS-px rect so the marks can be drawn on the screenshot. The
# index is cleared right after handles are grabbed — it exists only to pair a handle to a rect.
_LOOK_ENUM_JS = (
    r"""(() => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  const q = 'input,textarea,select,button,a[href],[role=button],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=listbox],[role=switch],[role=spinbutton],[role=tab],[contenteditable=true]';
  const vw = window.innerWidth, vh = window.innerHeight;
  const seen = new Set();
  const out = [];
  let n = 0;
  let truncated = false;
  for (const root of _roots(document)) {
    let els;
    try { els = root.querySelectorAll(q); } catch (e) { continue; }
    for (const el of els) {
      if (seen.has(el)) continue;
      seen.add(el);
      let r;
      try { r = el.getBoundingClientRect(); } catch (e) { continue; }
      if (r.width < 4 || r.height < 4) continue;
      if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) continue;
      let shown = true;
      try {
        shown = el.checkVisibility
          ? el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true})
          : true;
      } catch (e) {}
      if (!shown) continue;
      if (n >= """
    + str(_LOOK_MAX_MARKS)
    + r""") { truncated = true; break; }
      n += 1;
      try { el.setAttribute('data-tv3-look', String(n)); } catch (e) { n -= 1; continue; }
      let label = '';
      let placeholder = '';
      try {
        const t = (el.getAttribute('type') || '').toLowerCase();
        // .value is a useful label for a text/submit field but is 'on'/junk for a checkbox or radio —
        // and is the SECRET for a password field, which (like observe) must never enter the legend.
        const valuable = el.tagName === 'INPUT' && !['checkbox', 'radio', 'password'].includes(t) ? (el.value || '') : '';
        // Cap generously (not the 80-char display width): the value is masked for payload-minted
        // signed URLs Python-side, which needs the WHOLE URL to match by provenance before the label
        // is truncated for display. A tighter cap here would truncate the URL past recognition.
        // As in observe: the associated <label> outranks the placeholder, which is a template hint,
        // not a name -- and travels separately when it differs, since a format hint makes the value typeable.
        let named = '';
        if (el.labels) { for (const l of el.labels) { named = (l.innerText || '').trim(); if (named) break; } }
        placeholder = (el.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ').slice(0, 2000);
        label = (el.getAttribute('aria-label') || named || placeholder || valuable
          || el.innerText || el.getAttribute('title') || el.getAttribute('name') || '')
          .trim().replace(/\s+/g, ' ').slice(0, 2000);
      } catch (e) {}
      const rec = { n, x: r.left, y: r.top, w: r.width, h: r.height, tag: (el.tagName || '').toLowerCase(), label };
      if (placeholder && placeholder !== label) rec.placeholder = placeholder;
      out.push(rec);
    }
    if (truncated) break;
  }
  return { vw, vh, truncated, elements: out };
})()"""
)

# Write the transient act-by-mark attribute on an element handle the caller already resolved
# (Playwright's engine, which pierces open shadow). Returns whether the node is still connected; a
# detached handle errors rather than re-resolving by a stale coordinate.
_LOOK_TAG_HANDLE_JS = "(el, n) => { try { el.setAttribute('data-tv3-act', String(n)); } catch (e) { return false; } return el.isConnected; }"


def _annotate_screenshot(png_bytes: bytes, elements: list[dict[str, Any]], vw: int, *, max_width: int = 1024) -> bytes:
    """Draw a numbered set-of-marks box over each element on the viewport screenshot, server-side.

    Boxes are drawn in the SAME numbering the legend and act-by-mark use. The screenshot is in device
    pixels and the rects in CSS pixels; downscaling to `max_width` first and mapping CSS px through the
    single factor `final_width / vw` folds devicePixelRatio and the downscale into one transform, so
    the boxes land regardless of the display's pixel ratio."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((max_width, max(1, round(img.height * scale))))
    factor = (img.width / vw) if vw else 1.0
    draw = ImageDraw.Draw(img)
    for e in elements:
        x0 = e["x"] * factor
        y0 = e["y"] * factor
        x1 = (e["x"] + e["w"]) * factor
        y1 = (e["y"] + e["h"]) * factor
        draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=2)
        label = str(e["n"])
        tw = 6 * len(label) + 4
        # Sit the label tag just above the box, but drop it just inside the top edge when the box is
        # flush against the top of the viewport so a top-row mark's number stays legible.
        ly = y0 - 12 if y0 >= 12 else y0
        draw.rectangle([x0, ly, x0 + tw, ly + 12], fill=(255, 0, 0))
        draw.text((x0 + 2, ly + 1), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_VISIBLE_PROXY_JS = r"""(el) => {
  let named = el.labels && el.labels[0];
  if (!named) {
    const lbId = el.getAttribute('aria-labelledby');
    // An IDREF resolves inside the element's OWN tree, so a control in a shadow root must be looked
    // up there -- document.getElementById cannot see it. Read through the prototype like every other
    // root check here; an element whose root cannot hold ids simply has no name.
    let root = null;
    try { root = Node.prototype.getRootNode.call(el); } catch (e) { root = null; }
    named = lbId && root && root.getElementById ? root.getElementById(String(lbId).trim().split(/\s+/)[0]) : null;
  }
  const r = named ? named.getBoundingClientRect() : null;
  return r && r.width > 0 && r.height > 0 ? named : null;
}"""

# The executor's selector engine pierces open shadow roots, and observe now derives selectors from
# every one of them, so a probe resolving a selector against the document alone silently declines to
# act on a control it has just listed. Roots are visited in walk order, the order that engine matches
# in, and gathered once per payload so a probe never walks the page twice.
_ROOT_QUERY_JS = (
    r"""(() => {
  const _roots = """
    + _SHADOW_ROOTS_JS
    + r""";
  const roots = _roots(document);
  return {
    find: (sel) => {
      for (const root of roots) {
        let f = null;
        // A throw is the ROOT's, not the selector's -- it was already parsed by an earlier root.
        try { f = root.querySelector(sel); } catch (e) { continue; }
        if (f) return f;
      }
      return null;
    },
    all: (sel) => {
      const out = [];
      for (const root of roots) {
        try { for (const e of root.querySelectorAll(sel)) out.push(e); } catch (e) { /* this root only */ }
      }
      return out;
    },
  };
})()"""
)


_IN_COMPONENT_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const el = _q.find(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  try { return Node.prototype.getRootNode.call(el) !== document; } catch (e) { return false; }
}"""
)

# A component that mirrors its own id onto the native control inside its shadow root makes a bare
# `#id` match the HOST first (document is the first root, and Playwright picks the first match too).
# observe names such a control by tag (`input[id="…"]`), but the model routinely drops the tag. When the
# selector's first match is a non-control host whose shadow tree holds exactly one form control that
# the same selector also matches, name that control the way observe would have -- the host is what a
# person sees, the control is what accepts the value.
_MIRRORED_HOST_CONTROL_JS = (
    r"""(sel) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const CONTROL = 'INPUT,TEXTAREA,SELECT,BUTTON';
  const WIDGET_ROLE = /^(textbox|searchbox|combobox|listbox|button|checkbox|radio|switch|spinbutton|slider)$/i;
  const isControl = (e) => e.matches(CONTROL) || e.isContentEditable || WIDGET_ROLE.test(e.getAttribute('role') || '');
  let first = null;
  try { first = _q.find(sel); } catch (e) { return null; }
  if (!first) return null;
  let root = null;
  try { root = first.shadowRoot; } catch (e) { return null; }
  // A host that only DECLARES a widget role still delegates to the control inside it.
  if (!root || root.nodeType !== 11 || first.matches(CONTROL) || first.isContentEditable) return null;
  const inside = (e) => { for (let n = e; n; n = n.parentNode || n.host || null) if (n === first) return true; return false; };
  const controls = _q.all(sel).filter((e) => e !== first && inside(e) && isControl(e));
  if (controls.length !== 1) return null;
  const c = controls[0];
  if (!c.id || String(c.id) !== String(first.id)) return null;
  // The same screen observe applies to an id it hands out: this string becomes the selector every
  // later message names, so a forgeable character or an unbounded length must not pass through.
  const FORGEABLE = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/;
  const id = String(c.id);
  if (id.length > 200 || FORGEABLE.test(id)) return null;
  const tag = c.tagName.toLowerCase();
  if (!/^[a-z][a-z0-9-]*$/.test(tag)) return null;
  const named = tag + '[id="' + id.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
  return _q.all(named).length === 1 && _q.find(named) === c ? named : null;
}"""
)


# Every other probe here asks whether a control is VISIBLE. This one asks whether it is REACHABLE,
# which is a different question and the only one that separates these two cases: Playwright reports a
# covered input as "visible, enabled, stable" and then fails the separate hit-target check, retrying
# until the timeout.
_TYPE_TARGET_PROBE_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  // A host-anchored selector's two halves straddle a shadow boundary, so no single root can match it
  // and a per-root lookup finds nothing -- which would read as "no field here" and skip the check on
  // exactly the controls that addressing made reachable. The executor resolves it; take its element.
  const el = _q.find(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return { exists: false };
  const out = { exists: true, disabled: !!el.disabled, readOnly: !!el.readOnly };
  let r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return out;
  // elementFromPoint answers about the VIEWPORT, so a field below the fold returns null and would
  // read as unoccluded -- which is most fields on a real form. Playwright scrolls before it clicks,
  // so scrolling here asks about the same layout the click is about to meet.
  const inView = r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth;
  if (!inView) {
    // 'instant' matters: scrollIntoView inherits CSS scroll-behavior, and a page with smooth
    // scrolling animates over hundreds of ms while the rect below is read synchronously -- the
    // element is still off-screen, elementFromPoint returns null, and the probe reports nothing.
    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) { /* keep the rect */ }
    r = el.getBoundingClientRect();
  }
  let top = null;
  try { top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); } catch (e) { return out; }
  // document.elementFromPoint stops at the outermost host, so a control inside a component reads as
  // covered by that host -- and a form-sized outer component is too big to pass as a skin. Descend
  // through each hit host's own root to the composed hit target, the element a real click lands on.
  // `hit` stays the light-DOM element for NAMING below: the model needs a handle it can act on, and
  // a host is that handle when the layer lives inside a component.
  const hit = top;
  for (let hops = 0; top && hops < 32; hops++) {
    // A page can make shadowRoot a throwing getter; a throw here would escape the probe and read as
    // "not occluded", so it ends the descent instead.
    let root = null;
    try { root = top.shadowRoot; } catch (e) { break; }
    if (!root || root.nodeType !== 11) break;
    let inner = null;
    try { inner = root.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); } catch (e) { break; }
    if (!inner || inner === top) break;
    top = inner;
  }
  // The walk must hop ShadowRoot -> host, because Node.contains stays in the light tree and would
  // read every component control as covered by its own host.
  // Composed-tree containment: a slotted node renders inside the component's shadow (its
  // assignedSlot), so a hit on a control's slotted label is a hit on the control, not a cover.
  const related = (a, b) => {
    for (let n = b, hops = 0; n && hops < 256; hops++, n = n.assignedSlot || n.parentNode || n.host || null) if (n === a) return true;
    return false;
  };
  const domRelated = (a, b) => {
    for (let n = b, hops = 0; n && hops < 256; hops++, n = n.parentNode || n.host || null) if (n === a) return true;
    return false;
  };
  if (!top || top === el) return out;
  if (related(el, top)) {
    // Reachable only through slot assignment: the driver's DOM-containment hit-target check will
    // call this label an interceptor, so the caller dispatches without that check.
    if (!domRelated(el, top)) out.slotted = true;
    return out;
  }
  out.occluded = true;
  // Whether to force is a question about the OCCLUDER, not about the field. Structure alone is not
  // enough: when the field sits directly under <body>, or shares a container with a portal target,
  // EVERY overlay on the page is "inside its parent". So the occluder must also be the size of a
  // skin. A decoration drawn over one field stays within that field's box give or take its own
  // border; a dialog, cookie banner or backdrop is dramatically larger, and forcing past one would
  // type into something the user cannot see.
  // One property decides this: is the occluder part of the field's own control, or a surface layered
  // over the region the field sits in? There are three ways to be a layer, and every condition below
  // is one of them -- so a fourth would have to be a fourth way, not another special case.
  //   - it sits outside the field's own subtree (structure);
  //   - it is pinned to the viewport, where a control's decoration scrolls with its field;
  //   - it is the size of the viewport, where a decoration is the size of a control.
  // Ancestors are NOT exempt. "A dialog is never an ancestor of what it covers" was wrong: a wrapper
  // that disables its own contents while busy is exactly that, and so is a full-screen container
  // that wraps the form it blocks.
  const tr = top.getBoundingClientRect();
  const area = (b) => Math.max(1, b.width * b.height);
  const viewport = Math.max(1, innerWidth * innerHeight);
  // Pinning is inherited from whichever ancestor establishes it, so reading the hit element alone
  // misses the ordinary modal shape: a fixed backdrop wrapping a statically-positioned panel. And
  // sticky pins to the viewport too once it sticks -- a sticky header covering a field is not a
  // decoration of that field.
  let pinned = false;
  for (let n = top; n && n.nodeType === 1; n = n.parentNode || n.host || null) {
    let pos = '';
    try { pos = getComputedStyle(n).position; } catch (e) { break; }
    if (pos === 'fixed' || pos === 'sticky') { pinned = true; break; }
  }
  // Measured against the VIEWPORT, not the field: 10x a small input is a small box, but 10x a large
  // textarea is bigger than the screen, so a field-relative cap stops meaning anything exactly when
  // the field is big. A decoration covers a control; a dialog or backdrop covers the view.
  const coversTheView = area(tr) > 0.6 * viewport;
  // "The field's own control" is its containing block, not its immediate parent: an overlay skin is
  // positioned against that block, and design systems routinely put an inner wrapper between the
  // input and it. Walking to the nearest positioned ancestor finds the same element the skin itself
  // was laid out against, so a skin one wrapper deeper still reads as part of the control.
  // Walk up from the OCCLUDER to the block it was positioned against, and ask whether the field is
  // inside that block. Asking from the field's side instead stops at the field's own wrapper, and a
  // design system that puts an inner wrapper around the input then hides its own skin from us.
  let block = null;
  for (let n = top.parentElement; n; n = n.parentElement) {
    let pos = '';
    try { pos = getComputedStyle(n).position; } catch (e) { break; }
    if (pos !== 'static') { block = n; break; }
  }
  // With no positioned ancestor the occluder is laid out against the page itself, so fall back to
  // the field's own parent rather than letting it inherit the document as its unit.
  const unit = block || el.parentElement;
  // Small is not the same as THIS field's. A table row, a card or a list item is small and holds
  // several independent controls, so a sibling's dropdown or a row-level "saving" overlay would
  // otherwise read as this field's decoration. A control the field shares with no other control is
  // the field's own; one that holds others is a layout region.
  let unitOwnsOnlyThisField = false;
  if (unit && area(unit.getBoundingClientRect()) <= 0.6 * viewport) {
    try {
      unitOwnsOnlyThisField = !Array.from(
        unit.querySelectorAll('input,select,textarea,button,a[href],[contenteditable],[role="button"]')
      ).some((c) => c !== el && !related(el, c));
    } catch (e) { unitOwnsOnlyThisField = false; }
  }
  // A thing that announces itself as an overlay is one. This is the least ambiguous signal here --
  // a decoration has no role, while a tooltip, dialog or toast says so in its markup.
  const LAYER_ROLE = /^(tooltip|dialog|alertdialog|alert|status|menu|listbox|log|marquee)$/i;
  let declaresItselfALayer = false;
  for (let n = top; n && n.nodeType === 1 && n !== unit; n = n.parentNode || n.host || null) {
    const role = n.getAttribute && n.getAttribute('role');
    if ((role && LAYER_ROLE.test(role.trim())) || n.hasAttribute('aria-modal') || n.tagName === 'DIALOG') {
      declaresItselfALayer = true;
      break;
    }
  }
  const inFieldsOwnSubtree =
    related(top, el) || (unitOwnsOnlyThisField && related(unit, top) && related(unit, el));
  out.skinned = !pinned && !coversTheView && !declaresItselfALayer && inFieldsOwnSubtree;
  // An OPEN combobox's own popup is not a foreign occluder: the field aria-owns/controls the list it
  // just opened, so being "covered" by it means the widget is working, not blocked. Treat it like the
  // field's own skin -- force past it -- rather than refusing to type into the list the field opened.
  // Gated on aria-expanded="true" so this only fires for a combobox the page itself reports as OPEN,
  // never for a static field that merely happens to reference another element. Only the field's OWN
  // popup qualifies; a shared or unrelated layer never does.
  // Wrapped whole: a page can override getAttribute to throw (the same threat model the naming block
  // below guards against), and an escape here would fault page.evaluate and disable occlusion entirely.
  try {
    if (out.occluded && !out.skinned && el.getAttribute && el.getAttribute('aria-expanded') === 'true') {
      const popupIds = [];
      for (const a of ['aria-controls', 'aria-owns']) {
        const v = el.getAttribute && el.getAttribute(a);
        if (v) for (const id of v.split(/\s+/)) if (id) popupIds.push(id);
      }
      if (popupIds.length) {
        let ownRoot = null;
        try { ownRoot = Node.prototype.getRootNode.call(el); } catch (e) { ownRoot = null; }
        for (const id of popupIds.slice(0, 20)) {
          let pop = null;
          try { pop = ownRoot && ownRoot.getElementById ? ownRoot.getElementById(id) : document.getElementById(id); }
          catch (e) { pop = null; }
          // aria-controls/aria-owns express arbitrary relationships, so require the referenced element
          // to actually be a popup (listbox/menu/tree/grid/dialog -- the ARIA combobox-popup roles)
          // before forcing past it. Without this a field pointing at a plain region that happens to
          // hold a real occluder would type straight through it.
          const popRole = ((pop && pop.getAttribute && pop.getAttribute('role')) || '').toLowerCase();
          if (!/^(listbox|menu|tree|grid|dialog)$/.test(popRole)) continue;
          if (pop === top || related(pop, top)) {
            // The exemption forces past ONLY the layer-self-declaration, never the view-covering guard
            // the outer skin test applies: a full-screen dialog/listbox sheet, or a normal popup that
            // hosts a full-screen wall, hides what a person plainly sees, so it is a real occluder, not
            // the widget's working list. Refuse when EITHER the actually-hit occluder covers the view
            // (coversTheView, computed on `top` above -- catches a small popup hosting a fixed
            // full-screen child) OR the referenced popup itself does (catches a big sheet the hit
            // landed on a small option inside). A normal dropdown is a fraction of the viewport on
            // both counts and still qualifies. A thrown getBoundingClientRect reads as view-sized,
            // so a hostile page cannot forge its way back into the exemption.
            let popBig = true;
            try { popBig = area(pop.getBoundingClientRect()) > 0.6 * viewport; } catch (e) { popBig = true; }
            if (!coversTheView && !popBig) out.skinned = true;
            break;
          }
        }
      }
    }
  } catch (e) { /* best-effort: a thrown getAttribute must not disable occlusion detection */ }
  // The model needs a handle on the thing in the way, not just the fact that something is. Walk from
  // the hit element outward and stop at the FIRST ancestor that still reads as a layer -- pinned,
  // view-sized, or self-declared -- so a small dialog panel that happened to be hit directly is walked
  // past in favor of the backdrop wrapping it, but a real backdrop is never walked past in favor of a
  // still-more-outer app shell or scroll-lock wrapper that also happens to qualify (e.g. is itself
  // view-sized): the backdrop is closer to the hit, so it wins.
  // Named regardless of skinned: the typing path ignores the name when it forces past a skin, but the
  // CLICK path has no force fallback -- a click covered by the field's own open listbox times out, and
  // the model needs the occluder named (its options listed) rather than a bare 15s Page.click Timeout.
  if (out.occluded && hit && hit !== document.body && hit !== document.documentElement) {
   top = hit;
   // A throw anywhere below would otherwise escape page.evaluate() entirely and be read upstream
   // as "the probe failed" -- which _reachable_for_typing treats as reachable=True, skipping
   // occlusion detection altogether. Naming the occluder is best-effort; out.occluded/out.skinned
   // are already decided above and must survive regardless of what happens in here.
   try {
    // Same set observe() already rejects raw ids/testids on: a bidi override or zero-width
    // character in page-authored text can make the rendered guidance read as something different
    // from what the string actually is. Stripped, not rejected -- this is a label the model reads,
    // not an identifier trusted for its exact bytes, so the text minus the forgeable characters is
    // still useful.
    // Two copies, not one reused: a `g`-flagged regex is stateful across .test() calls (lastIndex
    // persists and silently skips matches on alternating calls), so .replace() and .test() each get
    // their own instance rather than sharing one that would behave correctly for only one of them.
    const FORGEABLE = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/;
    const FORGEABLE_G = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g;
    const clean = (s) => (s || '').replace(FORGEABLE_G, '').replace(/\s+/g, ' ').trim();
    // A page-controlled string (innerText, an attribute value) is unbounded, so the regex in
    // clean() runs on a capped prefix first -- never on the raw string -- and the result is
    // capped again to the field's display length.
    const boundedClean = (s, cap) => clean(String(s == null ? '' : s).slice(0, 2000)).slice(0, cap);
    // The mint shape observe() uses for data-tv3. A value that does not match it is not a marker
    // we minted, so it must never be interpolated into a selector -- that would let page content
    // forge a selector (e.g. break out of the quoted attribute value) that the model then acts on.
    const MINTED_MARKER_RE = /^t\d+(-\d+)?$/;
    // A selector is only safe to recommend if it is the ONLY match across every root -- _q.all()
    // already pierces open shadow roots, so a control named by an id or marker scoped to its own
    // component (the usual shape) is still counted, unlike a plain document.querySelectorAll would.
    // A cloned subtree (a templated dialog re-rendered from a copy that already carried a live
    // marker) can leave two elements sharing one data-tv3 value just as easily as two elements
    // sharing one id -- the marker's regex shape says it looks minted, not that it is still unique.
    // count === 1 alone is not enough: CSS selector matching reads the real id ATTRIBUTE, not the
    // JS `.id` property, so a page that overrides the property's getter to report a decoy value
    // gets a selector that resolves to whatever element genuinely owns that attribute -- one match,
    // just not `n`. The sole match must be `n` itself, not merely unique.
    const uniqueSelector = (s, n) => {
      let matches = [];
      try { matches = _q.all(s); } catch (e) { matches = []; }
      return matches.length === 1 && matches[0] === n ? s : null;
    };
    // An id carrying a forgeable character (the same set stripped from name/label text above) would
    // still reach the model unstripped here: CSS.escape() preserves it, and this string is a
    // selector interpolated straight into the message, not display text run through clean(). A
    // very long id is capped for the same reason boundedClean caps text -- an uncapped
    // page-controlled string turns into an uncapped escape+query, and this runs on every diagnosis.
    const idSelector = (n) =>
      n.id && n.id.length <= 200 && !FORGEABLE.test(n.id) ? uniqueSelector('#' + CSS.escape(n.id), n) : null;
    const markerSelector = (n) => {
      const m = n.getAttribute && n.getAttribute('data-tv3');
      return m && MINTED_MARKER_RE.test(m) ? uniqueSelector('[data-tv3="' + m + '"]', n) : null;
    };
    // A wizard's inactive step is a common shape for opacity:0 + pointer-events:none applied to the
    // STEP's own wrapper, not each control inside it -- a control's own computed style stays
    // untouched, so the ancestor chain (bounded: a pathological page cannot make this unbounded)
    // has to be walked too, not just the candidate itself.
    // forPaint asks "would a person SEE this", not "could a person interact with it": a scrim with
    // pointer-events:none is still seen even though clicks pass through it, so the paint scan
    // (layerShowsPaint) passes forPaint=true to keep such a child in view. Every other caller omits it
    // and keeps the interaction-strict default.
    const visible = (n, forPaint) => {
      const r = n.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      // pointer-events and visibility are both inherited, but either can be explicitly overridden by
      // a descendant (a click-through overlay with a poking-through button; a hidden wrapper with one
      // child restored via visibility:visible) -- the candidate's own computed value already resolves
      // cascade + override in one read, so both are checked once here, not per-ancestor below.
      // display has no such override: display:none removes the whole subtree from the render tree,
      // so it stays an ancestor-walk check, same as opacity and overflow.
      let ownCs;
      try { ownCs = getComputedStyle(n); } catch (e) { return false; }
      if ((!forPaint && ownCs.pointerEvents === 'none') || ownCs.visibility === 'hidden') return false;
      let steps = 0;
      for (
        let a = n;
        a && a !== document.body && a !== document.documentElement && steps < 40;
        a = a.parentNode || a.host || null, steps++
      ) {
        // A ShadowRoot reached mid-walk (nodeType 11, not 1) carries no style of its own -- skip
        // straight to its host via the update expression's `.host` fallback rather than stopping
        // the walk there, or a hidden host (or anything above it) never gets checked.
        if (a.nodeType !== 1) continue;
        // inert makes a subtree non-focusable and non-actionable without changing any computed style
        // property -- the .inert IDL property reflects the attribute directly, no matching needed.
        if (a.inert) return false;
        let cs;
        try { cs = getComputedStyle(a); } catch (e) { return false; }
        if (cs.display === 'none') return false;
        if (parseFloat(cs.opacity) === 0) return false;
        // A carousel/wizard routinely keeps an inactive slide's markup in the DOM, translated out of
        // its own overflow:hidden container -- present, sized, but never painted. Only 'hidden' is
        // checked (not scroll/auto): those stay reachable via the ordinary auto-scroll a click does
        // on its own, so treating them as clipped would wrongly drop a control that only needs that.
        // The two axes are independent: setting overflow-x:hidden alone computes overflow-y to
        // 'auto' (the CSS interop rule for a hidden/visible pair), so a control merely scrolled out
        // vertically must not be treated as X-clipped just because the container clips X.
        if (a !== n) {
          const clipX = cs.overflowX === 'hidden';
          const clipY = cs.overflowY === 'hidden';
          if (clipX || clipY) {
            const ar = a.getBoundingClientRect();
            if (clipX && (r.right <= ar.left || r.left >= ar.right)) return false;
            if (clipY && (r.bottom <= ar.top || r.top >= ar.bottom)) return false;
          }
        }
      }
      return true;
    };
    // elementFromPoint retargets a hit inside a component to its host, so the layer is often a host
    // whose name and controls live in its OPEN shadow tree, not its (usually empty) light DOM.
    // Bounded so a pathological page (many nested open roots) cannot make this walk unbounded.
    // shadowRoot reads are guarded like every other one in this file: a sealed host (its getter
    // overridden to throw) must drop out of the walk, not crash the whole probe -- a probe that
    // throws is caught upstream and read as "reachable", which skips occlusion detection entirely.
    const deepAll = (node, sel, pred) => {
      const out2 = [];
      let visited = 0;
      const visit = (n, depth) => {
        if (!n || depth > 12 || visited > 5000) return;
        let sr = null;
        try { sr = n.shadowRoot; } catch (e) { sr = null; }
        if (sr) { visited++; visit(sr, depth + 1); }
        let matched = [];
        try { matched = n.querySelectorAll(sel); } catch (e) { matched = []; }
        // The 5000 budget is spent by the shadow-root walk below via `visited`, but a single
        // querySelectorAll on a pathological layer (thousands of matching elements in one root) can
        // otherwise still return an unbounded NodeList here -- cap what actually gets collected too.
        for (const m of matched) {
          if (out2.length >= 5000) return;
          if (!pred || pred(m)) out2.push(m);
        }
        let all = [];
        try { all = n.querySelectorAll('*'); } catch (e) { all = []; }
        for (const child of all) {
          if (++visited > 5000) return;
          let csr = null;
          try { csr = child.shadowRoot; } catch (e) { csr = null; }
          if (csr) visit(csr, depth + 1);
        }
      };
      visit(node, 0);
      return out2;
    };
    const ownName = (n) => {
      if (!n) return '';
      const al = n.getAttribute && n.getAttribute('aria-label');
      if (al) { const v = boundedClean(al, 80); if (v) return v; }
      const lb = n.getAttribute && n.getAttribute('aria-labelledby');
      if (lb) {
        // Root-scoped, not document.getElementById: an id inside an open shadow root is only
        // visible to a getElementById call on that root.
        let root = null;
        try { root = Node.prototype.getRootNode.call(n); } catch (e) { root = null; }
        // Capped before splitting, same as every other page-controlled string here: an uncapped
        // attribute value turns into an uncapped token list, each doing a root lookup, inside
        // page.evaluate() where nothing else bounds the work.
        const txt = lb
          .slice(0, 2000)
          .split(/\s+/)
          .slice(0, 20)
          .map((id) => { const t = root && root.getElementById ? root.getElementById(id) : null; return t ? boundedClean(t.textContent, 2000) : ''; })
          .filter(Boolean)
          .join(' ');
        if (txt) return txt.slice(0, 80);
      }
      return '';
    };
    // Visibility-filtered like the controls loop below: an invisible heading or dialog inside the
    // layer (a hidden template, a not-yet-shown step) is not what a person actually sees naming it.
    const headingNameOf = (n) => {
      const h = deepAll(n, 'h1,h2,h3,h4,h5,h6', visible)[0];
      return h ? boundedClean(h.textContent, 80) : '';
    };
    // Does an element draw a surface a person can see -- a non-transparent background, an image, a
    // border, or a shadow? The alpha-0 forms of a color (`transparent`, `rgba(...,0)`) paint nothing.
    // A color is invisible only when its ALPHA is zero -- parse the alpha channel, never a trailing
    // ",0)", which also matches an opaque color whose blue channel is 0 (rgb(0,0,0), rgb(255,0,0)).
    // A form we can't parse is treated as paint, so the failure mode is under-suppression, not over.
    const opaquePaint = (color) => {
      const c = (color || '').replace(/\s+/g, '');
      if (!c || c === 'transparent') return false;
      const m = c.match(/^rgba?\(([\d.,-]+)\)$/);
      if (!m) return true;
      const comps = m[1].split(',');
      const alpha = comps.length >= 4 ? parseFloat(comps[3]) : 1;
      return !(alpha === 0);
    };
    // A replaced/embedded element paints pixels with no CSS surface of its own -- an icon-only spinner
    // or logo (img/svg/canvas/video/iframe) is plainly visible even though backgroundColor/border are
    // empty, so it must count as paint or such a layer reads as an invisible ghost. The caller filters
    // by visible(), so a zero-sized or hidden replaced element never reaches here.
    const REPLACED_PAINT = /^(img|svg|image|canvas|video|picture|object|embed|iframe)$/;
    const paintsSurface = (n) => {
      if (REPLACED_PAINT.test((n.tagName || '').toLowerCase())) return true;
      let s;
      try { s = getComputedStyle(n); } catch (e) { return false; }
      if (opaquePaint(s.backgroundColor)) return true;
      if (s.backgroundImage && s.backgroundImage !== 'none') return true;
      if (s.boxShadow && s.boxShadow !== 'none') return true;
      // A backdrop-filter (a frosted/blur wall) paints a plainly visible effect with no CSS surface of
      // its own -- no background, border, or shadow -- so without this such a wall reads as an
      // invisible ghost and the model is wrongly told to press Escape at a layer it can see.
      const bdf = s.backdropFilter || s.webkitBackdropFilter;
      if (bdf && bdf !== 'none') return true;
      const bw = (v) => parseFloat(v || '0') || 0;
      if (
        s.borderStyle !== 'none' &&
        bw(s.borderTopWidth) + bw(s.borderBottomWidth) + bw(s.borderLeftWidth) + bw(s.borderRightWidth) > 0
      ) return true;
      return false;
    };
    const hasDirectText = (n) => {
      for (const c of n.childNodes) if (c.nodeType === 3 && c.nodeValue && c.nodeValue.trim()) return true;
      return false;
    };
    // Whether the LAYER shows a person any paint of its own -- a surface, or a visible descendant that
    // paints a surface or renders text. opacity:0 anywhere in its chain zeroes all of it. The covered
    // field's OWN paint (it sits inside the layer in the ancestor case) is never the layer's, so it is
    // excluded. Bounded so a pathological layer cannot make the scan unbounded; the caller runs it only
    // for a control-less layer, keeping it off the hot path for ordinary dialogs.
    const layerShowsPaint = (root2) => {
      for (
        let n = root2;
        n && n.nodeType === 1 && n !== document.body && n !== document.documentElement;
        n = n.parentNode || n.host || null
      ) {
        let s;
        try { s = getComputedStyle(n); } catch (e) { break; }
        if (parseFloat(s.opacity) === 0) return false;
      }
      if (root2 !== el && !related(el, root2) && visible(root2, true) && (paintsSurface(root2) || hasDirectText(root2))) {
        return true;
      }
      // deepAll (not querySelectorAll) so the scan pierces open shadow roots -- a consent widget that
      // renders its visible surface/text entirely inside its own shadow tree must count as paint, the
      // same shadow-aware treatment the control and heading lookups already use. Bounded by deepAll.
      let nodes = [];
      try { nodes = deepAll(root2, '*', (n) => visible(n, true)); } catch (e) { nodes = []; }
      for (const n of nodes) {
        if (n === el || related(el, n)) continue;
        if (paintsSurface(n) || hasDirectText(n)) return true;
      }
      return false;
    };
    // Pinning (fixed/sticky) is a strong enough signal on its own -- a small cookie banner docked
    // to the viewport edge is exactly as real an occluder as a full-screen one. Being merely
    // ABSOLUTE and view-sized is weaker evidence (an ordinary in-flow-adjacent block can be
    // absolutely positioned for layout reasons having nothing to do with occlusion), so that path
    // still requires bigness. Either way, a wrongly-oversized OUTER ancestor (a scroll-lock shell
    // wrapping the real banner/backdrop) can never win: the walk below stops at the first qualifying
    // ancestor, and the real occluder is always closer to the hit point than any shell wrapping it.
    // The document root is layout, never content, and must never stand in as the thing blocking a click.
    const isLayer = (n, isHit) => {
      if (n === document.body || n === document.documentElement) return false;
      let pos = '';
      try { pos = getComputedStyle(n).position; } catch (e) { pos = ''; }
      if (pos === 'fixed' || pos === 'sticky') return true;
      const role = n.getAttribute && n.getAttribute('role');
      if ((role && LAYER_ROLE.test(role.trim())) || (n.hasAttribute && n.hasAttribute('aria-modal')) || n.tagName === 'DIALOG') {
        return true;
      }
      // Bigness alone is only trustworthy for an element that is NOT an ancestor of the field --
      // a clipped (not covered) field's hit-point routinely lands on the static layout/clipping
      // container that wraps it, and that container is exactly as big as a genuine backdrop. A
      // real full-screen blocking wrapper is always pinned or role-bearing (both already handled
      // above), so excluding an unpinned ancestor here costs nothing real.
      if (related(n, el)) return false;
      const big = area(n.getBoundingClientRect()) > 0.6 * viewport;
      return isHit ? big : pos === 'absolute' && big;
    };
    let layer = null;
    for (let n = top; n && n.nodeType === 1 && n !== document.body; n = n.parentNode || n.host || null) {
      if (isLayer(n, n === top)) { layer = n; break; }
    }
    if (!layer) {
      // Nothing in the walk qualified, and top is merely an ancestor/clipping container of the
      // field -- there is no honest occluder to name (the field is clipped, not covered). Bail
      // with out.occluder left unset so the caller falls back to its generic message instead of
      // naming a layout wrapper and listing every unrelated button on it.
      if (related(top, el)) {
        // One exception: a view-sized ancestor that paints NOTHING, over a field that is itself
        // un-clipped and visible, is not a clip -- it is a ghost cover (a leftover full-page consent
        // shield that still intercepts the pointer). Report it as invisible so the model is not told
        // to dismiss an overlay it cannot see. A truly clipped field fails visible(el), and a real
        // layout shell paints (its nav/content), so neither is caught here.
        if (visible(el) && coversTheView && !layerShowsPaint(top)) out.occluder = { invisible: true };
        return out;
      }
      layer = top;
    }
    // Own name, then whichever names the DIALOG this layer wraps (deepAll pierces into the layer's
    // shadow tree, since a component-hosted consent widget renders entirely inside one), then a
    // heading anywhere in the layer, then its own text, then its tag -- in that order.
    let layerName = ownName(layer);
    if (!layerName) {
      const dialog = deepAll(layer, '[role="dialog"],[role="alertdialog"],[aria-modal]', visible)[0];
      if (dialog) layerName = ownName(dialog) || headingNameOf(dialog);
    }
    if (!layerName) layerName = headingNameOf(layer);
    if (!layerName) layerName = boundedClean(layer.textContent, 60);
    if (!layerName) layerName = layer.tagName ? layer.tagName.toLowerCase() : 'layer';
    const layerSelector = idSelector(layer) || markerSelector(layer);
    const allControls = [];
    // observe() never mints data-tv3 inside a component, so a marker-shaped selector below can only
    // ever come from the light DOM -- a shadow-piercing find here does not risk minting a fresh one.
    // The role list mirrors observe()'s own _WIDGET_ROLES answer to "is this a control?" (minus the
    // form-field roles observe treats as fillable, not actionable), so a consent switch or a
    // role=menuitem Close action is not omitted just because it isn't a <button>.
    const found = deepAll(
      layer,
      'button,a[href],input[type="button"],input[type="submit"],input[type="image"],'
      + 'input[type="reset"],[role="button"],'
      + '[role="checkbox"],[role="radio"],[role="combobox"],[role="option"],[role="menuitem"],'
      + '[role="menuitemcheckbox"],[role="menuitemradio"],[role="listbox"],[role="switch"],'
      + '[role="spinbutton"],[role="tab"]'
    );
    // A disabled control cannot be the thing to click -- recommending one wastes a click timeout on
    // a target Playwright will refuse, and can crowd the real dismisser out of the eight-slot cap.
    // :disabled (not the .disabled IDL property) is what the browser actually uses to decide this,
    // so it is also true for a button whose OWN disabled attribute is unset but sits inside a
    // <fieldset disabled> -- the property alone would miss exactly that inherited case.
    const isDisabled = (n) => {
      let matched = false;
      try { matched = !!(n.matches && n.matches(':disabled')); } catch (e) { matched = false; }
      return matched || (n.getAttribute && n.getAttribute('aria-disabled') === 'true');
    };
    for (const c of found) {
      if (c === el || !visible(c) || isDisabled(c)) continue;
      const csel = idSelector(c) || markerSelector(c);
      // ownName covers aria-label and root-scoped aria-labelledby, same priority order and same
      // shadow-aware resolution the layer's own name uses.
      const label = boundedClean(ownName(c) || c.textContent || c.value || (c.getAttribute && c.getAttribute('title')) || '', 60);
      if (!label && !csel) continue;
      allControls.push({ selector: csel, label });
    }
    // A real dismisser (Accept, Confirm, Close) routinely comes AFTER a list of category rows or
    // toggles in document order -- a Privacy Preference Center's footer buttons follow its list of
    // per-vendor switches. Capping at the first eight would drop exactly the control the model
    // needs and keep only the toggles it was already flailing between, reproducing the ticket's own
    // motivating bug with more words. Keep both ends: the first few for context, the last few
    // because that is where a footer actually lives.
    const truncated = allControls.length > 8;
    const controls = truncated ? allControls.slice(0, 5).concat(allControls.slice(-3)) : allControls;
    out.occluder = { selector: layerSelector, name: layerName, controls, truncated };
    // Whether a PERSON would see this layer at all. A leftover consent backdrop still intercepts the
    // pointer (elementFromPoint returned it) but can paint nothing -- fully transparent, no visible
    // control, heading or text -- so the field looks clear on screen and "dismiss the overlay you
    // see" is a false instruction. Gated on there being no visible control (a real dialog has some),
    // so the bounded paint scan runs only for the ambiguous, control-less layer.
    if (!controls.length && !layerShowsPaint(layer)) out.occluder.invisible = true;
   } catch (e) { /* best-effort */ }
  }
  return out;
}"""
)

_ACTIVE_IS_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const el = _q.find(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return null;
  // A control inside a component reports its host as document.activeElement, so ask the root that
  // actually holds the control rather than the document.
  let root = null;
  try { root = Node.prototype.getRootNode.call(el); } catch (e) { root = null; }
  const active = root && root.activeElement ? root.activeElement : document.activeElement;
  return active === el;
}"""
)


class _FieldCovered(Exception):
    """The field exists and is rendered, but something unrelated is on top of it."""

    def __init__(self, selector: str, occluder: dict[str, Any] | None = None) -> None:
        super().__init__(selector)
        self.selector = selector
        self.occluder = occluder


class _FieldNotEditable(Exception):
    """The field cannot accept typed text at all -- it is disabled, or readonly."""

    def __init__(self, selector: str, read_only: bool) -> None:
        super().__init__(selector)
        self.selector = selector
        self.read_only = read_only


# Design-system forms render a <select> at zero size behind a styled listbox proxy. Playwright's
# actionability wait never resolves against it, so select_option probes visibility first and only
# forces past actionability when the element exists but is genuinely hidden this way.
_SELECT_VISIBILITY_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _visibleProxy = """
    + _VISIBLE_PROXY_JS
    + r""";
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  try {
    const el = _q.find(sel) || _executorEl;
    if (!el) return { exists: false, visible: false };
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    // Forcing a value onto a select nothing stands in for carries a value the user never saw into
    // whatever the run submits next.
    return {
      exists: true,
      nodeName: (el.nodeName || '').toLowerCase(),
      visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden',
      disabled: !!el.disabled,
      proxied: !!_visibleProxy(el),
    };
  } catch (e) { return { exists: false, visible: false }; }
}"""
)

# Whether a selector's own element is a typeable field (an input/textarea/contenteditable that can
# accept keystrokes) rather than a click-to-open anchor (a button/div that only opens a list). The
# shared custom-combobox commit path types into typeable anchors and refuses non-typeable ones, so a
# page.fill throw ("Element is not an <input>") never replaces the <select> throw this fix removes.
_ANCHOR_TYPEABLE_JS = (
    r"""(arg) => {
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  try {
    const el = _q.find(arg.sel) || _executorEl;
    if (!el) return false;
    const tag = el.tagName;
    if (tag === 'TEXTAREA') return !el.disabled && !el.readOnly;
    if (tag === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      const NONTEXT = new Set(['checkbox','radio','button','submit','reset','file','image','range','color','hidden']);
      return !NONTEXT.has(t) && !el.disabled && !el.readOnly;
    }
    return !!el.isContentEditable;
  } catch (e) { return false; }
}"""
)

# Read back after a forced select_option so a styled proxy that silently didn't sync from its
# native control is caught rather than reported as a successful selection.
_SELECT_READBACK_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  try {
    const el = _q.find(sel) || _executorEl;
    if (!el) return null;
    const idx = el.selectedIndex;
    const opt = idx >= 0 ? el.options[idx] : null;
    // Playwright matches label= against option.label (whitespace-collapsed), not raw text.
    return { value: el.value, selectedIndex: idx, selectedLabel: opt ? opt.label : null };
  } catch (e) { return null; }
}"""
)

# A skinned checkbox/radio is a zero-size or invisible native input whose visible <label> is the
# real click target; the label is tagged (stale tags cleared first) so click can act on it.
_SKINNED_CHECKBOX_PROBE_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _visibleProxy = """
    + _VISIBLE_PROXY_JS
    + r""";
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  try {
    // Cleared across roots, not just the document: a tag left inside a component would still be
    // matched by the executor's piercing engine and clicked as though it were this call's proxy.
    _q.all('[data-tv3-proxy]').forEach((e) => e.removeAttribute('data-tv3-proxy'));
    const el = _q.find(sel) || _executorEl;
    if (!el) return { exists: false, skinned: false, labelTagged: false };
    const type = String(el.type || '').toLowerCase();
    if (el.tagName === 'INPUT' && type === 'file') return { exists: true, skinned: false, labelTagged: false, file: true };
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const invisible = r.width === 0 || r.height === 0 || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.05;
    if (el.tagName === 'SELECT') {
      return { exists: true, skinned: false, labelTagged: false, select: true, invisible, proxied: !!_visibleProxy(el) };
    }
    if (el.tagName !== 'INPUT' || (type !== 'checkbox' && type !== 'radio')) {
      return { exists: true, skinned: false, labelTagged: false };
    }
    if (!invisible) return { exists: true, skinned: false, labelTagged: false };
    const radio = type === 'radio';
    const proxy = _visibleProxy(el);
    if (!proxy) return { exists: true, skinned: false, labelTagged: false, radio, unproxied: true };
    const disabled = !!el.disabled;
    // A label that also wraps another control (a button, link, or a second input) is not a safe
    // proxy: a real click on it can activate that control instead.
    const label = el.labels && el.labels[0];
    const wrapsOther = (l) => Array.from(l.querySelectorAll('button,a[href],input,select,textarea')).some((c) => c !== el);
    // Only a real <label> activates its control on click; an aria-labelledby target is a name, not a proxy.
    if (label && label === proxy && !wrapsOther(label)) {
      label.setAttribute('data-tv3-proxy', '1');
      return { exists: true, skinned: true, labelTagged: true, radio, disabled };
    }
    return { exists: true, skinned: true, labelTagged: false, radio, disabled };
  } catch (e) { return { exists: false, skinned: false, labelTagged: false }; }
}"""
)

# Read twice (before and after the forced click): a proxy that does not sync from its native input
# must fail loud, not read as a successful toggle.
_CHECKBOX_CHECKED_JS = (
    r"""(arg) => {
  const sel = arg.sel;
  // A node the page replaced between the executor's lookup and this evaluate is not evidence about
  // the live page: reading a detached one reports a stale value as a current verdict.
  const _executorEl = arg.el && arg.el.isConnected ? arg.el : null;
  const _q = """
    + _ROOT_QUERY_JS
    + r""";
  try { const el = _q.find(sel) || _executorEl; return el ? !!el.checked : null; } catch (e) { return null; }
}"""
)

# Pre-click state for the dropdown-commit path: whether a click-opened menu (rows tagged
# data-tv3-menu by _FIND_MENU_JS) is currently open, whether the click target IS one of its rows, and
# that row's state fingerprint — aria checked/selected/pressed, class, child count, text — so an option
# click on a multi-select menu (which commits WITHOUT closing) can be verified by its state change.
# Also takes the visible-DOM pre-snapshot (data-tv3-pre) so a menu the click opens reads as a reaction.
_CLICK_PRECHECK_JS = (
    r"""(arg) => {
  const clicked = arg.sel;"""
    + _PIERCED_QUERY_JS
    + r"""
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    // A menu mid-close (opacity fade, pointer-events cut) still has a nonzero rect; reading it as
    // "open" would turn a healthy committed selection into a false "did not commit" error.
    try {
      const s = getComputedStyle(el);
      if (s.visibility === 'hidden' || Number(s.opacity) < 0.05 || s.pointerEvents === 'none') return false;
    } catch (e) {}
    return true;
  };
  const state = (el) => {
    // .checked is a DOM property, not an attribute: a native-checkbox multi-select commits by
    // flipping only it, with no aria/class/text change. Same for inline-style-only toggles.
    let kids = '';
    try { for (const i of el.querySelectorAll('input')) kids += i.checked ? '1' : '0'; } catch (e) {}
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
      el.className, el.children.length, (el.innerText || '').trim(),
      el.getAttribute('style'), kids,
    ].join('|');
  };
  // Fixed-arity on purpose: every component is one attribute of the row itself, so a row that
  // restructures cannot change this string, and only being picked can.
  const selState = (el) => {
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
    ].join('|');
  };
  const openRows = [];
  for (const el of pQSA('[data-tv3-menu]')) if (vis(el)) openRows.push(el);
  let target = null;
  try { target = pQS(clicked) || (arg.el && arg.el.isConnected ? arg.el : null); } catch (e) { target = (arg.el && arg.el.isConnected ? arg.el : null); }
  let isOption = false;
  let containsMenu = false;
  let optText = '';
  let optState = '';
  let optSel = '';
  let optKids = -1;
  let optH = -1;
  let optVis = -1;
"""
    + _VIS_ROWS_JS
    + r"""
  if (target && openRows.length) {
    for (const el of openRows) {
      // The target being the row or inside it is an option pick. The target merely CONTAINING rows
      // (the card around the menu) is not — and since a center-point click on the card can land on
      // an arbitrary row, that case is flagged so the handler makes no claims about it at all.
      if (el === target || pContains(el, target)) {
        isOption = true;
        optText = (el.innerText || '').trim().slice(0, 80);
        optState = state(el);
        optSel = selState(el);
        optKids = el.children.length;
        optH = Math.round(el.getBoundingClientRect().height);
        optVis = _visRows(el);
        break;
      }
      if (pContains(target, el)) containsMenu = true;
    }
  }
  preReset();
  pScopeEach((el, inShadow) => { if (vis(el)) preMark(el, inShadow); });
  return { menuOpen: openRows.length > 0, isOption, containsMenu, optText, optState, optSel, optKids, optH, optVis };
}"""
)

# Same-document token for the click retry: a navigation destroys window, a pushState does not.
_CLICK_SAME_DOC_PLANT_JS = "() => { window.__tv3_click_same = 1; }"
_CLICK_SAME_DOC_CHECK_JS = "() => window.__tv3_click_same === 1"

# Planted on window before an option click; a navigation clears window, so its absence afterwards is
# the page saying "different document" even when the post-click probe's own JS is what failed.
_CLICK_DOC_PLANT_JS = "() => { window.__tv3_click_doc = 1; }"
_CLICK_DOC_CHECK_JS = "() => window.__tv3_click_doc === 1"

# Post-click menu state: how many previously-tagged menu rows are still visible (a closed menu — nodes
# destroyed or hidden — reads 0), plus the clicked row's current state fingerprint for the multi-select
# commit check. Field names are distinct from _CLICK_PRECHECK_JS's on purpose (tests dispatch on them).
_MENU_AFTER_JS = (
    r"""(arg) => {
  const clicked = arg.sel;"""
    + _PIERCED_QUERY_JS
    + r"""
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    try {
      const s = getComputedStyle(el);
      if (s.visibility === 'hidden' || Number(s.opacity) < 0.05 || s.pointerEvents === 'none') return false;
    } catch (e) {}
    return true;
  };
  const state = (el) => {
    // .checked is a DOM property, not an attribute: a native-checkbox multi-select commits by
    // flipping only it, with no aria/class/text change. Same for inline-style-only toggles.
    let kids = '';
    try { for (const i of el.querySelectorAll('input')) kids += i.checked ? '1' : '0'; } catch (e) {}
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
      el.className, el.children.length, (el.innerText || '').trim(),
      el.getAttribute('style'), kids,
    ].join('|');
  };
  // Fixed-arity on purpose: every component is one attribute of the row itself, so a row that
  // restructures cannot change this string, and only being picked can.
  const selState = (el) => {
    return [
      el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
    ].join('|');
  };
"""
    + _VIS_ROWS_JS
    + r"""
  let stillOpen = 0;
  const rows = [];
  for (const el of pQSA('[data-tv3-menu]')) if (vis(el)) { stillOpen++; rows.push(el); }
  let target = null;
  try { target = pQS(clicked) || (arg.el && arg.el.isConnected ? arg.el : null); } catch (e) { target = (arg.el && arg.el.isConnected ? arg.el : null); }
  let optState = '';
  let optSel = '';
  let optKids = -1;
  let optH = -1;
  let optVis = -1;
  if (target) {
    for (const el of rows) {
      if (el === target || pContains(el, target)) {
        optState = state(el);
        optSel = selState(el);
        optKids = el.children.length;
        optH = Math.round(el.getBoundingClientRect().height);
        optVis = _visRows(el);
        break;
      }
    }
  }
  return { stillOpen, optState, optSel, optKids, optH, optVis };
}"""
)

# Behavioral, site-agnostic menu finder: after a click (with the pre-snapshot taken first),
# look for the option list the page rendered IN REACTION — a NEW container (not data-tv3-pre: a
# pre-existing visible container whose rows merely changed, e.g. pagination refreshing a results list,
# is never a menu) holding >=2 new, visible, row-sized, mostly-clickable leaf rows, positioned adjacent
# to the clicked element. Keys off reaction + geometry — no CSS-class/ARIA/site vocabulary — mirroring
# _FIND_SUGGESTION_JS. Unlike the typeahead finder this only REPORTS (the model does the clicking), so
# navigational rows are listed too. Tags rows data-tv3-menu="1..N" (top-to-bottom) — in-DOM tags that
# stay valid until the menu re-renders, so the model can pick an option without a re-observe re-minting
# ids (the staging trace's staleness trap). Existing tags are cleared only when a new menu is tagged.
_FIND_MENU_JS = (
    r"""(arg) => {
  const clicked = arg.sel;"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  const MENU_ROW_ROLES = """
    + _MENU_ROW_ROLES_JS
    + r""";
  const vis = (r) => r.width > 0 && r.height > 0;
  let trigger = null;
  try { trigger = pQS(clicked) || (arg.el && arg.el.isConnected ? arg.el : null); } catch (e) { return null; }
  if (!trigger) return null;
  // The reaction gate below is the whole basis for calling these rows a menu the click just opened.
  // A navigation destroys window, so an absent snapshot here means the page under us is not the page
  // we clicked on, and every row would read as new. Refuse: "cannot judge" beats naming three
  // ordinary links on a fresh document as a menu and telling the model to pick one.
  if (!preReady()) return null;
  const tr = trigger.getBoundingClientRect();
  const rows = [];
  for (const el of pScopeAll()) {
    if (preHas(el) || focusHas(el)) continue;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LABEL' || tag === 'FORM') continue;
    if (el.children.length > 8) continue;
    const r = el.getBoundingClientRect();
    if (!vis(r) || r.height > 90) continue;
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 80) continue;
    // Options are individually actionable rows. Requiring it per-row keeps a dialog's title/body
    // text from being listed as "options" (and a horizontal Confirm/Cancel button pair then fails
    // the stacked-rows check below). The role set is observe's, minus the container and
    // navigational ones: a single-select built as a radiogroup and a multi-select built as
    // checkboxes are menus, and a probe that disagreed with observe about that left their rows
    // untagged -- which silently disarms every commit check in _click_reaction.
    const role = el.getAttribute('role');
    let ptr = false;
    try { ptr = getComputedStyle(el).cursor === 'pointer'; } catch (e) { ptr = false; }
    const clickable = tag === 'BUTTON' || tag === 'A' || MENU_ROW_ROLES.has(role) || ptr;
    if (!clickable) continue;
    rows.push({ el, r, txt });
  }
  if (rows.length < 2) return null;
  const leaves = rows.filter((c) => !rows.some((o) => o.el !== c.el && pContains(c.el, o.el)));
  if (leaves.length < 2) return null;
  // Group by parent AND grandparent so both flat menus (card > button*N) and nested ones
  // (ul > li > button) find their shared container.
  const groups = new Map();
  // parentElement is null at a shadow boundary (a ShadowRoot is not an Element), so a menu whose
  // rows are written straight into the root -- root.innerHTML = '<div role="option">...' -- would
  // group under nothing and never be found. The host stands in for the boundary.
  // A host reached this way stands in for the boundary, and a host necessarily pre-exists the menu
  // its component just rendered -- so the container-is-new check below must not be applied to it.
  // The rows' own newness still carries the reaction evidence.
  const boundaryStandIns = new Set();
  const parentOf = (el) => {
    if (!el) return null;
    const p = el.parentNode;
    if (!p) return null;
    if (p.nodeType === 11) {
      if (!p.host) return null;
      boundaryStandIns.add(p.host);
      return p.host;
    }
    return p.nodeType === 1 ? p : null;
  };
  for (const c of leaves) {
    const p1 = parentOf(c.el);
    for (const p of [p1, parentOf(p1)]) {
      if (!p || p === document.body || p === document.documentElement) continue;
      if (!groups.has(p)) groups.set(p, new Set());
      groups.get(p).add(c);
    }
  }
  let best = null;
  for (const [p, set] of groups) {
    const g = Array.from(set);
    if (g.length < 2) continue;
    // A pre-existing container normally is not a just-opened menu. The exception: the row you CLICKED
    // (or a container inside it) that expanded to reveal leaves which pre-existed hidden — there the
    // container is old but the leaves are the new reaction. Scoped to within the clicked row so an
    // unrelated pre-existing list that merely gained rows elsewhere is still rejected.
    const withinClicked = p === trigger || pContains(trigger, p);
    if (!boundaryStandIns.has(p) && preHas(p) && !withinClicked) continue;
    // A dialog is a page mode, not a menu — mislabeling its action buttons invites a wrong "pick an
    // option" move. But a real option list legitimately renders inside a modal (an application form's
    // select in a dialog), so exclude a dialog group ONLY when its rows are not explicit menu options:
    // a confirm dialog's Cancel/Confirm pair (plain buttons) stays excluded, a role=option listbox does not.
    try {
      if (p.closest('dialog,[role~="dialog"],[aria-modal="true"]')) {
        // Test the closest option-role ANCESTOR, not the reduced leaf: a role=option row with a styled
        // <span> child reduces to the span (null role), so a leaf-only check would wrongly reject it.
        // menuitem rows are enumerable here (a menu in a dialog is still a menu) but stay nav for clicking.
        if (!g.every((c) => c.el.closest(OPT_SEL + ',[role="menuitem"]'))) continue;
      }
    } catch (e) {}
    const pr = p.getBoundingClientRect();
    if (!vis(pr) || pr.height > 500) continue;
    if (pr.top < tr.top - 200 || pr.top > tr.bottom + 400) continue;
    if (pr.right < tr.left - 100 || pr.left > tr.right + 100) continue;
    const tops = new Set(g.map((c) => Math.round(c.r.top)));
    if (tops.size < 2) continue;
    if (!best || g.length > best.g.length || (g.length === best.g.length && pr.height < best.h)) best = { p, g, h: pr.height };
  }
  if (!best) return null;
  pQSA('[data-tv3-menu]').forEach((e) => e.removeAttribute('data-tv3-menu'));
  best.g.sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
  const options = [];
  let n = 0;
  for (const c of best.g) {
    n++;
    c.el.setAttribute('data-tv3-menu', String(n));
    if (options.length < 15) options.push({ n, text: c.txt.slice(0, 60) });
  }
  // Undeclared virtualisation: a list that renders only a window declares nothing (no aria-setsize),
  // but its scroll container carries the FULL extent (react-window sizes a spacer to the whole list).
  // Rendered-in-full lists fill their scroll extent; a window leaves more than a row of it uncovered.
  let partial = false;
  try {
    const first = best.g[0].r, last = best.g[best.g.length - 1].r;
    const span = last.bottom - first.top;
    const rowH = Math.max(1, span / best.g.length);
    for (let sc = best.p, hops = 0; sc && hops < 6; hops++, sc = sc.parentElement) {
      const ovy = getComputedStyle(sc).overflowY;
      if ((ovy === 'auto' || ovy === 'scroll' || ovy === 'overlay') && sc.scrollHeight > sc.clientHeight + 1) {
        partial = sc.scrollHeight - span >= 1.5 * rowH;
        break;
      }
    }
  } catch (e) { partial = false; }
  return { count: n, options, partial };
}"""
)

# Read the FULL (untruncated) label of every row _FIND_MENU_JS tagged data-tv3-menu, across the same
# pierced reach it tags in. _FIND_MENU_JS caps its returned `options` at 15 and truncates each to 60
# chars for payload size; the deterministic match must see the whole list at full length so a value at
# row 20, or a label longer than 60 chars, is neither missed nor matched on a cut-off token. `nav` marks
# a row this tool must not auto-click — _FIND_MENU_JS enumerates <a href>/<button>/menuitem rows because
# it only REPORTS, but clicking a navigational row would leave the form (mirrors _FIND_SUGGESTION_JS,
# which refuses exactly these unless role=option).
_MENU_OPTION_TEXTS_JS = (
    r"""() => {"""
    + _PIERCED_QUERY_JS
    + _ROW_SEMANTICS_JS
    + r"""
  return Array.from(pQSA('[data-tv3-menu]')).map((el) => {
    // `_FIND_MENU_JS` tags the innermost leaf; an option whose ancestor declares aria-setsize is a child
    // that declares none, so read the closest declaring ancestor or the incomplete-list guard is bypassed.
    const nav = isNavRow(el);
    const setEl = el.closest('[aria-setsize]');
    const setsize = setEl ? parseInt(setEl.getAttribute('aria-setsize'), 10) : NaN;
    return {
      n: parseInt(el.getAttribute('data-tv3-menu'), 10),
      text: (el.innerText || el.textContent || '').trim(),
      nav: nav,
      setsize: Number.isFinite(setsize) && setsize > 0 ? setsize : 0,
    };
  });
}"""
)

# Whether the anchor (or its nearest aria-expanded ancestor) currently reports an OPEN list. Used to
# gate the "close a stray open list" Escape: sending Escape with no menu open would bubble to and close
# a surrounding dialog, so we only send it once a menu is confirmed open.
_MENU_OPEN_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const el = pQS(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  const exp = el.getAttribute('aria-expanded') != null ? el : el.closest('[aria-expanded]');
  return !!(exp && exp.getAttribute('aria-expanded') === 'true');
}"""
)

# True when the anchor (or its combobox ancestor) declares aria-autocomplete list/both/inline -- the ARIA
# contract for a combobox that searches as you type. It is the reaction signal for a searchable widget that
# filtered to ZERO rows on an absent value (nothing new for _FIND_MENU_JS to count), which must still read
# as a genuine no-match rather than fall through to a click-to-open enumeration.
_DECLARES_SEARCH_AUTOCOMPLETE_JS = (
    r"""(arg) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const SEARCH = new Set(['list', 'both', 'inline']);
  const read = (n) => (n.getAttribute('aria-autocomplete') || '').toLowerCase();
  let el = pQS(arg.sel) || (arg.el && arg.el.isConnected ? arg.el : null);
  if (!el) return false;
  if (SEARCH.has(read(el))) return true;
  const cb = el.closest('[aria-autocomplete]');
  return !!(cb && SEARCH.has(read(cb)));
}"""
)

# Page total for `group` text across one observe, counted at the 200-character display width of each
# entry; the record retains up to the masking width, which Python masks and then caps to 200.
OBSERVE_GROUP_TEXT_TOTAL_CAP = 4000
# Display width of each masked-then-capped field of the observe digest. Every render site reads its
# width here, so the retain margin below is always sized for the widest window.
OBSERVE_DISPLAY_WIDTHS = {"label": 140, "placeholder": 60, "value": 100, "invalid": 140, "group": 200, "text": 300}
# Floor for the width the enumeration retains per field before Python masks and caps it. Widened per
# call so the longest payload-minted URL fits whole after the widest display window.
OBSERVE_RETAIN_WIDTH_MIN = 2000
OBSERVE_FIELD_DISPLAY_MAX = max(OBSERVE_DISPLAY_WIDTHS.values())

# Raw DOM perception: collect visible interactive elements with a stable selector each.
# Elements without a natural selector get a data-tv3 marker so later actions can target them.
_OBSERVE_JS_TEMPLATE = (
    r"""
async () => {
  // Field text is retained at this width and masked, then capped for display, in Python. Substituted
  // per call from the payload refs: any minted URL that starts inside a display window fits whole.
  const _RETAIN_WIDTH = __OBSERVE_RETAIN_WIDTH__;
  const _GROUP_TEXT_TOTAL_CAP = """
    + str(OBSERVE_GROUP_TEXT_TOTAL_CAP)
    + r""";
  const _GROUP_SEL = 'fieldset,[role=group],li,dd,.form-group,[class*="question"],[class*="field"]';
  // A previous control ends the walk back for question text; ARIA widgets count as controls here
  // exactly as they do in the element list, or a custom checkbox's own caption reads as the question.
  const _CTRL_SEL = 'input:not([type=hidden]),textarea,select,button,[role=button],[role=checkbox],[role=radio],[role=combobox],[role=switch],[role=listbox],[role=spinbutton],[contenteditable]:not([contenteditable="false" i])';
  const _normText = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // A choice control takes its group's text only when the group is purely options: a container
  // that also holds text fields has an innerText naming every question in it.
  const _NONCHOICE_SEL = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]),textarea,select,[role=combobox],[role=listbox],[role=spinbutton],[contenteditable]:not([contenteditable="false" i])';
  const _CHOICE_SEL = 'input[type=checkbox],input[type=radio],[role=checkbox],[role=radio],[role=switch]';
  // Read through the prototypes: the walk below crosses the control's <form>, whose named controls
  // shadow its own properties (<input name="matches"> makes form.matches that input).
  const _getter = (proto, name) => {
    const d = Object.getOwnPropertyDescriptor(proto, name);
    return d && d.get ? d.get : function () { return this[name]; };
  };
  const _parentOf = _getter(Node.prototype, 'parentElement');
  const _scrollLeftOf = _getter(Element.prototype, 'scrollLeft');
  const _prevOf = _getter(Node.prototype, 'previousSibling');
  const _nextOf = _getter(Node.prototype, 'nextSibling');
  const _firstChildOf = _getter(Node.prototype, 'firstChild');
  const _nodeTypeOf = _getter(Node.prototype, 'nodeType');
  const _contentOf = _getter(Node.prototype, 'textContent');
  const _innerTextOf = _getter(HTMLElement.prototype, 'innerText');
  const _matches = Element.prototype.matches;
  const _qs = Element.prototype.querySelector;
  const _qsa = Element.prototype.querySelectorAll;
  const _bcr = Element.prototype.getBoundingClientRect;
  // A sibling the user cannot see is not the question: unrendered, transparent, aria-hidden, or a
  // box under 2px (a zero-height clip, a 1px screen-reader-only hint). A display:contents wrapper
  // has no box of its own and is judged by the innerText of its rendered children.
  const _unseen = (s) => {
    const cs = window.getComputedStyle(s);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return true;
    if (s.getAttribute('aria-hidden') === 'true') return true;
    if (cs.display === 'contents') return false;
    const r = _bcr.call(s);
    return r.width < 2 || r.height < 2;
  };
  // Text a user can see inside el: innerText still includes transparent, aria-hidden and
  // screen-reader-only descendants, which _unseen excludes. Bounded to 4 levels.
  const _visibleText = (el, depth) => {
    if (depth > 4 || _unseen(el)) return '';
    let out = '';
    for (let c = _firstChildOf.call(el); c; c = _nextOf.call(c)) {
      const kind = _nodeTypeOf.call(c);
      if (kind === 3) out += ' ' + _contentOf.call(c);
      else if (kind === 1) out += ' ' + _visibleText(c, depth + 1);
    }
    return _normText(out);
  };
  const _captionHost = (el) => {
    if (_matches.call(el, _CHOICE_SEL)) return true;
    if (!_qs.call(el, _CHOICE_SEL)) return false;
    try { return _visibleText(el, 0).length < 2; } catch (e) { return true; }
  };
  // Question text for a control whose own name is weak. Choice controls take the text of the
  // nearest group ancestor that has any (legend + options), if it is purely options. Text fields
  // take the nearest text block that PRECEDES the control inside that ancestor, stopping at a
  // previous control: a container holding several questions has an innerText naming all of them,
  // and a group text that names the wrong question is the mis-association this field exists to
  // end. The ancestor's own text is used only when it wraps this one control. Bounded to 6 levels
  // and 8 siblings. Any throw yields no group text, never a dropped element.
  const _groupText = (el, isChoice) => {
    try {
      let node = el;
      for (let depth = 0; depth < 6; depth++) {
        const parent = _parentOf.call(node);
        if (!parent) break;
        if (!isChoice) {
          let scanned = 0;
          for (let s = _prevOf.call(node); s && scanned < 8; s = _prevOf.call(s), scanned++) {
            const kind = _nodeTypeOf.call(s);
            if (kind === 3) {
              const t = _normText(_contentOf.call(s));
              if (t.length >= 2) return t;
              continue;
            }
            if (kind !== 1) continue;
            if (_matches.call(s, _CTRL_SEL) || _qs.call(s, _CTRL_SEL)) break;
            if (_unseen(s)) continue;
            let t = '';
            try { t = _normText(_innerTextOf.call(s)); } catch (e) { continue; }
            // One character is decoration (a required marker), never a question.
            if (t.length < 2) continue;
            // Text right after a checkbox or radio -- bare, or in a wrapper with no text of its own --
            // is that control's caption, not this one's question. A previous question block that
            // happens to hold options is not a wrapper, and the text after it is the next question.
            let before = _prevOf.call(s);
            while (before && _nodeTypeOf.call(before) !== 1 && !_normText(_contentOf.call(before))) before = _prevOf.call(before);
            if (before && _nodeTypeOf.call(before) === 1 && _captionHost(before)) break;
            return t;
          }
        }
        node = parent;
        if (!_matches.call(node, _GROUP_SEL)) continue;
        const t = _normText(_innerTextOf.call(node));
        if (!t) continue;
        if (isChoice) return _qsa.call(node, _NONCHOICE_SEL).length === 0 ? t : '';
        return _qsa.call(node, _CTRL_SEL).length === 1 ? t : '';
      }
    } catch (e) { /* fail open: the record keeps today's shape */ }
    return '';
  };
  const _isAutocomplete = """
    + _IS_AUTOCOMPLETE_JS
    + r""";
  const _visibleProxy = """
    + _VISIBLE_PROXY_JS
    + r""";
  // [role=textbox] is deliberately absent: on a div without contenteditable it names a control that
  // cannot be filled, and the ones that can are already matched by [contenteditable=true].
  const _WIDGET_ROLES = """
    + _WIDGET_ROLES_JS
    + r""";
  const q = 'input,textarea,select,button,a[href],[role=button],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=listbox],[role=switch],[role=spinbutton],[role=tab],[contenteditable=true]';
  // Set wherever we learn that some region of the page cannot be read. Declared here because the
  // walk below is one of those places and it runs before the marker gather.
  let sawUnreadableRoot = false;
  // Narrower than sawUnreadableRoot, which a page-wide flag several unrelated failures also set:
  // this counts only roots the walk never discovered, so a channel that iterates allRoots can say
  // whether allRoots was the whole story.
  let undiscoveredRoots = 0;
  // A web component renders its real input/button inside an open shadow root, which
  // document.querySelectorAll does not cross. Playwright's selector engine does, so these elements
  // were always actionable and only perception was blind — a page of them reads as a handful of
  // chrome controls that never change. Each root's own matches are appended after the light DOM's,
  // NOT spliced in at the host's position, so the element budget spends itself on the page's own
  // controls first and the submit button survives a page of components; what that starves is
  // counted and disclosed. Every root is kept for the uniqueness probe below.
  const allRoots = [];
  const els = [];
  // Roots whose host chain reaches a <form>. `closest` stops at the root it starts in, so a block
  // inside a component cannot see the form its host sits in; this is that answer, carried down.
  const inFormRoots = new Set();
  {
    const seenRoots = new Set();
    // Pushes `root`'s own matches onto `els`, and collects the roots nested directly under it into
    // `kids` in document order.
    const enumerate = (root, host, kids) => {
      // Selected by the same CSS-string query the base used, not el.matches(q). This is base parity,
      // not a defense: a page that overrides querySelectorAll itself can still make <html> and <body>
      // enumerate, measured, exactly as it can on base. What it does avoid is widening the surface
      // to a SECOND overridable entry point for the same outcome.
      for (const el of root.querySelectorAll(q)) els.push({ el, host });
      for (const el of root.querySelectorAll('*')) {
       // Same clobbering hazard as the element loop below, and this walk runs before it: a form's
       // named getter can turn any read here into a foreign object, so one element pays for itself.
       try {
        // nodeType 11 because <input name="shadowRoot"> makes el.shadowRoot that input, and
        // walking it would add a non-root to the list every probe then queries.
        const sr = el.shadowRoot;
        if (sr && sr.nodeType === 11) {
          if (seenRoots.has(sr)) continue;
          seenRoots.add(sr);
          kids.push({ root: sr, host: el, parent: root });
        }
       } catch (e) {
        // This costs the element's entire root, not the element -- its own matches were pushed by
        // the query above. The root never reaches allRoots, so the loss is disclosed here instead.
        sawUnreadableRoot = true;
        undiscoveredRoots++;
       }
      }
    };
    // An explicit stack rather than recursion. Playwright's selector engine descends to any depth,
    // so a root we stop short of is a root resolvesTo cannot count -- an identity recurring beyond
    // the stopping point reads as unique here and as ambiguous to the executor.
    const stack = [];
    // Reversed, so popping walks a root's children in document order and both lists stay pre-order.
    const descend = (kids) => { for (let k = kids.length - 1; k >= 0; k--) stack.push(kids[k]); };
    allRoots.push(document);
    const seed = [];
    // Unguarded, unlike the roots below: a document that cannot be enumerated is an error worth
    // raising, not a page that happens to carry no controls.
    enumerate(document, null, seed);
    descend(seed);
    while (stack.length) {
      const frame = stack.pop();
      allRoots.push(frame.root);
      // Pre-order, so the parent root's answer is already settled when we get here.
      try {
        if (inFormRoots.has(frame.parent) || frame.host.closest('form')) inFormRoots.add(frame.root);
      } catch (e) { /* one host that cannot answer only costs its own root's ranking */ }
      const kids = [];
      // One root that cannot be enumerated costs its own subtree, not the walk. A root that throws
      // for every query is disclosed by the marker gather below; one that throws only for this
      // query is not, and is not defended here.
      // Its own matches are lost, and so is every root nested under it -- those never reach allRoots.
      try { enumerate(frame.root, frame.host, kids); } catch (e) { undiscoveredRoots++; }
      descend(kids);
    }
  }
  const out = [];
  const labelOfControl = new Map();
  // Monotonic across observe() calls (persisted on window), and never reassigned on an element that
  // already has one, so a data-tv3 marker always denotes the same element. Resetting the counter per
  // call let a selector remembered from an earlier observe silently resolve to a different node.
  if (!Number.isInteger(window.__tv3_next) || window.__tv3_next < 0 || window.__tv3_next > 1e9) window.__tv3_next = 0;
  // Unique is not enough -- it must be THIS element. A page can pre-seed a marker or exploit
  // U+0000 folding to U+FFFD so a selector matches exactly one node that is a different one.
  // Counted across every root, not just `document`: Playwright resolves a selector globally and
  // pierces, so a document-only check both misses a collision living in another root and rejects
  // every shadow-hosted element, whose one true match `document.querySelector` cannot see.
  // Gathered once, not re-queried per attempt: the mint search below is bounded at 64 attempts, and
  // when each attempt cost O(roots) a page could freeze window.__tv3_next, seed the 64 candidates in
  // the LAST root so no attempt short-circuits, and multiply the whole search by its own root count.
  // Measured before this was hoisted: 51 ms -> 36.4 s at 24,000 roots, past the 30 s tool bound.
  // Two structures, deliberately: `takenMarkers` is every value known to be in use, gathered AND
  // minted, and exists only so a fresh candidate never collides. `gatheredCounts` records what the
  // GATHER saw and nothing else -- reuse consults it, so a marker this call minted itself can never
  // be handed to a second element as though the page had been observed carrying it once.
  const takenMarkers = new Set();
  const gatheredCounts = new Map();
  for (const root of allRoots) {
    try {
      for (const e of root.querySelectorAll('[data-tv3]')) {
        const v = e.getAttribute('data-tv3');
        takenMarkers.add(v);
        gatheredCounts.set(v, (gatheredCounts.get(v) || 0) + 1);
      }
    }
    // One unreadable root must not cost the element list -- but it does mean takenMarkers is
    // incomplete, so every marker minted below is unverified. On a page whose controls are all
    // anonymous, resolvesTo is never called and this is the ONLY place that learns it.
    catch (e) { sawUnreadableRoot = true; }
  }
  // A root whose querySelectorAll throws makes uniqueness UNVERIFIABLE, not false -- Playwright's
  // engine pierces via CDP and would still see a collision living in there, so we must not hand out
  // a selector we could not check. resolvesTo therefore still refuses. What that used to cost was
  // the whole page: every element fell through to mintOn, whose reuse check runs through here too,
  // so a fresh marker was minted on every observe -- and a payload that churns each turn silently
  // disables the loop's perception-stall terminator. mintOn reuses an existing marker instead when
  // this is set, which keeps the payload byte-stable.
  // A NEW marker minted while this is set is itself unverified -- takenMarkers skipped the throwing
  // root, so a decoy planted in there can collide. Accepted deliberately: refusing to mint costs
  // every selector on the page, and the collision has not been shown to reach a wrong element
  // (Playwright ordered the light-DOM match first in every shape tried). The payload says so.
  // Per-check, unlike sawUnreadableRoot: distinguishes "this element's identity is genuinely
  // ambiguous" from "we could not tell", which are different omissions with different fixes.
  let checkInconclusive = false;
  const resolvesTo = (s, target) => {
    let found = null;
    let n = 0;
    for (const root of allRoots) {
      let hits;
      try { hits = root.querySelectorAll(s); }
      // A throw is the ROOT's, not ours: every candidate is valid by construction -- `#` + an escape
      // gated on CSS.escape being a no-op, a tag from safeTag's whitelist, or a quoted attr whose
      // value already passed _FORGEABLE. Asking the page to classify the error instead (its own
      // e.name, or a probe whose receiver it owns) hands it a one-line switch to the quiet path,
      // where the disclosure vanishes and markers churn every observe.
      catch (e) { sawUnreadableRoot = true; checkInconclusive = true; return false; }
      n += hits.length;
      if (n > 1) return false;
      if (hits.length === 1) found = hits[0];
    }
    return n === 1 && found === target;
  };
  // Values here are page-controlled: an unescaped `"` closes the selector and turns it into a
  // selector list aimed at an element of the page's choosing, which still resolves uniquely.
  // String() because form named getters make el.id/el.name return an ELEMENT, not a string.
  const attr = (name, value) => '[' + name + '="' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
  // The model copies these selectors back verbatim, and an escape sequence does not survive that:
  // `#\31 abc` addresses a different codepoint the moment its terminating space is dropped. The
  // escape being a no-op is exactly the condition for the id needing no escaping. The trim check
  // is separate: Playwright trims the selector string, so `#email<NBSP>` -- which CSS.escape leaves
  // alone, being above U+007F -- reaches the page as `#email` and silently selects a different
  // element. Only the tail can be trimmed away; a leading one sits behind the `#` and survives.
  // The selector is rendered bare inside [...] and CANNOT be sanitized -- stripping a character
  // would break the very matching the selector exists to do. So a value carrying anything that could
  // end or restructure a digest line is refused here and the element falls through to a minted
  // marker, the same route U+0000 already takes. U+000A is refused by CSS itself (an unescaped
  // newline is a bad-string, so the selector will not parse and resolvesTo rejects it) -- but U+2028
  // is a legal CSS ident AND string character, so it parses, resolves, and forges a clean line.
  const _FORGEABLE = /[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]/;
  // A tag name is rendered bare into the selector and is page-controlled, so it is whitelisted to
  // what a type selector may actually be rather than screened for known-bad characters. A tag may
  // hold `,` `.` `[` `:` and quotes, and `a,b[id="x"]` is a SELECTOR LIST, not a narrowing of
  // `#x` -- it reaches elements the bare id never matched. It may also not be a string at all: a
  // form's named getter makes el.tagName an ELEMENT, and String() of one is
  // "[object HTMLInputElement]", which is a CSS syntax error. Neither survives this.
  const safeTag = (el) => {
    const t = String(el.tagName || '').toLowerCase();
    return /^[a-z][a-z0-9_-]*$/.test(t) ? t : null;
  };
  // Why the last naturalSelector call returned null. The three causes need three different fixes,
  // so a single "no selector" tally would send the follow-up after the wrong one.
  let naturalWhy = '';
  const naturalSelector = (el) => {
    naturalWhy = '';
    checkInconclusive = false;
    const rawTestid = String(el.getAttribute('data-testid') || '');
    // Read before any attempt: a form's named getter can make el.id an ELEMENT, and String() of
    // one is truthy, so "has an identity to try" and "that identity is usable" are separate facts.
    const hasIdentity = !!(el.id || el.name || rawTestid);
    if (_FORGEABLE.test(String(el.id || '')) || _FORGEABLE.test(String(el.name || ''))
        || _FORGEABLE.test(rawTestid)) { naturalWhy = 'unsafe'; return null; }
    if (el.id) {
      const raw = String(el.id);
      const esc = window.CSS && CSS.escape ? CSS.escape(raw) : null;
      const s = esc === raw && raw === raw.trimEnd() ? '#' + esc : attr('id', raw);
      if (resolvesTo(s, el)) return s;
      // A component that mirrors its own id onto the native control inside its root makes the bare
      // id match twice with ONE instance on the page, and naming the tag separates them. Measured on
      // a production capture: the whole named-field set of a real application form resolves this way
      // and no other.
      // What makes this safe is NOT that the qualified form is a subset of `#id` -- it is a subset
      // only while the tag is a simple type selector, and `safeTag` is what keeps it one. The guard
      // that holds in general is resolvesTo's `found === target`: whatever the string turns out to
      // select, it is accepted only if the single element it selects is THIS one.
      const tq = safeTag(el);
      if (tq) { const s2 = tq + attr('id', raw); if (resolvesTo(s2, el)) return s2; }
    }
    const testid = el.getAttribute('data-testid');
    if (testid) { const s = attr('data-testid', testid); if (resolvesTo(s, el)) return s; }
    if (el.name) {
      const tq = safeTag(el);
      if (tq) { const s = tq + attr('name', el.name); if (resolvesTo(s, el)) return s; }
    }
    // An identity that exists but did not resolve uniquely is NOT anonymous. Shadow encapsulation
    // scopes ids to their root, so a design system reuses one internal id in every instance and the
    // cross-root count is 2 -- a duplicate, whose fix is host-anchored scoping. Distinct again from
    // a count we could not take because some root threw.
    naturalWhy = !hasIdentity ? 'anonymous' : (checkInconclusive ? 'unverifiable' : 'duplicated');
    return null;
  };
  // Minting writes an attribute, which is only safe in the light DOM. Inside a root it makes a
  // component watching that root re-render, destroying the marker before the model can click it --
  // and unlike the pre-snapshot the marker cannot move off-DOM, because the marker IS the handle we
  // hand out. Each fresh observe would mint another and the click would fail again, forever.
  // Of the markers this call hands out, those it wrote versus those it found already on the page:
  // the split is what says whether markers churn between observes, which the stall terminator's
  // digest comparison depends on. Both count the handing out, and an entry the post-walk check
  // strips is uncounted again.
  let markersWritten = 0;
  let markersReused = 0;
  // Bumped before every attribute write we make, verified or not: each one can run page code.
  let pageCodeEpoch = 0;
  const _isConnectedDesc = Object.getOwnPropertyDescriptor(Node.prototype, 'isConnected');
  const _isConnected = _isConnectedDesc && _isConnectedDesc.get ? _isConnectedDesc.get : function () { return document.contains(this); };
  const mintOn = (el) => {
    let m = el.getAttribute('data-tv3');
    // A marker already on the element is page-controlled text like any other attribute: it is
    // rendered bare inside the selector, so one carrying a line separator forges a whole element
    // line. Screened before it can be reused, the same as id/name/data-testid.
    if (m && _FORGEABLE.test(String(m))) m = null;
    // Reuse a marker only if it still uniquely resolves; otherwise mint a fresh monotonic one.
    // Keeps a marker stable across observe() calls without trusting a foreign, duplicated, or
    // syntactically-broken data-tv3 value that a remembered selector could resolve to the wrong node.
    if (m) {
      checkInconclusive = false;
      if (resolvesTo(attr('data-tv3', m), el)) { markersReused++; return attr('data-tv3', m); }
      // Reuse only when THIS check could not be taken AND the gather positively saw this marker
      // exactly once. Gating on the page-global let one unreadable root anywhere hand out a marker
      // resolvesTo had proven is a duplicate; gating on inconclusiveness alone still did, because a
      // throwing DOCUMENT root means the duplicate is never counted at all. Both times two payload
      // lines carried one selector and the click landed on whichever the executor matched first.
      if (checkInconclusive && gatheredCounts.get(m) === 1) { markersReused++; return attr('data-tv3', m); }
    }
    // Minting below is NOT verified when a root is unreadable: takenMarkers could not include that
    // root's markers, so a decoy planted in there can collide with a fresh candidate. Dropping
    // instead was measured and is worse -- it costs every selector on the page, including elements
    // whose own id is unique, which is the regression the unreadable-root test exists to prevent.
    // The page-level "uniqueness could not be verified" note discloses it; see SKY-14710.
    // Skip values the page already carries, or a pre-seeded data-tv3 collides with a freshly
    // minted one and two elements share a selector. Bounded, and the suffix varies per attempt:
    // a frozen or saturated counter makes ++ a no-op, and an unbounded search would wedge the
    // renderer for the rest of the run. An element we cannot name uniquely is left unlisted.
    m = null;
    for (let n = 0; n < 64 && m === null; n++) {
      const candidate = 't' + (window.__tv3_next++) + (n ? '-' + n : '');
      // Checked against every root's markers, for the same reason resolvesTo counts that way: a
      // decoy the page planted inside a shadow root is invisible to document.querySelector, and the
      // executor pierces -- so a document-only check hands out a marker that already denotes something.
      if (!takenMarkers.has(candidate)) m = candidate;
    }
    if (m === null) return null;
    // The `host` screened at enumeration time is bookkeeping, not a property of the element at the
    // moment of the write: a page accessor can move an element into a root after we enumerated it,
    // and an overridden document.querySelectorAll can hand us an in-root element with no host at
    // all. Re-read the real root here so "we never write inside a component" holds by construction.
    let rootNow = null;
    // Two independent signals, each read through the prototype rather than the instance. Every
    // in-page check is clobberable on its own -- Node.prototype.getRootNode included -- so they are
    // combined such that DISAGREEMENT refuses the write: a page must corrupt both consistently to
    // obtain one, and a throw from either is itself a refusal. An element outside the document is
    // also refused; we have nothing to gain by marking one.
    try { rootNow = Node.prototype.getRootNode.call(el); } catch (e) { return null; }
    if (!rootNow || rootNow.nodeType === 11) return null;
    try { if (!Node.prototype.contains.call(document, el)) return null; } catch (e) { return null; }
    pageCodeEpoch++;
    el.setAttribute('data-tv3', m);
    // Verify AFTER the write, against the live DOM rather than the gather. The candidate search
    // reads a snapshot taken before any mint on this page, so it cannot see a value the page added
    // since, nor one an ordinary attributeChangedCallback mirrors onto a sibling during this very
    // setAttribute. A PROVEN collision is dropped rather than handed out: a name that denotes two
    // elements is the wrong-element click this whole mechanism exists to prevent. An INCONCLUSIVE
    // check keeps the marker, which is the documented trade -- refusing there was measured and
    // costs every selector on the page. One check per listed element, not per candidate attempt,
    // which is what made the old per-attempt re-query unaffordable.
    checkInconclusive = false;
    if (!resolvesTo(attr('data-tv3', m), el) && !checkInconclusive) {
      pageCodeEpoch++;
      el.removeAttribute('data-tv3');
      return null;
    }
    takenMarkers.add(m);
    markersWritten++;
    return attr('data-tv3', m);
  };
  // Every scope the executor searches under a host: the host's own light subtree, the root it owns,
  // and any root nested beneath either. A descendant combinator is shadow-transparent to the
  // executor, so content SLOTTED into the component matches `#host #ctrl` too -- counting the root
  // alone undercounts, and an undercount is what hands out a selector that denotes two elements.
  // Memoised per host for the walk, or every control under one shell host pays for a fresh walk of
  // that shell's entire subtree. The only thing that runs page code during the evaluate is our own
  // marker write (an attributeChangedCallback can attach a root), so the memo is dropped after every
  // such write; a clobbered getter that mutates on read is left to the executor-side count that
  // gates every action.
  const hostScopeCache = new Map();
  let hostScopeEpoch = -1;
  const hostScopes = (host) => {
    const epoch = pageCodeEpoch;
    if (epoch !== hostScopeEpoch) { hostScopeCache.clear(); hostScopeEpoch = epoch; }
    if (hostScopeCache.has(host)) return hostScopeCache.get(host);
    const scopes = hostScopesWalk(host);
    hostScopeCache.set(host, scopes);
    return scopes;
  };
  const hostScopesWalk = (host) => {
    const scopes = [host];
    const stack = [host];
    const own = host.shadowRoot;
    if (own && own.nodeType === 11) { scopes.push(own); stack.push(own); }
    while (stack.length) {
      const scope = stack.pop();
      let kids;
      try { kids = scope.querySelectorAll('*'); } catch (e) { return null; }
      for (const k of kids) {
        let sr = null;
        try { sr = k.shadowRoot; } catch (e) { return null; }
        if (sr && sr.nodeType === 11 && scopes.indexOf(sr) === -1) { scopes.push(sr); stack.push(sr); }
      }
    }
    return scopes;
  };
  // resolvesTo, scoped to one host. Same shape and same `found === target` guarantee: whatever the
  // string turns out to select under this host, it is accepted only if the one element it selects
  // is THIS one.
  const scopedResolvesTo = (host, s, target) => {
    const scopes = hostScopes(host);
    if (!scopes) return false;
    let found = null;
    let n = 0;
    for (const scope of scopes) {
      let hits;
      try { hits = scope.querySelectorAll(s); } catch (e) { return false; }
      n += hits.length;
      if (n > 1) return false;
      if (hits.length === 1) found = hits[0];
    }
    return n === 1 && found === target;
  };
  // Shadow encapsulation scopes ids to their own root, so a design system reuses one internal id in
  // every instance and no unscoped selector can single one out. The host itself is outside the root
  // it owns, so it can be named the ordinary way, and anchoring on it scopes the reused id without
  // writing anything into the component.
  // The host of the root `n` lives in, read through the prototype so a named getter cannot supply one.
  const hostOf = (n) => {
    let r = null;
    try { r = Node.prototype.getRootNode.call(n); } catch (e) { return null; }
    return r && r.nodeType === 11 && r.host ? r.host : null;
  };
  // Tails for a control with no id of its own, smallest first: its tag, the tag qualified by type,
  // role or class tokens, and finally its position among same-tag siblings. Every tail is ONE
  // compound selector, never a combinator chain: under the executor a descendant combinator is
  // shadow-transparent and a child combinator is too, so a chain the page counts as unique in one
  // tree can denote a second element in a nested root. A compound is matched element by element,
  // and the union of the host's scopes is exactly the set the executor searches; a nested host's
  // anchor composes such compounds link by link, each verified under its own host. A positional tail
  // is a last resort: unlike a tag or class, a sibling inserted before the control retargets it
  // without changing the match count, which is the one drift the executor-side count cannot see.
  const structuralTails = (el) => {
    const tag = String(el.tagName || '').toLowerCase();
    if (!/^[a-z][a-z0-9-]*$/.test(tag)) return [];
    const tails = [tag];
    const type = el.getAttribute('type');
    if (type && /^[a-z-]+$/i.test(String(type))) tails.push(tag + '[type="' + String(type).toLowerCase() + '"]');
    const role = el.getAttribute('role');
    if (role && /^[a-z]+$/i.test(String(role))) tails.push(tag + '[role="' + String(role).toLowerCase() + '"]');
    // A design system's class tokens are as stable as its tags; each is screened to a plain
    // identifier and the whole is verified, so a token the page chose cannot forge a payload line
    // or denote a second element.
    let classes = [];
    try { classes = Array.from(el.classList || []).filter((c) => /^[A-Za-z_][\w-]*$/.test(c)).slice(0, 3); } catch (e) { classes = []; }
    const leaf = classes.length ? tag + '.' + classes.join('.') : tag;
    if (classes.length) tails.push(leaf);
    let k = 1;
    try { for (let sib = el.previousElementSibling; sib; sib = sib.previousElementSibling) { if (sib.tagName === el.tagName) k++; } } catch (e) { return tails; }
    tails.push(leaf + ':nth-of-type(' + k + ')');
    return tails;
  };
  // Shadow encapsulation scopes ids to their own root, so a design system reuses one internal id in
  // every instance and no unscoped selector can single one out; a component's native control often
  // carries no id at all. The host itself is outside the root it owns, so it can be named the
  // ordinary way -- by its own identity, by a marker written on it in the light DOM, or through ITS
  // host in turn -- and anchoring on it scopes the control without writing anything into the component.
  // Naming a host is paid once per walk: the same anchor serves every control under it, and a
  // marker written for the first is reused, not re-minted, for the rest.
  const anchorByHost = new Map();
  // Anchoring is bounded per walk. A control that cannot be named does not spend the element
  // budget, so without this a page of thousands of unnameable component controls would spend the
  // evaluate's whole time bound on tails that all fail.
  let anchorAttempts = 0;
  const _ANCHOR_ATTEMPTS = 3000;
  const _ANCHORED_MAX_LEN = 400;
  // Set when the LAST refusal was ours (a budget) rather than the page's, so the omission is
  // reported as such and not as a claim about the control.
  let anchorRefusedByBudget = false;
  // The host and tail of the last selector hostAnchored composed, kept so the record can be
  // re-resolved under its host later (a composed selector straddles a root; only scoped counting sees it).
  let lastAnchor = null;
  const hostAnchored = (el, host, depth) => {
    depth = depth || 0;
    if (depth === 0) anchorRefusedByBudget = false;
    if (!host) return null;
    if (depth > 8) { anchorRefusedByBudget = true; return null; }
    // A host already named this walk costs a lookup, not an attempt; the budget is charged for
    // naming a host, which is the part that walks the page.
    if (!anchorByHost.has(host) && ++anchorAttempts > _ANCHOR_ATTEMPTS) { anchorRefusedByBudget = true; return null; }
    let tails = [];
    if (el.id) {
      const raw = String(el.id);
      if (!_FORGEABLE.test(raw)) {
        const esc = window.CSS && CSS.escape ? CSS.escape(raw) : null;
        tails.push(esc === raw && raw === raw.trimEnd() ? '#' + esc : attr('id', raw));
      }
    }
    tails = tails.concat(structuralTails(el));
    let ctrl = null;
    for (const t of tails) { if (scopedResolvesTo(host, t, el)) { ctrl = t; break; } }
    if (!ctrl) return null;
    let hostSel = null;
    let hostTrail = [];
    if (anchorByHost.has(host)) {
      const cached = anchorByHost.get(host);
      hostSel = cached.sel;
      hostTrail = cached.trail;
      if (!hostSel && cached.budget) anchorRefusedByBudget = true;
    } else {
      const budgetBefore = anchorRefusedByBudget;
      // naturalSelector reports its cause through shared state; the control's own cause is already
      // settled by the time we get here and must survive naming the host.
      const why = naturalWhy;
      const inconclusive = checkInconclusive;
      hostSel = naturalSelector(host);
      // A host with an identity of its own is named by it or not at all: marking one whose identity
      // could not be verified would hand out a handle on the one page where uniqueness cannot be
      // checked. A host with no identity is marked in the light DOM like any other control there --
      // mintOn refuses to write inside a root by construction, so a host that is itself
      // component-hosted is anchored through its own host instead.
      if (!hostSel && naturalWhy === 'anonymous') {
        // A host that is itself a listed control already carries this walk's marker; reuse it
        // rather than re-entering mintOn, which would count the same marker twice.
        const prior = mintedOn.find((r) => r.el === host);
        if (prior) {
          hostSel = attr('data-tv3', prior.m);
          // The controls anchored on it are bound to a record of their own so losing the marker
          // drops them too; the marker itself is counted by the host's record, not again here.
          anchorsMinted.push({ rec: null, el: host, m: prior.m, fresh: false, shared: true });
        }
      }
      if (!hostSel && naturalWhy === 'anonymous') {
        const writtenBefore = markersWritten;
        hostSel = mintOn(host);
        if (hostSel) {
          let m = null;
          try { m = host.getAttribute('data-tv3'); } catch (e) { m = null; }
          anchorsMinted.push({ rec: null, el: host, m: m, fresh: markersWritten > writtenBefore });
        }
      }
      // A host that could not be marked (it lives in a root) or whose own id is reused by a sibling
      // instance is anchored through ITS host in turn, which can scope either.
      if (hostSel) hostTrail = [{ sel: hostSel, target: host }];
      if (!hostSel && (naturalWhy === 'anonymous' || naturalWhy === 'duplicated')) {
        const outer = hostOf(host);
        if (outer && outer !== host) {
          hostSel = hostAnchored(host, outer, depth + 1);
          if (hostSel) hostTrail = lastAnchor ? lastAnchor.trail : [];
        }
      }
      naturalWhy = why;
      checkInconclusive = inconclusive;
      // A refusal reached through the recursion may be the depth bound, which a control whose own
      // host this is would not hit; only a top-level or successful answer is worth remembering. A
      // refusal that was a budget stays a budget on every later hit.
      if (hostSel || depth === 0) anchorByHost.set(host, { sel: hostSel, trail: hostTrail, budget: !hostSel && anchorRefusedByBudget && !budgetBefore });
    }
    if (!hostSel) return null;
    // Naming the host may have written to it, and a component can re-render its root on any
    // attribute change: the tail was verified before that write, so it is verified again after,
    // or a replacement control would inherit this one's label and state.
    if (!scopedResolvesTo(host, ctrl, el)) return null;
    const sel = hostSel + ' ' + ctrl;
    if (sel.length > _ANCHORED_MAX_LEN) { anchorRefusedByBudget = true; return null; }
    // The whole chain that produced the selector, each link verified where it was taken, so the
    // record can be re-validated link by link: the tail under its host, and the host by its own name.
    lastAnchor = { sel: sel, trail: [{ scope: host, ctrl: ctrl, target: el }].concat(hostTrail) };
    return sel;
  };
  // The caption of a component's control is usually slotted from the host's light DOM, so the
  // control's own innerText is empty. Read the slot's assigned content first; when the control is
  // the only one in its root, the host's composed text is that control's caption.
  const slottedText = (el, host) => {
    let t = '';
    let slots;
    try { slots = el.querySelectorAll('slot'); } catch (e) { slots = []; }
    for (const sl of slots) {
      let nodes;
      try { nodes = sl.assignedNodes({ flatten: true }); } catch (e) { continue; }
      for (const n of nodes) t += ' ' + (n.nodeType === 1 ? (n.innerText || '') : (n.textContent || ''));
    }
    t = t.replace(/\s+/g, ' ').trim();
    if (t) return t;
    let root = null;
    try { root = Node.prototype.getRootNode.call(el); } catch (e) { return ''; }
    if (!root || root.nodeType !== 11) return '';
    let peers;
    try { peers = root.querySelectorAll(q); } catch (e) { return ''; }
    if (peers.length !== 1 || peers[0] !== el) return '';
    // Only a host whose light DOM is bare text is a caption; a card slotting headings and a body
    // beside its one icon button would otherwise hand that button the whole card as its name.
    let textOnly = true;
    try { for (const n of host.childNodes) { if (n.nodeType !== 3) { textOnly = false; break; } } } catch (e) { return ''; }
    if (!textOnly) return '';
    return String(host.innerText || '').replace(/\s+/g, ' ').trim().slice(0, _RETAIN_WIDTH);
  };
  // Controls inside a component that we could not name, split by CAUSE: these need different
  // fixes, and one merged tally would send the follow-up after the wrong one.
  //   anonymous    -- no id/name/data-testid at all
  //   duplicated   -- has one, but shadow encapsulation lets every instance reuse it, so the
  //                   cross-root count is >1 and no unscoped selector can single this one out
  //   unverifiable -- has one, but a root threw, so the count could not be taken
  //   unsafe       -- has one carrying a character that could forge a payload line
  // A duplicated ID is recovered by anchoring on the host (`#host #ctrl`), which is the shape a
  // design system produces; a name or testid reused across instances is not, and neither is the rest.
  // Records named by a marker we wrote, re-checked after the walk: a later element can mutate an
  // earlier one, and an element's own attributeChangedCallback can move our marker onto a peer.
  // Registered before the record is built, so a throw between the two still reaches that check.
  const mintedOn = [];
  const elOfRec = new Map();
  // What a record reported that would change its MEANING: properties, which no MutationObserver
  // records, the ARIA state attributes, and the naming attributes. Text is not fingerprinted -- it
  // is witnessed and answered by re-resolving the record, so a countdown that rewrites its own
  // caption keeps its listing while an aria-label rewritten to another action does not.
  const stampOfRec = new Map();
  const fingerprint = (el) => {
    try {
      return [
        // Sliced at the width the record retains: a change in any byte the rendered line depends on
        // (its masking reads the whole retained value) must invalidate the record.
        el.checked === true, el.type === 'password' ? '' : String(el.value || '').slice(0, _RETAIN_WIDTH), el.disabled === true,
        el.getAttribute('aria-checked'), el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'), el.getAttribute('aria-expanded'),
        el.getAttribute('aria-valuenow'),
        el.getAttribute('aria-label'), el.getAttribute('aria-labelledby'), el.getAttribute('title'), el.getAttribute('placeholder'),
        el.getAttribute('aria-disabled'), el.readOnly === true, el.required === true, el.hidden === true, el.getAttribute('aria-hidden'),
      ].join('\u0001');
    } catch (e) { return null; }
  };
  // Hosts marked to anchor component controls. `anchorsMinted` collects the hosts one hostAnchored
  // call marked; `anchorRecords` keeps every such record with the controls it anchors, so losing
  // the host's marker after the walk drops each of those controls, not the host.
  const anchorsMinted = [];
  const anchorRecords = [];
  let unnamedAnonymous = 0;
  let unnamedBudget = 0;
  let unnamedDuplicated = 0;
  let unnamedUnverifiable = 0;
  let unnamedUnsafe = 0;
  let i = 0;
  let dropped = 0;
  // Two counters, deliberately: hiddenKept bounds the retention work and is spent the moment a
  // control passes the styled-proxy gate, while hiddenListed is what the digest note claims. They
  // diverge whenever a retained control is dropped later for having no selector that names it.
  let hiddenKept = 0;
  let hiddenListed = 0;
  let phantomDropped = 0;
  let truncated = 0;
  let truncatedInComponents = 0;
  let lastGroup = '';
  let groupTotal = 0;
  const _PHANTOM_TEXT_TYPES = /^(?:text|search|email|tel|url|number|password|date|datetime-local|month|week|time)$/;
  // Our own witness for the walk: every marker write can run page code, synchronously or through
  // the page's own MutationObservers after we yield. Anything it changed is re-validated below;
  // an unchanged page pays nothing beyond the connection check.
  let _witness = null;
  const _witnessed = [];
  try {
    // Delivered records are consumed by the callback, so they are kept here and joined with
    // whatever is still queued when the walk asks.
    _witness = new MutationObserver((recs) => { for (const m of recs) _witnessed.push(m); });
    const opts = { subtree: true, childList: true, attributes: true, characterData: true };
    for (const root of allRoots) { try { _witness.observe(root, opts); } catch (e) {} }
  } catch (e) { _witness = null; }
  // v1's hasHorizontallyScrolledAncestor (domUtils.js): a scrolled overflow-x container keeps its
  // off-window columns on the page, so an off-canvas center inside one must not drop the control.
  const _hScrolledAncestor = (node) => {
    // Climb via the prototype getter, not node.parentElement: a <form> exposes named controls as own
    // properties, so <input name="parentElement"> makes form.parentElement that input -- a
    // form<->input 2-cycle that would loop this walk forever and hang the whole page.evaluate.
    for (let p = _parentOf.call(node); p; p = _parentOf.call(p)) {
      // scrollLeft via the prototype getter too: a <form> with <input name="scrollLeft"> would
      // otherwise shadow it with an always-truthy element and fake a scrolled ancestor.
      if (_scrollLeftOf.call(p)) {
        const ox = window.getComputedStyle(p).overflowX;
        if (ox === 'auto' || ox === 'scroll') return true;
      }
    }
    return false;
  };
  // v1 isElementVisible (domUtils.js) force-marks a native form control inside an open shadow root
  // as visible even when CSS hides it: web-component libraries hide the native input via
  // visibility:hidden / off-canvas positioning behind a styled overlay the user actually clicks.
  // Mirror that carve-out so the two gates above do not drop such a control. A closed dropdown host
  // (aria-expanded="false") and a closed combobox-filter sibling are the exceptions v1 still hides.
  const _shadowForcedVisible = (node) => {
    let root = null;
    try { root = Node.prototype.getRootNode.call(node); } catch (e) { return false; }
    if (!(root instanceof ShadowRoot)) return false;
    const tag = String(node.tagName || '').toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return false;
    if (node.disabled) return false;
    if (tag === 'input' && String(node.type || '').toLowerCase() === 'hidden') return false;
    const host = root.host;
    if (host && host.getAttribute('aria-expanded') === 'false') return false;
    if (node.getAttribute('role') === 'combobox') {
      const prev = node.previousElementSibling;
      if (prev && prev.getAttribute('aria-expanded') === 'false') return false;
    }
    return true;
  };
  // Does a display:contents host actually render visible content? Mirrors v1 isElementVisible's
  // display:contents recursion (domUtils.js): a rendered child is a non-empty visible text node, a
  // visible on-canvas element, or a nested display:contents wrapper that itself renders. Depth-bounded.
  const _contentsRenders = (node, depth) => {
    if (depth > 4) return false;
    for (let c = _firstChildOf.call(node); c; c = _nextOf.call(c)) {
      const k = _nodeTypeOf.call(c);
      if (k === 3) {
        // v1 isVisibleTextNode: a text node renders iff its range has a positive, on-canvas box --
        // so font-size:0 / clipped text (non-empty but zero-area) does not count.
        if (_normText(_contentOf.call(c)).length === 0) continue;
        let tr = null;
        try { const rng = document.createRange(); rng.selectNode(c); tr = rng.getBoundingClientRect(); } catch (e) { tr = null; }
        if (tr && tr.width > 0 && tr.height > 0 && (tr.left + tr.width) / 2 + window.scrollX >= 0) return true;
        continue;
      }
      if (k !== 1) continue;
      const cs = window.getComputedStyle(c);
      if (cs.display === 'contents') { if (_contentsRenders(c, depth + 1)) return true; continue; }
      // visibility !== 'visible' catches collapse too, matching v1's isElementStyleVisibilityVisible.
      if (cs.visibility !== 'visible' || _unseen(c)) continue;
      const cr = _bcr.call(c);
      if ((cr.left + cr.width) / 2 + window.scrollX < 0 && !_hScrolledAncestor(c)) continue;
      return true;
    }
    return false;
  };
  for (let idx = 0; idx < els.length; idx++) {
   const el = els[idx].el;
   const host = els[idx].host;
   let mintedValue = null;
   let minted = null;
   const anchorRecs = [];
   lastAnchor = null;
   // A form exposes its named controls as its own properties, so <input name="tagName"> makes
   // el.tagName that input. Every read below can therefore be a clobbered non-function, and the
   // loop is inside page.evaluate: one throw costs the whole element list, not one element.
   try {
    const r = el.getBoundingClientRect();
    // A native form control inside an open shadow root is force-kept by v1 regardless of CSS/position
    // (web-component overlay pattern), so it skips the two new own-element gates. And v1 judges a
    // native checkbox/radio by its PARENT rather than the control itself (domUtils.js) -- the
    // visually-hidden consent/option pattern -- so for those the gates below are applied to the parent.
    const _elTag = String(el.tagName || '').toLowerCase();
    const _elType = String(el.type || '').toLowerCase();
    const ownGated = !_shadowForcedVisible(el);
    let gateEl = el, gr = r;
    if (_elTag === 'input' && (_elType === 'checkbox' || _elType === 'radio')) {
      const gp = _parentOf.call(el);
      if (gp) { gateEl = gp; gr = _bcr.call(gp); }
    }
    // Off-canvas gate, mirroring v1 isElementVisible (domUtils.js): an element whose horizontal
    // center sits left of the page is off-screen and not interactable, unless a horizontally
    // scrolled ancestor explains it. X only, never Y -- an overflow ancestor makes Y unreliable, so
    // a below-the-fold control (positive center-x) stays listed. Scoped to non-zero-rect elements
    // like v1 (whose center_x check is only reached for a non-zero rect), so the zero-size
    // skinned-proxy carve-out below still runs for an off-screen-positioned skinned control.
    const centerX = (gr.left + gr.width) / 2 + window.scrollX;
    if (ownGated && gr.width !== 0 && gr.height !== 0 && centerX < 0 && !_hScrolledAncestor(gateEl)) { continue; }
    // v1's isElementStyleVisibilityVisible (domUtils.js) drops a control whose own computed
    // visibility is not 'visible'. Scoped to non-zero-rect elements so the zero-size skinned-proxy
    // carve-out below still runs; visibility is read per-element, so a visibility:visible child of a
    // hidden ancestor is kept. A native checkbox/radio judges the parent here instead of itself.
    if (ownGated && gr.width !== 0 && gr.height !== 0 && window.getComputedStyle(gateEl).visibility !== 'visible') { continue; }
    let hidden = false;
    if (r.width === 0 || r.height === 0) {
      // Design systems skin a native SELECT/checkbox/radio/file input at zero size behind a styled
      // proxy widget. Keep only that narrow shape, and only with a visible label pointing at it —
      // a genuinely hidden button/link/text-input is still dropped, same as before.
      const tag = el.tagName;
      const type = String(el.type || '').toLowerCase();
      const skinnable = tag === 'SELECT' || (tag === 'INPUT' && (type === 'checkbox' || type === 'radio' || type === 'file'));
      if (skinnable && _visibleProxy(el)) {
        if (hiddenKept >= 40) { dropped++; continue; }
        hidden = true;
        hiddenKept++;
      } else if (!_unseen(el) && _contentsRenders(el, 0)) {
        // A display:contents host has a zero rect of its own but is not hidden -- its rendered
        // children carry it, matching v1's isElementVisible. _unseen's only non-rect-gated false
        // path is display:contents, so this reaches exactly that case; genuinely hidden zero-rect
        // controls (display:none/visibility:hidden/opacity:0/aria-hidden) still drop below. Keep it
        // only when it actually renders visible content, as v1's recursion does -- an empty,
        // all-hidden, or all-off-canvas host is a phantom.
      } else {
        continue;
      }
    }
    // Tree-scoped for the same reason as _VISIBLE_PROXY_JS: the shadow walk feeds this loop
    // elements whose label id lives in their own root, not in the document.
    let lbRoot = null;
    try { lbRoot = Node.prototype.getRootNode.call(el); } catch (e) { lbRoot = null; }
    const byId = (attr) => {
      const id = el.getAttribute(attr);
      const n = id && lbRoot && lbRoot.getElementById ? lbRoot.getElementById(String(id).trim().split(/\s+/)[0]) : null;
      return n ? (n.innerText || '').trim() : '';
    };
    // The name the page gives the control, placeholder excluded: a placeholder is a hint shared by
    // every field of a template, not a name, so it does not count as one below.
    let strongLabel = (el.getAttribute('aria-label') || '').trim();
    if (!strongLabel && el.labels) {
      for (const l of el.labels) { strongLabel = (l.innerText || '').trim(); if (strongLabel) break; }
    }
    if (!strongLabel) strongLabel = byId('aria-labelledby');
    if (!strongLabel) strongLabel = (el.innerText || '').trim();
    let slottedName = false;
    if (!strongLabel && host) { strongLabel = slottedText(el, host); slottedName = !!strongLabel; }
    // A text control the page itself hides from assistive tech, takes out of the tab order and
    // leaves unnamed is one no person can reach; a non-zero box does not make it a field.
    const isTextLike = el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && _PHANTOM_TEXT_TYPES.test(String(el.type || '').toLowerCase()));
    const unnamed = !strongLabel && !['placeholder', 'aria-labelledby', 'title'].some((a) => (el.getAttribute(a) || '').trim());
    if (isTextLike && el.getAttribute('aria-hidden') === 'true' && el.getAttribute('tabindex') === '-1' && unnamed) {
      phantomDropped++;
      continue;
    }
    let selector = naturalSelector(el);
    if (!selector) {
      // We do not write inside a shadow root. Setting a marker there is a mutation of the
      // component's own subtree, and every mechanism that wrote one and then tried to manage the
      // consequences failed: the mark provokes the re-render that destroys it, and because the mark
      // IS the handle we hand out it cannot move off-DOM. Verifying it needed a wait, every clock
      // belongs to the page, and a fixed wait was accurate under 50 ms and silently wrong past it.
      // Worse, a marker that churns every observe makes the payload differ every turn, which
      // defeats the loop's perception-stall terminator -- so the page burned the whole budget where
      // the base engine terminated cleanly. Not writing restores that behavior exactly. A control
      // with an id, name or data-testid of its own is unaffected, which is the ordinary case.
      if (host) {
        anchorsMinted.length = 0;
        anchorRefusedByBudget = false;
        lastAnchor = null;
        if (naturalWhy === 'duplicated' || naturalWhy === 'anonymous') selector = hostAnchored(el, host);
        // A host marked during this attempt is accounted for whether or not the attempt produced a
        // selector: a marker nobody is bound to still has to be counted, and re-checked, after the walk.
        for (const a of anchorsMinted) { a.ctrls = []; mintedOn.push(a); anchorRecords.push(a); }
        if (selector) {
          for (const a of anchorRecords) { if (selector.indexOf(attr('data-tv3', a.m)) === 0) { a.ctrls.push({ el: el, rec: null }); anchorRecs.push(a); } }
        } else {
          if (anchorRefusedByBudget) unnamedBudget++;
          else if (naturalWhy === 'duplicated') unnamedDuplicated++;
          else if (naturalWhy === 'unverifiable') unnamedUnverifiable++;
          else if (naturalWhy === 'unsafe') unnamedUnsafe++;
          else unnamedAnonymous++;
          continue;
        }
      } else {
        const writtenBefore = markersWritten;
        selector = mintOn(el);
        if (!selector) { dropped++; continue; }
        mintedValue = el.getAttribute('data-tv3');
        minted = { rec: null, el: el, m: mintedValue, fresh: markersWritten > writtenBefore };
        mintedOn.push(minted);
      }
    }
    // The placeholder ranks below every real name (strongLabel already starts with aria-label) and
    // travels separately as a hint: a format placeholder ('dd/mm/yyyy') is what makes the value typeable.
    const placeholder = (el.getAttribute('placeholder') || '').trim();
    let label = strongLabel || placeholder;
    if (!label) label = (el.type === 'password' ? '' : el.value || '').trim();
    if (!label) label = (el.getAttribute('title') || '').trim();
    const role = el.getAttribute('role');
    // el.type is only trustworthy where the UA normalises it to a known keyword. On INPUT, BUTTON
    // and SELECT it is a reflected enum; on <a>, <link>, <embed>, <object> and <source> it hands
    // back the raw attribute, so `type` there is a page-controlled string that reached the rendered
    // line -- and a MIME type is noise to the model anyway.
    const _typed = el.tagName === 'INPUT' || el.tagName === 'BUTTON' || el.tagName === 'SELECT';
    // Label, placeholder and value are capped generously here, not at their display widths: each is
    // masked for payload-minted signed URLs Python-side, which needs the WHOLE URL to match by
    // provenance before the display cap lands. A tighter cap here would truncate the URL past
    // recognition and leak its signing tail.
    const rec = { i, tag: el.tagName.toLowerCase(), type: (_typed && el.type) || null, selector, label: label.slice(0, _RETAIN_WIDTH) };
    if (placeholder && placeholder !== label) rec.placeholder = placeholder.slice(0, _RETAIN_WIDTH);
    if (hidden) rec.hidden = true;
    // A widget role is what the element IS -- a <div role="switch"> renders as a bare div otherwise,
    // and the model cannot tell it from decoration. The role travels with its state below, or it is
    // not worth surfacing: an on switch and an off one that read identically invite toggling the
    // wrong way and calling it success.
    // Only ever one of the roles we queried for. The page's raw attribute never reaches the
    // rendered line: it is page-controlled, and a newline in it would print a second, fabricated
    // element line for a selector that does not exist.
    if (role && _WIDGET_ROLES.indexOf(String(role)) !== -1) rec.role = String(role);
    if (el.tagName === 'SELECT') rec.options = Array.from(el.options).map((o) => o.value + '|' + o.text).slice(0, 60);
    if (el.type === 'password') { if (el.value) rec.value = '(hidden)'; } else if (el.value) rec.value = String(el.value).slice(0, _RETAIN_WIDTH);
    // ARIA defines switch as a checkbox variant carrying the same aria-checked, so it belongs here.
    if (el.type === 'checkbox' || el.type === 'radio') rec.checked = !!el.checked;
    else if (role === 'checkbox' || role === 'radio' || role === 'switch') {
      // Presence-gated like `selected` below: an absent aria-checked, or "mixed", is a state the
      // page never stated, and reporting checked=False for an ON switch is the exact wrong-way
      // toggle this enumeration exists to prevent.
      const ck = el.getAttribute('aria-checked');
      if (ck === 'true' || ck === 'false') rec.checked = ck === 'true';
    }
    const selected = el.getAttribute('aria-selected');
    if ((role === 'tab' || role === 'option') && (selected === 'true' || selected === 'false')) rec.selected = selected === 'true';
    if (role === 'spinbutton') {
      const now = el.getAttribute('aria-valuenow');
      if (now !== null && !rec.value) rec.value = String(now).slice(0, _RETAIN_WIDTH);
    }
    if (el.getAttribute('aria-required') === 'true' || el.required) rec.required = true;
    const isChoice = el.type === 'checkbox' || el.type === 'radio' || role === 'checkbox' || role === 'radio';
    // Read .validity, never checkValidity(): that dispatches an 'invalid' event and perception must
    // not mutate the page. Checkbox/radio .value is the static attribute ("on"), so they are excluded.
    const ai = el.getAttribute('aria-invalid');
    if (ai && ai !== 'false') rec.invalid = true;
    // willValidate excludes readonly/disabled fields the agent cannot fix; password is excluded so
    // validationMessage (which can echo the typed value) never leaks it.
    else if (!isChoice && el.type !== 'password' && el.value && el.willValidate && !(el.form && el.form.noValidate) && el.validity && !el.validity.valid) {
      rec.invalid = (el.validationMessage || '').slice(0, _RETAIN_WIDTH) || true;
    }
    // Flag typeahead/autocomplete inputs so the model treats them as combobox fills instead of typing
    // raw text that never registers as a valid selection (type() also auto-commits them). See _IS_AUTOCOMPLETE_JS.
    if (_isAutocomplete(el)) rec.autocomplete = true;
    // Attach the question text for controls whose meaning lives in nearby non-interactive text
    // (radio/checkbox groups, fields named by nothing or only by a placeholder) so the agent can
    // answer without fetching raw HTML. Deduped against the previous element to keep grouped
    // options compact; capped per page so a long form cannot turn this into a second DOM dump.
    if (isChoice || strongLabel.length < 3) {
      // The description is the last rung: it is routinely a per-field hint ("This field is
      // required") shared by every field, which would name nothing and dedupe to nothing.
      // The record carries the text at the masking width; the budget, the dedupe and the
      // name-vs-description comparison all stay at the 200-char display width.
      const gtFull = (_groupText(el, isChoice) || byId('aria-describedby')).slice(0, _RETAIN_WIDTH);
      const gt = gtFull.slice(0, 200);
      // A slotted caption is compared at the 140 width it used to be stored at.
      const nameLength = slottedName ? Math.min(strongLabel.length, 140) : strongLabel.length;
      if (gt && gt.length > nameLength && gt !== lastGroup && groupTotal + gt.length <= _GROUP_TEXT_TOTAL_CAP) {
        rec.group = gtFull;
        lastGroup = gt;
        groupTotal += gt.length;
      }
    }
    const pressed = el.getAttribute('aria-pressed');
    if (pressed === 'true' || pressed === 'false') rec.pressed = pressed === 'true';
    if (minted !== null) minted.rec = rec;
    for (const a of anchorRecs) { for (const c of a.ctrls) { if (c.el === el) c.rec = rec; } }
    if (hidden) hiddenListed++;
    // A submit or button input is named by its caption, and a caption is what a refusal beside it
    // repeats; a field's own control is the only thing a wrapper holds.
    const captioned = rec.tag === 'input' && /^(?:submit|button|reset|image)$/.test(rec.type || '');
    if ((rec.tag === 'input' && !captioned) || rec.tag === 'select' || rec.tag === 'textarea') labelOfControl.set(el, rec.label.slice(0, 140).replace(/\s+/g, ' ').trim());
    out.push(rec);
    elOfRec.set(rec, el);
    stampOfRec.set(rec, { fp: fingerprint(el), anchor: lastAnchor && lastAnchor.sel === selector ? lastAnchor : null });
    if (++i > 250) {
      // Count what the budget actually cost, not what is left in the array: a zero-size match would
      // have been skipped anyway, and counting it overstates the loss on any page carrying a hidden
      // dialog. Shadow matches are tallied separately because they are appended last and are
      // therefore the first thing the budget starves.
      for (let k = idx + 1; k < els.length; k++) {
        try {
          const r2 = els[k].el.getBoundingClientRect();
          if (r2.width === 0 || r2.height === 0) continue;
          truncated++;
          if (els[k].host) truncatedInComponents++;
        } catch (e) { /* unreadable: not a control we could have listed either */ }
      }
      break;
    }
   } catch (e) { dropped++; continue; }
  }
  // Let the page's own MutationObservers deliver (they are queued, not synchronous), then check
  // marker ownership -- a callback can move a marker onto a peer -- and ask the witness what changed. Our marker writes are our own; anything else means a record may describe
  // an element that was replaced, mutated in place, or re-identified, so every record is re-resolved
  // to the element it was built for and dropped if it no longer denotes exactly that element.
  // `await null` yields through the intrinsic promise machinery: the page cannot replace it the way
  // it can replace setTimeout, and every observer notification queued during the walk is ahead of
  // this continuation in the microtask queue.
  // Bounded: a callback may defer its own work another turn, and each turn is answered by one
  // more yield; a page that keeps queueing forever is left to the witness, which records what it did.
  for (let turn = 0; turn < 16; turn++) await null;
  // A marker we wrote can be gone by the end of the walk: a component that mirrors attributes moves
  // it onto a peer, and the element we named is then addressed by a selector matching nothing. One
  // attribute read per named element, no re-query -- a natural selector cannot be invalidated this
  // way, so only minted ones are checked.
  for (const rem of mintedOn) {
    let still = null;
    try { still = rem.el.getAttribute('data-tv3'); } catch (e) { still = null; }
    // A record never built (the element threw mid-walk) was never handed out either.
    if (rem.ctrls) {
      const lost = still !== rem.m;
      for (const c of rem.ctrls) {
        // A later host's marking can re-render an earlier component, detaching a control that
        // passed its own check; its record would then describe a replacement the tail resolves to.
        let connected = false;
        try { connected = _isConnected.call(c.el); } catch (e) { connected = false; }
        if (!lost && connected) continue;
        const at = c.rec === null ? -1 : out.indexOf(c.rec);
        if (at !== -1) { out.splice(at, 1); labelOfControl.delete(c.el); dropped++; }
      }
      if (lost && !rem.shared) { if (rem.fresh) markersWritten--; else markersReused--; }
      continue;
    }
    if (rem.rec === null || still !== rem.m) {
      const at = rem.rec === null ? -1 : out.indexOf(rem.rec);
      if (at !== -1) { out.splice(at, 1); labelOfControl.delete(rem.el); dropped++; }
      if (rem.fresh) markersWritten--; else markersReused--;
    }
  }
  // Any marker write during the walk can have run page code that re-rendered an EARLIER record's
  // element, whatever named it: a record whose element is no longer connected describes a control
  // that no longer exists, while its selector may resolve to a replacement in a different state.
  let mutated = false;
  if (_witness) {
    try {
      for (const m of _witness.takeRecords()) _witnessed.push(m);
      _witness.disconnect();
      mutated = _witnessed.some((m) => !(m.type === 'attributes' && m.attributeName === 'data-tv3'));
      // A root attached during the walk was never observed, so a change inside it is invisible to
      // the witness; a root count that moved is treated as a change.
      if (!mutated) {
        let rootsNow = 0;
        const stack = [document];
        while (stack.length) {
          const r = stack.pop();
          rootsNow++;
          let kids;
          try { kids = r.querySelectorAll('*'); } catch (e) { mutated = true; break; }
          for (const k of kids) {
            let sr = null;
            try { sr = k.shadowRoot; } catch (e) { continue; }
            if (sr && sr.nodeType === 11) stack.push(sr);
          }
        }
        if (rootsNow !== allRoots.length) mutated = true;
      }
    } catch (e) { mutated = true; }
  }
  for (let k = out.length - 1; k >= 0; k--) {
    const rec = out[k];
    const el = elOfRec.get(rec);
    let connected = true;
    if (el) { try { connected = _isConnected.call(el); } catch (e) { connected = false; } }
    let ok = connected;
    const stamp = ok && el ? stampOfRec.get(rec) : null;
    // A property write (checked, value) leaves no mutation record, so the fingerprint is always
    // compared; re-resolving the selector is paid only when the witness saw the tree change.
    if (ok && stamp && stamp.fp !== fingerprint(el)) ok = false;
    if (ok && stamp && mutated) {
      if (stamp.anchor) {
        for (const link of stamp.anchor.trail) {
          if (link.ctrl) { if (!scopedResolvesTo(link.scope, link.ctrl, link.target)) { ok = false; break; } }
          else { checkInconclusive = false; if (!resolvesTo(link.sel, link.target) && !checkInconclusive) { ok = false; break; } }
        }
      } else { checkInconclusive = false; ok = resolvesTo(rec.selector, el) || checkInconclusive; }
    }
    if (!ok) { labelOfControl.delete(el); out.splice(k, 1); dropped++; }
  }
  // Page-text digest: outcome states (submission confirmations, rejection banners, validation
  // summaries) live in non-interactive nodes the element list can never carry. Three sources in
  // priority order — ARIA status channels (uncapped within the 900 total), class/id-named message
  // blocks (600, or 300 past what ARIA spent, whichever is larger, still inside the 900), then
  // headings (whatever the 900 leaves) — never a body-text
  // dump, so the digest stays bounded and can't regrow the context that transcript compaction
  // bounds. All three carry page-controlled text at the same trust level as element labels.
  const texts = [];
  // The digest is deduped and budgeted at its 300-char display width, and `text` carries exactly that.
  // An entry cut by that width also travels whole (at the masking width) in `textFull`, so Python can
  // mask a minted URL in it before capping the line.
  const fullText = new Map();
  let textTotal = 0;
  let textFull = false;
  let textDropped = 0;
  // `limit` is a cumulative reservation: a channel stops at its limit so the channels after it keep
  // a floor of the 900 total instead of being starved by whichever channel ran first.
  const pushText = (t, limit = 900) => {
    const full = (t || '').replace(/\s+/g, ' ').trim().slice(0, _RETAIN_WIDTH);
    t = full.slice(0, 300);
    if (!t) return;
    // Containment dedupe, richer message wins: an alert's text re-surfaces inside its heading's
    // parent text, and a terse early entry ("Saved") must not suppress a later superset
    // ("Saved — confirmation #A1B2") — supersets REPLACE their contained entries.
    if (texts.some((s) => s.includes(t))) return;
    const kept = texts.filter((s) => !t.includes(s));
    const keptTotal = kept.reduce((total, s) => total + s.length, 0);
    if (keptTotal + t.length > limit) { textFull = textFull || limit >= 900; textDropped++; return; }
    texts.length = 0; texts.push(...kept, t); textTotal = keptTotal + t.length;
    fullText.set(t, full);
  };
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // Isolated: a hostile page's throwing accessor (fingerprinting scripts poison innerText and
  // friends) must degrade to "no digest", never take element perception down with it.
  try {
    // ~= matches ARIA fallback role lists like role="alert status"; = would silently skip them.
    for (const root of allRoots) {
      if (textFull) break;
      for (const el of root.querySelectorAll('[role~=alert],[role~=status],[aria-live=polite],[aria-live=assertive],output')) {
        if (textFull) break;
        if (visible(el)) pushText(el.innerText);
      }
    }
    // Rejection messages most sites render as a plain styled block with no ARIA; class/id naming is
    // the only signal. A block with several form fields is a container (skipped unless short); one
    // with at most one field up to the digest total is a message; longer is prose.
    let messageCandidates = 0;
    // A prior channel (ARIA) can already occupy the shared total before this loop starts; spend is
    // measured relative to that starting point so this channel still gets its own floor.
    const blockStart = textTotal;
    const blockLimit = Math.min(900, Math.max(600, blockStart + 300));
    // Real validation errors live inside the form; cookie banners and alert dropdowns sit above it
    // in DOM order and must not spend the budget before the form's own error is seen.
    // Only "error" is matched against id: id-named chrome (#alert-count, #cookie-warning) is the
    // false-positive family this channel is most exposed to, and "error" is the one id that isn't.
    const msgSel = '[class*="error" i],[class*="invalid" i],[class*="alert" i],[class*="warning" i],[id*="error" i]';
    // A component renders its validation summary in its own shadow root, where a document query
    // cannot reach it -- the blindness already lifted for controls, on the channel that carries
    // refusal messages. Walked like the ARIA and heading channels above, so this asks the page no
    // new question, only the same one of more roots.
    // A bucket past its cap has to give something up, and a plain prefix gives up the end -- which
    // is where a page renders the outcome of a submission, after the fields it is about. The last
    // 50 are kept alongside the first 200 rather than instead of part of them, so nothing a 200
    // prefix read is given up, and the loss moves into the middle of the walk. The loop below reads
    // up to 250 for the same reason: one full bucket must fit. The other bucket's tail is reached
    // only when the first bucket is small, which is the same priority the concat order states.
    const formMsgs = { head: [], tail: [] };
    const otherMsgs = { head: [], tail: [] };
    const hold = (bucket, item) => {
      if (bucket.head.length < 200) { bucket.head.push(item); return; }
      bucket.tail.push(item);
      if (bucket.tail.length > 50) bucket.tail.shift();
    };
    for (const root of allRoots) {
      // nodeType 11 first: Document has no `host`, so `root.host` would hit the HTML named-property
      // getter and <form name="host"> would supply one.
      const host = root.nodeType === 11 ? root.host || null : null;
      // One component that refuses this query costs its own root, not the digest: an uncaught throw
      // reaches the outer catch and empties every channel. A root that throws for every query is
      // already disclosed by the marker gather; one that throws only for this selector is not, and
      // is not defended here.
      let cands;
      try { cands = root.querySelectorAll(msgSel); } catch (e) { continue; }
      // The iteration is inside the try because a root can hand back a non-iterable instead of
      // throwing, which is the same attack one line later.
      try {
        for (const el of cands) {
          // Per element, because reading `closest` off one is a page-controlled call: a form
          // exposes its named controls over its own methods, so <input name="closest"> turns it
          // into a throw. Uncaught it reaches the digest-wide catch, and an emptied digest is
          // indistinguishable from a page that rendered no messages at all.
          try {
            // Being inside a form is what separates a validation message from page chrome, and it is
            // a structural fact rather than a guess about which roots tend to hold content. Ranking
            // component blocks above light-DOM ones instead would bury a page's own banner under the
            // cookie-consent and chat widgets that also ship as components.
            hold((el.closest('form') || inFormRoots.has(root)) ? formMsgs : otherMsgs, { el: el, host: host });
          } catch (e) { continue; }
        }
      } catch (e) { continue; }
    }
    // A per-field state wrapper (`field--has-error`, `field--no-error`) matches this selector and its
    // text is just the control's own name, so it spends the channel's budget on what the element list
    // already carries -- enough of them and the page's real message never fits. Read off the records
    // already built, so recognising them asks the page nothing.
    const listedLabels = new Set();
    for (const r of out) {
      // Sliced to the label's display width: the messages below are compared at that width.
      const lb = (r.label || '').slice(0, 140).replace(/\s+/g, ' ').trim();
      if (lb) listedLabels.add(lb);
    }
    // Stricter than visible(): this channel's selector is broad and site chrome is routinely present
    // but hidden, whereas an ARIA live region styled invisible is not a pattern worth the extra reads.
    const visibleText = (el) => {
      if (!visible(el) || el.closest('[aria-hidden="true"]')) return false;
      const r = el.getBoundingClientRect();
      if (r.right <= 0 || r.bottom <= 0) return false;
      const cs = getComputedStyle(el);
      return cs.visibility !== 'hidden' && cs.opacity !== '0';
    };
    // The suppression above compares byte for byte, so a wrapper that renders its control's label
    // beside a required-field marker does not match it and spends the budget on a name the element
    // list already carries. Comparing with the decoration stripped is only safe as an ORDERING:
    // dropping on it would also swallow a real message that is nothing but a listed label and
    // punctuation ("Payment declined!" beside a "Payment declined" button), and a dropped banner
    // reads exactly like a page that never rendered one. So a near-match is offered to the budget
    // after every message the page did not build out of a label, and what the budget then does with
    // it -- take it, fold it into an entry that already holds it, or count it as dropped -- is what
    // it would have done at its place in the walk.
    const decoration = /^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu;
    // A word marker ("(required)", "optional") is letters, so stripping edge punctuation leaves
    // it in place and the wrapper still reads as a message. Listed labels include button captions,
    // so "Sign in required" beside a "Sign in" button would read as a wrapper too: the marker is
    // decoration only at an edge, and only on a block that holds the very control the rest names.
    const wordMarker = /^(?:required|optional)(?:[^\p{L}\p{N}]+|$)|(?:^|[^\p{L}\p{N}]+)(?:required|optional)$/giu;
    const nearLabel = (t, src) => {
      const trimmed = t.replace(decoration, '');
      if (listedLabels.has(trimmed.slice(0, 140))) return true;
      const unmarked = trimmed.replace(wordMarker, '').replace(decoration, '');
      if (!unmarked || unmarked === trimmed) return false;
      const key = unmarked.slice(0, 140);
      // A host can shadow querySelectorAll; a block that cannot be asked is not known to be a wrapper.
      try {
        for (const c of src.querySelectorAll('input,select,textarea')) {
          if (labelOfControl.get(c) === key) return true;
        }
      } catch (e) { return false; }
      return false;
    };
    const deferred = [];
    const takeCand = (cand, mayDefer) => {
      const el = cand.el;
      if (!visibleText(el)) return;
      // A component's message block is `<div class="alert"><slot></slot></div>`: the words are
      // slotted from the host's light DOM, so the block's own innerText is empty and the host
      // carries them. Same fallback the heading channel below uses, and the field count comes
      // from whichever node supplied the text.
      let t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      let src = el;
      if (!t && cand.host) {
        t = (cand.host.innerText || '').replace(/\s+/g, ' ').trim();
        src = cand.host;
      }
      if (!t) return;
      // Compared at the width labels are stored at, so a truncated one still matches.
      if (listedLabels.has(t.slice(0, 140))) return;
      if (mayDefer && nearLabel(t, src)) { deferred.push(cand); return; }
      if (t.length <= 300 || (t.length <= 900 && src.querySelectorAll('input,select,textarea').length < 2)) pushText(t, blockLimit);
    };
    for (const cand of formMsgs.head.concat(formMsgs.tail, otherMsgs.head, otherMsgs.tail)) {
      if (textFull || textTotal - blockStart >= 600 || ++messageCandidates > 250) break;
      // This selector set is broad, so one poisoned element degrades to "skip it", not to an
      // emptied digest (the outer catch is for the narrow ARIA channel).
      try { takeCand(cand, true); } catch (e) { continue; }
    }
    // Already counted against the candidate cap on the pass that deferred them, so this pass is
    // bounded by that same cap. Each is offered to pushText like any other entry, with no budget
    // short-circuit, so the dedupe, the length gate and the drop count apply exactly as they would
    // have at its place in the walk.
    for (const cand of deferred) {
      try { takeCand(cand, false); } catch (e) { continue; }
    }
    // role=heading alongside h1-h3: a component's heading is a custom element, so its tag name
    // carries no signal and only the ARIA role does.
    for (const root of allRoots) {
      if (textFull) break;
      // nodeType 11 first: Document has no `host`, so `root.host` would hit the HTML
      // named-property getter and <form name="host"> would supply one.
      const host = root.nodeType === 11 ? root.host || null : null;
      for (const h of root.querySelectorAll('h1,h2,h3,[role=heading]')) {
        if (textFull) break;
        if (!visible(h)) continue;
        // A component heading is `<h2><slot></slot></h2>`: the slotted text belongs to the host's
        // light DOM, so the heading's own innerText is empty and the host carries the words.
        let ht = (h.innerText || '').replace(/\s+/g, ' ').trim();
        if (!ht && host) ht = (host.innerText || '').replace(/\s+/g, ' ').trim();
        // A short parent is a banner/panel whose body text carries the message; a large parent would
        // drag in unrelated content, so the heading stands alone.
        const pt = h.parentElement ? (h.parentElement.innerText || '').replace(/\s+/g, ' ').trim() : '';
        pushText(pt && pt.length <= 300 ? pt : ht);
      }
    }
  } catch (e) { texts.length = 0; textDropped = 0; }
  // Cross-origin iframe PRESENCE: an anti-bot/captcha widget lives in one, and main-frame element
  // perception can never list its contents — record host + signature so the model can see the gate
  // exists. Attributes only, never the frame's document (page.frames-based traversal was considered
  // and rejected: presence is the contract here, not cross-frame reach). Same visibility rule as
  // elements, so hidden tracking pixels stay out. Isolated like the digest above.
  // `failed` and `unread` are this channel's own bookkeeping, not a question put to the page: on the
  // section that reports gates, "found none" and "could not look" must not render as one sentence.
  const iframeInfo = { total: 0, inComponents: 0, entries: [], failed: false, unread: 0 };
  try {
    const sig = /captcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|datadome|perimeterx|verify you are human|security challenge/i;
    // A design system packages the widget inside its own shadow root, where a document query cannot
    // reach it. Walked like the ARIA, message and heading channels above, so this asks the page no
    // new question, only the same one of more roots.
    for (const root of allRoots) {
      // One root that refuses this query costs its own root, not the channel: an uncaught throw
      // reaches the outer catch and empties every entry, including main-document ones a
      // document-only scan reported fine. The iteration is inside the try because a root can hand
      // back a non-iterable instead of throwing, which is the same attack one line later.
      try {
        for (const f of root.querySelectorAll('iframe')) {
          // Walking more roots means reading more frames, so one poisoned frame inside a component
          // must not cost the roots already scanned.
          try {
            const r = f.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            // A frame with srcdoc renders the inline (same-origin) document; its src is a dead fallback.
            if (f.hasAttribute('srcdoc')) continue;
            const src = f.getAttribute('src') || '';
            let u;
            try { u = new URL(src, location.href); } catch (e) { continue; }
            if ((u.protocol !== 'http:' && u.protocol !== 'https:') || u.origin === location.origin) continue;
            const ttl = (f.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
            const isCaptcha = sig.test(src + ' ' + ttl);
            // Counted once every throwable read has succeeded: incrementing earlier put a frame in
            // `total` and in `unread` at once, so the two summed past the page's real count.
            iframeInfo.total++;
            if (root !== document) iframeInfo.inComponents++;
            if (iframeInfo.entries.length < 8) {
              iframeInfo.entries.push({ host: u.host.slice(0, 80), title: ttl, captcha: isCaptcha });
            } else if (isCaptcha) {
              // Spending all 8 slots on ad embeds and dropping the one frame this channel exists to
              // report defeats the channel, so a gate displaces an embed; the cap and total hold.
              const at = iframeInfo.entries.findIndex((e) => !e.captcha);
              if (at !== -1) iframeInfo.entries[at] = { host: u.host.slice(0, 80), title: ttl, captcha: isCaptcha };
            }
          } catch (e) { iframeInfo.unread++; continue; }
        }
      } catch (e) { iframeInfo.unread++; continue; }
    }
  } catch (e) { iframeInfo.total = 0; iframeInfo.inComponents = 0; iframeInfo.entries.length = 0; iframeInfo.unread = 0; iframeInfo.failed = true; }

  return JSON.stringify({ url: location.href, title: document.title, text: texts, textFull: texts.map((t) => { const f = fullText.get(t); return f && f !== t ? f : null; }), textTruncated: textFull, textDropped: textDropped, iframes: iframeInfo, dropped: dropped, truncated: truncated, truncatedInComponents: truncatedInComponents, unnamedAnonymous: unnamedAnonymous, unnamedBudget: unnamedBudget, unnamedDuplicated: unnamedDuplicated, unnamedUnverifiable: unnamedUnverifiable, unnamedUnsafe: unnamedUnsafe, unreadableRoot: sawUnreadableRoot, undiscoveredRoots: undiscoveredRoots, rootCount: allRoots.length - 1, hiddenListed: hiddenListed, phantomDropped: phantomDropped, markersMinted: markersWritten, markersReused: markersReused, pageMutated: mutated, elements: out });
}
"""
)


def observe_js(retain_width: int = OBSERVE_RETAIN_WIDTH_MIN) -> str:
    return _OBSERVE_JS_TEMPLATE.replace("__OBSERVE_RETAIN_WIDTH__", str(int(retain_width)), 1)


_OBSERVE_JS = observe_js()


def _menu_mark_parts(options: list[dict[str, Any]], cap: int) -> list[str]:
    parts = []
    for o in (options or [])[:cap]:
        # option texts are page-controlled and land in the LLM transcript — same sanitation as filenames
        text = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(o.get("text", "")))
        parts.append(f'[data-tv3-menu="{o.get("n")}"] {text!r}')
    return parts


def _menu_open_note(found: dict[str, Any], selector: str, *, clicked_row: bool = False) -> str:
    count = int(found.get("count") or 0)
    parts = _menu_mark_parts(found.get("options") or [], 15)
    overflow = f" (+{count - len(parts)} more — re-observe for the full list)" if count > len(parts) else ""
    # Naming the raw selector would contradict the next sentence when the caller IS a menu row:
    # this note has just renumbered every data-tv3-menu, so the selector clicked to get here is one
    # of the ones it is about to declare stale.
    closer = "the row you just clicked" if clicked_row else selector
    return (
        f"This click opened a menu of {count} options: {'; '.join(parts)}{overflow}. To select one, click "
        f'its [data-tv3-menu="N"] selector NOW — clicking {closer} again or elsewhere closes the menu '
        "and destroys these options. These numbers are freshly assigned: any data-tv3-menu selector "
        "from an earlier result now points at a different row or at nothing."
    )


async def _categories_note(page: Any, selector: str) -> str | None:
    # Enrichment for the typeahead no-match path only: never lets the classifier's own failure become
    # the tool's failure, since a crash here would replace a real (if unhelpful) error with a worse one.
    try:
        found = await page.evaluate(_FIND_CATEGORIES_JS, {"field": selector})
    except Exception:
        return None
    if not found or not found.get("count"):
        return None
    items = "; ".join(_menu_mark_parts(found.get("categories") or [], 8))
    return (
        f"Some rows near this field carry an expand affordance and may be categories whose options are "
        f"nested rather than shown in the flat list: {items}. If one could contain your value, click its "
        '[data-tv3-menu="N"] selector to reveal its options, then re-observe to confirm what the click '
        "did before relying on it. These numbers are freshly assigned and change whenever the list "
        "re-renders: act on the newest data-tv3-menu list, not an earlier one."
    )


def _spec(
    name: str, description: str, params: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=params, handler=handler)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


# Mirror v1's default inter_action_delay (get_wait_time default 0.5 → random.uniform(base, 2*base)).
# v3's tool factory has no task/workflow context to thread the org-tunable wait_config, so the
# default constant is used; widen the factory only if per-org tuning is later shown to matter.
_UPLOAD_SUBMIT_DELAY_BASE_S = 0.5


async def _settle_after_upload(page: Any) -> None:
    """Let the page finish processing a just-uploaded file before the next action runs.

    v1 already settles after every upload; v3's tool loop can otherwise dispatch the upload and
    the next action back-to-back in one turn, before upload UI (spinner/progress/XHR) has mounted.
    Reuses v1's settle (`_wait_for_upload_processing`), but best-effort: v1 lets an unclassified
    settle error propagate, whereas here the upload has already succeeded, so a settle failure is
    logged and swallowed rather than turned into a tool error.
    """
    from skyvern.webeye.actions.handler import _wait_for_upload_processing

    try:
        # engine_selection is intentionally omitted (v3's tool factory has no engine context); its
        # only effect is error classification inside the settle, and the catch-all below tolerates
        # any settle error regardless.
        await _wait_for_upload_processing(page)
    except Exception:
        LOG.info("post-upload settle failed, continuing", exc_info=True)


async def _upload_submit_delay() -> None:
    """Small randomized delay after an upload, mirroring v1's per-action inter_action_delay default,
    so the upload and the following action are not dispatched in the same instant."""
    await asyncio.sleep(random.uniform(_UPLOAD_SUBMIT_DELAY_BASE_S, _UPLOAD_SUBMIT_DELAY_BASE_S * 2))


# A genuine file upload dispatches at least one of these: the API call that mints the upload handle
# and/or the write to storage. resource_type is limited to xhr/fetch so page analytics pings, image
# beacons, navigations, and static asset loads never register as upload activity.
_UPLOAD_ACTIVITY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_UPLOAD_ACTIVITY_RESOURCE_TYPES = frozenset({"xhr", "fetch"})


class _UploadActivityProbe:
    """Counts upload-like network dispatches during a file_upload: set_input_files populating the input
    at the Playwright layer does not prove the site registered the file (post-navigation the change
    handler may be unwired, so the site dispatches nothing). Counting request dispatch — not completion —
    is the earliest signal that the site reacted at all; a dispatched-but-failed upload still counts."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._count = 0

    def _on_request(self, request: Any) -> None:
        try:
            if (
                request.method in _UPLOAD_ACTIVITY_METHODS
                and str(request.resource_type).lower() in _UPLOAD_ACTIVITY_RESOURCE_TYPES
            ):
                self._count += 1
        except Exception:
            pass

    def start(self) -> None:
        try:
            self._page.on("request", self._on_request)
        except Exception:
            LOG.info("taskv3 upload-activity probe could not attach", exc_info=True)

    def stop(self) -> None:
        try:
            self._page.remove_listener("request", self._on_request)
        except Exception:
            pass

    def saw_upload(self) -> bool:
        return self._count > 0


# Rendered text across the document and every open shadow root. Used as a before/after pair around
# set_input_files: a filename that was absent and is now present could only have been written by the
# site's own file-handling code, which is the one thing a silent no-op (or ambient network noise) can
# never produce.
_PAGE_TEXT_JS = (
    r"""() => {
  const _shadowRoots = """
    + _SHADOW_ROOTS_JS
    + r""";
  let out = '';
  for (const root of _shadowRoots(document)) {
    try {
      // Rendered text only: textContent would count hidden nodes, <script> and <style>. A shadow
      // root has no innerText itself, so read each element child — but only rendered ones, since
      // innerText on an element that is not rendered (a <style>, a hidden chip) is its textContent.
      const tops = root === document ? [document.body] : Array.from(root.children);
      for (const el of tops) {
        if (!el || typeof el.innerText !== 'string') continue;
        if (!(el.getClientRects && el.getClientRects().length > 0)) continue;
        out += ' ' + el.innerText;
      }
    } catch (e) {}
  }
  return out;
}"""
)


# Words a site uses when it names a file it refused. A veto only: a spurious match turns a confirmation
# into a recoverable error, never the reverse; a rejection phrased outside this list is the known miss.
_UPLOAD_REJECTION_WORDS = re.compile(
    r"\b(error|invalid|reject\w*|unsupported|fail\w*|unsuccessful|exceed\w*|denied|blocked|declined|removed|"
    r"discarded|corrupt\w*|wrong|issue\w*|too\s+(large|big)|must be|unable|"
    r"(not|never|won['’]t|will not|do not)(\s+\w+){0,2}\s+(allow|support|accept|upload|permit|attach|save)\w*|"
    r"(can|could|would|is|was|do|did|does|has|have)\s?(n['’]?|['’])t|cannot|try(\s+\w+){0,2}\s+again)\b",
    re.IGNORECASE,
)
_FILENAME_MENTION_CHARS = 240


async def _page_rendered_text(page: Any) -> str | None:
    """The page's rendered text across the document and open shadow roots; None when it cannot be read."""
    try:
        text = await page.evaluate(_PAGE_TEXT_JS)
    except Exception:
        LOG.info("taskv3 file_upload page-text readback failed", exc_info=True)
        return None
    return text if isinstance(text, str) else None


def _mentions_filename(text: str, filename: str) -> bool:
    """Whole-token mention of the staged file's full name (the browser reports exactly this basename as
    File.name, so it is what a site renders). A name joined to more name characters ("old-cv.pdf",
    "cv.pdf.bak") is a different file, not this one."""
    name = os.path.basename(filename).strip()
    if not name:
        return False
    return re.search(r"(?<![\w.\-])" + re.escape(name) + r"(?![\w.\-])", text, re.IGNORECASE) is not None


def _newly_rendered_lines(before: str, after: str) -> list[str]:
    seen = {line.strip() for line in before.splitlines()}
    return [line.strip() for line in after.splitlines() if line.strip() and line.strip() not in seen]


async def _input_holds_file(el: Any) -> bool:
    """Playwright-layer readback that set_input_files populated the control — proves the file attached to
    the input element, not that the site registered it. Fail-open: an unreadable control must never turn a
    real upload into a false negative."""
    try:
        count = await el.evaluate("e => (e && e.files) ? e.files.length : 0")
        return bool(count) and int(count) > 0
    except Exception:
        LOG.info("taskv3 file-input populate readback failed, assuming populated", exc_info=True)
        return True


# Counts fields holding in-progress state a reload would discard, piercing shadow roots. Unlike the
# pre-submit form serializer it COUNTS file inputs (files.length > 0) — an attached file is exactly the
# progress the same-URL reload guard exists to protect — and it skips hidden fields (site-managed, always
# present) so their presence alone never trips the guard.
_FILLED_STATE_JS = (
    "(() => { const _q = " + _ROOT_QUERY_JS + "; let n = 0; for (const el of _q.all('input,textarea,select')) { "
    "const t = (el.type || '').toLowerCase(); "
    "if (t === 'hidden') continue; "
    "if (t === 'file') { if (el.files && el.files.length > 0) n++; continue; } "
    "if (t === 'checkbox' || t === 'radio') { if (el.checked) n++; continue; } "
    "if (el.value) n++; } return n; })()"
)


async def _count_filled_fields(page: Any) -> int:
    """How many fields hold state a reload would wipe (incl. an attached file). Fail-open to 0: a probe
    failure must never let this guard block a navigation."""
    try:
        return int(await page.evaluate(_FILLED_STATE_JS))
    except Exception:
        LOG.info("taskv3 filled-state probe failed, treating page as empty", exc_info=True)
        return 0


def build_browser_tools(
    page_provider: PageProvider,
    *,
    downloads_dir: str | None = None,
    organization_id: str | None = None,
    resolve_typed_text: Callable[[str], Any] | None = None,
    opaque_refs: OpaqueUrlRefs | None = None,
    vision_enabled: bool = True,
) -> list[ToolSpec]:
    """Raw-browser tools that resolve their page from `page_provider` on every call.

    `vision_enabled` gates the on-demand `look` tool: it is offered only when the run's model can
    actually receive the screenshot it produces (a non-vision model drops it before the request), so
    the tool is never advertised to a model that cannot see its output."""

    def _mask_refs(text: str) -> str:
        # A signed payload URL masked to a token in the payload must not reappear verbatim through a
        # free-text emit surface (observe's url= line, get_html, a download error) and get retyped by
        # the model. Masking is by provenance: only URLs the payload masker minted are rewritten, so a
        # live-page URL the model reasons about is never touched. No refs (page-free) → identity.
        return opaque_refs.mask(text) if opaque_refs is not None else text

    def _observe_js() -> str:
        # Retain exactly what the masker can recognise past the widest display window.
        window = opaque_url_echo_window(opaque_refs.refs.values()) if opaque_refs is not None else 0
        return observe_js(max(OBSERVE_RETAIN_WIDTH_MIN, OBSERVE_FIELD_DISPLAY_MAX + window))

    def _resolve_text(text: str) -> str:
        # Workflow credential values reach the model only as secret placeholders; resolve them to the
        # real value at fill time (the same boundary the step engine uses). Fail open to the literal.
        if resolve_typed_text is None:
            return text
        try:
            resolved = resolve_typed_text(text)
        except Exception:
            LOG.warning("taskv3 typed-text resolution failed; typing the literal text", exc_info=True)
            return text
        return resolved if isinstance(resolved, str) else text

    # INVARIANT: holds at most one page, written only by the preflight wrapper immediately before
    # its handler runs and consumed by that handler's single _resolve_page call; the wrapper clears
    # it in a finally. Relies on the loop dispatching tool calls sequentially — a concurrent
    # dispatcher or a twice-resolving handler must replace this handoff, not reuse it.
    _prefetched_page: list[Any] = []

    # Per-run set-of-marks from the most recent look(): mark index -> {handle, tag, label}. A
    # fresh look replaces it (marks renumber), and act-by-mark resolves mark=N against it at act time.
    _look_manifest: dict[int, dict[str, Any]] = {}
    # Opaque-id aliases, run-scoped and stable: the same emitted selector maps to the same alias for
    # the whole run, like opaque_url_ tokens, so the model never handles the raw identifier.
    _alias_for_selector: dict[str, str] = {}
    _selector_for_alias: dict[str, str] = {}

    def _alias_for(selector: str) -> str:
        if not _OPAQUE_ID_RUN_RE.search(selector):
            return selector
        alias = _alias_for_selector.get(selector)
        if alias is None:
            alias = f'[data-tv3-ref="{len(_alias_for_selector) + 1}"]'
            _alias_for_selector[selector] = alias
            _selector_for_alias[alias] = selector
        return alias

    def _mask_aliases(text: str) -> str:
        # Real selector -> alias, and a WHOLE `id="<raw>"` attribute in markup -> the alias attribute.
        # Anchored on purpose: the raw id also appears as a substring of other attributes, hrefs and
        # page values, and rewriting those would corrupt what the model reads. Longest selector first
        # so a host-anchored selector is not half-masked by its host's own alias.
        for real, alias in sorted(_alias_for_selector.items(), key=lambda kv: -len(kv[0])):
            if real in text:
                text = text.replace(real, alias)
            for m in _SELECTOR_ID_COMPONENTS_RE.finditer(real):
                attr = "id" if m.group(3) else m.group(1)
                raw = m.group(4) if m.group(3) else m.group(2)
                if raw and _OPAQUE_ID_RUN_RE.search(raw) and raw in text:
                    text = re.sub(r"(?<=\s)" + attr + '="' + re.escape(raw) + '"', alias[1:-1], text)
        return text

    def _with_alias_resolution(handler: ToolHandler) -> ToolHandler:
        async def wrapped(args: dict[str, Any]) -> ToolResult:
            selector = args.get("selector")
            alias_match = _ALIAS_SELECTOR_RE.match(selector) if isinstance(selector, str) else None
            if alias_match:
                real = _selector_for_alias.get(f'[data-tv3-ref="{alias_match.group(1)}"]')
                if real is None:
                    return ToolResult.error(
                        f"{alias_match.group(0).strip()} is not a selector from the latest observe — re-observe and "
                        "use a selector from the new observation"
                    )
                args = {**args, "selector": real}
            result = await handler(args)
            if _alias_for_selector and isinstance(result.content, str):
                masked = _mask_aliases(result.content)
                if masked != result.content:
                    result = ToolResult(result.status, masked, result.data, result.screenshots)
            return result

        return wrapped

    _look_count = [0]  # per-run look() invocations, capped at _LOOK_MAX_PER_RUN
    # The (canonical URL, filled-field count) of the last same-URL reload the destructive-nav guard
    # refused. A repeat to that URL confirms intent and is allowed — but only if the at-risk state has
    # not GROWN since (else a file attached after the refusal would be wiped by a stale confirmation).
    _reload_confirm_pending: list[tuple[str, int] | None] = [None]

    async def _resolve_page() -> tuple[Any, ToolResult | None]:
        # Single-use handoff from the preflight wrapper so a preflighted call resolves the page
        # once, not twice (each resolution is a must_get_working_page with its recovery path).
        page = _prefetched_page.pop() if _prefetched_page else await page_provider()
        if page is None:
            return None, ToolResult.error(PAGE_UNAVAILABLE_ERROR)
        return page, None

    async def _url(page: Any) -> str:
        try:
            return page.url
        except Exception:
            return ""

    def _is_context_teardown(exc: BaseException) -> bool:
        # Playwright's wording when a navigation destroys the context an evaluate was running in.
        # Matched by message because the driver raises a generic Error for it. Last resort only:
        # the driver also rewrites some unrelated protocol errors into this message.
        return "execution context was destroyed" in str(exc).lower()

    async def observe(_args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        # Bound the one perception call so a wedged page can't hang the turn indefinitely.
        raw = await asyncio.wait_for(page.evaluate(_observe_js()), timeout=30)
        data = json.loads(raw) if isinstance(raw, str) else raw
        elements = data.get("elements", [])
        omitted_anonymous = data.get("unnamedAnonymous") or 0
        omitted_duplicated = data.get("unnamedDuplicated") or 0
        omitted_unverifiable = data.get("unnamedUnverifiable") or 0
        omitted_unsafe = data.get("unnamedUnsafe") or 0
        omitted_budget = data.get("unnamedBudget") or 0
        omitted_in_components = (
            omitted_anonymous + omitted_duplicated + omitted_unverifiable + omitted_unsafe + omitted_budget
        )
        if omitted_in_components:
            # Sizes the capability this deliberately gives up, split by cause because the causes have
            # different fixes: `duplicated` is answered by host-anchored selectors with executor-side
            # verification (the SKY-14710 family), `anonymous` only by that same path, and neither by
            # another in-root identity mechanism. Merging them would over-report one and under-report
            # the other, and the follow-up would be chosen off the wrong number.
            LOG.info(
                "taskv3 observe omitted component controls it could not name",
                omitted_in_components=omitted_in_components,
                omitted_anonymous=omitted_anonymous,
                omitted_duplicated=omitted_duplicated,
                omitted_unverifiable=omitted_unverifiable,
                omitted_unsafe=omitted_unsafe,
                omitted_budget=omitted_budget,
                listed=len(elements),
            )
        # Compact rendering keeps the persistent-conversation prefix small (cost is ~linear in it).
        raw_url = _mask_refs(str(data.get("url") or ""))
        # Stripping forgery chars is not truncation: only the cap changes what the URL points at, so
        # the note is measured against the sanitized length rather than the raw one.
        sanitized_url = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", raw_url)
        shown_url = sanitized_url[:OBSERVE_URL_MAX_CHARS]
        # Every other cap in this payload names itself; a URL cut mid-query-string looks complete and
        # is a different, invalid URL.
        url_note = (
            f" (url truncated from {len(sanitized_url)} chars)" if len(sanitized_url) > OBSERVE_URL_MAX_CHARS else ""
        )
        lines = [f"url={shown_url}{url_note} title={data.get('title')!r} ({len(elements)} interactive elements)"]
        hidden_kept = data.get("hiddenListed") or 0
        if hidden_kept:
            lines.append(
                f"note: {hidden_kept} native control(s) hidden behind styled proxies are listed with [hidden-native]"
            )
        phantom_dropped = data.get("phantomDropped") or 0
        if phantom_dropped:
            lines.append(
                f"note: {phantom_dropped} unreachable input(s) omitted (aria-hidden, out of the tab order, unlabeled)"
            )

        # Mask before capping: the masker matches a payload-minted URL by provenance over its WHOLE
        # text, so a display cap applied first (as the JS once did) leaves a fragment it cannot
        # recognise, signing tail included.
        def _field(raw: object, width: int) -> str:
            return _mask_refs(str(raw))[:width]

        texts = data.get("text") or []
        texts_full = data.get("textFull") or []
        for i, t in enumerate(texts):
            full = texts_full[i] if i < len(texts_full) else None
            lines.append(f"text: {_field(full or t, OBSERVE_DISPLAY_WIDTHS['text'])!r}")
        text_dropped = data.get("textDropped") or 0
        if text_dropped:
            # A capped digest must say it was capped: silently showing the first N reads as "that is all".
            lines.append(f"note: {text_dropped} more page message(s) did not fit the text digest")
        iframe_info = data.get("iframes") or {}
        iframe_entries = iframe_info.get("entries") or []
        iframe_unread = iframe_info.get("unread") or 0
        # Every branch states the scope it actually covered: a confident absence is read as "no gate
        # here" on the page most likely to have one.
        #
        # Not a frame count: one unreadable region is a single frame or a whole root, and a root holds
        # any number of frames.
        if iframe_unread:
            iframe_hedge = f"{iframe_unread} unreadable region(s) may hold more"
        elif data.get("undiscoveredRoots"):
            # A root the walk never found holds frames that are missing from `total` without the scan
            # knowing they exist. Keyed off that count and not `unreadableRoot`, which is page-wide and
            # several failures unrelated to the root walk also set.
            iframe_hedge = "part of this page could not be read, so there may be more"
        else:
            iframe_hedge = ""
        if iframe_info.get("failed"):
            # Never "none" and never a count: the scan did not run, so the page's frames are unknown
            # rather than absent.
            lines.append("iframes: the frame scan failed on this page; frame presence is unknown")
        elif iframe_entries:
            total = iframe_info.get("total", len(iframe_entries))
            parts = []
            for f in iframe_entries:
                flag = "[captcha] " if f.get("captcha") else ""
                title = f" {f['title']!r}" if f.get("title") else ""
                parts.append(f"{flag}{_digest_token(f.get('host') or '?', 80)}{title}")
            overflow = f" (+{total - len(iframe_entries)} more)" if total > len(iframe_entries) else ""
            # `total` counts what was readable, so without this the sentence is an absolute claim
            # about a page some of which was never read.
            lines.append(
                f"iframes: {total} cross-origin in the page and its open component roots "
                "(contents NOT listed here and NOT reachable by selector): "
                + "; ".join(parts)
                + overflow
                + (f"; {iframe_hedge}" if iframe_hedge else "")
            )
        elif iframe_hedge:
            lines.append(f"iframes: none found; {iframe_hedge}")
        elif data.get("rootCount"):
            # Only where a component root actually exists. On a page with no components the line
            # says nothing the element list doesn't, and it would cost a line on every observe of
            # every run.
            lines.append("iframes: none in the page or its open component roots")
        dropped = data.get("dropped") or 0
        if dropped:
            # Without this an element list emptied by unreadable elements is indistinguishable from
            # a page that genuinely has no controls.
            lines.append(f"note: {dropped} element(s) could not be described and are not listed below")
        if data.get("unreadableRoot"):
            # The condition itself, not just its consequences: a root that throws makes uniqueness
            # unverifiable everywhere, so unnamed elements are dropped rather than given a name we
            # could not check. Left unsaid, that reads as a page with fewer controls than it has.
            lines.append(
                "note: part of this page could not be queried, so selector uniqueness could not be "
                "verified here; elements we could not name are not listed"
            )
        if data.get("textTruncated"):
            # Every other cap in this payload names itself. This one binds far more often now that
            # component-rendered live regions feed the digest, and it evicts page headings silently.
            lines.append("note: page-text digest hit its budget; some page text is not shown")
        truncated = data.get("truncated") or 0
        if truncated:
            # A page of components can spend the whole budget before reaching its submit control, and
            # a list that stops silently reads as the complete set of what the page offers. No remedy
            # is suggested because none exists: the list comes from querySelectorAll, so it is
            # viewport-independent and scrolling returns the identical list and the identical count.
            note = f"note: {truncated} more element(s) matched but exceeded the element budget and are not listed"
            in_components = data.get("truncatedInComponents") or 0
            if in_components:
                # The budget is spent light-DOM-first so the page's own submit control survives a
                # page of components — which means component internals are what it starves.
                note += f", {in_components} of them inside components"
            lines.append(note)
        if omitted_in_components:
            # A statement about OUR limitation, not about the page: naming these would mean writing
            # into the component's own root, which provokes the re-render that destroys the mark. No
            # remedy is offered because there is none the model can perform — re-observing returns
            # the same omission. Split by cause: saying "no id of their own" about a control that has
            # one, and whose id is merely reused by a sibling instance, tells the model something
            # false about the page to describe a limitation of ours.
            why = []
            if omitted_anonymous:
                why.append(f"{omitted_anonymous} have no id, name or data-testid of their own")
            if omitted_duplicated:
                why.append(
                    f"{omitted_duplicated} have one that is reused by another instance of the same "
                    "component, so it does not identify a single element"
                )
            if omitted_unverifiable:
                why.append(f"{omitted_unverifiable} could not be verified because a component root was unreadable")
            if omitted_unsafe:
                why.append(f"{omitted_unsafe} carry an identifier we cannot render safely")
            if omitted_budget:
                why.append(f"{omitted_budget} exceeded the naming budget for this page")
            lines.append(
                f"note: {omitted_in_components} control(s) inside components are not listed because we "
                f"have no selector that identifies them: {'; '.join(why)}"
            )

        for e in elements:
            extra = ""
            if e.get("value"):
                extra += f" value={_field(e['value'], OBSERVE_DISPLAY_WIDTHS['value'])!r}"
            if e.get("placeholder"):
                extra += f" placeholder={_field(e['placeholder'], OBSERVE_DISPLAY_WIDTHS['placeholder'])!r}"
            if e.get("options"):
                extra += f" options={e['options']}"
            if e.get("checked") is not None:
                extra += f" checked={e['checked']}"
            if e.get("selected") is not None:
                extra += f" selected={e['selected']}"
            if e.get("pressed") is not None:
                extra += f" pressed={e['pressed']}"
            if e.get("required"):
                extra += " *required"
            if e.get("invalid"):
                extra += (
                    " *invalid"
                    if e["invalid"] is True
                    else f" *invalid={_field(e['invalid'], OBSERVE_DISPLAY_WIDTHS['invalid'])!r}"
                )
            if e.get("autocomplete"):
                extra += " [autocomplete→use select_combobox]"
            if e.get("hidden"):
                if e.get("type") == "file":
                    extra += " [hidden-native: styled proxy; file_upload works on it directly]"
                elif e.get("tag") == "select":
                    extra += " [hidden-native: styled proxy; select_option acts on it directly]"
                else:
                    extra += " [hidden-native: styled proxy; click acts on it directly]"
            if e.get("group"):
                extra += f" group={_field(e['group'], OBSERVE_DISPLAY_WIDTHS['group'])!r}"
            # INVARIANT for this line and every line above it: no page-controlled byte reaches the
            # digest un-escaped, and the header's count and the number of element lines come from the
            # same list. Everything else here is either repr'd or a literal. `type` is the trap --
            # on <input> the UA normalises it, but HTMLAnchorElement.type reflects the raw attribute,
            # so <a href type="x&#10;[#pay] button 'Confirm'"> printed a second, fabricated element
            # line for a selector that does not exist. `role` is whitelisted at the source; `tag` and
            # `type` are stripped of anything that could end a line or reorder it.
            kind = _digest_token(e["tag"], 40)
            if e.get("type"):
                kind += "/" + _digest_token(e["type"], 40)
            elif e.get("role"):
                kind += "/" + _digest_token(e["role"], 40)
            lines.append(
                f"[{_alias_for(e['selector'])}] {kind} "
                f"{_field(e.get('label', ''), OBSERVE_DISPLAY_WIDTHS['label'])!r}{extra}"
            )
        # Counts only, for the per-call log record: every perception change that alters only what
        # this function renders is otherwise invisible to production telemetry.
        summary = {
            "text_dropped": text_dropped,
            "hidden_listed": hidden_kept,
            "phantom_dropped": phantom_dropped,
            "iframes_in_component_roots": iframe_info.get("inComponents") or 0,
            "undiscovered_roots": data.get("undiscoveredRoots") or 0,
            "omitted_unnameable": omitted_in_components,
            "invalid_fields": sum(1 for e in elements if e.get("invalid")),
            "markers_minted": data.get("markersMinted") or 0,
            "markers_reused": data.get("markersReused") or 0,
            "group_texts_found": sum(1 for e in elements if e.get("group")),
        }
        # Mask the whole rendered payload, not just url=: a signed payload ref can surface as page
        # text or a field value the model previously typed (a token resolved back to its URL), and
        # those lines would otherwise leak the signing artifact. Provenance-only, so benign page text
        # is untouched. url= is already masked before truncation above; re-masking a token is a no-op.
        return ToolResult.ok(_mask_refs("\n".join(lines)), data={"count": len(elements), "summary": summary})

    async def get_html(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if selector:
            el = await page.query_selector(selector)
            if el is None:
                return ToolResult.error(f"no element for selector {selector!r}")
            html = await el.inner_html()
            if not html:
                # Void/leaf elements have no inner HTML; their own tag+attributes are the answer,
                # not an empty string the model can't distinguish from a missing element. Best
                # effort: a navigation between the two reads must not turn "" into a tool error.
                try:
                    html = await el.evaluate("el => el.outerHTML")
                except Exception:
                    html = ""
        else:
            html = await page.content()
        # The click/type reaction gate stamps data-tv3-pre on every visible element and the skinned-click
        # probe stamps data-tv3-proxy on one label; both are internal bookkeeping, and left in place the
        # first costs a third of the truncation budget below in noise.
        html = html.replace(' data-tv3-pre="1"', "").replace(' data-tv3-proxy="1"', "")
        html = _mask_refs(html)
        if len(html) > 20000:
            return ToolResult.ok(html[:20000] + "…[truncated at 20000 chars]")
        return ToolResult.ok(html)

    def _unreachable_error(selector: str) -> ToolResult:
        return ToolResult.error(
            f"{selector} is not rendered and nothing visible stands in for it — its section is collapsed, "
            "closed or inactive, so a person could not reach this control either. Act on whatever reveals "
            "it (the section header, the step, the modal trigger), then re-observe."
        )

    def _not_editable_error(exc: _FieldNotEditable) -> ToolResult:
        if exc.read_only:
            return ToolResult.error(
                f"{exc.selector} is readonly — typing cannot change it. If it opens a list, click it and "
                "pick an option instead; otherwise act on whatever sets it."
            )
        return ToolResult.error(f"{exc.selector} is disabled — it cannot be typed into until the page enables it")

    def _covered_error(
        selector: str, occluder: dict[str, Any] | None = None, *, verb: str = "typed into"
    ) -> ToolResult:
        also = "" if verb == "clicked" else " — a person could not click it either"
        name = str((occluder or {}).get("name") or "").strip()
        layer_selector = (occluder or {}).get("selector")
        if occluder and occluder.get("invisible"):
            # The layer intercepts the pointer but paints nothing, so it is absent from the screenshot.
            # Telling the model to dismiss an overlay it can see is then a false instruction that makes
            # it flail; name the layer as invisible and point at recovery routes that do not depend on
            # seeing it. Controls are omitted on purpose: a ghost backdrop has none, and a still-present
            # named layer's controls did not dismiss it (that is why it is still here).
            if name and layer_selector:
                layer_desc = f'"{name}" ({layer_selector})'
            elif name:
                layer_desc = f'"{name}"'
            elif layer_selector:
                layer_desc = f"a layer ({layer_selector})"
            else:
                layer_desc = "a layer"
            return ToolResult.error(
                f"{selector} is covered by {layer_desc} that is INVISIBLE — it intercepts clicks but paints "
                f"nothing on screen, so you will not see it in a screenshot{also}. It is most likely a "
                "leftover backdrop from a dialog or cookie banner that was already dismissed. Do not keep "
                "trying to dismiss a visible overlay; press Escape, re-observe, or reach the field another way."
            )
        if not occluder:
            return ToolResult.error(
                f"{selector} is rendered but something else is on top of it, so it cannot be {verb}{also}. "
                "Dismiss whatever covers it (a dialog, an overlay, a cookie banner), then re-observe."
            )
        layer_desc = f'"{name}"' if name else "a layer"
        if layer_selector:
            layer_desc = f"{layer_desc} ({layer_selector})"
        parts = []
        for control in occluder.get("controls") or []:
            control_selector = control.get("selector") if isinstance(control, dict) else None
            label = str((control.get("label") if isinstance(control, dict) else "") or "").strip()
            if control_selector and label:
                parts.append(f'{control_selector} "{label}"')
            elif control_selector:
                parts.append(control_selector)
            elif label:
                parts.append(f'"{label}" (no selector — re-observe to address it)')
        if parts:
            controls_desc = "; ".join(parts)
        else:
            controls_desc = "re-observe — no controls were found on it"
        if occluder.get("truncated"):
            controls_desc += "; more controls exist (re-observe to see the rest)"
        return ToolResult.error(
            f"{selector} is covered by {layer_desc}, so it cannot be {verb}{also}. "
            # The layer may be a general modal, not just a consent wall -- these are every control
            # found on it, not confirmed dismissers, since a destructive or navigational action
            # (e.g. "Delete account") is not distinguishable here from a close/cancel button.
            f"Its controls: {controls_desc}. Pick whichever one actually closes or dismisses the "
            f"layer, then retry {selector}."
        )

    async def _probe_arg(page: Any, selector: str) -> dict[str, Any]:
        # Probes resolve per root, which cannot match a host-anchored selector whose two halves
        # straddle a shadow boundary. The executor's own engine can, so it supplies the element the
        # action will actually land on -- consulted only where the per-root lookup finds nothing.
        # Only a composed selector needs it, and hostAnchored composes with a space, so anything
        # without one keeps its single round trip. A quoted space costs a spare lookup, never a miss.
        if " " not in selector:
            return {"sel": selector, "el": None}
        try:
            element = await page.query_selector(selector)
        except Exception:
            element = None
        return {"sel": selector, "el": element}

    async def _resolve_mirrored_host_control(page: Any, selector: str) -> str:
        # Only a single compound selector can name a host by mistake; a composed (host-anchored) one
        # already points inside a root and a marker selector names exactly what observe marked.
        # Only a selector that names no tag can land on a host by mistake; a tag-qualified one already
        # says which element it means, so the page-wide walk is skipped for it.
        stripped = selector.strip()
        if (
            not stripped.startswith(("#", "["))
            or " " in _TV3_QUOTED_VALUE_RE.sub('""', stripped)
            or _TV3_MARKER_SELECTOR_RE.match(stripped)
        ):
            return selector
        try:
            named = await page.evaluate(_MIRRORED_HOST_CONTROL_JS, selector)
        except Exception:
            return selector
        if isinstance(named, str) and named:
            LOG.debug(
                "taskv3 selector resolved to a shadow host; acting on its mirrored control",
                selector=selector,
                control=named,
            )
            return named
        return selector

    async def _post_match_count(page: Any, selector: str) -> int:
        try:
            return await page.locator(selector).count()
        except Exception:
            return 1  # count unavailable → do not block, mirroring _marker_matches' fail-open

    async def _ambiguous_selector_error(page: Any, selector: str) -> ToolResult | None:
        # A host-anchored selector straddles a shadow boundary, which the per-root marker count
        # cannot see through; the executor's own engine can, so it supplies the count. Playwright's
        # actions are non-strict and would otherwise land on whichever match comes first.
        if not (_is_host_anchored_selector(selector) or _TV3_MARKER_SELECTOR_RE.match(selector.strip())):
            return None
        try:
            matches = await page.locator(selector).count()
        except Exception:
            # Left open, as the marker count is: refusing here would block every action on a page
            # whose engine hiccups, and the action's own actionability wait still applies.
            LOG.warning("taskv3 selector count unavailable; acting unverified", selector=selector)
            return None
        if matches == 1:
            return None
        if matches == 0:
            return ToolResult.error(
                f"{selector} no longer matches anything on the page — the page re-rendered since it was "
                "observed. Re-observe and act on fresh selectors from the new observation.",
                data={"page_state_changed": True},
            )
        return ToolResult.error(
            f"{selector} matches {matches} elements, so it does not identify one control. Re-observe and "
            "act on a selector from the new observation, or narrow this one until it matches exactly one."
        )

    async def _marker_matches(page: Any, selector: str) -> int:
        try:
            return int(await page.evaluate(_MARKER_MATCH_COUNT_JS, await _probe_arg(page, selector)))
        except Exception:
            return 1

    async def _clear_proxy_tags(page: Any) -> None:
        try:
            await page.evaluate(
                "() => { const _q = " + _ROOT_QUERY_JS + "; "
                "_q.all('[data-tv3-proxy]').forEach((e) => e.removeAttribute('data-tv3-proxy')); }"
            )
        except Exception:
            pass

    async def _click_reaction(
        page: Any, selector: str, pre: dict[str, Any], url_before: str, *, doc_planted: bool
    ) -> tuple[str | None, str | None]:
        # Returns (note, commit_error) — at most one set. Raises are the caller's to swallow (fail-open:
        # a probe failure must degrade to the bare pre-feature ok, never fail the click).
        opt = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(pre.get("optText") or "")) or selector
        if pre.get("isOption"):
            # Commit evidence, any one suffices: navigation, the menu closing, the option's own state
            # changing vs the post-hover baseline (multi-select menus commit WITHOUT closing), or a
            # submenu opening (a cascading option commits nothing yet — reporting the child menu beats
            # a false error).
            baseline = pre.get("optState") or ""
            sel_baseline = pre.get("optSel") or ""
            kids_baseline = pre.get("optKids")
            height_baseline = pre.get("optH")
            vis_baseline = pre.get("optVis")

            def _committed_state(after: dict[str, Any]) -> bool:
                return bool(after.get("optState")) and after.get("optState") != baseline

            def _picked(after: dict[str, Any]) -> bool:
                # One of the row's own selection attributes moved. Nothing a restructuring row does
                # can reach these, so this is commit evidence on its own.
                return bool(after.get("optSel")) and after.get("optSel") != sel_baseline

            def _grew(after: dict[str, Any]) -> bool:
                # The row got bigger of its own accord, which is the one shape where "it committed"
                # has a competitor. Child count, height, or visible descendant rows: a category whose
                # leaves pre-exist hidden and are revealed on click keeps its child COUNT and may keep
                # its height, but its visible descendant row count rises — measured within the clicked
                # row, so a sibling reveal (a real commit that also shows peers) does not trip it.
                def _up(now: Any, before: Any, by: int) -> bool:
                    ok = (int, float)
                    if isinstance(now, bool) or isinstance(before, bool):
                        return False
                    return isinstance(now, ok) and isinstance(before, ok) and before >= 0 and now - before > by

                return (
                    _up(after.get("optKids"), kids_baseline, 0)
                    or _up(after.get("optH"), height_baseline, 2)
                    or _up(after.get("optVis"), vis_baseline, 0)
                )

            async def _state_holds(state: str) -> tuple[dict[str, Any] | None, str | None]:
                # A real commit settles; self-updating content (a countdown, a live price) keeps
                # moving, so only a state that holds across two reads is evidence. The second read is
                # returned because 150ms later is the difference between measuring a CSS expansion
                # and measuring it mid-flight. Three outcomes, not two: a re-read that could not be
                # taken is neither "held" nor "moved" -- the second value names that failure.
                await asyncio.sleep(0.15)
                again, navigated = await _after_read()
                if again is None:
                    return None, "navigated" if navigated else "unreadable"
                return (again if again.get("optState") == state else None), None

            async def _after_read() -> tuple[dict[str, Any] | None, bool]:
                # Returns (read, navigated); read is None only when the probe raised. A raise is NOT
                # evidence of anything unless the page positively left -- a throwing probe, a detached
                # node, or a CDP timeout must never read as a commit. Asked in order of reliability:
                # the window token (the page itself says whether this is the same document, and
                # history.pushState cannot fool it the way it fools the URL); the URL; and only when
                # the page cannot be asked at all, the driver's own destroyed-context wording.
                try:
                    raw = await page.evaluate(_MENU_AFTER_JS, await _probe_arg(page, selector))
                except Exception as exc:
                    same_document = await _same_document()
                    if same_document is not None:
                        return None, not same_document
                    url_after = await _url(page)
                    if url_before and url_after and url_after != url_before:
                        return None, True
                    return None, _is_context_teardown(exc)
                return (raw if isinstance(raw, dict) else {}), False

            def _unverified(why: str) -> tuple[str | None, str | None]:
                return None, (
                    f"clicked option {opt!r} ({selector}) but its effect could not be verified — the {why} "
                    "read failed and the page did not navigate. The click was dispatched: do not repeat it "
                    "blindly and do not assume the selection committed; re-observe first."
                )

            async def _child_menu_note() -> str | None:
                # A row that expands a sub-list mutates ITSELF, so the fingerprint cannot tell
                # "committed" from "expanded" and the child rows can -- a cascading click that opened
                # them committed nothing yet.
                try:
                    found = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
                except Exception:
                    return None
                if isinstance(found, dict) and found.get("count"):
                    return _menu_open_note(found, selector, clicked_row=True)
                return None

            async def _state_change_note(opt_text: str, after: dict[str, Any]) -> str:
                picked = f"Selected option {opt_text!r} — its state changed (the menu stayed open)."
                # A row that did not grow cannot have expanded into itself, so nothing competes with
                # the commit reading and the probe is not worth its page walk -- which is the
                # ordinary multi-select click.
                if not _grew(after):
                    return picked
                child = await _child_menu_note()
                if not child:
                    return picked
                # It grew AND opened child rows, so a selection attribute means it did both and
                # dropping either half would be a false report. Without the "menu stayed open" clause:
                # the child note has just renumbered the markers, so what the model was holding is
                # precisely what did not stay.
                if _picked(after):
                    return f"Selected option {opt_text!r} — its state changed.\n{child}"
                return child

            async def _same_document() -> bool | None:
                # The page's own answer, or None when it cannot be asked.
                if not doc_planted:
                    return None
                try:
                    answer = await page.evaluate(_CLICK_DOC_CHECK_JS)
                except Exception:
                    return None
                return answer if isinstance(answer, bool) else None

            url_now = await _url(page)
            if url_before and url_now and url_now != url_before and await _same_document() is not True:
                # A moved URL is a navigation unless the page says it is the same document --
                # a menu that syncs its selection into the query string never left.
                return f"Selected option {opt!r} — the page navigated.", None
            after, navigated = await _after_read()
            if after is None and navigated:
                return f"Selected option {opt!r} — the page navigated.", None
            if after is not None and not after.get("stillOpen"):
                return f"Selected option {opt!r} — the menu closed.", None
            if after is not None and _committed_state(after):
                held, failure = await _state_holds(after.get("optState") or "")
                if failure == "navigated":
                    return f"Selected option {opt!r} — the page navigated.", None
                if failure == "unreadable":
                    return _unverified("state-hold")
                if held is not None:
                    return await _state_change_note(opt, held), None
            # Menus routinely close through a fade or an async server ack; declaring "did not commit"
            # off the instantaneous read would turn those healthy commits into false errors. One
            # bounded settle, only on this would-be-error path.
            await asyncio.sleep(0.6)
            settled, navigated = await _after_read()
            if settled is None:
                if navigated:
                    return f"Selected option {opt!r} — the page navigated.", None
                return _unverified("post-click" if after is None else "settle")
            if not settled.get("stillOpen"):
                return f"Selected option {opt!r} — the menu closed.", None
            if _committed_state(settled):
                held, failure = await _state_holds(settled.get("optState") or "")
                if failure == "navigated":
                    return f"Selected option {opt!r} — the page navigated.", None
                if failure == "unreadable":
                    return _unverified("state-hold")
                if held is not None:
                    return await _state_change_note(opt, held), None
            # No-commit evidence is already established: a crash of this last informational probe must
            # not fall through to the caller's fail-open bare ok (_child_menu_note swallows).
            late_child = await _child_menu_note()
            if late_child:
                return late_child, None
            return None, (
                f"clicked option {opt!r} ({selector}) but the selection did not commit — the menu is "
                "still open and unchanged. Do not repeat this click; press Enter on the option, or "
                "re-observe and try a different control."
            )
        if pre.get("menuOpen"):
            if pre.get("containsMenu"):
                # Clicked the card AROUND the menu: the center-point click may have landed on an
                # arbitrary row, so any open/closed/selected claim could be false. Say nothing.
                return None, None
            try:
                after_raw = await page.evaluate(_MENU_AFTER_JS, await _probe_arg(page, selector))
            except Exception:
                return None, None
            found = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
            if isinstance(found, dict) and found.get("count"):
                return _menu_open_note(found, selector), None
            if isinstance(after_raw, dict) and not after_raw.get("stillOpen"):
                return (
                    "Note: this click CLOSED the open menu — no option was selected. To select, click "
                    'an option\'s [data-tv3-menu="N"] selector while the menu is open.'
                ), None
            return None, None
        found = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
        if isinstance(found, dict) and found.get("count"):
            return _menu_open_note(found, selector), None
        return None, None

    async def click(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if not selector:
            return ToolResult.error("click needs a selector, or mark=N from the last look().")
        if _TV3_MARKER_SELECTOR_RE.match(selector.strip()):
            matches = await _marker_matches(page, selector)
            if matches == 0:
                # An absent marker cannot reappear without a re-observe, so Playwright's full 15s
                # actionability wait is pure loss (4x in the specimen trace). Short attach grace
                # tolerates a framework re-attaching the same node mid-render.
                try:
                    await page.wait_for_selector(selector, state="attached", timeout=1200)
                except Exception:
                    return ToolResult.error(
                        f"{selector} no longer exists on the page — element markers vanish when the "
                        "page re-renders (a closed menu destroys its options). Re-observe and act on "
                        "fresh selectors from the new observation.",
                        data={"page_state_changed": True},
                    )
                # The re-attach may have been a re-render that cloned the row, so the count is re-read.
                matches = await _marker_matches(page, selector)
            if matches > 1:
                # A clone of the marked element carries the same marker; the click would silently
                # land on whichever comes first in document order, so refuse before dispatching it.
                return ToolResult.error(
                    f"{selector} now matches {matches} elements — the page re-rendered and cloned the "
                    "marked element, so the marker no longer identifies one control. Re-observe and act "
                    "on fresh selectors from the new observation.",
                    data={"page_state_changed": True},
                )
        else:
            ambiguous = await _ambiguous_selector_error(page, selector)
            if ambiguous is not None:
                return ambiguous
        pre: dict[str, Any] | None = None
        try:
            pre_raw = await page.evaluate(_CLICK_PRECHECK_JS, await _probe_arg(page, selector))
            if isinstance(pre_raw, dict):
                pre = pre_raw
        except Exception:
            pre = None
        if pre is not None and pre.get("isOption"):
            # Playwright's click hovers first, and menus routinely restyle a row on hover — so the
            # commit baseline must be the POST-hover fingerprint, or a mere highlight would read as
            # "its state changed" commit evidence on a no-op click.
            try:
                await page.hover(selector, timeout=2000)
                hovered = await page.evaluate(_MENU_AFTER_JS, await _probe_arg(page, selector))
                if isinstance(hovered, dict) and hovered.get("optState"):
                    pre["optState"] = hovered["optState"]
                    # Every baseline the commit checks read, not some of them: whichever is left
                    # behind describes the row before the hover, so the hover's own doing -- an
                    # aria-selected mark, a row-hover toolbar that grows the row -- reads as the
                    # click's.
                    for key in ("optSel", "optKids", "optH", "optVis"):
                        if key in hovered:
                            pre[key] = hovered[key]
            except Exception:
                pass
        url_before = await _url(page)
        doc_planted = False
        if pre is not None and pre.get("isOption"):
            try:
                await page.evaluate(_CLICK_DOC_PLANT_JS)
                doc_planted = True
            except Exception:
                pass
        # One resolution for the whole pre-click phase: these run back to back with no mutation
        # between them, so re-asking the executor per probe would only buy round trips.
        pre_click_arg = await _probe_arg(page, selector)
        # Only a control inside a component can have a slotted label; a light-DOM click keeps its
        # single round trip.
        reach_pre = None
        try:
            if await page.evaluate(_IN_COMPONENT_JS, pre_click_arg):
                reach_pre = await page.evaluate(_TYPE_TARGET_PROBE_JS, pre_click_arg)
        except Exception:
            reach_pre = None
        if isinstance(reach_pre, dict) and reach_pre.get("exists") and reach_pre.get("disabled"):
            return ToolResult.error(f"{selector} is disabled — it cannot be clicked until the page enables it")
        slotted_label = isinstance(reach_pre, dict) and bool(reach_pre.get("slotted")) and not reach_pre.get("occluded")
        try:
            skin_probe = await page.evaluate(_SKINNED_CHECKBOX_PROBE_JS, pre_click_arg)
        except Exception:
            skin_probe = None
        if isinstance(skin_probe, dict) and skin_probe.get("file"):
            return ToolResult.error(
                f"{selector} is a file input — clicking it opens a native picker the run cannot drive; "
                "use file_upload with this selector instead"
            )
        if isinstance(skin_probe, dict) and skin_probe.get("select") and skin_probe.get("invisible"):
            # Playwright's actionability wait never resolves against it, so a click here is 15s of pure
            # loss followed by a raise; select_option forces past that on the same selector — but only
            # for one something visible stands in for, so an unreachable select is sent to reveal first
            # rather than to a tool that would refuse it a turn later.
            if not skin_probe.get("proxied"):
                return _unreachable_error(selector)
            return ToolResult.error(
                f"{selector} is a hidden native <select> — a click cannot open it; use select_option "
                "with this selector instead"
            )
        if isinstance(skin_probe, dict) and skin_probe.get("unproxied"):
            return _unreachable_error(selector)
        skinned = bool(isinstance(skin_probe, dict) and skin_probe.get("skinned"))
        label_tagged = bool(isinstance(skin_probe, dict) and skin_probe.get("labelTagged"))
        checked_before: bool | None = None
        if skinned:
            if skin_probe.get("disabled"):
                # Playwright refuses a label bound to a disabled control the same way it refuses the
                # control, so the click path would spend its full timeout and then blame a re-render.
                await _clear_proxy_tags(page)
                return ToolResult.error(f"{selector} is disabled — it cannot be toggled until the page enables it")
            try:
                checked_before = await page.evaluate(_CHECKBOX_CHECKED_JS, pre_click_arg)
            except Exception:
                checked_before = None
            if checked_before is True and skin_probe.get("radio"):
                # The probe tagged the label on its way here; left behind it shows up in get_html.
                await _clear_proxy_tags(page)
                return ToolResult.ok(f"{selector} is already selected — no change needed")

        if skinned and label_tagged:
            try:
                await page.click('[data-tv3-proxy="1"]', timeout=15000)
            except Exception as e:
                return ToolResult.error(
                    f"click on {selector} via its label failed ({type(e).__name__}) — the page may have "
                    "re-rendered; re-observe and act on fresh selectors"
                )
            finally:
                await _clear_proxy_tags(page)
            base = f"clicked {selector} via its label — now at {await _url(page)}"
        elif skinned:
            # Resolved again here rather than reused: this evaluate is the action, not a probe, and a
            # selector naming a control through its host resolves ONLY through the handle -- a node the
            # page replaced while the probes ran would be clicked off-document, silently.
            fired = await page.evaluate(
                "(arg) => { const _q = "
                + _ROOT_QUERY_JS
                + "; const el = _q.find(arg.sel) || arg.el;"
                + " if (!el || !el.isConnected) return false; el.click(); return true; }",
                await _probe_arg(page, selector),
            )
            if not fired:
                await _clear_proxy_tags(page)
                return ToolResult.error(
                    f"{selector} left the page before the click could land — it was replaced by a "
                    "re-render; re-observe and act on fresh selectors",
                    data={"page_state_changed": True},
                )
            base = f"clicked {selector} (hidden native control, toggled directly) — now at {await _url(page)}"
        else:
            try:
                await page.evaluate(_CLICK_SAME_DOC_PLANT_JS)
            except Exception:
                pass
            try:
                if slotted_label:
                    # The composed-tree probe already answered: the only thing "over" this control is its
                    # own slotted label, which the driver's containment check would wait 15s to reject.
                    await page.click(selector, timeout=15000, force=True)
                else:
                    await page.click(selector, timeout=15000)
            except Exception as e:
                gone = False
                try:
                    gone = not await page.evaluate(_SELECTOR_EXISTS_JS, await _probe_arg(page, selector))
                except Exception:
                    gone = False
                if gone:
                    # A same-document re-render: the URL and document nonce read unchanged, so only this
                    # flag tells the loop the rest of the batch was planned against a stale page.
                    return ToolResult.error(
                        f"click on {selector} failed: the element no longer exists on the page — it was "
                        "likely removed by a re-render (e.g. a menu closed and destroyed its options). "
                        f"Re-observe and act on fresh selectors. (original error: {type(e).__name__})",
                        data={"page_state_changed": True},
                    )
                # Diagnosed only now, after the full actionability wait: a transient overlay (a toast,
                # a closing menu) deserves the whole 15s to clear on its own, not a probe-shortened one.
                try:
                    reach_raw = await page.evaluate(_TYPE_TARGET_PROBE_JS, await _probe_arg(page, selector))
                except Exception:
                    reach_raw = None
                reach_probe = reach_raw if isinstance(reach_raw, dict) else None
                # `skinned` is the typing path's force-past signal; a click has no force fallback, so a
                # click that timed out on an occluded field is genuinely blocked -- even by the field's
                # own open listbox. Name the occluder rather than re-raise a bare Page.click Timeout.
                if reach_probe and reach_probe.get("occluded"):
                    return _covered_error(selector, reach_probe.get("occluder"), verb="clicked")
                # A URL is the wrong question (pushState moves it without leaving the page); the token
                # planted before the click answers "is this still the same document" exactly.
                try:
                    same_document = bool(await page.evaluate(_CLICK_SAME_DOC_CHECK_JS))
                except Exception:
                    same_document = False
                # Only the driver's own hit-target refusal is retried: any other failure (a download or
                # navigation the click started, a detached node) keeps its original error.
                intercepted = "intercepts pointer events" in str(e)
                if (
                    intercepted
                    and same_document
                    and reach_probe
                    and reach_probe.get("exists")
                    and not reach_probe.get("disabled")
                ):
                    # The driver's hit-target check reads DOM containment, so a control whose visible
                    # label is slotted into its shadow tree reads as intercepted by its own label. The
                    # composed-tree probe just said nothing covers it, so the click a person makes lands
                    # on it; dispatch that click at the same point without the containment check.
                    try:
                        await page.locator(selector).first.wait_for(state="visible", timeout=3000)
                        await page.click(selector, timeout=5000, force=True)
                    except Exception:
                        raise e from None
                else:
                    raise
            base = f"clicked {selector} — now at {await _url(page)}"

        # url_after vs url_before is the real page-transition signal the shadow net-progress ledger
        # reads (loop.py _ProgressLedger). Surfaced, not newly computed: _url is the page.url property,
        # not a probe, so this adds no evaluate. history.pushState can move the URL without leaving the
        # document, so this is a hint the ledger treats as re-baseline evidence, not a hard assertion.
        url_after = await _url(page)
        transition_data: dict[str, Any] = {
            "page_transitioned": bool(url_before and url_after and url_after != url_before)
        }

        if skinned:
            try:
                checked_after = await page.evaluate(_CHECKBOX_CHECKED_JS, await _probe_arg(page, selector))
            except Exception:
                checked_after = None
            matches = await _post_match_count(page, selector)
            post_state = {"checked": checked_after} if checked_after is not None else None
            verdict = _classify_commit({"checked": checked_before}, matches, post_state)
            if verdict is CommitStatus.UNVERIFIED:
                if post_state is None:
                    return ToolResult.ok(
                        f"{base} — the control left the page after the click, so its state could not be "
                        "verified; re-observe before relying on it",
                        data=transition_data,
                    )
                if matches != 1:
                    return ToolResult.ok(
                        f"{base} — it re-resolved to {matches} elements after the click, so its state could "
                        "not be verified; re-observe before relying on it",
                        data=transition_data,
                    )
                # Readable, singular post-click state but no pre-click baseline to diff against (the
                # pre-read raced): as before the unified verdict, fall through to the ordinary post path
                # rather than claim the control left the page.
            if verdict is CommitStatus.DID_NOT_COMMIT:
                return ToolResult.error(
                    f"click on {selector} did NOT commit: the control still reads checked={checked_after!r} — "
                    "the styled proxy may not sync from its hidden control; re-observe and act on the visible "
                    "proxy instead",
                    data=transition_data,
                )

        if pre is None:
            return ToolResult.ok(base, data=transition_data)
        try:
            note, commit_error = await _click_reaction(page, selector, pre, url_before, doc_planted=doc_planted)
        except Exception:
            LOG.debug("taskv3 click reaction probe failed", selector=selector, exc_info=True)
            return ToolResult.ok(base, data=transition_data)
        if commit_error is not None:
            return ToolResult.error(commit_error, data=transition_data)
        return ToolResult.ok(base + "\n" + note if note else base, data=transition_data)

    async def hover(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        await page.hover(selector, timeout=15000)
        return ToolResult.ok(f"hovered {selector}")

    async def _reachable_for_typing(page: Any, selector: str) -> tuple[bool, bool, dict[str, Any] | None]:
        """(reachable, occluded, occluder). Raises when the field cannot accept typed text at all. Shared
        by both typing paths: fill() does no hit-testing, so without this a covered password or email
        field is filled silently -- no timeout to notice, and a person could not have reached it."""
        try:
            probe = await page.evaluate(_TYPE_TARGET_PROBE_JS, await _probe_arg(page, selector))
        except Exception:
            probe = None
        if isinstance(probe, dict) and probe.get("exists"):
            # fill() waits for "enabled" and "editable" on its own, so without these the run pays a
            # second full timeout for a state the probe has already read.
            if probe.get("disabled") or probe.get("readOnly"):
                raise _FieldNotEditable(selector, bool(probe.get("readOnly")))
        occluded = bool(isinstance(probe, dict) and probe.get("occluded"))
        occluder = probe.get("occluder") if isinstance(probe, dict) else None
        if occluded and not probe.get("skinned"):
            return False, occluded, occluder
        # Reachable: a skinned own-popup is force-typed past, so there is no blocking occluder to
        # report. The probe still names it (the click path, which reads the probe directly, needs the
        # name), but surfacing it here would let a force-click that then navigates or remounts the
        # field raise a false "covered by <the field's own list>" message on a field that was reachable.
        return True, occluded, None

    async def _focus_for_typing(page: Any, selector: str) -> tuple[bool, dict[str, Any] | None]:
        """Put the caret in `selector`. A False first element means the field is genuinely covered and
        must not be typed into. A click is how a widget learns to open its suggestion list, so it stays
        the first move."""
        reachable, occluded, occluder = await _reachable_for_typing(page, selector)
        if not reachable:
            return False, occluder
        if occluded:
            # Forcing skips the hit-target check but still dispatches at coordinates, so the wrapper
            # can take the event; the focus check below is what makes the outcome deterministic.
            # Failures are NOT swallowed: force already removed the only reason this click was
            # expected to fail, so what is left (a detached node, a navigation) is real.
            # A URL is the wrong question: history.pushState changes it without leaving the page,
            # and a widget that syncs filter state into the URL on click would abort typing on a
            # field that never moved. A navigation clears window, so a token planted on it answers
            # "is this still the same document" exactly -- the same technique the pre-snapshot uses.
            await page.evaluate("() => { window.__tv3_doc = 1; }")
            await page.click(selector, timeout=15000, force=True)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=1000)
            except Exception:
                pass
            try:
                same_document = bool(await page.evaluate("() => window.__tv3_doc === 1"))
            except Exception:
                same_document = False
            if not same_document:
                # The wrapper was a link and the click followed it. The selector may well match
                # something on the destination, so typing now would put the text somewhere nobody
                # asked for.
                return False, occluder
            try:
                # The click may have remounted or hidden the field -- a wrapper that swaps its input
                # on click is an ordinary SPA shape. fill() would wait its own full timeout for a
                # node that is gone or invisible, which is the cost this whole path exists to avoid.
                await page.wait_for_selector(selector, state="visible", timeout=1200)
            except Exception:
                return False, occluder
        else:
            await page.click(selector, timeout=15000)
        try:
            focused = await page.evaluate(_ACTIVE_IS_JS, await _probe_arg(page, selector))
        except Exception:
            focused = None
        # None is "could not tell" -- a selector document.querySelector cannot parse, or a probe that
        # threw. Only an explicit False is evidence the caret went somewhere else.
        if focused is False:
            # focus() needs no hit target, so it repairs a skin that swallowed the click without
            # forwarding it. Typing then goes to the field rather than wherever the caret was.
            await page.focus(selector, timeout=15000)
        return True, None

    async def _commit_typeahead(
        page: Any, selector: str, value: str, rounds: int
    ) -> tuple[str | None, str | None, bool]:
        # Poll for the suggestion list rendered IN REACTION to the value already typed into `selector`,
        # click the best match, and verify the field committed. Site-agnostic (see _FIND_SUGGESTION_JS).
        # Returns (committed_value, suggestion_text): suggestion_text is None when no suggestion ever
        # surfaced (an ordinary field, or nothing matched); committed is None when a suggestion was
        # clicked but no value landed.
        best_txt: str | None = None
        from_focus = False
        declared: list[str] = []
        for _ in range(rounds):
            await asyncio.sleep(0.4)
            try:
                found = await page.evaluate(
                    _FIND_SUGGESTION_JS,
                    {"value": value, "field": selector, "el": (await _probe_arg(page, selector))["el"]},
                )
            except Exception as e:
                LOG.debug("taskv3 typeahead suggestion-find failed", selector=selector, error=str(e))
                found = None
            if isinstance(found, dict) and found.get("text"):
                best_txt = str(found["text"])
                from_focus = bool(found.get("fromFocus"))
                declared = [str(v) for v in (found.get("declared") or []) if isinstance(v, str)]
                break
        if not best_txt:
            return None, None, False
        # Click the tagged best row. If the list re-rendered and dropped the tag, re-find (re-tag the
        # current best) and click once more — never blind-press ArrowDown/Enter, which would commit
        # whichever row the widget happens to highlight rather than the one we actually scored.
        # A pick from a focus-opened list is verified under the pick contract, which needs the hidden
        # values as they were BEFORE the click so a stale leftover cannot read as the commit.
        # Captured before ANY click: a re-find after a failed first click may land on a focus-revealed
        # row, and the pick contract it is verified under needs the state as it was before the click.
        pre_hidden: list[str] = []
        pre_value = value
        try:
            raw_hidden = await page.locator(selector).first.evaluate(_HIDDEN_VALUES_JS, timeout=2000)
            if isinstance(raw_hidden, list):
                pre_hidden = [str(v) for v in raw_hidden if isinstance(v, str)]
        except Exception:
            pre_hidden = []
        try:
            pre_value = str(await page.locator(selector).first.input_value(timeout=2000))
        except Exception:
            pre_value = value
        clicked = False
        try:
            await page.click('[data-tv3-sugg="1"]', timeout=3000)
            clicked = True
        except Exception:
            try:
                refound = await page.evaluate(
                    _FIND_SUGGESTION_JS,
                    {"value": value, "field": selector, "el": (await _probe_arg(page, selector))["el"]},
                )
                if isinstance(refound, dict) and refound.get("text"):
                    best_txt = str(refound["text"])
                    from_focus = bool(refound.get("fromFocus"))
                    declared = [str(v) for v in (refound.get("declared") or []) if isinstance(v, str)]
                    await page.click('[data-tv3-sugg="1"]', timeout=3000)
                    clicked = True
            except Exception:
                clicked = False
        if not clicked:
            # a suggestion surfaced but we couldn't click it — report un-committed, don't guess
            LOG.debug("taskv3 typeahead could not click suggestion", selector=selector, suggestion=best_txt)
            return None, best_txt, False
        await asyncio.sleep(0.3)
        readable = False
        try:
            # A row the FOCUS click revealed was offered, not filtered: verify it under the pick
            # contract (the value must BE the chosen label), not the typeahead's change-based one.
            read = await page.evaluate(
                _VERIFY_COMMIT_JS,
                {
                    "field": selector,
                    "typed": pre_value if from_focus else value,
                    "chosen": best_txt,
                    "noSuggestionList": from_focus,
                    "preHidden": pre_hidden,
                    "chosenValues": declared,
                    "el": (await _probe_arg(page, selector))["el"],
                },
            )
            # null means the field could not be read at all; '' means it was read and holds nothing.
            readable = read is not None
            committed = str(read or "").strip()
        except Exception as e:
            LOG.debug("taskv3 typeahead commit-verify failed", selector=selector, error=str(e))
            committed = ""
        return (committed or None), best_txt, readable

    async def _type_and_commit(
        page: Any, selector: str, value: str, rounds: int
    ) -> tuple[str | None, str | None, bool]:
        # Keystroke-type (so a widget's async suggestion fetch fires on real key events). Snapshot the
        # visible DOM BEFORE the focus click, not just before typing: a widget that opens its full list on
        # focus and then filters it in place keeps the same row nodes, so a snapshot taken after the click
        # marks every option as pre-existing and the reaction gate rejects the rows the keystrokes kept.
        # Static page text that merely shares a word with the value is still excluded — it was visible
        # before the click too.
        presnapshot_ok = True
        try:
            await page.evaluate(_PRESNAPSHOT_JS)
        except Exception:
            presnapshot_ok = False
            LOG.info("taskv3 typeahead pre-snapshot failed; skipping suggestion probe", selector=selector)
        focused, occluder = await _focus_for_typing(page, selector)
        if not focused:
            raise _FieldCovered(selector, occluder)
        if presnapshot_ok:
            # Focus may reveal help text or a validation note as well as a menu; only rows of a list
            # are a reaction the finder may pick from, so everything else focus revealed is marked too.
            try:
                await page.evaluate(_FOCUS_SNAPSHOT_JS, await _probe_arg(page, selector))
            except Exception:
                pass
        await page.fill(selector, "", timeout=15000)
        await page.type(selector, value, delay=15, timeout=15000)
        if not presnapshot_ok:
            # Without the pre-snapshot the reaction-gate can't tell a new suggestion from static page
            # text, so don't run the finder ungated (it could click unrelated content) — leave the typed
            # value and let the caller re-observe.
            return None, None, False
        return await _commit_typeahead(page, selector, value, rounds)

    async def _typeahead_commit_verdict(
        page: Any, selector: str, committed: str | None, readable: bool
    ) -> tuple[CommitStatus, int]:
        # Route the typeahead's own commit truth through the unified classifier so the site gains INV-1
        # (a commit read off n≠1 → unverified) and INV-2 (unreadable → unverified) for free. `committed` (the
        # token-overlap result of _VERIFY_COMMIT_JS) is the value dimension the classifier cannot compute
        # itself, so it is handed in as committed_value; behavior on a single stable element is unchanged.
        matches = await _post_match_count(page, selector)
        # post carries the READABILITY dimension (INV-2), committed_value the value dimension. A field
        # read back empty is readable ("" has state) and did-not-commit; only an unreadable field (read
        # returned null → readable False) is INV-2 unverified. `committed or ""` keeps that split clean.
        post = {"value": committed or ""} if readable else None
        return _classify_commit(None, matches, post, committed_value=bool(committed)), matches

    # Input kinds that are never typeaheads — skip the suggestion probe (and its latency) for these.
    # `textarea` is included: free-text boxes never render a typeahead and would just pay the probe tax.
    _NON_TYPEAHEAD_TYPES = frozenset(
        {
            "textarea",
            "email",
            "tel",
            "number",
            "url",
            "password",
            "date",
            "datetime-local",
            "month",
            "time",
            "week",
            "color",
            "range",
        }
    )

    async def _unverifiable_because(page: Any, selector: str) -> str | None:
        # Returns the clause explaining why a check could not be run, or None when it could. "The
        # probe cannot carry this claim" is not one fact but three: a widget that unmounts its own
        # input, a control inside a component whose list may render beyond a pierced query's reach,
        # and a selector our probe cannot parse. Empty on failure -- claiming a reason we did not
        # establish would be its own false statement.
        try:
            reach = str(await page.evaluate(_PROBE_REACH_JS, await _probe_arg(page, selector)) or "")
        except Exception:
            return None
        if reach == "component":
            return "it is inside a component"
        if reach == "unprobeable":
            return "we cannot resolve that selector ourselves"
        return None

    async def _field_type(page: Any, selector: str) -> str:
        try:
            return (
                await page.eval_on_selector(
                    selector,
                    "el => el.tagName === 'TEXTAREA' ? 'textarea' : (el.getAttribute('type') || 'text').toLowerCase()",
                )
            ) or "text"
        except Exception:
            return "text"

    async def type_text(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if not selector:
            return ToolResult.error("type needs a selector, or mark=N from the last look().")
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        selector = await _resolve_mirrored_host_control(page, selector)
        text = _resolve_text(args.get("text", ""))
        press_enter = args.get("press_enter")
        clear = args.get("clear", True)
        # A typeahead silently rejects raw typed text — it only accepts a picked suggestion — and the
        # model does not reliably reach for select_combobox on its own. So after typing into a plain text
        # field, check whether the page REACTED with a suggestion list and, if so, commit the best match
        # here. Detection is behavioral (no per-site rules), so this holds across ATSes; non-text inputs
        # and append/enter typing skip it and fill normally (fast path, no polling).
        if text and clear and not press_enter and await _field_type(page, selector) not in _NON_TYPEAHEAD_TYPES:
            # keystroke-type (via _type_and_commit) so a widget that fetches suggestions on key events —
            # not just on a single `input` from fill — still surfaces them, then commit the best match.
            try:
                committed, opt_txt, readable = await _type_and_commit(page, selector, text, rounds=3)
            except _FieldCovered as exc:
                return _covered_error(exc.selector, exc.occluder)
            except _FieldNotEditable as exc:
                return _not_editable_error(exc)
            if opt_txt:
                verdict, matches = await _typeahead_commit_verdict(page, selector, committed, readable)
                if verdict is CommitStatus.OK:
                    return ToolResult.ok(
                        f"typed into {selector}; it is a typeahead — selected {opt_txt!r} "
                        f"(committed value: {committed!r})"
                    )
                if verdict is CommitStatus.UNVERIFIED and matches != 1:
                    # INV-1: the field re-resolved to n≠1 after the click (remounted or now ambiguous), so
                    # there is no stable element to read the commit off — soft, not a false did-not-commit.
                    return ToolResult.ok(
                        f"clicked suggestion {opt_txt!r} for {selector}, but it re-resolved to {matches} "
                        "elements so the commit could not be verified — re-observe to confirm the value "
                        "before relying on it"
                    )
                if verdict is CommitStatus.UNVERIFIED:
                    # INV-2 (unreadable). The verifier pierces open roots and also reads the element the
                    # executor resolved, so inside a component the failure is established rather than
                    # guessed. A list portaled elsewhere, or a field in a closed root, is still beyond both
                    # -- and that is exactly what the read reports by returning nothing, so the softening
                    # follows the read.
                    why = await _unverifiable_because(page, selector)
                    if why:
                        return ToolResult.ok(
                            f"clicked suggestion {opt_txt!r} for {selector}; {why}, so the commit could not "
                            "be verified — re-observe to confirm the value before relying on it"
                        )
                    return ToolResult.error(
                        f"clicked suggestion {opt_txt!r} for {selector} but it did not commit — the field is "
                        "NOT filled; re-observe and retry, do not proceed"
                    )
                # DID_NOT_COMMIT: the field is NOT filled. The loop then skips any later click or Enter
                # in the same batch -- it may be an unvalidated submit, and no production submit guard
                # exists yet.
                return ToolResult.error(
                    f"clicked suggestion {opt_txt!r} for {selector} but it did not commit — the field is NOT "
                    "filled; re-observe and retry, do not proceed"
                )
            # No suggestion list surfaced. The finder pierces open shadow roots, so it can see a list
            # inside one -- but not one the widget portals elsewhere in the page or renders in a
            # closed root, so inside a component this is still not evidence of absence. Saying
            # "typed into X" there reads as a verified fill, and on a typeahead that silently rejects
            # raw text it turns an honest failure into a confident wrong answer on a form we submit.
            why = await _unverifiable_because(page, selector)
            if why:
                return ToolResult.ok(
                    f"typed into {selector} — {why}, so the typeahead check could not see it and no "
                    "commit was verified; re-observe to confirm the value before relying on it"
                )
            return ToolResult.ok(f"typed into {selector}")
        # The types that skip the typeahead probe still must not be typed into through an overlay.
        # They reach fill()/type(), which do no hit-testing, so nothing here would fail on its own --
        # the text simply lands in a field the person could not have reached.
        try:
            reachable, _, occluder = await _reachable_for_typing(page, selector)
        except _FieldNotEditable as exc:
            return _not_editable_error(exc)
        if not reachable:
            return _covered_error(selector, occluder)
        if clear:
            await page.fill(selector, text, timeout=15000)
        else:
            await page.type(selector, text, timeout=15000)
        if press_enter:
            await page.press(selector, "Enter")
        return ToolResult.ok(f"typed into {selector}")

    async def _anchor_typeable(page: Any, selector: str) -> bool:
        # Fail-open on a probe error: a transient evaluate failure must not flip a valid typeahead into
        # a refusal (the far more common caller is select_combobox, whose contract is to type).
        try:
            return bool(await page.evaluate(_ANCHOR_TYPEABLE_JS, await _probe_arg(page, selector)))
        except Exception:
            return True

    async def _open_observe_pick(page: Any, selector: str, value: str, *, close_open_menu: bool = False) -> ToolResult:
        # Commit a click-to-open single-select in ONE call: open the list, enumerate the option rows the
        # click rendered (v3's own _FIND_MENU_JS tags them data-tv3-menu="N"), deterministically pick the
        # match, click it, and VERIFY — reusing the same commit-verify contract as the typeahead path.
        # This is the branch v3 lacked: a non-searchable react-select (a real <input> that never filters)
        # and a non-typeable button/div anchor both land here. When no deterministic match is found the
        # tool returns a truthful did-not-commit AND the observed options, so the model resolves a
        # genuinely unexpected widget by sight (look()/act-by-mark) — never a blind text-LLM guess.
        if close_open_menu:
            # A prior keystroke attempt may have opened this widget's list; close it so the pre-snapshot
            # captures the CLOSED page and _FIND_MENU_JS counts only rows THIS open-click renders. Escape
            # is sent ONLY once a menu is confirmed open (aria-expanded=true) — a stray Escape with nothing
            # open would bubble to and close a surrounding dialog, discarding the form. If the widget does
            # not expose aria-expanded, we skip Escape and let the reopen self-heal below handle a toggle.
            try:
                menu_open = bool(await page.evaluate(_MENU_OPEN_JS, await _probe_arg(page, selector)))
            except Exception:
                menu_open = False
            if menu_open:
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.1)
                except Exception:
                    pass

        async def _open_and_enumerate() -> tuple[dict[str, Any] | None, ToolResult | None]:
            try:
                await page.evaluate(_PRESNAPSHOT_JS)
            except Exception:
                return None, ToolResult.error(
                    f"could not snapshot the page to open {selector}'s option list — the field is NOT "
                    "filled; re-observe, then click the control and pick the option you want"
                )
            try:
                # 5s, not the 15s a routine click waits: the control is already present (we just typed
                # into it, or it is a visible button), so it opens at once — a long wait here only delays
                # the error on a field that unmounted itself, which must fail loudly, not slowly.
                await page.click(selector, timeout=5000)
            except Exception:
                return None, ToolResult.error(
                    f"could not click {selector} to open its option list — the field is NOT filled; "
                    "re-observe and retry"
                )
            for _ in range(6):
                await asyncio.sleep(0.4)
                try:
                    menu = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
                except Exception as e:
                    LOG.debug("taskv3 open-observe-pick menu-find failed", selector=selector, error=str(e))
                    menu = None
                if isinstance(menu, dict) and menu.get("count"):
                    return menu, None
            return None, None

        found, err = await _open_and_enumerate()
        if err is not None:
            return err
        if found is None:
            # The list may have been open on entry (a prior call, or an Escape the widget ignored), so the
            # open-click above TOGGLED it shut. Try once more: the second open-click reopens it and the
            # re-snapshot inside _open_and_enumerate makes the reopened rows read as new.
            found, err = await _open_and_enumerate()
            if err is not None:
                return err
        if not isinstance(found, dict) or not found.get("count"):
            # The open-click rendered no enumerable option list (portalled/closed-root, or not a menu).
            # Truthful did-not-commit; the model's vision handles it from here.
            return ToolResult.error(
                f"opened {selector} but no option list rendered to pick {value!r} from — the field is NOT "
                "filled; look() at the control and click the option you want"
            )
        # Read the whole tagged list at full length so the match is neither missed on a >60-char label
        # nor computed over a truncated ≤15 slice (which would let "unique in the first 15" stand in for
        # "unique in the menu").
        count = int(found.get("count") or 0)
        read: list[dict[str, Any]] = []
        try:
            full_rows = await page.evaluate(_MENU_OPTION_TEXTS_JS)
            if isinstance(full_rows, list):
                read = [o for o in full_rows if isinstance(o, dict) and isinstance(o.get("n"), int)]
        except Exception as e:
            LOG.debug("taskv3 open-observe-pick full-text read failed", selector=selector, error=str(e))

        def _n_order(o: dict[str, Any]) -> int:
            n = o.get("n")
            return n if isinstance(n, int) else 1 << 30

        # `overflowed` = the enumerated set is not the whole list, so uniqueness cannot be established and
        # ALL auto-commit is refused. That is true when the full read failed, when `_FIND_MENU_JS` tagged
        # more rows than the read returned, OR when a row's `aria-setsize` declares more options than were
        # rendered (a virtualised list whose window is all that is in the DOM — count == len(read) there).
        declared = max((int(o.get("setsize") or 0) for o in read), default=0)
        overflowed = not read or count > len(read) or declared > len(read) or bool(found.get("partial"))
        rows = read or (found.get("options") or [])
        rows.sort(key=_n_order)
        # Never auto-click a navigational row (`<a href>`/`<button>`/menuitem): `_FIND_MENU_JS` enumerates
        # them because it only reports, but clicking one would leave the form.
        options = [o for o in rows if not o.get("nav")]
        if rows and not options:
            # Refusing to auto-click is not the same as having nothing to show: hand the model the marks
            # so a button-built select is one deliberate click away instead of an empty listing.
            shown_nav = "; ".join(
                f'[data-tv3-menu="{o.get("n")}"] {str(o.get("text") or "")[:60]!r}' for o in rows[:15]
            )
            return ToolResult.error(
                f"opened {selector} but every row is a link/button this tool will not auto-click ({shown_nav}"
                f"{'; +' + str(len(rows) - 15) + ' more' if len(rows) > 15 else ''}) — the field is NOT filled; "
                'if one of them is the option you want, click it by its [data-tv3-menu="N"] selector'
            )
        idx = None if overflowed else _match_menu_option(value, options)
        if idx is None:
            # Match over the FULL list above, but bound the error PAYLOAD: enumerate at most 15 rows,
            # each ≤60 chars, so a miss on a 250-option country list does not ship a 15KB tool message.
            shown = options[:15]
            listing = "; ".join(f'[data-tv3-menu="{o.get("n")}"] {str(o.get("text") or "")[:60]!r}' for o in shown)
            if overflowed:
                return ToolResult.error(
                    f"{value!r} matched no option in the part of {selector}'s list we could read "
                    f"({listing}) — the list is longer than we could enumerate; scroll or look() to see "
                    'the rest, then click the option by its [data-tv3-menu="N"] selector'
                )
            not_shown = len(options) - len(shown)
            if not_shown > 0:
                # More selectable rows exist than we listed — do not claim the value is absent.
                return ToolResult.error(
                    f"{value!r} matched no option among the first {len(shown)} of {len(options)} in "
                    f"{selector} ({listing}; +{not_shown} more) — scroll or look() to see the rest, then "
                    'click the option by its [data-tv3-menu="N"] selector'
                )
            return ToolResult.error(
                f"opened {selector} but no option matched {value!r}; the list shows {listing} — "
                'pick the right one by its [data-tv3-menu="N"] selector, or look() to see the full menu'
            )
        matched = next((str(o.get("text") or "") for o in options if o.get("n") == idx), value)
        # Read the field's committable value BEFORE the click. The verifier then rests on a CHANGE from
        # this (passed as `typed`, with noSuggestionList so the always-true listClosed cannot stand in for
        # one) — so leftover text a type attempt left in the field, whether or not a clear would succeed,
        # can never read back as the commit.
        try:
            pre_value = str(await page.eval_on_selector(selector, "el => (el.value || '')") or "")
        except Exception:
            pre_value = ""
        pre_hidden: list[str] = []
        try:
            raw_hidden = await page.eval_on_selector(selector, _HIDDEN_VALUES_JS)
            if isinstance(raw_hidden, list):
                pre_hidden = [str(v) for v in raw_hidden if isinstance(v, str)]
        except Exception:
            pre_hidden = []
        try:
            await page.click(f'[data-tv3-menu="{idx}"]', timeout=5000)
        except Exception:
            return ToolResult.error(
                f"opened {selector} and matched {matched!r} but could not click it — re-observe and click "
                f'[data-tv3-menu="{idx}"]'
            )
        await asyncio.sleep(0.3)
        readable = False
        committed = ""
        try:
            read = await page.evaluate(
                _VERIFY_COMMIT_JS,
                {
                    "field": selector,
                    "typed": pre_value,
                    "chosen": matched,
                    "noSuggestionList": True,
                    "preHidden": pre_hidden,
                    "el": (await _probe_arg(page, selector))["el"],
                },
            )
            readable = read is not None
            committed = str(read or "").strip()
        except Exception as e:
            LOG.debug("taskv3 open-observe-pick commit-verify failed", selector=selector, error=str(e))
            committed = ""
        verdict, matches = await _typeahead_commit_verdict(page, selector, committed or None, readable)
        if verdict is CommitStatus.OK:
            return ToolResult.ok(f"selected {matched!r} for {selector} (committed value: {committed!r})")
        if verdict is CommitStatus.UNVERIFIED and matches != 1:
            return ToolResult.ok(
                f"selected {matched!r} for {selector}, but it re-resolved to {matches} elements so the "
                "commit could not be verified — re-observe to confirm the value before relying on it"
            )
        if verdict is CommitStatus.UNVERIFIED:
            why = await _unverifiable_because(page, selector)
            if why:
                return ToolResult.ok(
                    f"selected {matched!r} for {selector}; {why}, so the commit could not be verified — "
                    "re-observe to confirm the value before relying on it"
                )
            return ToolResult.error(f"clicked {matched!r} but {selector} did not commit a value")
        return ToolResult.error(f"clicked {matched!r} but {selector} did not commit a value")

    async def _commit_custom_combobox(page: Any, selector: str, value: str) -> ToolResult:
        # Shared custom-combobox commit — the ONE path select_combobox and select_option's non-native
        # branch both route through. Two mechanisms, one tool call: a TYPEAHEAD (searchable react-select /
        # spl-autocomplete) commits by keystroke-type -> WAIT for the reacting suggestion -> click ->
        # verify; a CLICK-TO-OPEN single-select (non-searchable react-select, button/div listbox) commits
        # by open -> observe the rendered options -> pick the match -> verify. A non-typeable anchor goes
        # straight to the open path; a typeable anchor tries typeahead first and falls through to the open
        # path only when NOTHING reacts to keystrokes (the widget doesn't filter). Fails loudly rather than
        # leaving raw typed text a widget won't accept (a false "filled" — the failure mode this prevents).
        if await _anchor_typeable(page, selector):
            try:
                committed, opt_txt, readable = await _type_and_commit(page, selector, value, rounds=8)
            except _FieldCovered as exc:
                return _covered_error(exc.selector, exc.occluder)
            except _FieldNotEditable as exc:
                return _not_editable_error(exc)
            if opt_txt is None:
                # No MATCHING suggestion reacted -- but that alone does not say a list never rendered: a
                # searchable typeahead that filtered to zero and a non-searchable widget that never filters
                # both land here. The finder pierces open shadow roots, so inside a component it saw the
                # list -- but a portalled/closed-root list stays invisible, so keep the honest "re-observe"
                # note for that reach case first.
                why = await _unverifiable_because(page, selector)
                if why:
                    return ToolResult.ok(
                        f"typed {value!r} into {selector}; {why}, so the suggestion list could not be seen "
                        "and no selection was verified — re-observe to confirm the value committed before "
                        "relying on it"
                    )
                # A drill-down widget hides its options under expandable category rows, so open→observe→pick's
                # flat enumeration cannot reach them — surface the categories and fail loudly instead.
                cats = await _categories_note(page, selector)
                if cats:
                    return ToolResult.error(
                        f"no autocomplete suggestion matched {value!r} for {selector}; the field is NOT filled. {cats}"
                    )
                # A searchable typeahead whose value is genuinely absent must report the honest no-match
                # rather than reopen a list the value is not in. Two independent signals establish
                # "searchable": (a) _FIND_MENU_JS finds rows NEW since the pre-type snapshot — a list that
                # reacted to the keystrokes (a menu opened on focus/click sits in that snapshot and does not
                # count, so a non-searchable widget still falls through); (b) the anchor declares
                # aria-autocomplete list/both/inline — the ARIA contract that catches a combobox which
                # filtered to ZERO rows, leaving nothing new to count.
                searchable = False
                try:
                    reacted_menu = await page.evaluate(_FIND_MENU_JS, await _probe_arg(page, selector))
                    searchable = isinstance(reacted_menu, dict) and bool(reacted_menu.get("count"))
                except Exception:
                    searchable = False
                if not searchable:
                    try:
                        searchable = bool(
                            await page.evaluate(_DECLARES_SEARCH_AUTOCOMPLETE_JS, await _probe_arg(page, selector))
                        )
                    except Exception:
                        searchable = False
                if searchable:
                    # Name the choices the widget offered when it opened, so the next call can use the
                    # exact label instead of guessing one; the contract stays an honest did-not-commit.
                    offered: list[str] = []
                    offered_total = 0
                    try:
                        raw_offered = await page.evaluate(_FOCUS_OFFERED_LABELS_JS, await _probe_arg(page, selector))
                        if isinstance(raw_offered, dict):
                            offered = [str(t) for t in (raw_offered.get("labels") or []) if isinstance(t, str)]
                            offered_total = int(raw_offered.get("total") or 0)
                    except Exception:
                        offered = []
                    if offered and offered_total > len(offered):
                        offered_note = (
                            f". The list offers {offered_total} rows; the first {len(offered)}: "
                            + "; ".join(repr(t[:60]) for t in offered)
                            + " — if the label you want is not among them, click the control and look() at the list"
                        )
                    elif offered:
                        offered_note = (
                            ". The list offers: "
                            + "; ".join(repr(t[:60]) for t in offered)
                            + " — call select_combobox again with one of these exact labels"
                        )
                    else:
                        offered_note = ""
                    return ToolResult.error(
                        f"no autocomplete suggestion matched {value!r} for {selector}; the field is NOT filled "
                        f"— do not assume success or move on as if it were{offered_note}"
                    )
                # The focus-click of the type attempt may have opened this widget's list, so close it first.
                return await _open_observe_pick(page, selector, value, close_open_menu=True)
            verdict, matches = await _typeahead_commit_verdict(page, selector, committed, readable)
            if verdict is CommitStatus.OK:
                return ToolResult.ok(f"selected {opt_txt!r} for {selector} (committed value: {committed!r})")
            if verdict is CommitStatus.UNVERIFIED and matches != 1:
                # INV-1: re-resolved to n≠1 after the click — no stable element to read the commit off.
                return ToolResult.ok(
                    f"selected {opt_txt!r} for {selector}, but it re-resolved to {matches} elements so the "
                    "commit could not be verified — re-observe to confirm the value before relying on it"
                )
            if verdict is CommitStatus.UNVERIFIED:
                # INV-2 (unreadable): keep the reach-based softening — a portalled/closed-root field is
                # beyond the verifier, so the read returning nothing is not evidence the value did not commit.
                why = await _unverifiable_because(page, selector)
                if why:
                    return ToolResult.ok(
                        f"selected {opt_txt!r} for {selector}; {why}, so the commit could not be verified — "
                        "re-observe to confirm the value before relying on it"
                    )
                return ToolResult.error(f"selected suggestion {opt_txt!r} but {selector} did not commit a value")
            return ToolResult.error(f"selected suggestion {opt_txt!r} but {selector} did not commit a value")
        # A non-typeable anchor (a button/div that only opens a list on click): open, observe, pick.
        return await _open_observe_pick(page, selector, value)

    async def select_option(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        label = args.get("label")
        value = args.get("value")
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        selector = await _resolve_mirrored_host_control(page, selector)
        try:
            probe = await page.evaluate(_SELECT_VISIBILITY_JS, await _probe_arg(page, selector))
        except Exception:
            probe = None
        # A disabled control cannot be set whichever kind it is; check before diverting so a disabled
        # custom combobox gets the accurate "is disabled" message rather than the typeable-gate refusal.
        if isinstance(probe, dict) and probe.get("exists") and probe.get("disabled"):
            return ToolResult.error(f"{selector} is disabled — it cannot be set until the page enables it")
        # Native-vs-custom gates on the authoritative nodeName (a structural signal, not a heuristic).
        # Divert to the shared custom-combobox path ONLY when the probe positively confirms a
        # non-<select> element (React-Select, spl-autocomplete, div-list) that page.select_option would
        # throw "Element is not a <select> element" on — so it commits in ONE action instead of degrading
        # the model into click-open + click-option flail. When the probe is unavailable or the element
        # could not be read, default to the native path below (unchanged), so a real <select> whose probe
        # momentarily fails is never misrouted into typing.
        probe_node = str(probe.get("nodeName") or "") if isinstance(probe, dict) and probe.get("exists") else None
        if probe_node is not None and probe_node != "select":
            chosen = label if label is not None else value
            if not isinstance(chosen, str) or not chosen:
                return ToolResult.error("select_option needs a label or value to choose")
            return await _commit_custom_combobox(page, selector, _resolve_text(chosen))
        # force bypasses actionability for a select a design system hides behind a styled proxy;
        # Playwright still sets the value and dispatches native input/change on the real element.
        force = bool(isinstance(probe, dict) and probe.get("exists") and not probe.get("visible"))
        if force and not probe.get("proxied"):
            return _unreachable_error(selector)
        if label is not None:
            await page.select_option(selector, label=label, timeout=15000, force=force)
        else:
            await page.select_option(selector, value=value, timeout=15000, force=force)
        if not force:
            return ToolResult.ok(f"selected on {selector}")
        try:
            readback = await page.evaluate(_SELECT_READBACK_JS, await _probe_arg(page, selector))
        except Exception:
            readback = None
        value_read: Any = None
        post: dict[str, Any] | None = None
        committed_value: bool | None = None
        if isinstance(readback, dict):
            value_read = readback.get("value")
            post = {"value": value_read}
            committed_value = readback.get("selectedLabel") == label if label is not None else value_read == value
        matches = await _post_match_count(page, selector)
        verdict = _classify_commit(None, matches, post, committed_value=committed_value)
        if verdict is CommitStatus.DID_NOT_COMMIT:
            return ToolResult.error(
                f"select on {selector} did NOT commit: native select still reads {value_read!r} — the styled "
                "widget may not sync from its hidden control; re-observe and act on the visible proxy instead"
            )
        if verdict is CommitStatus.UNVERIFIED:
            reason = (
                "the control left the page afterwards"
                if post is None
                else f"it re-resolved to {matches} elements afterwards"
            )
            return ToolResult.ok(
                f"selected on {selector} — {reason}, so the selection could not be verified; re-observe "
                "before relying on it"
            )
        return ToolResult.ok(f"selected on {selector} (hidden native select, set directly)")

    async def press_key(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        key = args["key"]
        selector = args.get("selector")
        if selector:
            ambiguous = await _ambiguous_selector_error(page, selector)
            if ambiguous is not None:
                return ambiguous
            await page.press(selector, key)
        else:
            await page.keyboard.press(key)
        return ToolResult.ok(f"pressed {key}")

    async def scroll(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if selector:
            ambiguous = await _ambiguous_selector_error(page, selector)
            if ambiguous is not None:
                return ambiguous
            el = await page.query_selector(selector)
            if el:
                await el.scroll_into_view_if_needed()
                return ToolResult.ok(f"scrolled {selector} into view")
        amount = int(args.get("amount", 800))
        if args.get("direction") == "up":
            amount = -amount
        await page.mouse.wheel(0, amount)
        return ToolResult.ok(f"scrolled {amount}px")

    async def wait(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args.get("selector")
        if selector:
            state = args.get("state", "visible")
            # Cap the model-supplied timeout so a single wait can't stall the run (mirrors the 20s sleep cap).
            timeout_ms = min(int(args.get("timeout_ms", 15000)), 30000)
            await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            return ToolResult.ok(f"{selector} is {state}")
        await asyncio.sleep(min(float(args.get("time_ms", 1000)) / 1000.0, 20.0))
        return ToolResult.ok("waited")

    async def navigate(args: dict[str, Any]) -> ToolResult:
        from skyvern.utils.url_validators import validate_fetch_url

        page, error = await _resolve_page()
        if error is not None:
            return error
        requested = args["url"]
        resolved = _resolve_text(requested)
        # Payload provenance means an opaque token was resolved, not any substitution (a credential
        # placeholder resolves too, but a page reached through one is the model's own to see).
        from_ref = opaque_refs is not None and opaque_refs.resolve(requested) != requested
        url = await asyncio.to_thread(validate_fetch_url, resolved)
        # Destructive same-URL reload guard: a full reload of the page we are already on discards any
        # in-progress form state (filled fields, an attached file) — an unforced state-wipe the loop
        # otherwise scores as progress. Refuse it once with an actionable message; a repeat to the same
        # URL confirms the model means to reset and is allowed through.
        target_canonical = canonical_url(url)
        same_page = canonical_url(await _url(page)) == target_canonical
        filled = await _count_filled_fields(page) if same_page else 0
        if filled > 0:
            pending = _reload_confirm_pending[0]
            # Confirm only a repeat whose at-risk state did not grow since the refusal: a file attached
            # after the first warning must be re-refused, not silently wiped by a stale confirmation.
            # Keyed by URL + count, not page identity or field content: a second page on the same
            # canonical URL, or a same-count content swap, falls open to a single unguarded reload (the
            # pre-guard behavior), never a new failure.
            if pending is not None and pending[0] == target_canonical and filled <= pending[1]:
                _reload_confirm_pending[0] = None
                LOG.info("taskv3 navigate destructive-reload guard confirmed on repeat")
            else:
                _reload_confirm_pending[0] = (target_canonical, filled)
                LOG.info("taskv3 navigate destructive-reload guard refused", filled_fields=filled)
                return ToolResult.error(
                    "already on this page and it has filled fields (including any attached file); reloading "
                    "it would discard them. Act on the current page instead — or, if you intend to reset the "
                    "form, navigate here again to confirm."
                )
        else:
            _reload_confirm_pending[0] = None
        try:
            response = await page.goto(url, timeout=60000, wait_until="load")
        except Exception as exc:
            if not from_ref:
                raise
            # Playwright names the URL that failed, which after a redirect is not the ref: every URL
            # in the cause was reached by following the ref, so the model sees it as the token.
            return ToolResult.error(f"navigation failed: {URL_IN_TEXT.sub(lambda _m: requested, str(exc))}")
        landed = await _url(page)
        # A payload ref that redirects hands its provenance to wherever it lands, so a credential the
        # landing URL carries is masked at the boundary exactly like the ref itself. An error page
        # (chrome-error://) is not a landing.
        if from_ref and landed.startswith(("http://", "https://")) and canonical_url(landed) != canonical_url(url):
            assert opaque_refs is not None
            opaque_refs.derive(landed)
        # Surface the HTTP status: an error page otherwise reads as a successful navigation, hiding
        # dead URLs and blank shells from the model.
        status = f" (HTTP {response.status})" if response is not None else ""
        # page_state_changed tells the loop's action-loop guard the world moved: a re-attempt after
        # a navigation is a fresh attempt, not a repeat against unchanged state.
        data: dict[str, Any] = {"page_state_changed": True}
        # A hard 404/410 landing is a dead/removed target: flag it so the loop ends the run as
        # terminated (v1's behavior) rather than defaulting the outcome to failed.
        if response is not None and response.status in NAVIGATION_DEAD_END_STATUSES:
            data["navigation_dead_end"] = response.status
        return ToolResult.ok(f"navigated to {landed}{status}", data=data)

    async def file_upload(args: dict[str, Any]) -> ToolResult:
        # Lazy import: keeps this module importable for unit tests without the full forge/storage graph.
        from skyvern.forge.sdk.api.files import download_file

        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        # Validate the selector before fetching: an invalid or missing selector fails here, before anything
        # is staged into downloads_dir, so the selector guard's residual error can never leave a phantom
        # upload for the download-signal wrapper to misread as a browser download.
        if await page.query_selector(selector) is None:
            return ToolResult.error(f"no file input for selector {selector!r}")
        source = _resolve_text(args["file"])
        # A failed download echoes the source back in the loop's generic tool_error; the model-facing
        # masking boundary (hide_from_model) rewrites any signed payload ref to its token there, so this
        # handler no longer catches locally just to mask the URL (the SKY-14492 retype case).
        local_path = await download_file(source, output_dir=downloads_dir, organization_id=organization_id)
        # For http(s) sources download_file stages into downloads_dir; naming the file lets the
        # download-signal wrapper suppress it without swallowing unrelated downloads that complete
        # during this call (for other schemes the key is inert — nothing in the dir matches).
        staged = {"staged_download": os.path.basename(local_path)}
        # Re-resolve after the download: a rerender during a slow fetch can detach the earlier handle, so
        # bind the element fresh immediately before uploading (missing now means it vanished mid-download).
        el = await page.query_selector(selector)
        if el is None:
            return ToolResult(
                "error", f"no file input for selector {selector!r}", {**staged, "page_state_changed": True}
            )
        # Verify the upload took EFFECT, not just that set_input_files did not raise. Watch upload-like
        # network dispatches across the set_input_files + settle window (the window we already dwell in,
        # so this adds no latency); a genuine upload dispatches at least one, a silent no-op none.
        probe = _UploadActivityProbe(page)
        # Read before the attach on every call: the consume-and-clear check needs the pre-attach text,
        # and whether it will be needed is only known afterwards. One local evaluate, no wait.
        text_before = await _page_rendered_text(page)
        probe.start()
        try:
            await el.set_input_files([local_path])
            populated = await _input_holds_file(el)
            # Settle + a small randomized delay so the upload and a following submit are not dispatched
            # in the same instant, matching v1's upload cadence (the engine that clears this step reliably).
            await _settle_after_upload(page)
            await _upload_submit_delay()
        finally:
            probe.stop()
        if not populated:
            # A consume-and-clear dropzone reads the file on change, uploads it and resets the input, so
            # an empty control after a genuine upload is normal there. Confirming it needs every signal
            # a silent no-op cannot fake at once: the file's own name newly rendered on the page AND an
            # upload dispatched (a client-side rejection names the file but sends nothing; ambient
            # traffic sends but never names it), and no rejection wording anywhere the site newly rendered.
            text_after = await _page_rendered_text(page)
            shown_newly = (
                text_before is not None
                and text_after is not None
                and not _mentions_filename(text_before, local_path)
                and _mentions_filename(text_after, local_path)
            )
            if shown_newly:
                assert text_before is not None and text_after is not None
                new_lines = _newly_rendered_lines(text_before, text_after)
                new_text = "\n".join(new_lines)
                said = " | ".join(line for line in new_lines if _mentions_filename(line, local_path))
                said = said[:_FILENAME_MENTION_CHARS]
                if probe.saw_upload() and not _UPLOAD_REJECTION_WORDS.search(new_text):
                    LOG.info("taskv3 file_upload input cleared after attach but the page shows the uploaded file")
                    return ToolResult.ok(
                        f"uploaded 1 file to {selector} (the site consumed the file and now shows it: {said!r})",
                        staged,
                    )
                LOG.info(
                    "taskv3 file_upload input cleared after attach; page names the file without confirming it",
                    upload_activity=probe.saw_upload(),
                )
                return ToolResult(
                    "error",
                    f"the file input {selector} is empty after the attach and the page now says {said!r} — "
                    f"re-observe to confirm the file was accepted before submitting",
                    staged,
                )
            return ToolResult("error", f"file did not attach to {selector} — re-observe the field", staged)
        if not probe.saw_upload():
            # The file is on the input but the site never reacted: report a recoverable error (not a
            # confident OK) so the loop re-verifies before submitting. A submit-time-upload form lands
            # here too and costs one re-plan turn, never a lost file.
            return ToolResult(
                "error",
                f"attached the file to {selector} but observed no upload activity — re-observe the field "
                f"to confirm the file is shown before submitting; if the form uploads on submit this may "
                f"be expected",
                staged,
            )
        return ToolResult.ok(f"uploaded 1 file to {selector}", staged)

    async def select_combobox(args: dict[str, Any]) -> ToolResult:
        # Explicit typeahead fill (type() also drives this automatically). Routes through the shared
        # custom-combobox commit path: type the value, WAIT for the async suggestion list, pick the
        # best-matching suggestion, and VERIFY the field committed. Fails loudly if nothing matches
        # rather than leaving raw typed text the widget won't accept as a valid selection.
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        ambiguous = await _ambiguous_selector_error(page, selector)
        if ambiguous is not None:
            return ambiguous
        selector = await _resolve_mirrored_host_control(page, selector)
        value = _resolve_text(args["value"])
        return await _commit_custom_combobox(page, selector, value)

    async def _clear_look_tags(page: Any) -> None:
        try:
            await page.evaluate(
                "() => { const _q = " + _ROOT_QUERY_JS + "; "
                "_q.all('[data-tv3-look]').forEach((e) => e.removeAttribute('data-tv3-look')); }"
            )
        except Exception:
            pass

    async def _clear_act_tags(page: Any) -> None:
        try:
            await page.evaluate(
                "() => { const _q = " + _ROOT_QUERY_JS + "; "
                "_q.all('[data-tv3-act]').forEach((e) => e.removeAttribute('data-tv3-act')); }"
            )
        except Exception:
            pass

    async def look(_args: dict[str, Any]) -> ToolResult:
        if _look_count[0] >= _LOOK_MAX_PER_RUN:
            return ToolResult.error(
                f"look budget reached ({_LOOK_MAX_PER_RUN} per run) — rely on observe/get_html and act on "
                "what you already saw instead of looking again."
            )
        page, error = await _resolve_page()
        if error is not None:
            return error
        _look_count[0] += 1
        # Passive read + server-side render. The screenshot is a viewport frame (device px); the marks
        # are enumerated separately so the boxes are drawn in PIL, never injected into the DOM.
        try:
            png = await page.screenshot()
        except Exception as exc:
            LOG.warning("taskv3 look screenshot failed", exc_info=True)
            return ToolResult.error(f"look failed to capture the page: {type(exc).__name__}: {exc}")
        try:
            data = await asyncio.wait_for(page.evaluate(_LOOK_ENUM_JS), timeout=30)
        except Exception as exc:
            await _clear_look_tags(page)
            LOG.warning("taskv3 look enumeration failed", exc_info=True)
            return ToolResult.error(f"look failed to enumerate controls: {type(exc).__name__}: {exc}")
        elements = data.get("elements", []) if isinstance(data, dict) else []
        vw = int(data.get("vw") or 0) if isinstance(data, dict) else 0
        # Release the prior look's retained handles before minting a new set (they leak in the driver
        # otherwise), then grab one live handle per mark while the transient index is still on the DOM.
        for old in _look_manifest.values():
            try:
                await old["handle"].dispose()
            except Exception:
                pass
        _look_manifest.clear()
        # From here on the old marks are gone, whatever happens next -- the loop keys on this.
        renumbered = {"marks_renumbered": True}
        for e in elements:
            n = int(e["n"])
            try:
                handle = await page.query_selector(f'[data-tv3-look="{n}"]')
            except Exception:
                handle = None
            if handle is None:
                continue
            _look_manifest[n] = {
                "handle": handle,
                "tag": e.get("tag", ""),
                "label": e.get("label", ""),
            }
        await _clear_look_tags(page)
        # Draw ONLY the marks we retained a handle for, so every number on the image is one the model
        # can actually act on (a control that detached between enumeration and handle-grab is dropped
        # from both the image and the legend, never shown as an unusable box).
        kept = [e for e in elements if int(e["n"]) in _look_manifest]
        try:
            annotated = await asyncio.get_running_loop().run_in_executor(None, _annotate_screenshot, png, kept, vw)
        except Exception as exc:
            LOG.warning("taskv3 look annotation failed", exc_info=True)
            return ToolResult.error(f"look failed to render marks: {type(exc).__name__}: {exc}", data=renumbered)
        if not kept:
            return ToolResult.ok(
                "look: no interactive controls are visible in the viewport. Scroll or re-observe.",
                data=renumbered,
                screenshots=[annotated],
            )

        # Mask payload-minted signed URLs, then truncate for display — in that ORDER. A label taken
        # from an input's value can carry a resolved presigned URL; masking (as observe and get_html do)
        # rewrites it to its opaque token so the model can't retype it, and truncating first would sever
        # the URL past the provenance match and leak a partial signed URL into the transcript.
        def _label(raw: object, width: int = 80) -> str:
            return _digest_token(_mask_refs(str(raw)), width)

        lines = [
            f"[{int(e['n'])}] {_digest_token(e.get('tag', ''), 20)} {_label(e.get('label', ''))!r}"
            + (f" placeholder={_label(e['placeholder'], 60)!r}" if e.get("placeholder") else "")
            for e in kept
        ]
        header = (
            f"look: {len(kept)} visible control(s), numbered on the screenshot. Act on one with "
            "click(mark=N) or type(mark=N, text=...)."
        )
        if isinstance(data, dict) and data.get("truncated"):
            header += f" (only the first {_LOOK_MAX_MARKS} are marked; scroll for more.)"
        legend = header + "\n" + "\n".join(lines)
        return ToolResult.ok(legend, data=renumbered, screenshots=[annotated])

    async def _resolve_mark(page: Any, mark: int) -> tuple[str | None, ToolResult | None]:
        # Turn mark=N into a selector the existing click/type handlers act through. Resolution is the
        # SAME live element handle look retained (Playwright's engine, which pierces open shadow), tagged
        # data-tv3-act=N at act time so the marker branch uniqueness-checks and commit-verifies it like
        # any other marker. A detached handle errors rather than re-guessing by coordinates: a stale
        # look-time point could hit whatever now occupies those pixels after a scroll, which is exactly
        # the wrong-element class this must not introduce.
        entry = _look_manifest.get(mark)
        if entry is None:
            return None, ToolResult.error(
                f"mark {mark} is not in the current set of marks. Call look() first, then act on a number it drew."
            )
        await _clear_act_tags(page)
        handle = entry.get("handle")
        connected = False
        if handle is not None:
            try:
                connected = bool(await handle.evaluate(_LOOK_TAG_HANDLE_JS, mark))
            except Exception:
                connected = False
        if not connected:
            return None, ToolResult.error(
                f"mark {mark} no longer points to an element on the page — it moved or the page "
                "re-rendered since look(). Call look() again and act on a fresh number.",
                data={"page_state_changed": True},
            )
        return f'[data-tv3-act="{mark}"]', None

    def _with_act_by_mark(handler: ToolHandler) -> ToolHandler:
        async def wrapped(args: dict[str, Any]) -> ToolResult:
            mark = args.get("mark")
            if mark is None:
                return await handler(args)
            if args.get("selector"):
                return ToolResult.error("Pass either mark or selector to act on a control, not both.")
            try:
                mark_int = int(mark)
            except (TypeError, ValueError):
                return ToolResult.error(f"mark must be an integer from the last look(), got {mark!r}.")
            page, error = await _resolve_page()
            if error is not None:
                return error
            selector, mark_error = await _resolve_mark(page, mark_int)
            if mark_error is not None:
                return mark_error
            try:
                return await handler({**args, "selector": selector})
            finally:
                await _clear_act_tags(page)

        return wrapped

    tools = [
        _spec(
            "observe",
            'Snapshot the page\'s visible interactive elements (raw DOM) with a CSS selector, label, type, value, and options for each. A selector printed as [data-tv3-ref="N"] is a short handle for a control whose real id is long and opaque; copy it exactly as printed. Also reports cross-origin iframes present (host + captcha signature); their contents cannot be observed or reached by selector. Call once per page, then act by selector.',
            _obj({}),
            observe,
        ),
        _spec(
            "get_html",
            "Get raw outer/inner HTML of the page or a specific element (for detail beyond observe).",
            _obj({"selector": {"type": "string", "description": "CSS selector; omit for whole page"}}),
            get_html,
        ),
        _spec(
            "look",
            "Rare last resort: take ONE annotated screenshot of the viewport when the text tools are "
            "insufficient — the layout is confusing, a control you expect isn't in observe (custom/"
            "shadow-DOM widgets), or an action isn't taking and you can't tell why. Returns the page "
            "image with every visible control boxed and numbered, plus a legend. Then act on a number "
            "with click(mark=N) or type(mark=N, text=...). NEVER call it just to double-check observe.",
            _obj({}),
            look,
        ),
        _spec(
            "click",
            "Click an element by CSS selector (or by mark=N from the last look()). If the click opens a "
            'menu of options, the result lists them with [data-tv3-menu="N"] selectors — click one of '
            "those to select (verified: you get a loud error, not a silent no-op, if the selection does "
            "not commit; do not blindly repeat a failed click). If the click triggers a file download, "
            "the tool result reports it when detected.",
            _obj(
                {
                    "selector": {"type": "string"},
                    "mark": {
                        "type": "integer",
                        "description": "A number from the last look(); use instead of selector",
                    },
                },
            ),
            click,
        ),
        _spec(
            "hover",
            "Hover over an element by CSS selector (e.g. to open a hover menu).",
            _obj({"selector": {"type": "string"}}, ["selector"]),
            hover,
        ),
        _spec(
            "type",
            "Type text into an input/textarea by CSS selector (or by mark=N from the last look()); "
            "clears first by default.",
            _obj(
                {
                    "selector": {"type": "string"},
                    "mark": {
                        "type": "integer",
                        "description": "A number from the last look(); use instead of selector",
                    },
                    "text": {"type": "string"},
                    "clear": {"type": "boolean"},
                    "press_enter": {"type": "boolean"},
                },
                ["text"],
            ),
            type_text,
        ),
        _spec(
            "select_option",
            "Choose an option in a <select> by value or visible label.",
            _obj(
                {"selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"}}, ["selector"]
            ),
            select_option,
        ),
        _spec(
            "select_combobox",
            "Fill an autocomplete/typeahead/combobox field (location, school, employer lookups): types the "
            "value, waits for the suggestion list to render, selects the best-matching suggestion, and "
            "verifies the field committed. Use this INSTEAD of `type` for such fields — it errors if no "
            "suggestion matches so you never leave uncommitted raw text.",
            _obj({"selector": {"type": "string"}, "value": {"type": "string"}}, ["selector", "value"]),
            select_combobox,
        ),
        _spec(
            "press_key",
            "Press a keyboard key (optionally focused on a selector), e.g. Enter, Escape, Tab.",
            _obj({"key": {"type": "string"}, "selector": {"type": "string"}}, ["key"]),
            press_key,
        ),
        _spec(
            "scroll",
            "Scroll the page (direction up/down + amount) or scroll a selector into view.",
            _obj(
                {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer"},
                    "selector": {"type": "string"},
                }
            ),
            scroll,
        ),
        _spec(
            "wait",
            "Wait for a selector to reach a state (visible/attached/hidden) or wait a fixed time_ms.",
            _obj(
                {
                    "selector": {"type": "string"},
                    "state": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                    "time_ms": {"type": "integer"},
                }
            ),
            wait,
        ),
        _spec("navigate", "Navigate the browser to a URL.", _obj({"url": {"type": "string"}}, ["url"]), navigate),
        _spec(
            "file_upload",
            "Upload a file (local path or URL) into a file input by CSS selector.",
            _obj({"selector": {"type": "string"}, "file": {"type": "string"}}, ["selector", "file"]),
            file_upload,
        ),
    ]
    if not vision_enabled:
        # A non-vision model drops the screenshot before the request, so `look` would advertise an
        # image the model never sees. Drop the tool entirely; a `mark=N` with no look then just errors
        # "not in the current set of marks" (a clean no-op), so click/type need no further change.
        tools = [t for t in tools if t.name != "look"]
    for _tool_spec in tools:
        if _tool_spec.name in (
            "click",
            "hover",
            "type",
            "select_option",
            "select_combobox",
            "press_key",
            "file_upload",
        ):
            _tool_spec.billable = True
        if _tool_spec.name in ("observe", "get_html", "look"):
            # Large perception dumps: only the latest snapshot is relevant, so let the loop elide older
            # ones from the re-sent transcript (bounds context on perception-heavy pages). look's legend
            # (not its ephemeral image, which never enters the transcript) rides the same rule.
            _tool_spec.compactable = True
        if _tool_spec.name in PREFLIGHT_TOOL_NAMES:
            _tool_spec.handler = _with_preflight(_tool_spec.name, _tool_spec.handler, page_provider, _prefetched_page)
        if _tool_spec.name in _SELECTOR_GUARD_TOOL_NAMES:
            # Outside preflight (it builds its action from the normalized selector), inside act_by_mark
            # (mark=N resolves to a selector first), so every selector tool inherits the guard.
            _tool_spec.handler = _with_alias_resolution(_with_selector_guard(_tool_spec.handler))
        if _tool_spec.name in ("click", "type"):
            # OUTERMOST wrapper: resolve mark=N to a selector before preflight builds its action from
            # args["selector"], so the whole verified click/type path (uniqueness gate, commit-verify)
            # runs on the act-by-mark selector unchanged.
            _tool_spec.handler = _with_act_by_mark(_tool_spec.handler)
    _apply_download_signal(tools, downloads_dir)
    return tools


_DOWNLOAD_UUID_INFIX_RE = re.compile(r"\.[0-9a-f]{32}$")
_DOWNLOAD_SIGNAL_MAX_LINES = 5
# Filenames are server-controlled and get surfaced into the LLM transcript: strip control chars plus
# Unicode line/paragraph separators, zero-width, and bidi-control characters from the DISPLAYED name
# (seen-set tracking keeps the raw filesystem name).
_DOWNLOAD_NOTICE_SANITIZE_RE = re.compile(
    "[\\x00-\\x1f\\x7f\\u0085\\u2028\\u2029\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)


def _digest_token(value: object, cap: int) -> str:
    """Strip anything a page could use to forge a digest line: line separators, bidi overrides,
    zero-width joiners. For payload fields printed bare rather than through `!r`.

    For `role` and `type` this is the second of two layers -- both are gated at their source
    (`role` against a whitelist, `type` to the tags whose UA normalises it), so removing either
    layer alone leaves the payload safe, and removing both forges a line.

    For `tag` it is the ONLY layer, and a tag name is page-controlled. LF and CR never reach one,
    but U+0085/U+2028/U+2029 do: the HTML tokenizer ends a tag name on ASCII whitespace only, so
    `<a\\u2028b>` parses to a tagName carrying the separator. `createElement` is stricter and
    rejects the same name on some builds but not others, which is why the parser is the case that
    matters. None of them forges a whole element line -- a tag name may not hold `[`, a space or a
    quote -- but they do split a digest line in two.
    """
    return _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(value))[:cap]


def _download_signal_identity(name: str) -> str:
    """`report.pdf.<32-hex-uuid>.crdownload` -> `report.pdf` (see cdp_download_interceptor's temp
    naming), so an in-progress file and its completed rename are recognized as the same download.
    The uuid strip applies only to suffix-carrying temp names — a completed file legitimately named
    `export.<32hex>` must keep its full identity."""
    if name.endswith(BROWSER_DOWNLOADING_SUFFIX):
        name = name[: -len(BROWSER_DOWNLOADING_SUFFIX)]
        return _DOWNLOAD_UUID_INFIX_RE.sub("", name)
    return name


def _human_download_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    size = num_bytes / 1024
    for unit in ("KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _apply_download_signal(tools: list[ToolSpec], downloads_dir: str | None) -> None:
    """Wrap every tool in the given list so a file landing in `downloads_dir` during (or between)
    calls is reported in the next tool result, without a dedicated tool call. Tools assembled later
    (finish, auth/captcha extras) are not wrapped; a download landing during those surfaces on the
    next wrapped call. State (seen files, pending re-delivery lines) is shared across all wrapped
    tools via this closure, one instance per build_browser_tools call. No-op without downloads_dir."""
    if not downloads_dir:
        return

    seen_completed: set[str] = set()
    seen_started: set[str] = set()
    pending: list[str] = []
    baseline = {"done": False}

    def _list_split() -> tuple[list[str], list[str]]:
        try:
            names = sorted(os.listdir(downloads_dir))
        except OSError:
            return [], []
        completed = [n for n in names if not n.endswith(BROWSER_DOWNLOADING_SUFFIX)]
        in_progress = [n for n in names if n.endswith(BROWSER_DOWNLOADING_SUFFIX)]
        return completed, in_progress

    for tool_spec in tools:

        async def wrapped(
            args: dict[str, Any],
            _handler: Callable[[dict[str, Any]], Awaitable[ToolResult]] = tool_spec.handler,
            _compactable: bool = tool_spec.compactable,
            _tool_name: str = tool_spec.name,
        ) -> ToolResult:
            if not baseline["done"]:
                baseline["done"] = True
                try:
                    # Snapshot BEFORE the first handler runs, so a download triggered by the very
                    # first tool call is reported rather than absorbed into the baseline.
                    completed0, in_progress0 = _list_split()
                    seen_completed.update(completed0)
                    seen_started.update(_download_signal_identity(n) for n in in_progress0)
                except Exception:
                    LOG.warning("taskv3 download signal baseline snapshot failed", tool=_tool_name, exc_info=True)
            result = await _handler(args)
            try:
                # A tool that stages its own file into downloads_dir (file_upload) names it in
                # result.data; only that exact file is absorbed silently — an unrelated download
                # completing during the same call still gets reported.
                staged_name = (result.data or {}).get("staged_download")
                completed, in_progress = _list_split()
                new_lines: list[str] = []
                for name in completed:
                    if name in seen_completed:
                        continue
                    seen_completed.add(name)
                    seen_started.add(_download_signal_identity(name))
                    if name == staged_name:
                        continue
                    try:
                        size = os.path.getsize(os.path.join(downloads_dir, name))
                    except OSError:
                        size = 0
                    display = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", name)
                    new_lines.append(f"Downloaded: {display} ({_human_download_size(size)})")
                for name in in_progress:
                    identity = _download_signal_identity(name)
                    if identity in seen_started or identity in seen_completed:
                        continue
                    seen_started.add(identity)
                    display = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", identity)
                    new_lines.append(f"Download started: {display} (in progress — not yet complete)")
                deliver = list(dict.fromkeys(pending + new_lines))
                if not deliver:
                    pending[:] = []
                    return result
                capped = deliver[:_DOWNLOAD_SIGNAL_MAX_LINES]
                overflow = len(deliver) - len(capped)
                if overflow > 0:
                    capped = capped + [f"+{overflow} more files downloaded"]
                pending[:] = deliver if _compactable else []
                # The flag lets the loop's action-loop guard treat the download as progress without
                # sniffing the notice lines back out of the content string. Preserve screenshots so a
                # result that also carried a look image (or any future image) is not silently dropped.
                return ToolResult(
                    result.status,
                    result.content + "\n" + "\n".join(capped),
                    {**(result.data or {}), "download_notice": True},
                    result.screenshots,
                )
            except Exception:
                LOG.warning("taskv3 download signal computation failed", tool=_tool_name, exc_info=True)
                return result

        tool_spec.handler = wrapped


def _with_preflight(
    name: str,
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
    page_provider: PageProvider,
    prefetched_page: list[Any] | None = None,
) -> Callable[[dict[str, Any]], Awaitable[ToolResult]]:
    async def wrapped(args: dict[str, Any]) -> ToolResult:
        page = await page_provider()
        if page is not None:
            preflight_tool_action(name, args, page)
            if prefetched_page is not None:
                prefetched_page.append(page)
        try:
            return await handler(args)
        finally:
            if prefetched_page is not None:
                prefetched_page.clear()

    return wrapped
