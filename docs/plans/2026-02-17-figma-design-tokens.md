# Skyvern Design System Extraction for Figma

This document extracts all design tokens, typography, spacing, grid system, and component inventory from the Skyvern frontend codebase to support the Figma mockup workflow (SKY-35).

---

## 1. Color Palette

All colors use HSL values with CSS custom properties for theming support.

### Light Mode Colors

| Token | HSL Value | Hex (Approximate) | Usage |
|-------|-----------|-------------------|-------|
| `--background` | `0 0% 100%` | `#FFFFFF` | Page background |
| `--foreground` | `222.2 84% 4.9%` | `#020617` | Primary text |
| `--card` | `0 0% 100%` | `#FFFFFF` | Card background |
| `--card-foreground` | `222.2 84% 4.9%` | `#020617` | Card text |
| `--popover` | `0 0% 100%` | `#FFFFFF` | Popover background |
| `--popover-foreground` | `222.2 84% 4.9%` | `#020617` | Popover text |
| `--primary` | `222.2 47.4% 11.2%` | `#0F172A` | Primary actions |
| `--primary-foreground` | `210 40% 98%` | `#F8FAFC` | Primary action text |
| `--secondary` | `210 40% 96.1%` | `#F1F5F9` | Secondary surfaces |
| `--secondary-foreground` | `222.2 47.4% 11.2%` | `#0F172A` | Secondary text |
| `--tertiary` | `227.6 11.6% 48.8%` | `#6E7891` | Tertiary elements |
| `--tertiary-foreground` | `213 27% 84%` | `#CBD5E1` | Tertiary text |
| `--muted` | `210 40% 96.1%` | `#F1F5F9` | Muted background |
| `--muted-foreground` | `215.4 16.3% 46.9%` | `#64748B` | Muted text |
| `--accent` | `210 40% 96.1%` | `#F1F5F9` | Accent background |
| `--accent-foreground` | `222.2 47.4% 11.2%` | `#0F172A` | Accent text |
| `--destructive` | `0 84.2% 60.2%` | `#EF4444` | Destructive actions |
| `--destructive-foreground` | `210 40% 98%` | `#F8FAFC` | Destructive text |
| `--border` | `214.3 31.8% 91.4%` | `#E2E8F0` | Border color |
| `--input` | `214.3 31.8% 91.4%` | `#E2E8F0` | Input border |
| `--ring` | `222.2 84% 4.9%` | `#020617` | Focus ring |

### Dark Mode Colors (Primary Theme)

| Token | HSL Value | Hex (Approximate) | Usage |
|-------|-----------|-------------------|-------|
| `--background` | `222.2 84% 4.9%` | `#020617` | Page background |
| `--foreground` | `210 40% 98%` | `#F8FAFC` | Primary text |
| `--card` | `222.2 84% 4.9%` | `#020617` | Card background |
| `--card-foreground` | `210 40% 98%` | `#F8FAFC` | Card text |
| `--popover` | `222.2 84% 4.9%` | `#020617` | Popover background |
| `--popover-foreground` | `210 40% 98%` | `#F8FAFC` | Popover text |
| `--primary` | `210 40% 98%` | `#F8FAFC` | Primary actions |
| `--primary-foreground` | `222.2 47.4% 11.2%` | `#0F172A` | Primary action text |
| `--secondary` | `217.2 32.6% 17.5%` | `#1E293B` | Secondary surfaces |
| `--secondary-foreground` | `210 40% 98%` | `#F8FAFC` | Secondary text |
| `--muted` | `217.2 32.6% 17.5%` | `#1E293B` | Muted background |
| `--muted-foreground` | `215 20.2% 65.1%` | `#94A3B8` | Muted text |
| `--accent` | `217.2 32.6% 17.5%` | `#1E293B` | Accent background |
| `--accent-foreground` | `210 40% 98%` | `#F8FAFC` | Accent text |
| `--destructive` | `0 72.2% 50.6%` | `#DC2626` | Destructive (red-600) |
| `--destructive-foreground` | `0 85.7% 97.3%` | `#FEF2F2` | Destructive text (red-50) |
| `--warning` | `40.6 96.1% 40.4%` | `#CA8A04` | Warning (yellow-600) |
| `--warning-foreground` | `54.5 91.7% 95.3%` | `#FEFCE8` | Warning text (yellow-50) |
| `--success` | `142.1 76.2% 36.3%` | `#16A34A` | Success (green-600) |
| `--success-foreground` | `138.5 76.5% 96.7%` | `#F0FDF4` | Success text (green-50) |
| `--border` | `215.3 25% 26.7%` | `#334155` | Border color |
| `--input` | `215.3 25% 26.7%` | `#334155` | Input border |
| `--ring` | `212.7 26.8% 83.9%` | `#CBD5E1` | Focus ring |

