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

# Snapshot of everything visible BEFORE typing. The finder ignores anything marked here, so only DOM
# that appeared (or became visible) IN REACTION to typing can be treated as a suggestion — static page
# text that merely happens to share a word with the value (a nearby card, nav item, prior answer) is
# never eligible. This is what makes "detect by the page's reaction" rigorous rather than a claim.
_PRESNAPSHOT_JS = r"""() => {
  document.querySelectorAll('[data-tv3-pre]').forEach((e) => e.removeAttribute('data-tv3-pre'));
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) el.setAttribute('data-tv3-pre', '1');
  }
}"""

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
    r"""(args) => {
  const STOP = """
    + _STOPWORDS_JS
    + r""";
  const toks = (s) => new Set(String(s).toLowerCase().replace(/[\/,]/g, ' ').split(/\s+/).filter((w) => w.length >= 3 && !STOP.has(w)));
  const want = toks(args.value || '');
  document.querySelectorAll('[data-tv3-sugg]').forEach((e) => e.removeAttribute('data-tv3-sugg'));
  if (!want.size) return null;
  const field = document.querySelector(args.field);
  const fr = field ? field.getBoundingClientRect() : null;
  const cands = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.hasAttribute('data-tv3-pre')) continue;                    // existed/was visible before typing → not a reaction
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
  const leaves = cands.filter((c) => !cands.some((o) => o.el !== c.el && c.el.contains(o.el)));
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
_VERIFY_COMMIT_JS = r"""(args) => {
  const toks = (s) => new Set(String(s).toLowerCase().replace(/[\/,]/g, ' ').split(/\s+/).filter((w) => w.length >= 3));
  const overlaps = (a, b) => { const B = toks(b); for (const w of toks(a)) if (B.has(w)) return true; return false; };
  const el = document.querySelector(args.field);
  const typed = String(args.typed || '').trim();
  const chosen = String(args.chosen || '').trim() || typed;
  const cur = el ? (el.value || '').trim() : '';
  const tagged = document.querySelector('[data-tv3-sugg]');
  const listClosed = !tagged || tagged.getBoundingClientRect().height === 0;
  // A short normalized value ("New York" -> "NY", "United States" -> "US") has no >=3-char token to
  // overlap, so accept it on causality alone (it changed / the list closed). Longer values must still
  // relate to the chosen suggestion so an unrelated change can't read as a successful commit.
  if (cur && (cur !== typed || listClosed) && (toks(cur).size === 0 || overlaps(cur, chosen) || overlaps(cur, typed))) return cur;
  const cont = el ? el.closest('div,li,fieldset') : null;
  if (cont) {
    for (const h of cont.querySelectorAll('input[type=hidden]')) {
      const v = (h.value || '').trim();
      if (v && (overlaps(v, chosen) || overlaps(v, typed))) return v;
    }
  }
  return '';
}"""

# Whether a selector currently resolves. `true` on a broken selector: existence is only ever used to
# soften/enrich behavior, so an unparseable selector must take the normal (unenriched) path.
_SELECTOR_EXISTS_JS = r"""(sel) => { try { return !!document.querySelector(sel); } catch (e) { return true; } }"""

# Pre-click state for the dropdown-commit path: whether a click-opened menu (rows tagged
# data-tv3-menu by _FIND_MENU_JS) is currently open, whether the click target IS one of its rows, and
# that row's state fingerprint — aria checked/selected/pressed, class, child count, text — so an option
# click on a multi-select menu (which commits WITHOUT closing) can be verified by its state change.
# Also takes the visible-DOM pre-snapshot (data-tv3-pre) so a menu the click opens reads as a reaction.
_CLICK_PRECHECK_JS = r"""(clicked) => {
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
  const openRows = [];
  for (const el of document.querySelectorAll('[data-tv3-menu]')) if (vis(el)) openRows.push(el);
  let target = null;
  try { target = document.querySelector(clicked); } catch (e) { target = null; }
  let isOption = false;
  let containsMenu = false;
  let optText = '';
  let optState = '';
  if (target && openRows.length) {
    for (const el of openRows) {
      // The target being the row or inside it is an option pick. The target merely CONTAINING rows
      // (the card around the menu) is not — and since a center-point click on the card can land on
      // an arbitrary row, that case is flagged so the handler makes no claims about it at all.
      if (el === target || el.contains(target)) {
        isOption = true;
        optText = (el.innerText || '').trim().slice(0, 80);
        optState = state(el);
        break;
      }
      if (target.contains(el)) containsMenu = true;
    }
  }
  document.querySelectorAll('[data-tv3-pre]').forEach((e) => e.removeAttribute('data-tv3-pre'));
  for (const el of document.querySelectorAll('body *')) if (vis(el)) el.setAttribute('data-tv3-pre', '1');
  return { menuOpen: openRows.length > 0, isOption, containsMenu, optText, optState };
}"""

# Post-click menu state: how many previously-tagged menu rows are still visible (a closed menu — nodes
# destroyed or hidden — reads 0), plus the clicked row's current state fingerprint for the multi-select
# commit check. Field names are distinct from _CLICK_PRECHECK_JS's on purpose (tests dispatch on them).
_MENU_AFTER_JS = r"""(clicked) => {
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
  let stillOpen = 0;
  const rows = [];
  for (const el of document.querySelectorAll('[data-tv3-menu]')) if (vis(el)) { stillOpen++; rows.push(el); }
  let target = null;
  try { target = document.querySelector(clicked); } catch (e) { target = null; }
  let optState = '';
  if (target) {
    for (const el of rows) {
      if (el === target || el.contains(target)) { optState = state(el); break; }
    }
  }
  return { stillOpen, optState };
}"""

# Behavioral, site-agnostic menu finder: after a click (with the pre-snapshot taken first),
# look for the option list the page rendered IN REACTION — a NEW container (not data-tv3-pre: a
# pre-existing visible container whose rows merely changed, e.g. pagination refreshing a results list,
# is never a menu) holding >=2 new, visible, row-sized, mostly-clickable leaf rows, positioned adjacent
# to the clicked element. Keys off reaction + geometry — no CSS-class/ARIA/site vocabulary — mirroring
# _FIND_SUGGESTION_JS. Unlike the typeahead finder this only REPORTS (the model does the clicking), so
# navigational rows are listed too. Tags rows data-tv3-menu="1..N" (top-to-bottom) — in-DOM tags that
# stay valid until the menu re-renders, so the model can pick an option without a re-observe re-minting
# ids (the staging trace's staleness trap). Existing tags are cleared only when a new menu is tagged.
_FIND_MENU_JS = r"""(clicked) => {
  const vis = (r) => r.width > 0 && r.height > 0;
  let trigger = null;
  try { trigger = document.querySelector(clicked); } catch (e) { return null; }
  if (!trigger) return null;
  const tr = trigger.getBoundingClientRect();
  const rows = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.hasAttribute('data-tv3-pre')) continue;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LABEL' || tag === 'FORM') continue;
    if (el.children.length > 8) continue;
    const r = el.getBoundingClientRect();
    if (!vis(r) || r.height > 90) continue;
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 80) continue;
    // Options are individually actionable rows. Requiring it per-row keeps a dialog's title/body
    // text from being listed as "options" (and a horizontal Confirm/Cancel button pair then fails
    // the stacked-rows check below).
    const role = el.getAttribute('role');
    let ptr = false;
    try { ptr = getComputedStyle(el).cursor === 'pointer'; } catch (e) { ptr = false; }
    const clickable = tag === 'BUTTON' || tag === 'A' || role === 'option' || role === 'menuitem' || role === 'menuitemcheckbox' || role === 'menuitemradio' || ptr;
    if (!clickable) continue;
    rows.push({ el, r, txt });
  }
  if (rows.length < 2) return null;
  const leaves = rows.filter((c) => !rows.some((o) => o.el !== c.el && c.el.contains(o.el)));
  if (leaves.length < 2) return null;
  // Group by parent AND grandparent so both flat menus (card > button*N) and nested ones
  // (ul > li > button) find their shared container.
  const groups = new Map();
  for (const c of leaves) {
    const p1 = c.el.parentElement;
    for (const p of [p1, p1 ? p1.parentElement : null]) {
      if (!p || p === document.body || p === document.documentElement) continue;
      if (!groups.has(p)) groups.set(p, new Set());
      groups.get(p).add(c);
    }
  }
  let best = null;
  for (const [p, set] of groups) {
    const g = Array.from(set);
    if (g.length < 2) continue;
    if (p.hasAttribute('data-tv3-pre')) continue;
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
  document.querySelectorAll('[data-tv3-menu]').forEach((e) => e.removeAttribute('data-tv3-menu'));
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

# Raw DOM perception: collect visible interactive elements with a stable selector each.
# Elements without a natural selector get a data-tv3 marker so later actions can target them.
_OBSERVE_JS = (
    r"""
() => {
  const _isAutocomplete = """
    + _IS_AUTOCOMPLETE_JS
    + r""";
  const q = 'input,textarea,select,button,a[href],[role=button],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[contenteditable=true]';
  const els = document.querySelectorAll(q);
  const out = [];
  // Monotonic across observe() calls (persisted on window), and never reassigned on an element that
  // already has one, so a data-tv3 marker always denotes the same element. Resetting the counter per
  // call let a selector remembered from an earlier observe silently resolve to a different node.
  if (typeof window.__tv3_next !== 'number') window.__tv3_next = 0;
  let i = 0;
  let lastGroup = '';
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    let selector = null;
    const uniq = (s) => { try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; } };
    if (el.id) { const s = '#' + CSS.escape(el.id); if (uniq(s)) selector = s; }
    if (!selector && el.getAttribute('data-testid')) { const s = '[data-testid="' + el.getAttribute('data-testid') + '"]'; if (uniq(s)) selector = s; }
    if (!selector && el.name) { const s = el.tagName.toLowerCase() + '[name="' + el.name + '"]'; if (uniq(s)) selector = s; }
    if (!selector) {
      let m = el.getAttribute('data-tv3');
      // Reuse a marker only if it still uniquely resolves; otherwise mint a fresh monotonic one.
      // Keeps a marker stable across observe() calls without trusting a foreign, duplicated, or
      // syntactically-broken data-tv3 value that a remembered selector could resolve to the wrong node.
      if (!m || !uniq('[data-tv3="' + m + '"]')) { m = 't' + (window.__tv3_next++); el.setAttribute('data-tv3', m); }
      selector = '[data-tv3="' + m + '"]';
    }
    let label = (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim();
    if (!label && el.labels && el.labels[0]) label = (el.labels[0].innerText || '').trim();
    if (!label) label = (el.innerText || (el.type === 'password' ? '' : el.value) || '').trim();
    const rec = { i, tag: el.tagName.toLowerCase(), type: el.type || null, selector, label: label.slice(0, 140) };
    if (el.tagName === 'SELECT') rec.options = Array.from(el.options).map((o) => o.value + '|' + o.text).slice(0, 60);
    if (el.type === 'password') { if (el.value) rec.value = '(hidden)'; } else if (el.value) rec.value = String(el.value).slice(0, 100);
    if (el.type === 'checkbox' || el.type === 'radio') rec.checked = !!el.checked;
    else if (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'radio') rec.checked = el.getAttribute('aria-checked') === 'true';
    if (el.getAttribute('aria-required') === 'true' || el.required) rec.required = true;
    // Flag typeahead/autocomplete inputs so the model treats them as combobox fills instead of typing
    // raw text that never registers as a valid selection (type() also auto-commits them). See _IS_AUTOCOMPLETE_JS.
    if (_isAutocomplete(el)) rec.autocomplete = true;
    // Attach the surrounding question/group text for controls whose meaning lives in nearby
    // non-interactive text (radio/checkbox groups, weakly-labeled fields) so the agent can answer
    // without fetching raw HTML. Deduped against the previous element to keep grouped options compact.
    const isChoice = el.type === 'checkbox' || el.type === 'radio' || el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'radio';
    if (isChoice || label.length < 3) {
      const g = el.closest('fieldset,[role=group],li,dd,.form-group,[class*="question"],[class*="field"]');
      if (g) {
        const gt = (g.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 200);
        if (gt && gt.length > label.length && gt !== lastGroup) { rec.group = gt; lastGroup = gt; }
      }
    }
    const pressed = el.getAttribute('aria-pressed');
    if (pressed === 'true' || pressed === 'false') rec.pressed = pressed === 'true';
    out.push(rec);
    if (++i > 250) break;
  }
  // Page-text digest: outcome states (submission confirmations, rejection banners, validation
  // summaries) live in non-interactive nodes the element list can never carry. Sources are
  // structural only — ARIA status channels first, then headings — never a body-text dump, so the
  // digest stays bounded and can't regrow the context that transcript compaction bounds.
  const texts = [];
  let textTotal = 0;
  let textFull = false;
  const pushText = (t) => {
    t = (t || '').replace(/\s+/g, ' ').trim().slice(0, 300);
    if (!t) return;
    // Containment dedupe, richer message wins: an alert's text re-surfaces inside its heading's
    // parent text, and a terse early entry ("Saved") must not suppress a later superset
    // ("Saved — confirmation #A1B2") — supersets REPLACE their contained entries.
    if (texts.some((s) => s.includes(t))) return;
    const kept = texts.filter((s) => !t.includes(s));
    const keptTotal = kept.reduce((total, s) => total + s.length, 0);
    if (keptTotal + t.length > 900) { textFull = true; return; }
    texts.length = 0; texts.push(...kept, t); textTotal = keptTotal + t.length;
  };
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // Isolated: a hostile page's throwing accessor (fingerprinting scripts poison innerText and
  // friends) must degrade to "no digest", never take element perception down with it.
  try {
    // ~= matches ARIA fallback role lists like role="alert status"; = would silently skip them.
    for (const el of document.querySelectorAll('[role~=alert],[role~=status],[aria-live=polite],[aria-live=assertive],output')) {
      if (textFull) break;
      if (visible(el)) pushText(el.innerText);
    }
    for (const h of document.querySelectorAll('h1,h2,h3')) {
      if (textFull) break;
      if (!visible(h)) continue;
      // A short parent is a banner/panel whose body text carries the message; a large parent would
      // drag in unrelated content, so the heading stands alone.
      const pt = h.parentElement ? (h.parentElement.innerText || '').replace(/\s+/g, ' ').trim() : '';
      pushText(pt && pt.length <= 300 ? pt : h.innerText);
    }
  } catch (e) { texts.length = 0; }
  // Cross-origin iframe PRESENCE: an anti-bot/captcha widget lives in one, and main-frame element
  // perception can never list its contents — record host + signature so the model can see the gate
  // exists. Attributes only, never the frame's document (page.frames-based traversal was considered
  // and rejected: presence is the contract here, not cross-frame reach). Same visibility rule as
  // elements, so hidden tracking pixels stay out. Isolated like the digest above.
  const iframeInfo = { total: 0, entries: [] };
  try {
    const sig = /captcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|datadome|perimeterx|verify you are human|security challenge/i;
    for (const f of document.querySelectorAll('iframe')) {
      const r = f.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      // A frame with srcdoc renders the inline (same-origin) document; its src is a dead fallback.
      if (f.hasAttribute('srcdoc')) continue;
      const src = f.getAttribute('src') || '';
      let u;
      try { u = new URL(src, location.href); } catch (e) { continue; }
      if ((u.protocol !== 'http:' && u.protocol !== 'https:') || u.origin === location.origin) continue;
      iframeInfo.total++;
      if (iframeInfo.entries.length >= 8) continue;
      const ttl = (f.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
      iframeInfo.entries.push({ host: u.host.slice(0, 80), title: ttl, captcha: sig.test(src + ' ' + ttl) });
    }
  } catch (e) { iframeInfo.total = 0; iframeInfo.entries.length = 0; }
  return JSON.stringify({ url: location.href, title: document.title, text: texts, iframes: iframeInfo, elements: out });
}
"""
)


def _menu_open_note(found: dict[str, Any], selector: str) -> str:
    count = int(found.get("count") or 0)
    parts = []
    for o in (found.get("options") or [])[:15]:
        # option texts are page-controlled and land in the LLM transcript — same sanitation as filenames
        text = _DOWNLOAD_NOTICE_SANITIZE_RE.sub("", str(o.get("text", "")))
        parts.append(f'[data-tv3-menu="{o.get("n")}"] {text!r}')
    overflow = f" (+{count - len(parts)} more — re-observe for the full list)" if count > len(parts) else ""
    return (
        f"This click opened a menu of {count} options: {'; '.join(parts)}{overflow}. To select one, click "
        f'its [data-tv3-menu="N"] selector NOW — clicking {selector} again or elsewhere closes the menu '
        "and destroys these options."
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

    async def observe(_args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        # Bound the one perception call so a wedged page can't hang the turn indefinitely.
        raw = await asyncio.wait_for(page.evaluate(_OBSERVE_JS), timeout=30)
        data = json.loads(raw) if isinstance(raw, str) else raw
        elements = data.get("elements", [])
        # Compact rendering keeps the persistent-conversation prefix small (cost is ~linear in it).
        lines = [f"url={data.get('url')} title={data.get('title')!r} ({len(elements)} interactive elements)"]
        for t in data.get("text") or []:
            lines.append(f"text: {t!r}")
        iframe_info = data.get("iframes") or {}
        iframe_entries = iframe_info.get("entries") or []
        if iframe_entries:
            total = iframe_info.get("total", len(iframe_entries))
            parts = []
            for f in iframe_entries:
                flag = "[captcha] " if f.get("captcha") else ""
                title = f" {f['title']!r}" if f.get("title") else ""
                parts.append(f"{flag}{f.get('host', '?')}{title}")
            overflow = f" (+{total - len(iframe_entries)} more)" if total > len(iframe_entries) else ""
            lines.append(
                f"iframes: {total} cross-origin (contents NOT listed here and NOT reachable by selector): "
                + "; ".join(parts)
                + overflow
            )
        for e in elements:
            extra = ""
            if e.get("value"):
                extra += f" value={e['value']!r}"
            if e.get("options"):
                extra += f" options={e['options']}"
            if e.get("checked") is not None:
                extra += f" checked={e['checked']}"
            if e.get("pressed") is not None:
                extra += f" pressed={e['pressed']}"
            if e.get("required"):
                extra += " *required"
            if e.get("autocomplete"):
                extra += " [autocomplete→use select_combobox]"
            if e.get("group"):
                extra += f" group={e['group']!r}"
            lines.append(
                f"[{e['selector']}] {e['tag']}{('/' + e['type']) if e.get('type') else ''} {e.get('label', '')!r}{extra}"
            )
        return ToolResult.ok("\n".join(lines), data={"count": len(elements)})

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
        # The click/type reaction gate stamps data-tv3-pre on every visible element; it is internal
        # bookkeeping, and left in place it costs a third of the truncation budget below in noise.
        html = html.replace(' data-tv3-pre="1"', "")
        if len(html) > 20000:
            return ToolResult.ok(html[:20000] + "…[truncated at 20000 chars]")
        return ToolResult.ok(html)

    async def _click_reaction(
        page: Any, selector: str, pre: dict[str, Any], url_before: str
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

            def _committed_state(after: dict[str, Any]) -> bool:
                return bool(after.get("optState")) and after.get("optState") != baseline

            async def _state_holds(state: str) -> bool:
                # A real commit settles into its new state; self-updating row content (a countdown,
                # a live price) keeps moving. Only a state that holds across two reads is evidence.
                await asyncio.sleep(0.15)
                try:
                    again_raw = await page.evaluate(_MENU_AFTER_JS, selector)
                except Exception:
                    return True
                again = again_raw if isinstance(again_raw, dict) else {}
                return bool(again.get("optState") == state)

            url_now = await _url(page)
            if url_before and url_now and url_now != url_before:
                return f"Selected option {opt!r} — the page navigated.", None
            try:
                after_raw = await page.evaluate(_MENU_AFTER_JS, selector)
            except Exception:
                # the page tearing down right after an option click is a navigation — commit evidence
                return f"Selected option {opt!r} — the page navigated.", None
            after = after_raw if isinstance(after_raw, dict) else {}
            if not after.get("stillOpen"):
                return f"Selected option {opt!r} — the menu closed.", None
            if _committed_state(after) and await _state_holds(after.get("optState") or ""):
                return f"Selected option {opt!r} — its state changed (the menu stayed open).", None
            # Menus routinely close through a fade or an async server ack; declaring "did not commit"
            # off the instantaneous read would turn those healthy commits into false errors. One
            # bounded settle, only on this would-be-error path.
            await asyncio.sleep(0.6)
            try:
                settled_raw = await page.evaluate(_MENU_AFTER_JS, selector)
            except Exception:
                return f"Selected option {opt!r} — the page navigated.", None
            settled = settled_raw if isinstance(settled_raw, dict) else {}
            if not settled.get("stillOpen"):
                return f"Selected option {opt!r} — the menu closed.", None
            if _committed_state(settled) and await _state_holds(settled.get("optState") or ""):
                return f"Selected option {opt!r} — its state changed (the menu stayed open).", None
            # No-commit evidence is already established: a crash of this last informational probe must
            # not fall through to the caller's fail-open bare ok.
            try:
                submenu = await page.evaluate(_FIND_MENU_JS, selector)
            except Exception:
                submenu = None
            if isinstance(submenu, dict) and submenu.get("count"):
                return _menu_open_note(submenu, selector), None
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
                after_raw = await page.evaluate(_MENU_AFTER_JS, selector)
            except Exception:
                return None, None
            found = await page.evaluate(_FIND_MENU_JS, selector)
            if isinstance(found, dict) and found.get("count"):
                return _menu_open_note(found, selector), None
            if isinstance(after_raw, dict) and not after_raw.get("stillOpen"):
                return (
                    "Note: this click CLOSED the open menu — no option was selected. To select, click "
                    'an option\'s [data-tv3-menu="N"] selector while the menu is open.'
                ), None
            return None, None
        found = await page.evaluate(_FIND_MENU_JS, selector)
        if isinstance(found, dict) and found.get("count"):
            return _menu_open_note(found, selector), None
        return None, None

    async def click(args: dict[str, Any]) -> ToolResult:
        page, error = await _resolve_page()
        if error is not None:
            return error
        selector = args["selector"]
        if _TV3_MARKER_SELECTOR_RE.match(selector.strip()):
            missing = False
            try:
                missing = not await page.evaluate(_SELECTOR_EXISTS_JS, selector)
            except Exception:
                missing = False
            if missing:
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
        pre: dict[str, Any] | None = None
        try:
            pre_raw = await page.evaluate(_CLICK_PRECHECK_JS, selector)
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
                hovered = await page.evaluate(_MENU_AFTER_JS, selector)
                if isinstance(hovered, dict) and hovered.get("optState"):
                    pre["optState"] = hovered["optState"]
            except Exception:
                pass
        url_before = await _url(page)
        try:
            await page.click(selector, timeout=15000)
        except Exception as e:
            gone = False
            try:
                gone = not await page.evaluate(_SELECTOR_EXISTS_JS, selector)
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
        if pre is None:
            return ToolResult.ok(base)
        try:
            note, commit_error = await _click_reaction(page, selector, pre, url_before)
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

    async def _commit_typeahead(page: Any, selector: str, value: str, rounds: int) -> tuple[str | None, str | None]:
        # Poll for the suggestion list rendered IN REACTION to the value already typed into `selector`,
        # click the best match, and verify the field committed. Site-agnostic (see _FIND_SUGGESTION_JS).
        # Returns (committed_value, suggestion_text): suggestion_text is None when no suggestion ever
        # surfaced (an ordinary field, or nothing matched); committed is None when a suggestion was
        # clicked but no value landed.
        best_txt: str | None = None
        for _ in range(rounds):
            await asyncio.sleep(0.4)
            try:
                found = await page.evaluate(_FIND_SUGGESTION_JS, {"value": value, "field": selector})
            except Exception as e:
                LOG.debug("taskv3 typeahead suggestion-find failed", selector=selector, error=str(e))
                found = None
            if isinstance(found, dict) and found.get("text"):
                best_txt = str(found["text"])
                break
        if not best_txt:
            return None, None
        # Click the tagged best row. If the list re-rendered and dropped the tag, re-find (re-tag the
        # current best) and click once more — never blind-press ArrowDown/Enter, which would commit
        # whichever row the widget happens to highlight rather than the one we actually scored.
        clicked = False
        try:
            await page.click('[data-tv3-sugg="1"]', timeout=3000)
            clicked = True
        except Exception:
            try:
                refound = await page.evaluate(_FIND_SUGGESTION_JS, {"value": value, "field": selector})
                if isinstance(refound, dict) and refound.get("text"):
                    best_txt = str(refound["text"])
                    await page.click('[data-tv3-sugg="1"]', timeout=3000)
                    clicked = True
            except Exception:
                clicked = False
        if not clicked:
            # a suggestion surfaced but we couldn't click it — report un-committed, don't guess
            LOG.debug("taskv3 typeahead could not click suggestion", selector=selector, suggestion=best_txt)
            return None, best_txt
        await asyncio.sleep(0.3)
        try:
            committed = (
                (await page.evaluate(_VERIFY_COMMIT_JS, {"field": selector, "typed": value, "chosen": best_txt})) or ""
            ).strip()
        except Exception as e:
            LOG.debug("taskv3 typeahead commit-verify failed", selector=selector, error=str(e))
            committed = ""
        return (committed or None), best_txt

    async def _type_and_commit(page: Any, selector: str, value: str, rounds: int) -> tuple[str | None, str | None]:
        # Keystroke-type (so a widget's async suggestion fetch fires on real key events). Snapshot the
        # visible DOM just before typing so the finder treats only NEW/reacting nodes as suggestions —
        # static page text that merely shares a word with the value can't be mistaken for one.
        await page.click(selector, timeout=15000)
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
            return None, None
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
            committed, opt_txt = await _type_and_commit(page, selector, text, rounds=3)
            if opt_txt and committed:
                return ToolResult.ok(
                    f"typed into {selector}; it is a typeahead — selected {opt_txt!r} (committed value: {committed!r})"
                )
            if opt_txt and not committed:
                # A suggestion surfaced but the field did not accept it — the field is NOT filled. Return
                # an error so a batched turn halts here (the loop stops the rest of the batch on error)
                # instead of proceeding — e.g. to a queued submit — on an uncommitted field.
                return ToolResult.error(
                    f"clicked suggestion {opt_txt!r} for {selector} but it did not commit — the field is NOT "
                    "filled; re-observe and retry, do not proceed"
                )
            return ToolResult.ok(f"typed into {selector}")
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
        if args.get("label") is not None:
            await page.select_option(selector, label=args["label"], timeout=15000)
        else:
            await page.select_option(selector, value=args.get("value"), timeout=15000)
        return ToolResult.ok(f"selected on {selector}")

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
        committed, opt_txt = await _type_and_commit(page, selector, value, rounds=8)
        if opt_txt is None:
            return ToolResult.error(
                f"no autocomplete suggestion matched {value!r} for {selector}; the field is NOT filled "
                "— do not assume success or move on as if it were"
            )
        if not committed:
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
    "[\\x00-\\x1f\\x7f\\u2028\\u2029\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)


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
