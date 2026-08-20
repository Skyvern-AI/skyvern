"""Browser-side expressions used by Workflow Copilot composition inspection."""

from __future__ import annotations

import json

from skyvern.forge.sdk.copilot.composition_evidence import (
    _ANTI_BOT_PATTERNS,
    _ANTI_BOT_SCAN_BYTES,
    _MAX_CHALLENGE_CONTROLS,
    _MAX_CLICKABLE_CONTROLS,
    _MAX_FIELDS_PER_FORM,
    _MAX_FORMS,
    _MAX_KEY_VALUE_RELATIONS,
    _MAX_MODAL_DISMISS_CONTROLS,
    _MAX_MODAL_OVERLAYS,
    _MAX_NAVIGATION_TARGETS,
    _MAX_PAGE_OBSTRUCTIONS,
    _MAX_RESULT_CONTAINERS,
    _MAX_RESULT_SAMPLE_ROWS,
    _MAX_REVEAL_KEY_VALUE_RELATIONS,
    _MAX_SELECT_OPTIONS,
    _MAX_SELECTOR_CHARS,
    _MAX_TABLE_HEADERS,
    _MAX_VISIBLE_TEXT_EXCERPT_CHARS,
    _MODAL_IDENTITY_PATTERNS,
    _MODAL_ROLE_VALUES,
    _RESULT_CONTAINER_HINTS,
)

# Keep stripped-body evaluate results under the shared MCP response cap while
# preserving as much below-fold page structure as possible.
COMPOSITION_STRIPPED_HTML_MAX_CHARS = 135000
COMPOSITION_STRIPPED_HTML_EXPRESSION = (
    "(() => {"
    "  const b = document.body; if (!b) return '';"
    "  const c = b.cloneNode(true);"
    "  c.querySelectorAll('script,style,noscript,svg,template,iframe,canvas,link').forEach(n => n.remove());"
    "  const w = document.createTreeWalker(c, NodeFilter.SHOW_COMMENT, null);"
    "  const comments = []; while (w.nextNode()) comments.push(w.currentNode); comments.forEach(n => n.remove());"
    "  const h = c.innerHTML.replace(/>\\s+</g, '><').replace(/\\s{2,}/g, ' ');"
    f"  return h.length > {COMPOSITION_STRIPPED_HTML_MAX_CHARS} ? "
    f"h.slice(0, {COMPOSITION_STRIPPED_HTML_MAX_CHARS}) : h;"
    "})()"
)


_JS_TEXT_HELPER = "const text = (v) => String(v == null ? '' : v).replace(/\\s+/g, ' ').trim();"

_JS_IS_EDITABLE_HELPER = (
    "const isEditable = (node) => {"
    "  const tag = (node.tagName || '').toLowerCase();"
    "  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;"
    "  return node.isContentEditable === true;"
    "};"
)

_JS_IMPLICIT_ROLE_HELPER = (
    "const implicitRole = (node) => {"
    "  const tag = (node.tagName || '').toLowerCase();"
    "  const type = (node.getAttribute('type') || '').toLowerCase();"
    "  if (tag === 'a' && node.hasAttribute('href')) return 'link';"
    "  if (tag === 'button') return 'button';"
    "  if (tag === 'select') return 'combobox';"
    "  if (tag === 'textarea') return 'textbox';"
    "  if (tag === 'input') {"
    "    if (['button', 'submit', 'reset'].includes(type)) return 'button';"
    "    if (type === 'checkbox') return 'checkbox';"
    "    if (type === 'radio') return 'radio';"
    "    if (['text', 'search', 'email', 'tel', 'url', 'password', ''].includes(type)) return 'textbox';"
    "  }"
    "  if (/^h[1-6]$/.test(tag)) return 'heading';"
    "  return '';"
    "};"
)

# ARIA roles whose accessible name is computed from the element's own text content (ARIA name-from-content).
# Editable roles (textbox/combobox/searchbox/spinbutton/listbox) are deliberately absent so a typed-into
# control never leaks its value as an accessible name.
_JS_NAME_FROM_CONTENT_ROLES = (
    "const nameFromContentRoles = new Set(["
    "'button', 'link', 'checkbox', 'radio', 'heading', 'tab', 'menuitem', "
    "'menuitemcheckbox', 'menuitemradio', 'option', 'switch', 'treeitem', "
    "'cell', 'gridcell', 'columnheader', 'rowheader', 'row', 'tooltip'"
    "]);"
)

_JS_ACCESSIBLE_NAME_HELPER = (
    "const accessibleName = (node, role) => {"
    "  const aria = text(node.getAttribute('aria-label'));"
    "  if (aria) return aria;"
    "  const labelledby = node.getAttribute('aria-labelledby');"
    "  if (labelledby) {"
    "    const parts = labelledby.split(/\\s+/).map((id) => {"
    "      const ref = document.getElementById(id);"
    "      return ref ? text(ref.textContent) : '';"
    "    }).filter(Boolean);"
    "    if (parts.length) return text(parts.join(' '));"
    "  }"
    "  const id = node.getAttribute('id');"
    "  if (id) {"
    "    let lab = null;"
    "    try { lab = document.querySelector('label[for=\"' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '\"]'); } catch (e) { lab = null; }"
    "    if (lab) { const t = text(lab.textContent); if (t) return t; }"
    "  }"
    "  const parentLabel = node.closest ? node.closest('label') : null;"
    "  if (parentLabel) { const t = text(parentLabel.textContent); if (t) return t; }"
    "  const title = text(node.getAttribute('title'));"
    "  if (title) return title;"
    "  const placeholder = text(node.getAttribute('placeholder'));"
    "  if (placeholder) return placeholder;"
    "  if (role && nameFromContentRoles.has(role) && !isEditable(node)) {"
    "    const content = text(node.textContent);"
    "    if (content) return content;"
    "  }"
    "  return '';"
    "};"
)


# Given a CSS selector, return the element's ARIA role and accessible name so the code-block
# synthesizer has a get_by_role fallback anchor for a positional/unstable captured selector. The name
# follows the ARIA algorithm: label sources first, then name-from-content for content-named roles only.
def scout_accessible_role_name_expression(css_selector: str) -> str:
    sel = json.dumps(css_selector)
    return (
        "(() => {"
        f"  const el = document.querySelector({sel});"
        "  if (!el) return null;"
        f"  {_JS_TEXT_HELPER}"
        f"  {_JS_IS_EDITABLE_HELPER}"
        f"  {_JS_IMPLICIT_ROLE_HELPER}"
        f"  {_JS_NAME_FROM_CONTENT_ROLES}"
        f"  {_JS_ACCESSIBLE_NAME_HELPER}"
        "  const role = text(el.getAttribute('role')) || implicitRole(el);"
        "  return { role: role, accessible_name: accessibleName(el, role) };"
        "})()"
    )


# Count elements whose computed ARIA role and accessible name exactly match, so a scout-ambiguous
# selector's get_by_role(role, name, exact=True) re-anchor is only trusted when it resolves uniquely.
def role_name_match_count_expression(role: str, name: str) -> str:
    target_role = json.dumps(role)
    target_name = json.dumps(name)
    return (
        "(() => {"
        "  try {"
        f"    {_JS_TEXT_HELPER}"
        f"    {_JS_IS_EDITABLE_HELPER}"
        f"    {_JS_IMPLICIT_ROLE_HELPER}"
        f"    {_JS_NAME_FROM_CONTENT_ROLES}"
        f"    {_JS_ACCESSIBLE_NAME_HELPER}"
        f"    const targetRole = {target_role};"
        f"    const targetName = {target_name};"
        "    let count = 0;"
        "    const nodes = document.querySelectorAll('*');"
        "    for (const el of nodes) {"
        "      const role = text(el.getAttribute('role')) || implicitRole(el);"
        "      if (role !== targetRole) continue;"
        "      if (accessibleName(el, role) === targetName) count++;"
        "    }"
        "    return count;"
        "  } catch (e) { return -1; }"
        "})()"
    )


# Live count of elements a CSS selector resolves to right now. An invalid selector returns -1 so the
# caller can tell "matched nothing" (0) apart from "could not evaluate" (-1).
def selector_match_count_expression(css_selector: str) -> str:
    sel = json.dumps(css_selector)
    return f"(() => {{  try {{ return document.querySelectorAll({sel}).length; }}  catch (e) {{ return -1; }}}})()"