### Slate Elevation System (Dark Mode)

Used for layered UI elements with visual depth.

| Token | HSL Value | Hex (Approximate) | Usage |
|-------|-----------|-------------------|-------|
| `--slate-elevation-1` | `228 45% 9%` | `#0D1321` | Base elevation |
| `--slate-elevation-2` | `228 37% 10.6%` | `#111827` | Second level |
| `--slate-elevation-3` | `227 30% 12%` | `#151D2E` | Third level |
| `--slate-elevation-4` | `231 26% 14%` | `#1A2235` | Fourth level |
| `--slate-elevation-5` | `230 22% 16%` | `#1F283D` | Highest elevation |

---

## 2. Typography Scale

The site uses the default Tailwind CSS typography with no custom font-family overrides in the config.

### Font Families

- **Sans-serif (default)**: System font stack via Tailwind
  ```
  ui-sans-serif, system-ui, sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji"
  ```

### Text Sizes (Tailwind Defaults)

| Class | Font Size | Line Height | Usage |
|-------|-----------|-------------|-------|
| `text-xs` | 0.75rem (12px) | 1rem (16px) | Captions, labels |
| `text-sm` | 0.875rem (14px) | 1.25rem (20px) | Body small, buttons |
| `text-base` | 1rem (16px) | 1.5rem (24px) | Body text |
| `text-lg` | 1.125rem (18px) | 1.75rem (28px) | Large body |
| `text-xl` | 1.25rem (20px) | 1.75rem (28px) | H3 |
| `text-2xl` | 1.5rem (24px) | 2rem (32px) | H2 |
| `text-3xl` | 1.875rem (30px) | 2.25rem (36px) | H1 |
| `text-4xl` | 2.25rem (36px) | 2.5rem (40px) | Display |

### Font Weights Used

| Class | Weight | Usage |
|-------|--------|-------|
| `font-normal` | 400 | Body text |
| `font-medium` | 500 | Buttons, labels |
| `font-semibold` | 600 | Badges, emphasis |
| `font-bold` | 700 | Primary buttons, headings |

---

## 3. Spacing Scale

Tailwind default spacing scale (base unit: 4px).

| Token | Value | Pixels |
|-------|-------|--------|
| `0` | 0 | 0px |
| `0.5` | 0.125rem | 2px |
| `1` | 0.25rem | 4px |
| `1.5` | 0.375rem | 6px |
| `2` | 0.5rem | 8px |
| `2.5` | 0.625rem | 10px |
| `3` | 0.75rem | 12px |
| `4` | 1rem | 16px |
| `5` | 1.25rem | 20px |
| `6` | 1.5rem | 24px |
| `8` | 2rem | 32px |
| `10` | 2.5rem | 40px |
| `12` | 3rem | 48px |
| `16` | 4rem | 64px |
| `20` | 5rem | 80px |
| `24` | 6rem | 96px |

---

## 4. Grid System & Layout

### Container

```css
container: {
  center: true,
  padding: "2rem" /* 32px horizontal padding */
}
```

### Border Radius

| Token | Value | Pixels |
|-------|-------|--------|
| `--radius` | 0.5rem | 8px |
| `rounded-sm` | calc(0.5rem - 4px) | 4px |
| `rounded-md` | calc(0.5rem - 2px) | 6px |
| `rounded-lg` | 0.5rem | 8px |

---

## 5. Component Library Inventory

### Base UI Components (shadcn/ui style)

Located in `src/components/ui/`:

