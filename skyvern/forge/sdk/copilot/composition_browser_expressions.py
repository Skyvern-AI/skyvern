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
    _MAX_TABLE_HEADERS,
    _MAX_VISIBLE_TEXT_EXCERPT_CHARS,
    _MODAL_DISMISS_HINTS,
    _MODAL_DISMISS_SYMBOLS,
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


# Given a CSS selector, return the element's ARIA role and accessible name so the code-block
# synthesizer has a get_by_role fallback anchor for a positional/unstable captured selector. The
# name is read only from true label sources, never the element's own textContent/value.
def scout_accessible_role_name_expression(css_selector: str) -> str:
    sel = json.dumps(css_selector)
    return (
        "(() => {"
        f"  const el = document.querySelector({sel});"
        "  if (!el) return null;"
        "  const text = (v) => String(v == null ? '' : v).replace(/\\s+/g, ' ').trim();"
        "  const implicitRole = (node) => {"
        "    const tag = (node.tagName || '').toLowerCase();"
        "    const type = (node.getAttribute('type') || '').toLowerCase();"
        "    if (tag === 'a' && node.hasAttribute('href')) return 'link';"
        "    if (tag === 'button') return 'button';"
        "    if (tag === 'select') return 'combobox';"
        "    if (tag === 'textarea') return 'textbox';"
        "    if (tag === 'input') {"
        "      if (['button', 'submit', 'reset'].includes(type)) return 'button';"
        "      if (type === 'checkbox') return 'checkbox';"
        "      if (type === 'radio') return 'radio';"
        "      if (['text', 'search', 'email', 'tel', 'url', 'password', ''].includes(type)) return 'textbox';"
        "    }"
        "    if (/^h[1-6]$/.test(tag)) return 'heading';"
        "    return '';"
        "  };"
        "  const accessibleName = (node) => {"
        "    const aria = text(node.getAttribute('aria-label'));"
        "    if (aria) return aria;"
        "    const labelledby = node.getAttribute('aria-labelledby');"
        "    if (labelledby) {"
        "      const parts = labelledby.split(/\\s+/).map((id) => {"
        "        const ref = document.getElementById(id);"
        "        return ref ? text(ref.textContent) : '';"
        "      }).filter(Boolean);"
        "      if (parts.length) return text(parts.join(' '));"
        "    }"
        "    const id = node.getAttribute('id');"
        "    if (id) {"
        "      let lab = null;"
        "      try { lab = document.querySelector('label[for=\"' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '\"]'); } catch (e) { lab = null; }"
        "      if (lab) { const t = text(lab.textContent); if (t) return t; }"
        "    }"
        "    const parentLabel = node.closest ? node.closest('label') : null;"
        "    if (parentLabel) { const t = text(parentLabel.textContent); if (t) return t; }"
        # textContent/value are never name sources: for a typed-into textbox/contenteditable
        # they would leak the raw typed value as accessible_name.
        "    const title = text(node.getAttribute('title'));"
        "    if (title) return title;"
        "    const placeholder = text(node.getAttribute('placeholder'));"
        "    if (placeholder) return placeholder;"
        "    return '';"
        "  };"
        "  const role = text(el.getAttribute('role')) || implicitRole(el);"
        "  return { role: role, accessible_name: accessibleName(el) };"
        "})()"
    )


# Live count of elements a CSS selector resolves to right now. An invalid selector returns -1 so the
# caller can tell "matched nothing" (0) apart from "could not evaluate" (-1).
def selector_match_count_expression(css_selector: str) -> str:
    sel = json.dumps(css_selector)
    return f"(() => {{  try {{ return document.querySelectorAll({sel}).length; }}  catch (e) {{ return -1; }}}})()"


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

# Safety bound; an over-cap payload is treated as a failed extraction and falls back to get_html.
COMPOSITION_STRUCTURED_EVIDENCE_MAX_CHARS = 120_000

