# Skyvern Frontend Pain Points

This document identifies areas for improvement discovered during the Phase 1 architecture analysis. Issues are categorized by priority and include specific file locations.

## 1. Hardcoded Color Values (High Priority)

Multiple color values are hardcoded instead of using design tokens. This makes theming and consistency difficult.

### NavLinkGroup.tsx
- **Lines 79-80**: Hardcoded badge colors
  ```typescript
  backgroundColor: groupIsActive ? "#301615" : "#1E1016",
  color: groupIsActive ? "#EA580C" : "#8D3710",
  ```

### FloatingWindow.tsx
- **Line 52**: Hardcoded disabled color `#444`
- **Line 73**: Inline rgba `hover:bg-[rgba(255,255,255,0.2)]`
- **Line 577**: Background `#020817`
- **Lines 677, 685, 694**: macOS traffic light colors (`#FF605C`, `#FFBD44`, `#00CA4E`)

### Splitter.tsx
- **Lines 29, 34, 41, 46, 53, 58**: Repeated `bg-[#666]` and `bg-[#222]`
- **Line 476**: Hardcoded `bg-[#ccc]`

### WorkflowRun.tsx
- **Line 222**: Inline rgba for error state
  ```typescript
  backgroundColor: "rgba(220, 38, 38, 0.10)"
  ```

### FlowRenderer.tsx
- **Line 913**: Hardcoded dark background `bgColor="#020617"`

### WorkflowVisualComparisonDrawer.tsx & WorkflowComparisonPanel.tsx
- **Lines 271-276 / 285-290**: Duplicate hardcoded status colors
  ```typescript
  "#86efac" (green), "#facc15" (yellow), "#c2410c" (orange)
  ```

### MicroDropdown.tsx & WorkflowRunTimelineBlockItem.tsx
- Hardcoded cyan colors `text-[#00d2ff]` and `text-[#00ecff]`

### Workspace.tsx
- **Lines 1468, 1584, 1607**: Multiple `bg-[#020617]`

### DebugIcon.tsx
- **Line 21**: Hardcoded error red `text-[#ff7e7e]`

---

## 2. Inconsistent Styling Patterns (High Priority)

Mix of inline `style={{}}` objects and Tailwind classes for similar functionality.

### BrowserSession.tsx
- **Lines 186-189, 200-203**: Inline visibility/pointerEvents
  ```typescript
  style={{ visibility: activeTab === "stream" ? "visible" : "hidden" }}
  ```
  Should use Tailwind's `hidden`/`block` classes.

### WorkflowCopilotChat.tsx
- **Lines 645, 731**: Multiple inline style blocks

### WorkflowAdderBusy.tsx
- **Lines 100, 111, 124, 145, 175**: Inline styles with transformOrigin and backgroundColor

### Flippable.tsx
- **Lines 58, 68, 76, 86**: Inline perspective and transform animations

### RadialMenu.tsx
- **Lines 116, 172, 193, 206**: Multiple inline style blocks

---

## 3. Duplicate Code Patterns (Medium Priority)

### getComparisonColor() Function
Identical functions in two files:
- `WorkflowVisualComparisonDrawer.tsx` (Lines 266-280)
- `WorkflowComparisonPanel.tsx` (Lines 280-294)

Should be extracted to a shared utility.

### Color Legend Display
Nearly identical legend implementations in:
- `WorkflowVisualComparisonDrawer.tsx` (Lines 313-328)
- `WorkflowComparisonPanel.tsx` (Lines 344-359)

Should create a reusable `ColorLegend` component.

---

## 4. Missing Accessibility (Medium Priority)

### WorkflowTemplateCard.tsx
- **Line 9**: Clickable div should be a button or have `role="button"` and `tabIndex={0}`
  ```tsx
  <div className="cursor-pointer" onClick={onClick}>
  ```
- Missing `aria-label` for the card

### WorkflowVisualComparisonDrawer.tsx
- **Line 301**: Modal overlay missing `role="dialog"` and `aria-modal="true"`
- **Lines 313-328**: Color legend indicators missing `aria-label`

### FloatingWindow.tsx
- **Line 648**: Window container missing proper ARIA attributes

### Splitter.tsx
- **Line 25**: Splitter handle missing `role="separator"` and `aria-orientation`

---

## 5. Tech Debt and Anti-Patterns (Medium Priority)

### FlushSync Usage
Both files note excessive use of `flushSync` that should be removed:
- `FloatingWindow.tsx` (top comment)
- `Workspace.tsx` (top comment)

### TODO Comments
- `FlowRenderer.tsx:507` - TypeScript issue with conditional rendering
- `FlowRenderer.tsx:725` - TODO marked as "hack"
- `Workspace.tsx:945` - TODO for JSON diff comparison
- `FloatingWindow.tsx:341` - TODO about dev console warnings

### Missing Memoization
Multiple files in `routes/tasks/create/` have inline event handlers without `useCallback` memoization, causing unnecessary re-renders.

---

## 6. Component Complexity (Medium Priority)

### Workspace.tsx
- 100+ line component mixing editor logic, browser stream, and timeline
- Should separate into smaller, focused components

### FloatingWindow.tsx
- 700+ lines with extensive state management
- Window control buttons could be extracted

---

## 7. Summary of Recommendations

### Priority 1 - Design System Foundation
1. Create centralized color tokens file
2. Define semantic colors (error, success, warning, info)
3. Create status color mapping for workflow states
4. Add elevation/surface color tokens

### Priority 2 - Styling Consistency
1. Replace all inline `style={{}}` with Tailwind classes
2. Enforce consistent use of `cn()` utility
3. Create shared style constants for recurring patterns
4. Establish component variant patterns

### Priority 3 - Accessibility Improvements
1. Add `role` attributes to interactive divs
2. Add `aria-label` to icon-only buttons
3. Use semantic HTML (`<button>` instead of `<div onClick>`)
4. Add keyboard navigation support

### Priority 4 - Code Quality
1. Extract duplicate `getComparisonColor()` to shared utility
2. Create reusable `ColorLegend` component
3. Split large components into smaller pieces
4. Add `useCallback` to inline handlers

### Priority 5 - Tech Debt
1. Remove excessive `flushSync` usage
2. Resolve outstanding TODOs
3. Add proper TypeScript types where missing

---

## Files Requiring Most Attention

| File | Issues | Priority |
|------|--------|----------|
| `FloatingWindow.tsx` | Colors, flushSync, complexity | High |
| `Workspace.tsx` | Colors, complexity, TODOs | High |
| `Splitter.tsx` | Repeated hardcoded colors | High |
| `NavLinkGroup.tsx` | Hardcoded colors | High |
| `WorkflowRun.tsx` | Inline styles, colors | Medium |
| `WorkflowVisualComparisonDrawer.tsx` | Duplicate code, a11y | Medium |
| `WorkflowComparisonPanel.tsx` | Duplicate code | Medium |
| `BrowserSession.tsx` | Inline visibility styles | Medium |
| `WorkflowTemplateCard.tsx` | Accessibility | Medium |