| Component | File | States/Variants |
|-----------|------|-----------------|
| **Accordion** | `accordion.tsx` | Default |
| **Alert** | `alert.tsx` | Default, destructive |
| **Aspect Ratio** | `aspect-ratio.tsx` | - |
| **Badge** | `badge.tsx` | default, secondary, success, warning, destructive, outline |
| **Button** | `button.tsx` | default, destructive, disabled, outline, secondary, tertiary, ghost, link |
| **Card** | `card.tsx` | Header, Title, Description, Content, Footer |
| **Carousel** | `carousel.tsx` | - |
| **Checkbox** | `checkbox.tsx` | Default, checked, disabled |
| **Collapsible** | `collapsible.tsx` | - |
| **Command** | `command.tsx` | Input, List, Group, Item, Separator |
| **Dialog** | `dialog.tsx` | Header, Title, Description, Content, Footer |
| **Drawer** | `drawer.tsx` | - |
| **Dropdown Menu** | `dropdown-menu.tsx` | Trigger, Content, Item, Separator, Checkbox, Radio |
| **Form** | `form.tsx` | Field, Item, Label, Control, Description, Message |
| **Grid Form** | `grid-form.tsx` | - |
| **Input** | `input.tsx` | Default, focus, error, disabled |
| **Label** | `label.tsx` | - |
| **Multi-Select** | `multi-select.tsx` | - |
| **Pagination** | `pagination.tsx` | - |
| **Popover** | `popover.tsx` | Trigger, Content |
| **Radio Group** | `radio-group.tsx` | Item, checked |
| **Scroll Area** | `scroll-area.tsx` | - |
| **Search** | `search.tsx` | - |
| **Select** | `select.tsx` | Trigger, Content, Item, Group |
| **Separator** | `separator.tsx` | Horizontal, vertical |
| **Skeleton** | `skeleton.tsx` | - |
| **Switch** | `switch.tsx` | Default, checked |
| **Table** | `table.tsx` | Header, Body, Row, Head, Cell, Footer |
| **Tabs** | `tabs.tsx` | List, Trigger, Content |
| **Textarea** | `textarea.tsx` | - |
| **Toast** | `toast.tsx`, `toaster.tsx` | Default, success, error, warning |
| **Tooltip** | `tooltip.tsx` | Trigger, Content |

### Button Variants (Detail)

```typescript
variants: {
  variant: {
    default: "bg-primary text-primary-foreground shadow hover:bg-primary/90 font-bold",
    destructive: "bg-red-900 text-destructive-foreground shadow-sm hover:bg-destructive/90",
    disabled: "hover:bg-accent hover:text-accent-foreground opacity-50 pointer-events-none",
    outline: "border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground",
    secondary: "bg-secondary text-secondary-foreground shadow-sm hover:bg-secondary/80",
    tertiary: "bg-tertiary/20 text-tertiary-foreground border border-slate-500 hover:bg-tertiary/10 rounded-lg",
    ghost: "hover:bg-accent hover:text-accent-foreground",
    link: "text-primary underline-offset-4 hover:underline",
  },
  size: {
    default: "h-9 px-4 py-2",     // 36px height
    sm: "h-8 rounded-md px-3 text-xs",  // 32px height
    lg: "h-10 rounded-md px-8",   // 40px height
    icon: "h-9 w-9",              // 36x36px
  },
}
```

### Badge Variants (Detail)

```typescript
variants: {
  variant: {
    default: "border-transparent bg-primary text-primary-foreground shadow hover:bg-primary/80",
    secondary: "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
    success: "border-transparent bg-success/40 text-success-foreground shadow hover:bg-success/30",
    warning: "border-transparent bg-warning/40 text-warning-foreground shadow hover:bg-warning/30",
    destructive: "border-transparent bg-destructive/40 text-destructive-foreground shadow hover:bg-destructive/30",
    outline: "text-foreground",
  },
}
```

### Custom Business Components

Located in `src/components/`:

| Component | Purpose |
|-----------|---------|
| AnimatedWave | Visual animation element |
| ApiWebhookActionsMenu | Webhook management actions |
| AutoResizingTextarea | Dynamic textarea height |
| AzureClientSecretCredentialTokenForm | Azure credential form |
| BadgeLoading | Loading state badge |
| BrowserStream | Browser session display |
| CopyApiCommandDropdown | Copy API command utility |
| CopyButton | Clipboard copy action |
| CustomCredentialServiceConfigForm | Credential configuration |
| DataSchemaInputGroup | Data schema input |
| DeleteConfirmationDialog | Deletion confirmation |
| DropdownWithOptions | Generic dropdown |
| EngineSelector | Execution engine selection |
| FileUpload | File upload component |
| Flippable | Flip animation wrapper |
| FloatingWindow | Draggable window |
| GeoTargetSelector | Geographic targeting |
| HelpTooltip | Help information tooltip |
| ImprovePrompt | Prompt improvement UI |
| KeyValueInput | Key-value pair input |
| Logo / LogoMinimized | Brand logos |
| ModelSelector | LLM model selection |
| NavLinkGroup | Navigation links |
| NoticeMe | Attention element |
| OnePasswordTokenForm | 1Password integration |
| Orgwalled | Organization gate |
| PageLayout | Page structure |
| ProxySelector | Proxy configuration |
| PushTotpCodeForm | TOTP code input |
| RadialMenu | Radial navigation |
| RotateThrough | Rotating display |
| SelfHealApiKeyBanner | API key banner |
| Splitter | Panel splitter |
| Status404 | 404 error page |
| StatusBadge | Status display |
| StatusFilterDropdown | Status filtering |
| SwitchBar | Toggle bar |
| SwitchBarNavigation | Navigation toggle |
| TableSearchInput | Table search |
| TestWebhookDialog | Webhook testing |
| ThemeProvider / ThemeSwitch | Theme management |
| Timer | Time display |
| Tip | Tooltip/tip display |
| WebhookReplayDialog | Webhook replay |
| WorkflowBlockInput* | Workflow input variants |
| ZoomableImage | Image zoom |