# Injected from composition_evidence so the JS matches the parser's caps/vocabulary (single source of truth).
_STRUCTURED_CONST_HEADER = (
    f"const ANTI_BOT_PATTERNS={json.dumps(list(_ANTI_BOT_PATTERNS))};"
    f"const MODAL_IDENTITY_PATTERNS={json.dumps(sorted(_MODAL_IDENTITY_PATTERNS))};"
    f"const MODAL_ROLE_VALUES={json.dumps(sorted(_MODAL_ROLE_VALUES))};"
    f"const MODAL_DISMISS_HINTS={json.dumps(sorted(_MODAL_DISMISS_HINTS))};"
    f"const MODAL_DISMISS_SYMBOLS={json.dumps(sorted(_MODAL_DISMISS_SYMBOLS))};"
    f"const RESULT_CONTAINER_HINTS={json.dumps(sorted(_RESULT_CONTAINER_HINTS))};"
    f"const MAX_FORMS={int(_MAX_FORMS)};"
    f"const MAX_FIELDS_PER_FORM={int(_MAX_FIELDS_PER_FORM)};"
    f"const MAX_NAVIGATION_TARGETS={int(_MAX_NAVIGATION_TARGETS)};"
    f"const MAX_RESULT_CONTAINERS={int(_MAX_RESULT_CONTAINERS)};"
    f"const MAX_KEY_VALUE_RELATIONS={int(_MAX_KEY_VALUE_RELATIONS)};"
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
    f"const ANTI_BOT_SCAN_BYTES={int(_ANTI_BOT_SCAN_BYTES)};"
)

