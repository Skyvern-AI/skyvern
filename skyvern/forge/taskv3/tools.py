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
import json
import os
import re
from typing import Any, Awaitable, Callable

import structlog

from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX
from skyvern.forge.taskv3.loop import ToolResult, ToolSpec
from skyvern.forge.taskv3.preflight import PREFLIGHT_TOOL_NAMES, preflight_tool_action

LOG = structlog.get_logger()

# Resolved fresh per tool call rather than a page bound once, so a click that opens a new
# tab/popup is followed on the next call instead of leaving the loop stuck on a stale page.
PageProvider = Callable[[], Awaitable[Any]]

PAGE_UNAVAILABLE_ERROR = "browser page unavailable"

# Cap on the page URL observe() echoes. Callers that register a secret URL for exact-match redaction
# must register this prefix too, or the truncated echo survives the scrub.
OBSERVE_URL_MAX_CHARS = 300

# The exact selector shapes our own enrichment mints: data-tv3 by observe(), data-tv3-menu by the
# click menu probe. Either exists only where we set it, so one that matches nothing now cannot
# reappear without a fresh observe / menu-opening click.
_TV3_MARKER_SELECTOR_RE = re.compile(r'^\[data-tv3(?:-menu)?="[^"\\]+"\]$')

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
_FIND_SUGGESTION_JS = (
    r"""(args) => {"""
    + _PIERCED_QUERY_JS
    + r"""
  const STOP = """
    + _STOPWORDS_JS
    + r""";
  const toks = (s) => new Set(String(s).toLowerCase().replace(/[\/,]/g, ' ').split(/\s+/).filter((w) => w.length >= 3 && !STOP.has(w)));
  const want = toks(args.value || '');
  pQSA('[data-tv3-sugg]').forEach((e) => e.removeAttribute('data-tv3-sugg'));
  if (!want.size || !preReady()) return null;
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
    const role = el.getAttribute('role');
    // never click something navigational (would leave the form) unless it's explicitly an option
    const nav = (tag === 'A' && el.hasAttribute('href')) || tag === 'BUTTON' || role === 'button' || role === 'link' || role === 'menuitem' || role === 'tab';
    if (nav && role !== 'option') continue;
    if (el.children.length > 8) continue;                             // a suggestion row, not a big container
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > 120) continue;  // visible, row-sized (allows a 2-line row)
    if (fr) {                                                          // in the dropdown region: below, or above if it flipped up
      if (r.top < fr.top - 400 || r.top > fr.bottom + 500) continue;
      if (r.right < fr.left || r.left > fr.right) continue;
    }
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 80) continue;
    const have = toks(txt);
    let score = 0;
    for (const w of want) if (have.has(w)) score++;
    if (score > 0) cands.push({ el, score, h: r.height });
  }
  if (!cands.length) return null;
  // Drop any candidate that CONTAINS another candidate (a dropdown container over its own rows), then
  // take the highest score, breaking ties toward the smallest (innermost) row.
  const leaves = cands.filter((c) => !cands.some((o) => o.el !== c.el && pContains(c.el, o.el)));
  const pool = leaves.length ? leaves : cands;
  pool.sort((a, b) => b.score - a.score || a.h - b.h);
  const best = pool[0];
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
  return { text: (best.el.innerText || '').trim(), score: best.score };
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
  const el = pQS(args.field) || (args.el && args.el.isConnected ? args.el : null);
  // null (not '') when there is nothing to read: the caller must tell "read it, no commit" from
  // "could not read it", and a later second probe would answer about a different instant.
  if (!el) return null;
  const typed = String(args.typed || '').trim();
  const chosen = String(args.chosen || '').trim() || typed;
  const cur = (el.value || '').trim();
  const tagged = pQS('[data-tv3-sugg]');
  const listClosed = !tagged || tagged.getBoundingClientRect().height === 0;
  // A short normalized value ("New York" -> "NY", "United States" -> "US") has no >=3-char token to
  // overlap, so accept it on causality alone (it changed / the list closed). Longer values must still
  // relate to the chosen suggestion so an unrelated change can't read as a successful commit.
  if (cur && (cur !== typed || listClosed) && (toks(cur).size === 0 || overlaps(cur, chosen) || overlaps(cur, typed))) return cur;
  const cont = el.closest('div,li,fieldset');
  if (cont) {
    for (const h of cont.querySelectorAll('input[type=hidden]')) {
      const v = (h.value || '').trim();
      if (v && (overlaps(v, chosen) || overlaps(v, typed))) return v;
    }
  }
  return '';
}"""
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
  // elementFromPoint sees the top-level document only, so a control inside a component resolves to
  // its host. Treat that as reachable: the host IS what a real click lands on, and the browser
  // retargets the event inward. The walk must hop ShadowRoot -> host, because Node.contains stays in
  // the light tree and would read every component control as covered by its own host.
  const related = (a, b) => {
    for (let n = b; n; n = n.parentNode || n.host || null) if (n === a) return true;
    return false;
  };
  if (!top || top === el || related(el, top)) return out;
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
      visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden',
      disabled: !!el.disabled,
      proxied: !!_visibleProxy(el),
    };
  } catch (e) { return { exists: false, visible: false }; }
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
        break;
      }
      if (pContains(target, el)) containsMenu = true;
    }
  }
  preReset();
  pScopeEach((el, inShadow) => { if (vis(el)) preMark(el, inShadow); });
  return { menuOpen: openRows.length > 0, isOption, containsMenu, optText, optState, optSel, optKids, optH };
}"""
)

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
  let stillOpen = 0;
  const rows = [];
  for (const el of pQSA('[data-tv3-menu]')) if (vis(el)) { stillOpen++; rows.push(el); }
  let target = null;
  try { target = pQS(clicked) || (arg.el && arg.el.isConnected ? arg.el : null); } catch (e) { target = (arg.el && arg.el.isConnected ? arg.el : null); }
  let optState = '';
  let optSel = '';
  let optKids = -1;
  let optH = -1;
  if (target) {
    for (const el of rows) {
      if (el === target || pContains(el, target)) {
        optState = state(el);
        optSel = selState(el);
        optKids = el.children.length;
        optH = Math.round(el.getBoundingClientRect().height);
        break;
      }
    }
  }
  return { stillOpen, optState, optSel, optKids, optH };
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
    if (preHas(el)) continue;
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
    if (!boundaryStandIns.has(p) && preHas(p)) continue;
    // A dialog is a page mode, not a menu — mislabeling it invites a wrong "pick an option" move.
    try { if (p.closest('dialog,[role~="dialog"],[aria-modal="true"]')) continue; } catch (e) {}
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
  return { count: n, options };
}"""
)