### Custom Icons

Located in `src/components/icons/`:

| Icon | Domain/Usage |
|------|--------------|
| BagIcon | Commerce |
| BookIcon | Documentation |
| BrainIcon | AI/Intelligence |
| BrowserIcon | Web browser |
| BugIcon | Bug/Issue |
| CartIcon | Shopping |
| ClickIcon | User interaction |
| CompassIcon | Navigation |
| DebugIcon | Debugging |
| DocumentIcon | Documents |
| ExtractIcon | Data extraction |
| FolderIcon | File system |
| GarbageIcon | Delete/Trash |
| GitBranchIcon | Version control |
| GovernmentIcon | Government domain |
| GraphIcon | Charts/Analytics |
| HospitalIcon | Healthcare |
| InboxIcon | Messages |
| KeyIcon | Authentication |
| LogisticsIcon | Shipping/Logistics |
| MessageIcon | Communication |
| OutputIcon | Results/Output |
| PackageIcon | Packages |
| PowerIcon | Power/State |
| QRCodeIcon | QR codes |
| ReceiptIcon | Transactions |
| RobotIcon | Automation |
| SaveIcon | Save action |
| SearchIcon | Search |
| ToolIcon | Settings/Tools |
| TranslateIcon | Translation |
| TrophyIcon | Achievements |
| VersionHistoryIcon | History |

---

## 6. Animation & Transitions

### Keyframes

```css
accordion-down: height 0 → var(--radix-accordion-content-height)
accordion-up: height var(--radix-accordion-content-height) → 0
```

### Standard Transitions

- **Duration**: 0.2s (accordion), 300ms (smart animate recommended for Figma)
- **Easing**: `ease-out` for accordion

---

## 7. Dependencies for Reference

| Package | Version | Purpose |
|---------|---------|---------|
| tailwindcss | 3.4.17 | Utility CSS |
| tailwindcss-animate | 1.0.7 | Animation utilities |
| @radix-ui/* | Various | Accessible primitives |
| class-variance-authority | - | Variant management |
| clsx | - | Class merging |
| tailwind-merge | - | Tailwind class deduplication |
| react-hook-form | 7.51.1 | Form handling |
| zod | 3.22.4 | Schema validation |

---

## Figma Implementation Notes

### Color Styles to Create

Use slash-naming convention:
- `colors/background`
- `colors/foreground`
- `colors/primary/DEFAULT`
- `colors/primary/foreground`
- `colors/secondary/DEFAULT`
- `colors/secondary/foreground`
- `colors/destructive/DEFAULT`
- `colors/destructive/foreground`
- `colors/warning/DEFAULT`
- `colors/warning/foreground`
- `colors/success/DEFAULT`
- `colors/success/foreground`
- `colors/muted/DEFAULT`
- `colors/muted/foreground`
- `colors/elevation/1` through `colors/elevation/5`

### Typography Styles to Create

- `text/display`
- `text/h1`
- `text/h2`
- `text/h3`
- `text/body-large`
- `text/body`
- `text/body-small`
- `text/caption`
- `text/label`

### Component Priority for Figma

1. Button (7 variants x 4 sizes)
2. Badge (6 variants)
3. Input/Textarea (default, focus, error, disabled)
4. Select (default, open)
5. Checkbox/Radio/Switch
6. Card (with subcomponents)
7. Dialog/Modal
8. Table
9. Navigation elements
10. Toast/Alert

---

*Generated for SKY-35: Figma Mockup Workflow*