def selector_candidates_expression(css_selector: str) -> str:
    """Return every bounded CSS identity observed for the selected source-page element.

    Candidate order is capture order only. The result does not rank, filter by uniqueness, or choose
    a replacement for the selector supplied by the model.
    """
    sel = json.dumps(css_selector)
    return (
        "(() => {"
        f"  const requested = {sel};"
        "  let el = null; try { el = document.querySelector(requested); } catch (e) { return []; }"
        "  if (!el) return [];"
        "  const esc = (v) => window.CSS && CSS.escape ? CSS.escape(String(v)) : String(v);"
        "  const attr = (n, k) => n && n.getAttribute ? String(n.getAttribute(k) || '') : '';"
        "  const tag = (el.tagName || '*').toLowerCase();"
        "  const candidates = [];"
        "  const add = (selector, source) => {"
        "    if (!selector || candidates.some((item) => item.selector === selector)) return;"
        "    let matches = []; try { matches = Array.from(document.querySelectorAll(selector)); } catch (e) { return; }"
        "    if (matches.includes(el)) candidates.push({selector: selector, source: source});"
        "  };"
        "  add(requested, 'requested');"
        "  const id = attr(el, 'id'); if (id) add('#' + esc(id), 'id');"
        "  const name = attr(el, 'name'); if (name) add(tag + '[name=\"' + name.replaceAll('\\\\', '\\\\\\\\').replaceAll('\"', '\\\"') + '\"]', 'name');"
        "  const aria = attr(el, 'aria-label'); if (aria) add(tag + '[aria-label=\"' + aria.replaceAll('\\\\', '\\\\\\\\').replaceAll('\"', '\\\"') + '\"]', 'aria_label');"
        "  const type = attr(el, 'type'); if (type) add(tag + '[type=\"' + type.replaceAll('\\\\', '\\\\\\\\').replaceAll('\"', '\\\"') + '\"]', 'type');"
        "  const classes = Array.from(el.classList || []).filter(Boolean);"
        "  if (classes.length) add(tag + classes.map((value) => '.' + esc(value)).join(''), 'class_list');"
        "  return candidates;"
        "})()"
    )


# Submit controls belonging to the form that contains a just-filled field. A login page often
# offers a federated button that is larger and earlier in the DOM than the form's own submit, so
# naming the filled form's submits keeps "how do I submit what I just filled" from being a guess.
def enclosing_form_submit_controls_expression(css_selector: str) -> str:
    sel = json.dumps(css_selector)
    return (
        "(() => {"
        "  try {"
        f"    const el = document.querySelector({sel});"
        "    const form = el && el.closest ? el.closest('form') : null;"
        "    if (!form) return [];"
        "    const uniq = (s) => { try { return s && document.querySelectorAll(s).length === 1; } catch (e) { return false; } };"
        "    const esc = (v) => String(v).split('\\\\').join('\\\\\\\\').split('\"').join('\\\\\"');"
        "    const out = [];"
        "    for (const c of form.querySelectorAll('button, input[type=submit]')) {"
        "      if (out.length >= 5) break;"
        "      const aria = c.getAttribute('aria-label') || '';"
        "      const label = (String(c.textContent || '').replace(/\\s+/g, ' ').trim()"
        "        || aria || c.getAttribute('title') || c.getAttribute('value') || '').slice(0, 80);"
        "      const tag = (c.tagName || '').toLowerCase();"
        "      const id = c.getAttribute('id');"
        "      let s = '';"
        "      if (id && uniq('#' + id)) s = '#' + id;"
        "      else if (aria && uniq(tag + '[aria-label=\"' + esc(aria) + '\"]')) s = tag + '[aria-label=\"' + esc(aria) + '\"]';"
        "      out.push({ label: label, selector: s });"
        "    }"
        "    return out;"
        "  } catch (e) { return []; }"
        "})()"
    )


# Read only the readonly/disabled control-state booleans for a CSS or XPath selector; never reads the
# element's value. An unresolvable selector or non-CSS/XPath engine returns null (UNKNOWN editability).
def scout_control_state_expression(selector: str) -> str:
    sel = json.dumps(selector)
    return (
        "(() => {"
        f"  const sel = {sel};"
        "  let el = null;"
        "  try {"
        "    if (/^\\s*(xpath=|\\(?\\/)/.test(sel)) {"
        "      const x = sel.replace(/^\\s*xpath=/, '');"
        "      const r = document.evaluate(x, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);"
        "      el = r ? r.singleNodeValue : null;"
        "    } else {"
        "      el = document.querySelector(sel);"
        "    }"
        "  } catch (e) { return null; }"
        "  if (!el) return null;"
        "  const attrOf = (k) => (el.getAttribute && el.getAttribute(k)) || '';"
        "  const readonly = el.readOnly === true || (el.hasAttribute && el.hasAttribute('readonly'))"
        "    || attrOf('aria-readonly').toLowerCase() === 'true';"
        "  const disabled = el.disabled === true || (el.hasAttribute && el.hasAttribute('disabled'))"
        "    || attrOf('aria-disabled').toLowerCase() === 'true';"
        "  return { readonly: !!readonly, disabled: !!disabled };"
        "})()"
    )


COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION = (
    "(() => {"
    "  const body = document.body; if (!body) return [];"
    "  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;"
    "  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;"
    "  const highZIndex = (value) => {"
    "    const numeric = Number.parseFloat(value);"
    "    return Number.isFinite(numeric) && numeric >= 10;"
    "  };"
    "  const visible = (style, rect) => ("
    "    style.display !== 'none' && style.visibility !== 'hidden' &&"
    "    Number.parseFloat(style.opacity || '1') > 0.05 && rect.width > 0 && rect.height > 0"
    "  );"
    "  const coversViewport = (rect) => ("
    "    viewportWidth > 0 && viewportHeight > 0 &&"
    "    rect.left <= viewportWidth * 0.05 && rect.top <= viewportHeight * 0.05 &&"
    "    rect.right >= viewportWidth * 0.95 && rect.bottom >= viewportHeight * 0.95"
    "  );"
    "  const hasControl = (element) => Array.from("
    "    element.querySelectorAll('button,a,input,[role=\"button\"]')"
    "  ).some((control) => {"
    "    const text = `${control.innerText || ''} ${control.value || ''} "
    "${control.getAttribute('aria-label') || ''}`.trim();"
    "    if (!text) return false;"
    "    const tag = control.tagName.toLowerCase();"
    "    const type = (control.getAttribute('type') || '').toLowerCase();"
    "    return tag !== 'input' || ['button', 'submit', 'reset'].includes(type);"
    "  });"
    "  const candidates = [];"
    "  for (const element of Array.from(body.querySelectorAll('*'))) {"
    "    if (candidates.length >= 5) break;"
    "    const style = window.getComputedStyle(element);"
    "    if (!['fixed', 'sticky'].includes(style.position)) continue;"
    "    if (!highZIndex(style.zIndex)) continue;"
    "    const rect = element.getBoundingClientRect();"
    "    if (!visible(style, rect) || !coversViewport(rect)) continue;"
    "    candidates.push({"
    "      source: 'computed_style',"
    "      position: style.position,"
    "      coverage: 'viewport',"
    "      has_visible_controls: hasControl(element),"
    "    });"
    "  }"
    "  return candidates;"
    "})()"
)

# Safety bound; an over-cap payload is returned as a typed structured-extraction failure.
COMPOSITION_STRUCTURED_EVIDENCE_MAX_CHARS = 120_000

# Injected from composition_evidence so the JS matches the parser's caps/vocabulary (single source of truth).
_STRUCTURED_CONST_HEADER = (
    f"const ANTI_BOT_PATTERNS={json.dumps(list(_ANTI_BOT_PATTERNS))};"
    f"const MODAL_IDENTITY_PATTERNS={json.dumps(sorted(_MODAL_IDENTITY_PATTERNS))};"
    f"const MODAL_ROLE_VALUES={json.dumps(sorted(_MODAL_ROLE_VALUES))};"
    f"const RESULT_CONTAINER_HINTS={json.dumps(sorted(_RESULT_CONTAINER_HINTS))};"
    f"const MAX_FORMS={int(_MAX_FORMS)};"
    f"const MAX_FIELDS_PER_FORM={int(_MAX_FIELDS_PER_FORM)};"
    f"const MAX_NAVIGATION_TARGETS={int(_MAX_NAVIGATION_TARGETS)};"
    f"const MAX_RESULT_CONTAINERS={int(_MAX_RESULT_CONTAINERS)};"
    f"const MAX_KEY_VALUE_RELATIONS={int(_MAX_KEY_VALUE_RELATIONS)};"
    f"const MAX_SELECTOR_CHARS={int(_MAX_SELECTOR_CHARS)};"
    f"const MAX_REVEAL_KEY_VALUE_RELATIONS={int(_MAX_REVEAL_KEY_VALUE_RELATIONS)};"
    f"const MAX_TABLE_HEADERS={int(_MAX_TABLE_HEADERS)};"
    f"const MAX_RESULT_SAMPLE_ROWS={int(_MAX_RESULT_SAMPLE_ROWS)};"
    f"const MAX_SELECT_OPTIONS={int(_MAX_SELECT_OPTIONS)};"
    f"const MAX_CHALLENGE_CONTROLS={int(_MAX_CHALLENGE_CONTROLS)};"
    f"const MAX_CLICKABLE_CONTROLS={int(_MAX_CLICKABLE_CONTROLS)};"
    f"const MAX_MODAL_OVERLAYS={int(_MAX_MODAL_OVERLAYS)};"
    f"const MAX_MODAL_DISMISS_CONTROLS={int(_MAX_MODAL_DISMISS_CONTROLS)};"
    f"const MAX_PAGE_OBSTRUCTIONS={int(_MAX_PAGE_OBSTRUCTIONS)};"
    f"const MAX_VISIBLE_TEXT_EXCERPT_CHARS={int(_MAX_VISIBLE_TEXT_EXCERPT_CHARS)};"
    f"const ANTI_BOT_SCAN_BYTES={int(_ANTI_BOT_SCAN_BYTES)};" + _JS_IMPLICIT_ROLE_HELPER
)