# Mirrors parse_composition_html's structural extraction; Python re-bounds the values to the exact caps.
_STRUCTURED_EVIDENCE_BODY = r"""
const lower = (v) => String(v == null ? '' : v).toLowerCase();
// Cap fields in-page so a giant element can't bloat the JSON past the size bound; Python re-bounds.
const FIELD_CAP = 2048;
const cap = (s) => (s.length > FIELD_CAP ? s.slice(0, FIELD_CAP) : s);
const attr = (el, k) => { const v = el && el.getAttribute ? el.getAttribute(k) : null; return typeof v === 'string' ? cap(v.trim()) : ''; };
const nodeText = (el) => { if (!el) return ''; return cap(String(el.textContent || '').replace(/\s+/g, ' ').trim()); };
const classesFor = (el) => Array.from((el && el.classList) || []).map((c) => String(c).trim()).filter(Boolean);
const cssAttr = (v) => String(v).split('\\').join('\\\\').split('"').join('\\"');
const simpleIdent = (v) => { if (!v) return false; if (!/[A-Za-z_-]/.test(v[0])) return false; for (let i = 1; i < v.length; i++) { if (!/[A-Za-z0-9_-]/.test(v[i])) return false; } return true; };
const classSelector = (classes) => { const parts = []; for (const c of classes.slice(0, 3)) { parts.push(simpleIdent(c) ? '.' + c : '[class~="' + cssAttr(c) + '"]'); } return parts.join(''); };
const selectorFor = (el) => {
  const tag = (el.tagName || '*').toLowerCase();
  const id = attr(el, 'id'); if (id) return '#' + id;
  const name = attr(el, 'name'); const value = attr(el, 'value');
  if (name && value) return tag + '[name="' + cssAttr(name) + '"][value="' + cssAttr(value) + '"]';
  const classes = classesFor(el); const cs = classSelector(classes);
  if (cs && value) return tag + cs + '[value="' + cssAttr(value) + '"]';
  if (name) return tag + '[name="' + cssAttr(name) + '"]';
  const href = attr(el, 'href');
  if (tag === 'a' && href) return 'a[href="' + cssAttr(href) + '"]';
  if (cs) return tag + cs;
  return tag;
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
const modalDismissControls = (node) => {
  const out = [];
  const seen = new Set();
  for (const c of node.querySelectorAll('button,a,input')) {
    if (out.length >= MAX_MODAL_DISMISS_CONTROLS) break;
    const selector = selectorFor(c);
    if (seen.has(selector)) continue;
    const text = nodeText(c) || attr(c, 'value');
    const ariaLabel = attr(c, 'aria-label');
    const title = attr(c, 'title');
    const explicit = [text.trim().toLowerCase(), ariaLabel.trim().toLowerCase(), title.trim().toLowerCase()];
    const identity = (text + ' ' + ariaLabel + ' ' + title + ' ' + modalIdentity(c)).toLowerCase();
    const hasDataDismiss = c.hasAttribute && c.hasAttribute('data-dismiss');
    const hasSymbol = MODAL_DISMISS_SYMBOLS.some((s) => explicit.includes(s));
    const hasText = MODAL_DISMISS_HINTS.some((h) => identity.includes(h));
    if (!(hasDataDismiss || hasSymbol || hasText)) continue;
    seen.add(selector);
    out.push({ tag: (c.tagName || '').toLowerCase(), text: text, aria_label: ariaLabel, title: title, selector: selector, type: attr(c, 'type') });
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
    const fieldType = lower(attr(node, 'type') || tag || 'text');
    if (tag === 'input' && (fieldType === 'hidden' || fieldType === 'reset')) continue;
    if (tag === 'button' || fieldType === 'submit' || fieldType === 'button') {
      submitControls.push({ text: nodeText(node) || attr(node, 'value'), name: attr(node, 'name'), id: attr(node, 'id'), value: attr(node, 'value'), class: classesFor(node), type: fieldType, disabled: controlDisabled(node), selector: selectorFor(node) });
      continue;
    }
    if (fields.length >= MAX_FIELDS_PER_FORM) continue;
    fields.push({ name: attr(node, 'name'), id: attr(node, 'id'), label: fieldLabel(node), type: fieldType, value: attr(node, 'value'), class: classesFor(node), placeholder: attr(node, 'placeholder'), required: !!(node.hasAttribute('required') || lower(attr(node, 'aria-required')) === 'true'), disabled: controlDisabled(node), checked: node.hasAttribute('checked'), options: tag === 'select' ? selectOptions(node) : [], selector: selectorFor(node) });
  }
  forms.push({ id: attr(form, 'id'), name: attr(form, 'name'), action: attr(form, 'action'), method: attr(form, 'method'), fields: fields, submit_controls: submitControls });
}

const navTargets = [];
const baseHost = location.host.toLowerCase();
for (const link of document.querySelectorAll('a[href]')) {
  if (navTargets.length >= MAX_NAVIGATION_TARGETS) break;
  const rawHref = attr(link, 'href');
  if (!rawHref || rawHref.startsWith('#') || lower(rawHref).startsWith('javascript:')) continue;
  let resolved; try { resolved = new URL(rawHref, location.href).href; } catch (e) { continue; }
  let host; try { host = new URL(resolved).host.toLowerCase(); } catch (e) { continue; }
  if (!host || host !== baseHost) continue;
  const entry = { text: nodeText(link), href: resolved, selector: selectorFor(link) };
  if (link.hasAttribute('download')) entry.has_download_attr = true;
  navTargets.push(entry);
}

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
const usedClickableSelectors = new Set();
for (const f of forms) for (const sc of (f.submit_controls || [])) if (sc.selector) usedClickableSelectors.add(sc.selector);
for (const n of navTargets) if (n.selector) usedClickableSelectors.add(n.selector);
const clickableControls = [];
const seenClickableText = new Set();
for (const el of document.querySelectorAll('button,[role="button"],[data-action]')) {
  if (clickableControls.length >= MAX_CLICKABLE_CONTROLS) break;
  const tag = (el.tagName || '').toLowerCase();
  if (SKIP_TAGS.has(tag)) continue;
  if (el.closest && el.closest('form')) continue;
  const text = clickableText(el);
  const selector = clickableSelector(el);
  let unique = false;
  if (selector) { try { unique = document.querySelectorAll(selector).length === 1; } catch (e) { unique = false; } }
  if (selector && unique && !usedClickableSelectors.has(selector)) {
    clickableControls.push({ text: text, selector: selector, tag: tag });
    usedClickableSelectors.add(selector);
    if (text) seenClickableText.add(text);
    continue;
  }
  if (!text || seenClickableText.has(text)) continue;
  clickableControls.push({ text: text, tag: tag });
  seenClickableText.add(text);
}

const resultContainers = [];
let resultContainersTruncated = false;
const selectorMatchCount = (selector) => { if (!selector) return 0; try { return document.querySelectorAll(selector).length; } catch (e) { return 0; } };
const elementVisible = (node) => {
  if (!node || !node.getBoundingClientRect) return false;
  let style; try { style = window.getComputedStyle(node); } catch (e) { return false; }
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden' && Number.parseFloat(style.opacity || '1') > 0.05 && rect.width > 0 && rect.height > 0;
};
const resultRowTextIsContent = (s) => {
  const text = lower(String(s || '').replace(/\s+/g, ' ').trim());
  return !!text && !['0 results', 'no matching records', 'no records found', 'no results', 'no results found', 'nothing found'].some((p) => text.includes(p));
};
const resultEntry = (node, tag) => {
  const selector = selectorFor(node);
  const entry = { tag: tag, id: attr(node, 'id'), selector: selector, selector_match_count: selectorMatchCount(selector), visible: elementVisible(node), is_table: tag === 'table' };
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
	for (const node of all) {
	  const tag = (node.tagName || '').toLowerCase();
	  if (SKIP_TAGS.has(tag)) continue;
	  const identity = (attr(node, 'id') + ' ' + classesFor(node).join(' ')).toLowerCase();
	  if (tag === 'table' || RESULT_CONTAINER_HINTS.some((h) => identity.includes(h))) {
	    if (resultContainers.length >= MAX_RESULT_CONTAINERS) { resultContainersTruncated = true; break; }
	    resultContainers.push(resultEntry(node, tag));
	  }
	}

const keyValueRelations = [];
let keyValueRelationsTruncated = false;
const keyValueSkipTags = new Set(['body', 'form', 'html', 'table', 'tbody', 'thead', 'tr']);
for (const node of all) {
  const tag = (node.tagName || '').toLowerCase();
  if (keyValueSkipTags.has(tag) || !elementVisible(node)) continue;
  const children = Array.from(node.children || []);
  if (children.length !== 2) continue;
  if (children[0].children && children[0].children.length > 0) continue;
  const keyText = nodeText(children[0]);
  const valueText = nodeText(children[1]);
  if (!keyText || keyText.length > 120 || !valueText || keyText === valueText) continue;
  if (keyValueRelations.length >= MAX_KEY_VALUE_RELATIONS) { keyValueRelationsTruncated = true; break; }
  const selector = selectorFor(node);
  const matches = selectorMatchCount(selector);
  if (!matches) continue;
  let position = -1;
  try { position = Array.from(document.querySelectorAll(selector)).indexOf(node); } catch (e) { position = -1; }
  if (position < 0) continue;
  keyValueRelations.push({ key_text: keyText, value_text: valueText, container_selector: selector, container_match_count: matches, container_position: position, value_child_index: 1, direct_child_count: children.length, visible: true, value_visible: elementVisible(children[1]) });
}

const revealHintTokens = (node) => (attr(node, 'id') + ' ' + classesFor(node).join(' ')).toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
const matchesResultHintToken = (node) => revealHintTokens(node).some((t) => RESULT_CONTAINER_HINTS.includes(t));
const revealHeadingTags = new Set(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']);
let revealRelationCount = 0;
let revealRelationsTruncated = false;
for (const node of all) {
  const tag = (node.tagName || '').toLowerCase();
  if (keyValueSkipTags.has(tag) || !elementVisible(node)) continue;
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
  const revealKeyText = valueLeaves.length === 1 ? keyText : '';
  let capped = false;
  for (const leaf of valueLeaves) {
    if (keyValueRelations.length >= MAX_KEY_VALUE_RELATIONS || revealRelationCount >= MAX_REVEAL_KEY_VALUE_RELATIONS) { revealRelationsTruncated = true; capped = true; break; }
    keyValueRelations.push({ key_text: revealKeyText, value_text: leaf.valueText, container_selector: selector, container_match_count: matches, container_position: position, value_child_index: leaf.index, direct_child_count: children.length, visible: true, value_visible: true });
    revealRelationCount++;
  }
  if (capped) break;
}

const challengeControls = [];
const seenChallenge = new Set();
for (const node of all) {
  if (challengeControls.length >= MAX_CHALLENGE_CONTROLS) break;
  const tag = (node.tagName || '').toLowerCase();
  const identity = [tag, attr(node, 'id'), attr(node, 'name'), '', attr(node, 'src'), attr(node, 'type'), attr(node, 'data-sitekey'), attr(node, 'data-callback'), attr(node, 'data-expired-callback'), attr(node, 'data-error-callback'), attr(node, 'aria-label'), attr(node, 'title')].join(' ').toLowerCase();
  if (!ANTI_BOT_PATTERNS.some((p) => identity.includes(p))) continue;
  const selector = selectorFor(node);
  if (seenChallenge.has(selector)) continue;
  seenChallenge.add(selector);
  const entry = { tag: tag, id: attr(node, 'id'), name: attr(node, 'name'), class: classesFor(node), type: attr(node, 'type'), selector: selector, text: nodeText(node) || attr(node, 'aria-label') };
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
  navigation_targets: navTargets,
  result_containers: resultContainers,
  result_containers_truncated: resultContainersTruncated,
  key_value_relations: keyValueRelations,
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

COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION = "(() => {" + _STRUCTURED_CONST_HEADER + _STRUCTURED_EVIDENCE_BODY + "})()"