# Page total for `group` text across one observe; per entry stays at 200 characters.
OBSERVE_GROUP_TEXT_TOTAL_CAP = 4000

# Raw DOM perception: collect visible interactive elements with a stable selector each.
# Elements without a natural selector get a data-tv3 marker so later actions can target them.
_OBSERVE_JS = (
    r"""
() => {
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
  const hostScopes = (host) => {
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
  const hostAnchored = (el, host) => {
    if (!host || !el.id) return null;
    const raw = String(el.id);
    if (_FORGEABLE.test(raw)) return null;
    const esc = window.CSS && CSS.escape ? CSS.escape(raw) : null;
    const ctrl = esc === raw && raw === raw.trimEnd() ? '#' + esc : attr('id', raw);
    if (!scopedResolvesTo(host, ctrl, el)) return null;
    // naturalSelector reports its cause through shared state; the control's own cause is already
    // settled by the time we get here and must survive naming the host.
    const why = naturalWhy;
    const inconclusive = checkInconclusive;
    const hostSel = naturalSelector(host);
    naturalWhy = why;
    checkInconclusive = inconclusive;
    // A host we could not name is left alone rather than marked. Minting here would hand out a
    // handle on the one page where uniqueness could not be checked -- an unreadable root makes the
    // host's own count inconclusive, and mintOn keeps a marker it could not verify.
    if (!hostSel) return null;
    return hostSel + ' ' + ctrl;
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
  let unnamedAnonymous = 0;
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
  let truncated = 0;
  let truncatedInComponents = 0;
  let lastGroup = '';
  let groupTotal = 0;
  for (let idx = 0; idx < els.length; idx++) {
   const el = els[idx].el;
   const host = els[idx].host;
   let mintedValue = null;
   let minted = null;
   // A form exposes its named controls as its own properties, so <input name="tagName"> makes
   // el.tagName that input. Every read below can therefore be a clobbered non-function, and the
   // loop is inside page.evaluate: one throw costs the whole element list, not one element.
   try {
    const r = el.getBoundingClientRect();
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
      } else {
        continue;
      }
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
        if (naturalWhy === 'duplicated') selector = hostAnchored(el, host);
        if (!selector) {
          if (naturalWhy === 'duplicated') unnamedDuplicated++;
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
    if (!strongLabel && el.labels && el.labels[0]) strongLabel = (el.labels[0].innerText || '').trim();
    if (!strongLabel) strongLabel = byId('aria-labelledby');
    if (!strongLabel) strongLabel = (el.innerText || '').trim();
    let label = (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim() || strongLabel;
    if (!label) label = (el.type === 'password' ? '' : el.value || '').trim();
    const role = el.getAttribute('role');
    // el.type is only trustworthy where the UA normalises it to a known keyword. On INPUT, BUTTON
    // and SELECT it is a reflected enum; on <a>, <link>, <embed>, <object> and <source> it hands
    // back the raw attribute, so `type` there is a page-controlled string that reached the rendered
    // line -- and a MIME type is noise to the model anyway.
    const _typed = el.tagName === 'INPUT' || el.tagName === 'BUTTON' || el.tagName === 'SELECT';
    const rec = { i, tag: el.tagName.toLowerCase(), type: (_typed && el.type) || null, selector, label: label.slice(0, 140) };
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
    if (el.type === 'password') { if (el.value) rec.value = '(hidden)'; } else if (el.value) rec.value = String(el.value).slice(0, 100);
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
      if (now !== null && !rec.value) rec.value = String(now).slice(0, 100);
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
      rec.invalid = (el.validationMessage || '').slice(0, 140) || true;
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
      const gt = (_groupText(el, isChoice) || byId('aria-describedby')).slice(0, 200);
      if (gt && gt.length > strongLabel.length && gt !== lastGroup && groupTotal + gt.length <= _GROUP_TEXT_TOTAL_CAP) {
        rec.group = gt;
        lastGroup = gt;
        groupTotal += gt.length;
      }
    }
    const pressed = el.getAttribute('aria-pressed');
    if (pressed === 'true' || pressed === 'false') rec.pressed = pressed === 'true';
    if (minted !== null) minted.rec = rec;
    if (hidden) hiddenListed++;
    out.push(rec);
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
  // A marker we wrote can be gone by the end of the walk: a component that mirrors attributes moves
  // it onto a peer, and the element we named is then addressed by a selector matching nothing. One
  // attribute read per named element, no re-query -- a natural selector cannot be invalidated this
  // way, so only minted ones are checked.
  for (const rem of mintedOn) {
    let still = null;
    try { still = rem.el.getAttribute('data-tv3'); } catch (e) { still = null; }
    // A record never built (the element threw mid-walk) was never handed out either.
    if (rem.rec === null || still !== rem.m) {
      const at = rem.rec === null ? -1 : out.indexOf(rem.rec);
      if (at !== -1) { out.splice(at, 1); dropped++; }
      if (rem.fresh) markersWritten--; else markersReused--;
    }
  }
  // Page-text digest: outcome states (submission confirmations, rejection banners, validation
  // summaries) live in non-interactive nodes the element list can never carry. Three sources in
  // priority order — ARIA status channels (uncapped within the 900 total), class/id-named message
  // blocks (600, or 300 past what ARIA spent, whichever is larger, still inside the 900), then
  // headings (whatever the 900 leaves) — never a body-text
  // dump, so the digest stays bounded and can't regrow the context that transcript compaction
  // bounds. All three carry page-controlled text at the same trust level as element labels.
  const texts = [];
  let textTotal = 0;
  let textFull = false;
  let textDropped = 0;
  // `limit` is a cumulative reservation: a channel stops at its limit so the channels after it keep
  // a floor of the 900 total instead of being starved by whichever channel ran first.
  const pushText = (t, limit = 900) => {
    t = (t || '').replace(/\s+/g, ' ').trim().slice(0, 300);
    if (!t) return;
    // Containment dedupe, richer message wins: an alert's text re-surfaces inside its heading's
    // parent text, and a terse early entry ("Saved") must not suppress a later superset
    // ("Saved — confirmation #A1B2") — supersets REPLACE their contained entries.
    if (texts.some((s) => s.includes(t))) return;
    const kept = texts.filter((s) => !t.includes(s));
    const keptTotal = kept.reduce((total, s) => total + s.length, 0);
    if (keptTotal + t.length > limit) { textFull = textFull || limit >= 900; textDropped++; return; }
    texts.length = 0; texts.push(...kept, t); textTotal = keptTotal + t.length;
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
    const orderedCands = formMsgs.head.concat(formMsgs.tail, otherMsgs.head, otherMsgs.tail);
    // A per-field state wrapper (`field--has-error`, `field--no-error`) matches this selector and its
    // text is just the control's own name, so it spends the channel's budget on what the element list
    // already carries -- enough of them and the page's real message never fits. Read off the records
    // already built, so recognising them asks the page nothing.
    const listedLabels = new Set();
    for (const r of out) {
      const lb = (r.label || '').replace(/\s+/g, ' ').trim();
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
      if (mayDefer && listedLabels.has(t.replace(decoration, '').slice(0, 140))) { deferred.push(cand); return; }
      if (t.length <= 300 || (t.length <= 900 && src.querySelectorAll('input,select,textarea').length < 2)) pushText(t, blockLimit);
    };
    for (const cand of orderedCands) {
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

  return JSON.stringify({ url: location.href, title: document.title, text: texts, textTruncated: textFull, textDropped: textDropped, iframes: iframeInfo, dropped: dropped, truncated: truncated, truncatedInComponents: truncatedInComponents, unnamedAnonymous: unnamedAnonymous, unnamedDuplicated: unnamedDuplicated, unnamedUnverifiable: unnamedUnverifiable, unnamedUnsafe: unnamedUnsafe, unreadableRoot: sawUnreadableRoot, undiscoveredRoots: undiscoveredRoots, rootCount: allRoots.length - 1, hiddenListed: hiddenListed, markersMinted: markersWritten, markersReused: markersReused, elements: out });
}
"""
)