# Mirrors parse_composition_html's structural extraction; Python re-bounds the values to the exact caps.
_STRUCTURED_EVIDENCE_BODY = r"""
const lower = (v) => String(v == null ? '' : v).toLowerCase();
// Cap fields in-page so a giant element can't bloat the JSON past the size bound; Python re-bounds.
const FIELD_CAP = 2048;
const cap = (s) => (s.length > FIELD_CAP ? s.slice(0, FIELD_CAP) : s);
const attr = (el, k) => { const v = el && el.getAttribute ? el.getAttribute(k) : null; return typeof v === 'string' ? cap(v.trim()) : ''; };
const nodeText = (el) => { if (!el) return ''; return cap(String(el.textContent || '').replace(/\s+/g, ' ').trim()); };
  const readsAsOneLeaf = (el) => {
    if (!el || !el.children || !el.children.length) return true;
    let bearing = 0;
    const stack = Array.from(el.children);
    while (stack.length) {
      const cur = stack.pop();
      const kids = Array.from(cur.children || []);
      if (!kids.length) { if (nodeText(cur)) bearing += 1; }
      else { for (let i = 0; i < kids.length; i++) stack.push(kids[i]); }
      if (bearing > 1) return false;
    }
    return bearing <= 1;
  };
const classesFor = (el) => Array.from((el && el.classList) || []).map((c) => String(c).trim()).filter(Boolean);
const cssAttr = (v) => String(v).split('\\').join('\\\\').split('"').join('\\"');
const simpleIdent = (v) => { if (!v) return false; if (!/[A-Za-z_-]/.test(v[0])) return false; for (let i = 1; i < v.length; i++) { if (!/[A-Za-z0-9_-]/.test(v[i])) return false; } return true; };
const classSelector = (classes) => { const parts = []; for (const c of classes.slice(0, 3)) { parts.push(simpleIdent(c) ? '.' + c : '[class~="' + cssAttr(c) + '"]'); } return parts.join(''); };
const resolvesUniquely = (sel, el) => {
  if (!sel) return false;
  try { const m = document.querySelectorAll(sel); return m.length === 1 && m[0] === el; } catch (e) { return false; }
};
const TEXT_ANCHOR_MAX_HOPS = 4;
const TEXT_ANCHOR_MAX_LABELS = 4;
const TEXT_ANCHOR_MAX_LABEL_CHARS = 60;
const TEXT_ANCHOR_MAX_VALUE_ANCHORS = 2;
const labelLike = (text) => text.length >= 2 && text.indexOf('{') < 0;
// Page data ("9.42K", "+13.4%", "1,284") renames itself on the next refresh, so it anchors a node only
// when no wordy leaf in the same ancestor does. A digit alone does not make a name unstable — "Q3
// revenue" is as durable as any label — so a word carries the text back to the label side.
const valueLike = (text) => /\d/.test(text) && !/[A-Za-z]{3}/.test(text);
// Playwright resolves :has-text() itself and document.querySelectorAll throws on it, so the rung that
// emits a text anchor owns its own uniqueness check and the CSS filter must skip what it emitted.
// nodeText truncates at FIELD_CAP so one element cannot bloat the payload, but :has-text() reads the
// element's whole text. Matching on the capped copy would miss a same-shape competitor whose label sits
// past the cap and certify an anchor Playwright then resolves to two elements — so this compare, which
// feeds no payload, reads the full text.
// :has-text() reads rendered text and sees through open shadow roots, while querySelectorAll stops at
// the shadow boundary and textContent counts script bodies. Matching on the narrower view certifies an
// anchor Playwright then resolves to two elements, so this walks the same ground the engine will.
let openRootIndex = null;
const openRoots = () => {
  if (!openRootIndex) {
    openRootIndex = [document];
    const stack = [document];
    while (stack.length) {
      const root = stack.pop();
      let hosts; try { hosts = Array.from(root.querySelectorAll('*')); } catch (e) { hosts = []; }
      for (const host of hosts) {
        if (host.shadowRoot) { openRootIndex.push(host.shadowRoot); stack.push(host.shadowRoot); }
      }
    }
  }
  return openRootIndex;
};
const renderedText = (el) => {
  if (!el) return '';
  let parts = el.shadowRoot ? ' ' + String(el.shadowRoot.textContent || '') : '';
  let kids; try { kids = Array.from(el.querySelectorAll('*')); } catch (e) { kids = []; }
  for (const kid of kids) if (kid.shadowRoot) parts += ' ' + String(kid.shadowRoot.textContent || '');
  const own = String(el.textContent || '');
  let scripts; try { scripts = Array.from(el.querySelectorAll('script,style')); } catch (e) { scripts = []; }
  let text = own;
  for (const dead of scripts) text = text.split(String(dead.textContent || '')).join(' ');
  return (text + parts).split(/\s+/).join(' ').trim();
};
const untruncatedText = renderedText;
const anchorMatches = (base, label) => {
  const needle = String(label).toLowerCase();
  const found = [];
  for (const root of openRoots()) {
    let nodes; try { nodes = Array.from(root.querySelectorAll(base)); } catch (e) { continue; }
    for (const node of nodes) if (renderedText(node).toLowerCase().indexOf(needle) >= 0) found.push(node);
  }
  return found;
};
const shapeSelector = (el) => (el.tagName || '*').toLowerCase() + classSelector(classesFor(el));
const anchorSelector = (base, label) => base + ':has-text("' + cssAttr(label) + '")';
// One document pass keyed by ancestor, so a page of candidate nodes costs a single walk rather than
// a subtree scan per node; only leaves within TEXT_ANCHOR_MAX_HOPS of an ancestor are reachable.
let anchorLeafIndex = null;
const anchorLeavesFor = (el) => {
  if (!anchorLeafIndex) {
    anchorLeafIndex = new Map();
    for (const leaf of document.querySelectorAll('*')) {
      if (leaf.children && leaf.children.length) continue;
      const leafText = nodeText(leaf);
      if (!leafText || leafText.length > TEXT_ANCHOR_MAX_LABEL_CHARS || !labelLike(leafText)) continue;
      let node = leaf.parentElement;
      for (let hops = 0; node && hops < TEXT_ANCHOR_MAX_HOPS; hops++, node = node.parentElement) {
        const bucket = anchorLeafIndex.get(node);
        // Every leaf is kept: dropping by document order would decide which labels are even
        // considered before any of them is ranked, and the best label is often not the first.
        if (!bucket) { anchorLeafIndex.set(node, [{ el: leaf, text: leafText }]); continue; }
        bucket.push({ el: leaf, text: leafText });
      }
    }
  }
  return anchorLeafIndex.get(el) || [];
};
const textAnchorCandidateFor = (base, label, anchor, inner, el) => {
  const selector = anchorSelector(base, label) + (inner ? ' ' + inner : '');
  if (selector.length > MAX_SELECTOR_CHARS) return null;
  const anchors = anchorMatches(base, label);
  if (anchors.length !== 1 || anchors[0] !== anchor) return null;
  if (inner) {
    let within; try { within = Array.from(anchor.querySelectorAll(inner)); } catch (e) { return null; }
    if (within.length !== 1 || within[0] !== el) return null;
  }
  return { selector: selector, source: 'text_anchor' };
};
const textAnchorCandidatesFor = (el) => {
  const out = [];
  const ownText = nodeText(el);
  const shape = shapeSelector(el);
  // A control usually names itself ("Export", "Sign in"); anchoring it on a neighbour's text when its
  // own text is unique offers a weaker selector than the one a person would write.
  if (ownText && readsAsOneLeaf(el) && ownText.length <= TEXT_ANCHOR_MAX_LABEL_CHARS && labelLike(ownText)) {
    const candidate = textAnchorCandidateFor(shape, ownText, el, '', el);
    if (candidate) out.push(candidate);
  }
  let node = el;
  for (let hops = 0; node && node.tagName !== 'BODY' && node.tagName !== 'HTML' && hops <= TEXT_ANCHOR_MAX_HOPS; hops++, node = node.parentElement) {
    const base = shapeSelector(node);
    if (!base) continue;
    const inner = node === el ? '' : shape;
    const leaves = anchorLeavesFor(node).filter((leaf) => leaf.el !== el && leaf.text !== ownText);
    leaves.sort((a, b) => (valueLike(a.text) ? 1 : 0) - (valueLike(b.text) ? 1 : 0) || a.text.length - b.text.length);
    for (const leaf of leaves.slice(0, TEXT_ANCHOR_MAX_LABELS)) {
      const candidate = textAnchorCandidateFor(base, leaf.text, node, inner, el);
      if (!candidate) continue;
      // A stable label is the anchor worth having, so it ends the climb. Anchors built from page data
      // never do: a card whose figure and delta sit beside each other would otherwise stop the walk on
      // two volatile anchors and never reach the label one level up that actually names it.
      if (!valueLike(leaf.text)) { out.unshift(candidate); return out; }
      if (out.length < TEXT_ANCHOR_MAX_VALUE_ANCHORS) out.push(candidate);
    }
  }
  return out;
};
// A wrong label is worse than none: it is the field a staleness check compares against, so every
// rung here must name THIS node. Own name first, then the explicit label[for] association, then the
// label-then-value shape, and only then the nearest labelling ancestor scoped to that ancestor's own
// children — an ancestor-wide querySelector returns the first label in the whole subtree, which on a
// flat form is a sibling field's label rather than this one's.
const NAMING_SELECTOR = 'label,h1,h2,h3,h4,[aria-label]';
const CONTROL_SELECTOR = 'input,select,textarea,button,[role="textbox"],[role="combobox"]';
const VALUE_DEPENDENT_SOURCES = new Set(['name_value', 'class_value']);
const NAMED_BY_OWN_TEXT_ROLES = new Set(['button', 'link', 'menuitem', 'tab', 'option', 'checkbox', 'radio']);
const namingTextOf = (node) => attr(node, 'aria-label') || nodeText(node);
const labelContextFor = (el) => {
  const own = attr(el, 'aria-label');
  if (own) return own;
  const id = attr(el, 'id');
  if (id) {
    let associated = null;
    try { associated = document.querySelector('label[for="' + cssAttr(id) + '"]'); } catch (e) { associated = null; }
    if (associated) return namingTextOf(associated);
  }
  const wrapping = el.closest && el.closest('label');
  if (wrapping) {
    const text = nodeText(wrapping).split(nodeText(el)).join('').trim();
    if (text) return text;
  }
  if (readsAsOneLeaf(el)) {
    // A control's own text is its name, digits and all — a button reading "2024" is called 2024. A
    // passive node whose text is the datum ("9.42K") is not named by it, and echoing the value there
    // would make every refresh look like the element changed identity.
    const ownText = nodeText(el);
    const named = NAMED_BY_OWN_TEXT_ROLES.has(attr(el, 'role') || implicitRole(el));
    if (ownText && (named || !valueLike(ownText))) return ownText;
  }
  const inner = el.querySelector && el.querySelector(NAMING_SELECTOR);
  if (inner) return namingTextOf(inner);
  const first = el.firstElementChild;
  if (first && !(first.children || []).length && (el.children || []).length > 1) {
    const leading = nodeText(first);
    if (leading && !valueLike(leading)) return leading;
  }
  let node = el.parentElement;
  for (let hops = 0; node && hops < TEXT_ANCHOR_MAX_HOPS; hops++, node = node.parentElement) {
    // A label that sits beside a field names the field it precedes, so document order decides which
    // one is ours. Taking the first match instead would give every field on a flat form the first
    // field's label — a confident wrong name, which a staleness check trusts as readily as a right one.
    const kids = Array.from(node.children || []);
    const mine = kids.indexOf(kids.find((k) => k === el || k.contains(el)));
    const naming = kids.filter((k) => k !== el && !k.contains(el) && k.matches && k.matches(NAMING_SELECTOR));
    // A label names the control it sits next to, so another control standing between the two means the
    // label is that one's, not ours. Without this a lone trailing label is handed to every field above
    // it, and a leading label to every field below — a confident wrong name, which the check that reads
    // this field trusts exactly as readily as a right one.
    const controlBetween = (from, to) => kids
      .slice(Math.min(from, to) + 1, Math.max(from, to))
      .some((k) => k.matches && (k.matches(CONTROL_SELECTOR) || k.querySelector(CONTROL_SELECTOR)));
    const adjacent = naming.filter((k) => !controlBetween(kids.indexOf(k), mine));
    if (adjacent.length) {
      const preceding = adjacent.filter((k) => kids.indexOf(k) < mine);
      // Nothing precedes us and several could apply: no honest pick, so say nothing.
      if (!preceding.length && adjacent.length > 1) return '';
      return namingTextOf(preceding.length ? preceding[preceding.length - 1] : adjacent[0]);
    }
  }
  return '';
};
// The label is reported whole. It is what a later check compares an element against, and a prefix that
// reads like a name would make a truncation look like a rename; nodeText's own bound still applies.
const identityFor = (el) => ({
  tag: (el.tagName || '').toLowerCase(),
  role: attr(el, 'role') || implicitRole(el),
  label_context: labelContextFor(el),
});
const structuralPath = (el) => {
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && parts.length < 8) {
    const tag = (node.tagName || '').toLowerCase();
    if (!tag || tag === 'html') break;
    const parent = node.parentElement;
    if (!parent) { parts.unshift(tag); break; }
    let idx = 1;
    for (let i = 0; i < parent.children.length; i++) {
      const sib = parent.children[i];
      if (sib === node) break;
      if (sib.tagName === node.tagName) idx++;
    }
    parts.unshift(tag + ':nth-of-type(' + idx + ')');
    const pid = attr(parent, 'id');
    if (pid && simpleIdent(pid)) { parts.unshift('#' + pid); break; }
    node = parent;
  }
  const full = parts.join(' > ');
  if (full.length <= MAX_SELECTOR_CHARS) return full;
  for (let start = 1; start < parts.length; start++) {
    const tail = parts.slice(start).join(' > ');
    if (tail.length <= MAX_SELECTOR_CHARS && resolvesUniquely(tail, el)) return tail;
  }
  return full;
};
// The selector is a contract: the model clicks it and authors it into submitted blocks, so an
// ambiguous or unresolvable guess costs a failed run rather than a retry. Every candidate is
// verified to match this exact node and nothing else before it is handed out.
const selectorCandidatesFor = (el) => {
  const tag = (el.tagName || '*').toLowerCase();
  const candidates = [];
  // Same bound the parser applies, enforced before the payload is measured: an over-length selector is
  // dropped either way, so shipping it only spends the packet's size budget — and an oversized packet
  // is discarded whole, costing every other carrier on the page.
  const offer = (selector, source) => {
    if (selector && selector.length <= MAX_SELECTOR_CHARS) candidates.push({ selector: selector, source: source });
  };
  const id = attr(el, 'id');
  if (id) offer(simpleIdent(id) ? '#' + id : tag + '[id="' + cssAttr(id) + '"]', 'id');
  const name = attr(el, 'name'); const value = attr(el, 'value');
  if (name && value) offer(tag + '[name="' + cssAttr(name) + '"][value="' + cssAttr(value) + '"]', 'name_value');
  const classes = classesFor(el); const cs = classSelector(classes);
  if (cs && value) offer(tag + cs + '[value="' + cssAttr(value) + '"]', 'class_value');
  if (name) offer(tag + '[name="' + cssAttr(name) + '"]', 'name');
  const ariaLabel = attr(el, 'aria-label');
  if (ariaLabel) offer(tag + '[aria-label="' + cssAttr(ariaLabel) + '"]', 'aria_label');
  const href = attr(el, 'href');
  if (tag === 'a' && href) offer('a[href="' + cssAttr(href) + '"]', 'href');
  if (cs) offer(tag + cs, 'class');
  const type = attr(el, 'type');
  if (cs && type) offer(tag + cs + '[type="' + cssAttr(type) + '"]', 'class_type');
  // The text rung costs a document walk, so it is paid for only when no attribute rung already names
  // this node alone; it is offered above the positional path and below any unique attribute. A rung
  // that is unique only because of a current value does not count as naming it: the value is what
  // changes on the next render, which is the failure this rung exists to avoid.
  if (!candidates.some((candidate) => !VALUE_DEPENDENT_SOURCES.has(candidate.source) && resolvesUniquely(candidate.selector, el))) {
    for (const candidate of textAnchorCandidatesFor(el)) candidates.push(candidate);
  }
  offer(structuralPath(el), 'structural');
  const seen = new Set();
  const offered = [];
  for (const candidate of candidates) {
    if (seen.has(candidate.selector)) continue;
    seen.add(candidate.selector);
    // querySelectorAll throws on :has-text(), so re-verifying a text anchor here would drop every one
    // it emitted; the rung already proved this selector resolves to this node alone.
    if (candidate.source === 'text_anchor') { offered.push(candidate); continue; }
    try { if (!Array.from(document.querySelectorAll(candidate.selector)).includes(el)) continue; } catch (e) { continue; }
    offered.push(candidate);
  }
  return offered;
};
// A relation's key is the label the page itself prints beside the value, so it is the anchor the
// carrier is offered under before any shape the generic ladder happens to pick.
const relationCandidatesFor = (carrier, keyText) => {
  const label = String(keyText || '').trim();
  const base = shapeSelector(carrier);
  const keyed = label && labelLike(label) && label.length <= TEXT_ANCHOR_MAX_LABEL_CHARS && base
    ? textAnchorCandidateFor(base, label, carrier, '', carrier)
    : null;
  const rest = selectorCandidatesFor(carrier).filter((candidate) => !keyed || candidate.selector !== keyed.selector);
  return keyed ? [keyed].concat(rest) : rest;
};
// The primary selector feeds CSS APIs (match counts, position lookups), so it stays CSS-only even
// when a text anchor is the sturdier offer.
const selectorFor = (el) => {
  const candidates = selectorCandidatesFor(el).filter((item) => item.source !== 'text_anchor').map((item) => item.selector);
  for (let i = 0; i < candidates.length; i++) {
    if (resolvesUniquely(candidates[i], el)) return candidates[i];
  }
  return candidates.length ? candidates[0] : (el.tagName || '*').toLowerCase();
};
// A submit control with no text still has an identity in title/aria-label/alt (an icon-only
// "Sign in with Google" is the common shape). Reporting it as an empty string offers the model an
// anonymous control alongside the named one it actually wants.
const controlLabel = (el) => {
  const own = nodeText(el) || attr(el, 'value');
  if (own) return own;
  const img = el.querySelector && el.querySelector('img[alt], [aria-label]');
  return (
    attr(el, 'aria-label') ||
    attr(el, 'title') ||
    (img ? attr(img, 'alt') || attr(img, 'aria-label') : '') ||
    ''
  );
};
// A filled value lives on the DOM property, not the attribute, and a password's value must never
// be reported at all. Without a non-secret filled signal the page looks unfilled after a credential
// fill, and the agent concludes the fill failed and hunts for another way to sign in.
const isFilled = (el) => {
  try {
    return typeof el.value === 'string' && el.value.length > 0;
  } catch (e) {
    return false;
  }
};
const FIELD_TAGS = new Set(['input', 'select', 'textarea', 'button']);
const adjacentText = (field) => {
  for (const dir of ['next', 'prev']) {
    let sib = dir === 'next' ? field.nextSibling : field.previousSibling;
    let count = 0;
    while (sib && count < 4) {
      const isEl = sib.nodeType === 1;
      const tag = isEl ? sib.tagName.toLowerCase() : '';
      if (isEl && FIELD_TAGS.has(tag)) break;
      const text = isEl ? nodeText(sib) : String(sib.textContent || '').trim();
      if (text) return text;
      sib = dir === 'next' ? sib.nextSibling : sib.previousSibling;
      count++;
    }
  }
  return '';
};
const parentTextLabel = (field) => {
  for (const tag of ['td', 'th', 'li', 'div', 'span']) {
    const p = field.closest ? field.closest(tag) : null;
    if (!p) continue;
    const text = nodeText(p);
    if (text.length > 0 && text.length <= 240) return text;
  }
  return '';
};
const fieldLabel = (field) => {
  const id = attr(field, 'id');
  if (id) {
    let lab = null;
    try { lab = document.querySelector('label[for="' + cssAttr(id) + '"]'); } catch (e) { lab = null; }
    if (lab) { const t = nodeText(lab); if (t) return t; }
  }
  const parentLabel = field.closest ? field.closest('label') : null;
  if (parentLabel) {
    const ft = nodeText(field);
    let t = nodeText(parentLabel);
    if (ft) t = t.split(ft).join('');
    t = t.trim();
    if (t) return t;
  }
  for (const v of [attr(field, 'aria-label'), adjacentText(field), parentTextLabel(field), attr(field, 'title'), attr(field, 'value')]) {
    if (v) return v;
  }
  return '';
};
const selectOptions = (el) => {
  const out = [];
  const opts = el.querySelectorAll('option');
  for (let i = 0; i < opts.length && out.length < MAX_SELECT_OPTIONS; i++) {
    out.push({ text: nodeText(opts[i]), value: attr(opts[i], 'value'), selected: opts[i].hasAttribute('selected') });
  }
  return out;
};
const controlDisabled = (el) => !!(el.hasAttribute('disabled') || lower(attr(el, 'aria-disabled')) === 'true' || lower(attr(el, 'data-disabled')) === 'true');
const controlReadonly = (el) => !!(el.readOnly === true || el.hasAttribute('readonly') || lower(attr(el, 'aria-readonly')) === 'true');
const modalIdentity = (el) => [
  (el.tagName || '').toLowerCase(), attr(el, 'id'), classesFor(el).join(' '), attr(el, 'role'),
  attr(el, 'aria-label'), attr(el, 'title'), attr(el, 'data-testid'), attr(el, 'data-test'), attr(el, 'data-dismiss'),
].join(' ').toLowerCase();
const isModalCandidate = (el) => {
  if (MODAL_ROLE_VALUES.includes(lower(attr(el, 'role')).trim())) return true;
  if (lower(attr(el, 'aria-modal')).trim() === 'true') return true;
  const ident = modalIdentity(el);
  return MODAL_IDENTITY_PATTERNS.some((p) => ident.includes(p));
};
const isHiddenModal = (el) => {
  let cur = el;
  while (cur && cur.nodeType === 1) {
    if (lower(attr(cur, 'aria-hidden')).trim() === 'true') return true;
    if (cur.hasAttribute && cur.hasAttribute('hidden')) return true;
    const style = lower(attr(cur, 'style')).split(' ').join('');
    if (style.includes('display:none') || style.includes('visibility:hidden')) return true;
    cur = cur.parentElement;
  }
  return false;
};
const controlVisible = (node) => {
  if (!node || !node.getBoundingClientRect) return false;
  let style; try { style = window.getComputedStyle(node); } catch (e) { return false; }
  const rect = node.getBoundingClientRect();
  // Match Playwright for form-control readiness: opacity alone does not make a control hidden.
  return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
};
const modalDismissControls = (node) => {
  const out = [];
  const seen = new Set();
  for (const c of node.querySelectorAll('button,a,input')) {
    if (out.length >= MAX_MODAL_DISMISS_CONTROLS) break;
    const selector = selectorFor(c);
    if (seen.has(selector)) continue;
    // Every control the dialog offers is reported. A keyword list cannot name every way a dialog
    // closes ("No, keep ...", an icon-only glyph), and filtering on one leaves the agent looking at
    // a modal it has no way to clear.
    const text = controlLabel(c);
    seen.add(selector);
    out.push({ tag: (c.tagName || '').toLowerCase(), text: text, aria_label: attr(c, 'aria-label'), title: attr(c, 'title'), selector: selector, selector_candidates: selectorCandidatesFor(c), identity: identityFor(c), type: attr(c, 'type') });
  }
  return out;
};

const all = document.querySelectorAll('*');
const SKIP_TAGS = new Set(['script', 'style', 'noscript']);

const forms = [];
for (const form of document.querySelectorAll('form')) {
  if (forms.length >= MAX_FORMS) break;
  const fields = [];
  const submitControls = [];
  for (const node of form.querySelectorAll('input,select,textarea,button')) {
    const tag = (node.tagName || '').toLowerCase();
    const declaredType = lower(attr(node, 'type'));
    const fieldType = tag === 'button'
      ? (['button', 'reset', 'submit'].includes(declaredType) ? declaredType : 'submit')
      : (declaredType || tag || 'text');
    if (tag === 'input' && (fieldType === 'hidden' || fieldType === 'reset')) continue;
    if (tag === 'button' || fieldType === 'submit' || fieldType === 'button') {
      submitControls.push({ text: controlLabel(node), name: attr(node, 'name'), id: attr(node, 'id'), value: attr(node, 'value'), class: classesFor(node), type: fieldType, disabled: controlDisabled(node), visible: controlVisible(node), selector: selectorFor(node), selector_candidates: selectorCandidatesFor(node), identity: identityFor(node) });
      continue;
    }
    if (fields.length >= MAX_FIELDS_PER_FORM) continue;
    fields.push({ name: attr(node, 'name'), id: attr(node, 'id'), label: fieldLabel(node), type: fieldType, value: attr(node, 'value'), filled: isFilled(node), class: classesFor(node), placeholder: attr(node, 'placeholder'), required: !!(node.hasAttribute('required') || lower(attr(node, 'aria-required')) === 'true'), disabled: controlDisabled(node), readonly: controlReadonly(node), visible: controlVisible(node), checked: node.hasAttribute('checked'), options: tag === 'select' ? selectOptions(node) : [], selector: selectorFor(node), selector_candidates: selectorCandidatesFor(node), identity: identityFor(node) });
  }
  forms.push({ id: attr(form, 'id'), name: attr(form, 'name'), action: attr(form, 'action'), method: attr(form, 'method'), fields: fields, submit_controls: submitControls });
}

const NAV_REGION_TAGS = ['header', 'nav', 'footer', 'main'];
// Outermost landmark, so a card <header> inside <main> counts as content: resolving to the
// nearest one splits nested landmarks into extra buckets and costs content its share.
const navRegionOf = (el) => {
  let region = 'other';
  for (let cur = el.parentElement; cur; cur = cur.parentElement) {
    const tagName = (cur.tagName || '').toLowerCase();
    if (NAV_REGION_TAGS.indexOf(tagName) !== -1) region = tagName;
  }
  return region;
};
const navEligibleLinks = [];
const navBuckets = new Map();
const baseHost = location.host.toLowerCase();
for (const link of document.querySelectorAll('a[href]')) {
  const rawHref = attr(link, 'href');
  if (!rawHref || rawHref.startsWith('#') || lower(rawHref).startsWith('javascript:')) continue;
  let resolved; try { resolved = new URL(rawHref, location.href).href; } catch (e) { continue; }
  let host; try { host = new URL(resolved).host.toLowerCase(); } catch (e) { continue; }
  if (!host || host !== baseHost) continue;
  const region = navRegionOf(link);
  const eligible = { link: link, href: resolved, region: region };
  if (!navBuckets.has(region)) navBuckets.set(region, []);
  navBuckets.get(region).push(eligible);
  navEligibleLinks.push(eligible);
}
// A site's global header can hold more links than the whole budget, so filling in document
// order spends every slot before the scan reaches page content. Take one per region per pass,
// and only when the budget actually has to cut.
const navSelected = [];
if (navEligibleLinks.length <= MAX_NAVIGATION_TARGETS) {
  for (const eligible of navEligibleLinks) navSelected.push(eligible);
} else {
  const navLists = Array.from(navBuckets.values());
  for (let depth = 0; navSelected.length < MAX_NAVIGATION_TARGETS; depth++) {
    let placed = false;
    for (const bucket of navLists) {
      if (navSelected.length >= MAX_NAVIGATION_TARGETS) break;
      if (depth < bucket.length) { navSelected.push(bucket[depth]); placed = true; }
    }
    if (!placed) break;
  }
}
const navTargets = [];
for (const picked of navSelected) {
  const link = picked.link;
  const entry = { text: nodeText(link), href: picked.href, region: picked.region, selector: selectorFor(link), selector_candidates: selectorCandidatesFor(link), identity: identityFor(link) };
  if (link.hasAttribute('download')) entry.has_download_attr = true;
  navTargets.push(entry);
}
const navigationTargetsTruncated = navEligibleLinks.length > navTargets.length;

const clickableSelector = (el) => {
  const tag = (el.tagName || '*').toLowerCase();
  const id = attr(el, 'id'); if (id) return '#' + id;
  const da = attr(el, 'data-action'); if (da) return tag + '[data-action="' + cssAttr(da) + '"]';
  const al = attr(el, 'aria-label'); if (al) return tag + '[aria-label="' + cssAttr(al) + '"]';
  const name = attr(el, 'name'); const value = attr(el, 'value');
  if (name && value) return tag + '[name="' + cssAttr(name) + '"][value="' + cssAttr(value) + '"]';
  const cs = classSelector(classesFor(el));
  if (cs) return tag + cs;
  return '';
};
const clickableText = (el) => nodeText(el) || attr(el, 'aria-label') || attr(el, 'value') || attr(el, 'title');
const elementVisible = (node) => {
  if (!node || !node.getBoundingClientRect) return false;
  let style; try { style = window.getComputedStyle(node); } catch (e) { return false; }
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden' && Number.parseFloat(style.opacity || '1') > 0.05 && rect.width > 0 && rect.height > 0;
};
const usedClickableSelectors = new Set();
for (const f of forms) for (const sc of (f.submit_controls || [])) if (sc.selector) usedClickableSelectors.add(sc.selector);
for (const n of navTargets) if (n.selector) usedClickableSelectors.add(n.selector);
const clickableControls = [];
const seenClickableText = new Set();
for (const el of document.querySelectorAll('button,[role="button"],[data-action]')) {
  if (clickableControls.length >= MAX_CLICKABLE_CONTROLS) break;
  const tag = (el.tagName || '').toLowerCase();
  if (SKIP_TAGS.has(tag)) continue;
  if (!elementVisible(el)) continue;
  if (el.closest && el.closest('form')) continue;
  const text = clickableText(el);
  const selector = clickableSelector(el);
  let unique = false;
  if (selector) { try { unique = document.querySelectorAll(selector).length === 1; } catch (e) { unique = false; } }
  if (selector && unique && !usedClickableSelectors.has(selector)) {
    clickableControls.push({ text: text, selector: selector, selector_candidates: selectorCandidatesFor(el), identity: identityFor(el), tag: tag });
    usedClickableSelectors.add(selector);
    if (text) seenClickableText.add(text);
    continue;
  }
  if (!text || seenClickableText.has(text)) continue;
  // No CSS selector singles this control out, which is the case the text rung exists for: reporting
  // the control with its text alone leaves the model nothing to address it by.
  clickableControls.push({ text: text, tag: tag, selector_candidates: selectorCandidatesFor(el), identity: identityFor(el) });
  seenClickableText.add(text);
}

const resultContainers = [];
let resultContainersTruncated = false;
const selectorMatchCount = (selector) => { if (!selector) return 0; try { return document.querySelectorAll(selector).length; } catch (e) { return 0; } };
const resultRowTextIsContent = (s) => {
  const text = lower(String(s || '').replace(/\s+/g, ' ').trim());
  return !!text && !['0 results', 'no matching records', 'no records found', 'no results', 'no results found', 'nothing found'].some((p) => text.includes(p));
};
const resultEntry = (node, tag) => {
  const selector = selectorFor(node);
  const entry = { tag: tag, id: attr(node, 'id'), selector: selector, selector_candidates: selectorCandidatesFor(node), identity: identityFor(node), selector_match_count: selectorMatchCount(selector), visible: elementVisible(node), is_table: tag === 'table' };
	  if (tag === 'table') {
	    let rows = Array.from(node.querySelectorAll(':scope > tbody > tr')).filter((r) => r.querySelector(':scope > td'));
	    if (!rows.length) rows = Array.from(node.querySelectorAll(':scope > tr')).filter((r) => r.querySelector(':scope > td'));
	    entry.row_count = rows.length;
	    entry.rows_truncated = rows.length > MAX_RESULT_SAMPLE_ROWS;
	    const headerNodes = Array.from(node.querySelectorAll(':scope > thead > tr > th'));
	    const headers = headerNodes.slice(0, MAX_TABLE_HEADERS).map((h, i) => ({ text: nodeText(h), column_index: i })).filter((h) => !!h.text);
	    if (headers.length) entry.headers = headers;
	    entry.span_free = !node.querySelector('th[colspan],th[rowspan],td[colspan],td[rowspan]');
	    entry.nested_table_free = !node.querySelector(':scope table');
	    entry.row_selector = selector ? selector + ' > tbody > tr' : '';
	    entry.rows = rows.slice(0, MAX_RESULT_SAMPLE_ROWS).map((row, rowIndex) => ({
	      row_index: rowIndex,
	      visible: elementVisible(row),
	      has_row_header: !!row.querySelector(':scope > th'),
	      cells: Array.from(row.querySelectorAll(':scope > th, :scope > td')).slice(0, MAX_TABLE_HEADERS).map((cell, columnIndex) => ({
	        column_index: columnIndex,
	        visible: elementVisible(cell),
	        has_text: !!nodeText(cell),
	        text: nodeText(cell),
	      })),
	    }));
	    const sampleRows = rows.map((r) => Array.from(r.children || []).map((c) => nodeText(c)).filter(Boolean).join(' ') || nodeText(r)).filter(resultRowTextIsContent).slice(0, MAX_RESULT_SAMPLE_ROWS);
	    if (sampleRows.length) entry.sample_rows = sampleRows;
	  } else {
	    const text = nodeText(node);
	    if (text) entry.text_excerpt = text;
	  }
	  return entry;
	};
	// Whole tokens, matching the parser twin: "arrow" contains "row", so a substring test hands every
	// slot of the bounded budget to an icon sprite sheet before the document reaches a real result.
	const matchesResultHint = (identity) => identity.split(/[^a-z0-9]+/).some((token) => !!token && RESULT_CONTAINER_HINTS.includes(token));
	for (const node of all) {
	  const tag = (node.tagName || '').toLowerCase();
	  if (SKIP_TAGS.has(tag)) continue;
	  const identity = (attr(node, 'id') + ' ' + classesFor(node).join(' ')).toLowerCase();
	  if (tag === 'table' || matchesResultHint(identity)) {
	    if (resultContainers.length >= MAX_RESULT_CONTAINERS) { resultContainersTruncated = true; break; }
	    resultContainers.push(resultEntry(node, tag));
	  }
	}

const keyValueRelations = [];
let keyValueRelationsTruncated = false;
// Counting continues past the cap even though recording stops: whether a captured label is unique
// on the page is what decides a bind, and stopping both turned "there is more" into "trust none".
const foldedKeyCounts = new Map();
const countFoldedKey = (text) => { const k = String(text || '').toLowerCase(); if (k) foldedKeyCounts.set(k, (foldedKeyCounts.get(k) || 0) + 1); };
const walkedValueCounts = new Map();
const countWalkedValue = (text) => { const v = String(text || ''); if (v) walkedValueCounts.set(v, (walkedValueCounts.get(v) || 0) + 1); };
const keyValueSkipTags = new Set(['body', 'form', 'html', 'table', 'tbody', 'thead', 'tr']);
const nonContentChildTags = new Set(['style', 'script', 'noscript', 'svg', 'template']);
const bareMagnitude = /^-?\$?\d{1,3}(?:[,\s]?\d{3})*(?:\.\d+)?[KMB]?$/i;
const insidePageChrome = (node) => !!(node.closest && node.closest('nav,aside,header,footer,[role=navigation],[role=menu],[role=menubar],[role=banner],[role=complementary]'));
const metricCardNodes = new Set();
const insideMetricCard = (node) => { let ancestor = node; while (ancestor) { if (metricCardNodes.has(ancestor)) return true; ancestor = ancestor.parentElement; } return false; };
// Mirror of _append_requested_target_relations: the labels the turn asked for are resolved before
// any shape pass, so a tile that nests its figure deeper is still read by its own label rather than
// skipped and replaced by whatever generic pair the page happens to offer.
const isLeafEl = (el) => !el.children || el.children.length === 0;
const withinEl = (el, ancestor) => { let c = el; while (c) { if (c === ancestor) return true; c = c.parentElement; } return false; };
const valueBesideLabel = (labelEl) => {
  let branch = labelEl;
  let ancestor = labelEl.parentElement;
  while (ancestor && ancestor.tagName !== 'BODY' && ancestor.tagName !== 'HTML') {
    const candidates = [];
    for (const leaf of Array.from(ancestor.querySelectorAll('*'))) {
      if (!isLeafEl(leaf) || withinEl(leaf, branch)) continue;
      if (bareMagnitude.test(nodeText(leaf))) candidates.push(leaf);
    }
    if (candidates.length > 1) return null;
    if (candidates.length === 1) {
      const valueLeaf = candidates[0];
      const carrier = valueLeaf.parentElement;
      if (!carrier) return null;
      const kids = Array.from(carrier.children || []);
      const childIndex = kids.indexOf(valueLeaf);
      if (childIndex < 0) return null;
      const labelText = nodeText(labelEl);
      let labelSelector = '';
      if (!kids.length || nodeText(kids[0]) !== labelText) {
        labelSelector = selectorFor(labelEl);
        if (!labelSelector || labelSelector.length > MAX_SELECTOR_CHARS || !resolvesUniquely(labelSelector, labelEl)) return null;
      }
      const sel = selectorFor(carrier);
      if (!sel || sel.length > MAX_SELECTOR_CHARS) return null;
      const matches = selectorMatchCount(sel);
      if (!matches) return null;
      let pos = -1;
      try { pos = Array.from(document.querySelectorAll(sel)).indexOf(carrier); } catch (e) { pos = -1; }
      if (pos < 0) return null;
      return { owner: ancestor, relation: { key_text: labelText, selector_candidates: relationCandidatesFor(carrier, labelText), identity: identityFor(carrier), label_selector: labelSelector, value_text: nodeText(valueLeaf), container_selector: sel, container_match_count: matches, container_position: pos, value_child_index: childIndex, direct_child_count: kids.length, visible: true, value_visible: true } };
    }
    branch = ancestor;
    ancestor = ancestor.parentElement;
  }
  return null;
};
for (const target of REQUESTED_TARGETS) {
  const wanted = String(target || '').trim().toLowerCase();
  if (!wanted) continue;
  for (const node of all) {
    if (!isLeafEl(node) || !elementVisible(node) || insidePageChrome(node)) continue;
    if (nodeText(node).trim().toLowerCase() !== wanted) continue;
    const found = valueBesideLabel(node);
    if (!found) continue;
    countFoldedKey(found.relation.key_text);
    countWalkedValue(found.relation.value_text);
    keyValueRelations.push(found.relation);
    metricCardNodes.add(found.owner);
    break;
  }
}
for (const node of all) {
  const tag = (node.tagName || '').toLowerCase();
  if (keyValueSkipTags.has(tag) || !elementVisible(node) || insidePageChrome(node) || insideMetricCard(node)) continue;
  const children = Array.from(node.children || []);
  if (children.length < 2 || children.length > 6) continue;
  if (children.some((child) => nonContentChildTags.has((child.tagName || '').toLowerCase()))) continue;
  const leaves = [];
  let nestedOk = true;
  for (let index = 0; index < children.length; index++) {
    const child = children[index];
    const grand = Array.from(child.children || []);
    if (!grand.length) { const text = nodeText(child); if (text) leaves.push({ index: index, text: text, carrier: null, carrierIndex: 0, carrierCount: 0 }); continue; }
    if (grand.some((g) => !readsAsOneLeaf(g))) { nestedOk = false; break; }
    for (let gi = 0; gi < grand.length; gi++) { const text = nodeText(grand[gi]); if (text) leaves.push({ index: index, text: text, carrier: child, carrierIndex: gi, carrierCount: grand.length }); }
  }
  if (!nestedOk || !leaves.length || leaves.length > 8) continue;
  const magnitudeLeaves2 = leaves.filter((leaf) => bareMagnitude.test(leaf.text));
  if (magnitudeLeaves2.length !== 1) continue;
  const cardValueIndex = magnitudeLeaves2[0].index;
  const cardValueText = magnitudeLeaves2[0].text;
  // The compiled read takes a direct child's whole text, so the relation is anchored wherever the
  // figure is that child: the tile when the figure is a direct child, the row holding it when the
  // figure sits beside a comparison.
  let cardCarrier = node;
  let cardChildIndex = cardValueIndex;
  let cardChildCount = children.length;
  if (nodeText(children[cardValueIndex]) !== cardValueText) {
    const anchor = magnitudeLeaves2[0];
    if (!anchor.carrier) continue;
    cardCarrier = anchor.carrier;
    cardChildIndex = anchor.carrierIndex;
    cardChildCount = anchor.carrierCount;
  }
  const headingLeaves = leaves.filter((leaf) => leaf.index !== cardValueIndex && leaf.text.length <= 60 && labelLike(leaf.text) && !bareMagnitude.test(leaf.text));
  const headingChildIndex = headingLeaves.length ? headingLeaves[0].index : 0;
  if (!headingLeaves.length) continue;
  const cardSelector = selectorFor(cardCarrier);
  const cardMatches = selectorMatchCount(cardSelector);
  if (!cardMatches) continue;
  let cardPosition = -1;
  try { cardPosition = Array.from(document.querySelectorAll(cardSelector)).indexOf(cardCarrier); } catch (e) { cardPosition = -1; }
  if (cardPosition < 0) continue;
  countFoldedKey(headingLeaves[0].text);
  countWalkedValue(cardValueText);
  if (keyValueRelations.length >= MAX_KEY_VALUE_RELATIONS) { keyValueRelationsTruncated = true; continue; }
  keyValueRelations.push({ key_text: headingLeaves[0].text, selector_candidates: relationCandidatesFor(cardCarrier, headingLeaves[0].text), identity: identityFor(cardCarrier), value_text: cardValueText, container_selector: cardSelector, container_match_count: cardMatches, container_position: cardPosition, value_child_index: cardChildIndex, label_child_index: (cardCarrier === node ? headingChildIndex : -1), direct_child_count: cardChildCount, visible: true, value_visible: true });
  metricCardNodes.add(node);
}
for (const node of all) {
  const tag = (node.tagName || '').toLowerCase();
  if (keyValueSkipTags.has(tag) || !elementVisible(node) || insidePageChrome(node) || insideMetricCard(node)) continue;
  const children = Array.from(node.children || []);
  if (children.length !== 2) continue;
  if (children[0].children && children[0].children.length > 0) continue;
  if (children.some((child) => nonContentChildTags.has((child.tagName || '').toLowerCase()))) continue;
  const keyText = nodeText(children[0]);
  const valueText = nodeText(children[1]);
  if (!keyText || keyText.length > 120 || !valueText || keyText === valueText || !labelLike(keyText)) continue;
  countFoldedKey(keyText);
  countWalkedValue(valueText);
  if (keyValueRelations.length >= MAX_KEY_VALUE_RELATIONS) { keyValueRelationsTruncated = true; continue; }
  const selector = selectorFor(node);
  const matches = selectorMatchCount(selector);
  if (!matches) continue;
  let position = -1;
  try { position = Array.from(document.querySelectorAll(selector)).indexOf(node); } catch (e) { position = -1; }
  if (position < 0) continue;
  keyValueRelations.push({ key_text: keyText, selector_candidates: relationCandidatesFor(node, keyText), identity: identityFor(node), value_text: valueText, container_selector: selector, container_match_count: matches, container_position: position, value_child_index: 1, direct_child_count: children.length, visible: true, value_visible: elementVisible(children[1]) });
}

const revealHintTokens = (node) => (attr(node, 'id') + ' ' + classesFor(node).join(' ')).toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
const matchesResultHintToken = (node) => revealHintTokens(node).some((t) => RESULT_CONTAINER_HINTS.includes(t));
const revealHeadingTags = new Set(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']);
let revealRelationCount = 0;
let revealRelationsTruncated = false;
for (const node of all) {
  const tag = (node.tagName || '').toLowerCase();
  if (keyValueSkipTags.has(tag) || !elementVisible(node) || insidePageChrome(node) || insideMetricCard(node)) continue;
  if (!matchesResultHintToken(node)) continue;
  const children = Array.from(node.children || []);
  if (children.length < 3 || children.length > 6) continue;
  if (children.some((c) => c.children && c.children.length > 0)) continue;
  const heading = children[0];
  if (!revealHeadingTags.has((heading.tagName || '').toLowerCase()) || !elementVisible(heading)) continue;
  const keyText = nodeText(heading);
  if (!keyText || keyText.length > 120) continue;
  const selector = selectorFor(node);
  const matches = selectorMatchCount(selector);
  if (!matches) continue;
  let position = -1;
  try { position = Array.from(document.querySelectorAll(selector)).indexOf(node); } catch (e) { position = -1; }
  if (position < 0) continue;
  const valueLeaves = [];
  for (let i = 1; i < children.length; i++) {
    const leaf = children[i];
    if (!elementVisible(leaf)) continue;
    const valueText = nodeText(leaf);
    if (!valueText || keyText === valueText) continue;
    valueLeaves.push({ index: i, valueText: valueText });
  }
  const magnitudeLeaves = valueLeaves.filter((leaf) => bareMagnitude.test(leaf.valueText));
  const designatedIndex = valueLeaves.length === 1 ? valueLeaves[0].index : (magnitudeLeaves.length === 1 ? magnitudeLeaves[0].index : null);
  let capped = false;
  // A leaf that claims no key text gets no label-keyed anchor: an anchor naming a label this
  // relation does not itself claim reads as though it identified this leaf's value.
  for (const leaf of valueLeaves) {
    if (keyValueRelations.length >= MAX_KEY_VALUE_RELATIONS || revealRelationCount >= MAX_REVEAL_KEY_VALUE_RELATIONS) { revealRelationsTruncated = true; capped = true; break; }
    keyValueRelations.push({ key_text: leaf.index === designatedIndex ? keyText : '', selector_candidates: relationCandidatesFor(node, leaf.index === designatedIndex ? keyText : ''), identity: identityFor(node), value_text: leaf.valueText, container_selector: selector, container_match_count: matches, container_position: position, value_child_index: leaf.index, direct_child_count: children.length, visible: true, value_visible: true });
    revealRelationCount++;
  }
  if (capped) break;
}

const challengeControls = [];
const seenChallenge = new Set();
const challengeIdentity = (node) => {
  const tag = (node.tagName || '').toLowerCase();
  return [tag, attr(node, 'id'), attr(node, 'name'), classesFor(node).join(' '), attr(node, 'src'), attr(node, 'type'), attr(node, 'data-sitekey'), attr(node, 'data-callback'), attr(node, 'data-expired-callback'), attr(node, 'data-error-callback'), attr(node, 'aria-label'), attr(node, 'title')].join(' ').toLowerCase();
};
const insideChallengeCarrier = (node) => {
  for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
    const identity = challengeIdentity(ancestor);
    if (ANTI_BOT_PATTERNS.some((p) => identity.includes(p))) return true;
  }
  return false;
};
for (const node of all) {
  if (challengeControls.length >= MAX_CHALLENGE_CONTROLS) break;
  if (!elementVisible(node)) continue;
  const tag = (node.tagName || '').toLowerCase();
  const identity = challengeIdentity(node);
  const interactiveDescendant = ['a', 'button', 'input', 'select', 'textarea'].includes(tag) && insideChallengeCarrier(node);
  if (!ANTI_BOT_PATTERNS.some((p) => identity.includes(p)) && !interactiveDescendant) continue;
  const selector = selectorFor(node);
  if (seenChallenge.has(selector)) continue;
  seenChallenge.add(selector);
  const entry = { tag: tag, id: attr(node, 'id'), name: attr(node, 'name'), class: classesFor(node), type: attr(node, 'type'), selector: selector, text: nodeText(node) || attr(node, 'value') || attr(node, 'aria-label'), checked: !!node.checked, disabled: controlDisabled(node) };
  for (const k of ['src', 'title', 'data-sitekey', 'data-callback', 'data-expired-callback', 'data-error-callback']) {
    const v = attr(node, k);
    if (v) entry[k.split('-').join('_')] = v;
  }
  challengeControls.push(entry);
}

const modalOverlays = [];
const seenModal = new Set();
for (const node of all) {
  if (modalOverlays.length >= MAX_MODAL_OVERLAYS) break;
  const tag = (node.tagName || '').toLowerCase();
  if (SKIP_TAGS.has(tag)) continue;
  if (!isModalCandidate(node)) continue;
  if (isHiddenModal(node)) continue;
  const selector = selectorFor(node);
  if (seenModal.has(selector)) continue;
  const role = attr(node, 'role');
  const ariaModal = lower(attr(node, 'aria-modal')).trim() === 'true';
  const dismiss = modalDismissControls(node);
  if (!(role || ariaModal || dismiss.length > 0)) continue;
  seenModal.add(selector);
  modalOverlays.push({ role: role, aria_modal: ariaModal, id: attr(node, 'id'), class: classesFor(node), selector: selector, text: nodeText(node), dismiss_controls: dismiss });
}

const visualObstructionCandidates = [];
const vw = window.innerWidth || document.documentElement.clientWidth || 0;
const vh = window.innerHeight || document.documentElement.clientHeight || 0;
const highZ = (v) => { const n = Number.parseFloat(v); return Number.isFinite(n) && n >= 10; };
const obstructionVisible = (style, rect) => style.display !== 'none' && style.visibility !== 'hidden' && Number.parseFloat(style.opacity || '1') > 0.05 && rect.width > 0 && rect.height > 0;
const coversViewport = (rect) => vw > 0 && vh > 0 && rect.left <= vw * 0.05 && rect.top <= vh * 0.05 && rect.right >= vw * 0.95 && rect.bottom >= vh * 0.95;
const obstructionHasControl = (el) => Array.from(el.querySelectorAll('button,a,input,[role="button"]')).some((c) => {
  const text = ((c.innerText || '') + ' ' + (c.value || '') + ' ' + (c.getAttribute('aria-label') || '')).trim();
  if (!text) return false;
  const t = c.tagName.toLowerCase();
  const ty = (c.getAttribute('type') || '').toLowerCase();
  return t !== 'input' || ['button', 'submit', 'reset'].includes(ty);
});
for (const el of all) {
  if (visualObstructionCandidates.length >= MAX_PAGE_OBSTRUCTIONS) break;
  const tag = (el.tagName || '').toLowerCase();
  if (SKIP_TAGS.has(tag)) continue;
  let style; try { style = window.getComputedStyle(el); } catch (e) { continue; }
  if (!['fixed', 'sticky'].includes(style.position)) continue;
  if (!highZ(style.zIndex)) continue;
  const rect = el.getBoundingClientRect();
  if (!obstructionVisible(style, rect) || !coversViewport(rect)) continue;
  visualObstructionCandidates.push({ source: 'computed_style', position: style.position, coverage: 'viewport', has_visible_controls: obstructionHasControl(el) });
}

const titleParts = [];
for (const t of ['title', 'h1']) {
  const el = document.querySelector(t);
  const txt = el ? nodeText(el) : '';
  if (txt && !titleParts.includes(txt)) titleParts.push(txt);
}
const pageTitle = titleParts.join(' ');
// Same scan window as the get_html path (body innerHTML); head-injected challenges are caught by challengeControls.
const scanHtml = document.body ? document.body.innerHTML : (document.documentElement ? document.documentElement.outerHTML : '');
const haystack = (pageTitle + '\n' + scanHtml.slice(0, ANTI_BOT_SCAN_BYTES)).toLowerCase();
const antiBotIndicators = ANTI_BOT_PATTERNS.filter((p) => haystack.includes(p));
const visibleText = document.body ? (document.body.innerText || '') : '';

return JSON.stringify({
  page_title: pageTitle,
  forms: forms,
  navigation_targets_truncated: navigationTargetsTruncated,
  navigation_targets: navTargets,
  result_containers: resultContainers,
  result_containers_truncated: resultContainersTruncated,
  key_value_relations: keyValueRelations.map((r) => Object.assign({}, r,
    String(r.key_text || '') ? { key_text_walked_count: foldedKeyCounts.get(String(r.key_text).toLowerCase()) || 1 } : {},
    String(r.value_text || '') ? { value_text_walked_count: walkedValueCounts.get(String(r.value_text)) || 1 } : {})),
  key_value_relations_truncated: keyValueRelationsTruncated,
  reveal_relations_truncated: revealRelationsTruncated,
  clickable_controls: clickableControls,
  challenge_controls: challengeControls,
  modal_overlays: modalOverlays,
  visual_obstruction_candidates: visualObstructionCandidates,
  visible_text_excerpt: visibleText.length > MAX_VISIBLE_TEXT_EXCERPT_CHARS * 2 ? visibleText.slice(0, MAX_VISIBLE_TEXT_EXCERPT_CHARS * 2) : visibleText,
  body_has_markup: !!(document.body && (document.body.children.length > 0 || (document.body.textContent || '').trim().length > 0)),
  anti_bot_indicators: antiBotIndicators,
});
"""


def composition_structured_evidence_expression(requested_targets: tuple[str, ...] = ()) -> str:
    """The capture expression, told which labels the turn asked for.

    Capture that does not know the request can only guess which of a page's relations matters, and a
    tile whose shape it fails to recognise is replaced by whatever generic pair sits nearby.
    """
    targets = [target.strip() for target in requested_targets if isinstance(target, str) and target.strip()]
    header = f"const REQUESTED_TARGETS={json.dumps(targets[:_MAX_KEY_VALUE_RELATIONS])};"
    return "(() => {" + header + _STRUCTURED_CONST_HEADER + _STRUCTURED_EVIDENCE_BODY + "})()"


COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION = composition_structured_evidence_expression()