def _menu_open_note(found: dict[str, Any], selector: str, *, clicked_row: bool = False) -> str:
    count = int(found.get("count") or 0)
    parts = []
    for o in (found.get("options") or [])[:15]:
        # option texts are page-controlled and land in the LLM transcript — same sanitation as filenames
        text = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(o.get("text", "")))
        parts.append(f'[data-tv3-menu="{o.get("n")}"] {text!r}')
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


def _spec(
    name: str, description: str, params: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=params, handler=handler)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def build_browser_tools(
    page_provider: PageProvider,
    *,
    downloads_dir: str | None = None,
    organization_id: str | None = None,
    resolve_typed_text: Callable[[str], Any] | None = None,
) -> list[ToolSpec]:
    """Raw-browser tools that resolve their page from `page_provider` on every call."""

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
        raw = await asyncio.wait_for(page.evaluate(_OBSERVE_JS), timeout=30)
        data = json.loads(raw) if isinstance(raw, str) else raw
        elements = data.get("elements", [])
        omitted_anonymous = data.get("unnamedAnonymous") or 0
        omitted_duplicated = data.get("unnamedDuplicated") or 0
        omitted_unverifiable = data.get("unnamedUnverifiable") or 0
        omitted_unsafe = data.get("unnamedUnsafe") or 0
        omitted_in_components = omitted_anonymous + omitted_duplicated + omitted_unverifiable + omitted_unsafe
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
                listed=len(elements),
            )
        # Compact rendering keeps the persistent-conversation prefix small (cost is ~linear in it).
        raw_url = str(data.get("url") or "")
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
        for t in data.get("text") or []:
            lines.append(f"text: {t!r}")
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
            lines.append(
                f"note: {omitted_in_components} control(s) inside components are not listed because we "
                f"have no selector that identifies them: {'; '.join(why)}"
            )
        for e in elements:
            extra = ""
            if e.get("value"):
                extra += f" value={e['value']!r}"
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
                extra += " *invalid" if e["invalid"] is True else f" *invalid={e['invalid']!r}"
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
                extra += f" group={e['group']!r}"
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
            lines.append(f"[{e['selector']}] {kind} {e.get('label', '')!r}{extra}")
        # Counts only, for the per-call log record: every perception change that alters only what
        # this function renders is otherwise invisible to production telemetry.
        summary = {
            "text_dropped": text_dropped,
            "hidden_listed": hidden_kept,
            "iframes_in_component_roots": iframe_info.get("inComponents") or 0,
            "undiscovered_roots": data.get("undiscoveredRoots") or 0,
            "omitted_unnameable": omitted_in_components,
            "invalid_fields": sum(1 for e in elements if e.get("invalid")),
            "markers_minted": data.get("markersMinted") or 0,
            "markers_reused": data.get("markersReused") or 0,
            "group_texts_found": sum(1 for e in elements if e.get("group")),
        }
        return ToolResult.ok("\n".join(lines), data={"count": len(elements), "summary": summary})

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

    def _covered_error(selector: str) -> ToolResult:
        return ToolResult.error(
            f"{selector} is rendered but something else is on top of it, so it cannot be typed into — a "
            "person could not click it either. Dismiss whatever covers it (a dialog, an overlay, a cookie "
            "banner), then re-observe."
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

            def _committed_state(after: dict[str, Any]) -> bool:
                return bool(after.get("optState")) and after.get("optState") != baseline

            def _picked(after: dict[str, Any]) -> bool:
                # One of the row's own selection attributes moved. Nothing a restructuring row does
                # can reach these, so this is commit evidence on its own.
                return bool(after.get("optSel")) and after.get("optSel") != sel_baseline

            def _grew(after: dict[str, Any]) -> bool:
                # The row got bigger of its own accord, which is the one shape where "it committed"
                # has a competitor. Height as well as child count, because a row whose single wrapper
                # is REPLACED by an expanded one keeps its count and still grows taller.
                def _up(now: Any, before: Any, by: int) -> bool:
                    ok = (int, float)
                    if isinstance(now, bool) or isinstance(before, bool):
                        return False
                    return isinstance(now, ok) and isinstance(before, ok) and before >= 0 and now - before > by

                return _up(after.get("optKids"), kids_baseline, 0) or _up(after.get("optH"), height_baseline, 2)

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
        selector = args["selector"]
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
                        "fresh selectors from the new observation."
                    )
                # The re-attach may have been a re-render that cloned the row, so the count is re-read.
                matches = await _marker_matches(page, selector)
            if matches > 1:
                # A clone of the marked element carries the same marker; the click would silently
                # land on whichever comes first in document order, so refuse before dispatching it.
                return ToolResult.error(
                    f"{selector} now matches {matches} elements — the page re-rendered and cloned the "
                    "marked element, so the marker no longer identifies one control. Re-observe and act "
                    "on fresh selectors from the new observation."
                )
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
                    for key in ("optSel", "optKids", "optH"):
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
                    "re-render; re-observe and act on fresh selectors"
                )
            base = f"clicked {selector} (hidden native control, toggled directly) — now at {await _url(page)}"
        else:
            try:
                await page.click(selector, timeout=15000)
            except Exception as e:
                gone = False
                try:
                    gone = not await page.evaluate(_SELECTOR_EXISTS_JS, await _probe_arg(page, selector))
                except Exception:
                    gone = False
                if gone:
                    return ToolResult.error(
                        f"click on {selector} failed: the element no longer exists on the page — it was "
                        "likely removed by a re-render (e.g. a menu closed and destroyed its options). "
                        f"Re-observe and act on fresh selectors. (original error: {type(e).__name__})"
                    )
                raise
            base = f"clicked {selector} — now at {await _url(page)}"

        if skinned:
            try:
                checked_after = await page.evaluate(_CHECKBOX_CHECKED_JS, await _probe_arg(page, selector))
            except Exception:
                checked_after = None
            if checked_after is None:
                return ToolResult.ok(
                    f"{base} — the control left the page after the click, so its state could not be "
                    "verified; re-observe before relying on it"
                )
            if checked_after == checked_before:
                return ToolResult.error(
                    f"click on {selector} did NOT commit: the control still reads checked={checked_after!r} — "
                    "the styled proxy may not sync from its hidden control; re-observe and act on the visible "
                    "proxy instead"
                )

        if pre is None:
            return ToolResult.ok(base)
        try:
            note, commit_error = await _click_reaction(page, selector, pre, url_before, doc_planted=doc_planted)
        except Exception:
            LOG.debug("taskv3 click reaction probe failed", selector=selector, exc_info=True)
            return ToolResult.ok(base)
        if commit_error is not None:
            return ToolResult.error(commit_error)
        return ToolResult.ok(base + "\n" + note if note else base)

    async def hover(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        await page.hover(selector, timeout=15000)
        return ToolResult.ok(f"hovered {selector}")

    async def _reachable_for_typing(page: Any, selector: str) -> tuple[bool, bool]:
        """(reachable, occluded). Raises when the field cannot accept typed text at all. Shared by both
        typing paths: fill() does no hit-testing, so without this a covered password or email field is
        filled silently -- no timeout to notice, and a person could not have reached it."""
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
        if occluded and not probe.get("skinned"):
            return False, occluded
        return True, occluded

    async def _focus_for_typing(page: Any, selector: str) -> bool:
        """Put the caret in `selector`. False means the field is genuinely covered and must not be typed
        into. A click is how a widget learns to open its suggestion list, so it stays the first move."""
        reachable, occluded = await _reachable_for_typing(page, selector)
        if not reachable:
            return False
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
                return False
            try:
                # The click may have remounted or hidden the field -- a wrapper that swaps its input
                # on click is an ordinary SPA shape. fill() would wait its own full timeout for a
                # node that is gone or invisible, which is the cost this whole path exists to avoid.
                await page.wait_for_selector(selector, state="visible", timeout=1200)
            except Exception:
                return False
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
        return True

    async def _commit_typeahead(
        page: Any, selector: str, value: str, rounds: int
    ) -> tuple[str | None, str | None, bool]:
        # Poll for the suggestion list rendered IN REACTION to the value already typed into `selector`,
        # click the best match, and verify the field committed. Site-agnostic (see _FIND_SUGGESTION_JS).
        # Returns (committed_value, suggestion_text): suggestion_text is None when no suggestion ever
        # surfaced (an ordinary field, or nothing matched); committed is None when a suggestion was
        # clicked but no value landed.
        best_txt: str | None = None
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
                break
        if not best_txt:
            return None, None, False
        # Click the tagged best row. If the list re-rendered and dropped the tag, re-find (re-tag the
        # current best) and click once more — never blind-press ArrowDown/Enter, which would commit
        # whichever row the widget happens to highlight rather than the one we actually scored.
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
            read = await page.evaluate(
                _VERIFY_COMMIT_JS,
                {
                    "field": selector,
                    "typed": value,
                    "chosen": best_txt,
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
        # visible DOM just before typing so the finder treats only NEW/reacting nodes as suggestions —
        # static page text that merely shares a word with the value can't be mistaken for one.
        if not await _focus_for_typing(page, selector):
            raise _FieldCovered(selector)
        await page.fill(selector, "", timeout=15000)
        presnapshot_ok = True
        try:
            await page.evaluate(_PRESNAPSHOT_JS)
        except Exception:
            presnapshot_ok = False
            LOG.info("taskv3 typeahead pre-snapshot failed; skipping suggestion probe", selector=selector)
        await page.type(selector, value, delay=15, timeout=15000)
        if not presnapshot_ok:
            # Without the pre-snapshot the reaction-gate can't tell a new suggestion from static page
            # text, so don't run the finder ungated (it could click unrelated content) — leave the typed
            # value and let the caller re-observe.
            return None, None, False
        return await _commit_typeahead(page, selector, value, rounds)

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
        selector = args["selector"]
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
            except _FieldCovered:
                return _covered_error(selector)
            except _FieldNotEditable as exc:
                return _not_editable_error(exc)
            if opt_txt and committed:
                return ToolResult.ok(
                    f"typed into {selector}; it is a typeahead — selected {opt_txt!r} (committed value: {committed!r})"
                )
            if opt_txt and not committed:
                # The verifier pierces open roots and also reads the element the executor resolved,
                # so inside a component the failure is established rather than guessed. A list portaled
                # elsewhere, or a field in a closed root, is still beyond both -- and that is exactly
                # what the read reports by returning nothing, so the softening follows the read.
                why = None if readable else await _unverifiable_because(page, selector)
                if why:
                    return ToolResult.ok(
                        f"clicked suggestion {opt_txt!r} for {selector}; {why}, so the commit could not "
                        "be verified — re-observe to confirm the value before relying on it"
                    )
                # A suggestion surfaced but the field did not accept it — the field is NOT filled. Return
                # an error so a batched turn halts here (the loop stops the rest of the batch on error)
                # instead of proceeding — e.g. to a queued submit — on an uncommitted field.
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
            reachable, _ = await _reachable_for_typing(page, selector)
        except _FieldNotEditable as exc:
            return _not_editable_error(exc)
        if not reachable:
            return _covered_error(selector)
        if clear:
            await page.fill(selector, text, timeout=15000)
        else:
            await page.type(selector, text, timeout=15000)
        if press_enter:
            await page.press(selector, "Enter")
        return ToolResult.ok(f"typed into {selector}")

    async def select_option(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        label = args.get("label")
        value = args.get("value")
        try:
            probe = await page.evaluate(_SELECT_VISIBILITY_JS, await _probe_arg(page, selector))
        except Exception:
            probe = None
        # force bypasses actionability for a select a design system hides behind a styled proxy;
        # Playwright still sets the value and dispatches native input/change on the real element.
        if isinstance(probe, dict) and probe.get("exists") and probe.get("disabled"):
            return ToolResult.error(f"{selector} is disabled — it cannot be set until the page enables it")
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
        committed = False
        value_read: Any = None
        if isinstance(readback, dict):
            value_read = readback.get("value")
            committed = readback.get("selectedLabel") == label if label is not None else value_read == value
        if readback is None:
            return ToolResult.ok(
                f"selected on {selector} — the control left the page afterwards, so the selection could not "
                "be verified; re-observe before relying on it"
            )
        if not committed:
            return ToolResult.error(
                f"select on {selector} did NOT commit: native select still reads {value_read!r} — the styled "
                "widget may not sync from its hidden control; re-observe and act on the visible proxy instead"
            )
        return ToolResult.ok(f"selected on {selector} (hidden native select, set directly)")

    async def press_key(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        key = args["key"]
        selector = args.get("selector")
        if selector:
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
        url = await asyncio.to_thread(validate_fetch_url, args["url"])
        response = await page.goto(url, timeout=60000, wait_until="load")
        # Surface the HTTP status: an error page otherwise reads as a successful navigation, hiding
        # dead URLs and blank shells from the model.
        status = f" (HTTP {response.status})" if response is not None else ""
        # page_state_changed tells the loop's action-loop guard the world moved: a re-attempt after
        # a navigation is a fresh attempt, not a repeat against unchanged state.
        return ToolResult.ok(f"navigated to {await _url(page)}{status}", data={"page_state_changed": True})

    async def file_upload(args: dict[str, Any]) -> ToolResult:
        # Lazy import: keeps this module importable for unit tests without the full forge/storage graph.
        from skyvern.forge.sdk.api.files import download_file

        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        source = _resolve_text(args["file"])
        local_path = await download_file(source, output_dir=downloads_dir, organization_id=organization_id)
        # For http(s) sources download_file stages into downloads_dir; naming the file lets the
        # download-signal wrapper suppress it without swallowing unrelated downloads that complete
        # during this call (for other schemes the key is inert — nothing in the dir matches).
        staged = {"staged_download": os.path.basename(local_path)}
        paths = [local_path]
        el = await page.query_selector(selector)
        if el is None:
            return ToolResult("error", f"no file input for selector {selector!r}", staged)
        await el.set_input_files(paths)
        return ToolResult.ok(f"uploaded 1 file to {selector}", staged)

    async def select_combobox(args: dict[str, Any]) -> ToolResult:
        # Explicit typeahead fill (type() also drives this automatically): type the value, WAIT for the
        # async suggestion list, pick the best-matching suggestion, and VERIFY the field committed. Fails
        # loudly if nothing matches rather than leaving raw typed text the widget won't accept as a valid
        # selection (a false "filled" — the failure mode this exists to prevent).
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        value = _resolve_text(args["value"])
        try:
            committed, opt_txt, readable = await _type_and_commit(page, selector, value, rounds=8)
        except _FieldCovered:
            return _covered_error(selector)
        except _FieldNotEditable as exc:
            return _not_editable_error(exc)
        if opt_txt is None:
            # The suggestion finder pierces open shadow roots, so inside a component it now sees a
            # list rendered in that root -- but a portalled or closed-root list stays invisible, so a
            # missing list there is still not evidence there was none, and "the field is NOT filled"
            # would be measurably false (the value is typed in). Erroring would also strand the
            # country/phone comboboxes on the very forms this targets. observe pierces too, so
            # re-observing is a check the model can run.
            why = await _unverifiable_because(page, selector)
            if why:
                return ToolResult.ok(
                    f"typed {value!r} into {selector}; {why}, so the suggestion list could not be seen "
                    "and no selection was verified — re-observe to confirm the value committed before "
                    "relying on it"
                )
            return ToolResult.error(
                f"no autocomplete suggestion matched {value!r} for {selector}; the field is NOT filled "
                "— do not assume success or move on as if it were"
            )
        if not committed:
            why = None if readable else await _unverifiable_because(page, selector)
            if why:
                return ToolResult.ok(
                    f"selected {opt_txt!r} for {selector}; {why}, so the commit could not be verified — "
                    "re-observe to confirm the value before relying on it"
                )
            return ToolResult.error(f"selected suggestion {opt_txt!r} but {selector} did not commit a value")
        return ToolResult.ok(f"selected {opt_txt!r} for {selector} (committed value: {committed!r})")

    tools = [
        _spec(
            "observe",
            "Snapshot the page's visible interactive elements (raw DOM) with a CSS selector, label, type, value, and options for each. Also reports cross-origin iframes present (host + captcha signature); their contents cannot be observed or reached by selector. Call once per page, then act by selector.",
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
            "click",
            "Click an element by CSS selector. If the click opens a menu of options, the result lists "
            'them with [data-tv3-menu="N"] selectors — click one of those to select (verified: you get '
            "a loud error, not a silent no-op, if the selection does not commit; do not blindly repeat "
            "a failed click). If the click triggers a file download, the tool result reports it when "
            "detected.",
            _obj({"selector": {"type": "string"}}, ["selector"]),
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
            "Type text into an input/textarea by CSS selector (clears first by default).",
            _obj(
                {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "clear": {"type": "boolean"},
                    "press_enter": {"type": "boolean"},
                },
                ["selector", "text"],
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
        if _tool_spec.name in ("observe", "get_html"):
            # Large perception dumps: only the latest snapshot is relevant, so let the loop elide older
            # ones from the re-sent transcript (bounds context on perception-heavy pages).
            _tool_spec.compactable = True
        if _tool_spec.name in PREFLIGHT_TOOL_NAMES:
            _tool_spec.handler = _with_preflight(_tool_spec.name, _tool_spec.handler, page_provider, _prefetched_page)
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
                # sniffing the notice lines back out of the content string.
                return ToolResult(
                    result.status,
                    result.content + "\n" + "\n".join(capped),
                    {**(result.data or {}), "download_notice": True},
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
