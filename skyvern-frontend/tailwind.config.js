/** @type {import('tailwindcss').Config} */
import animate from "tailwindcss-animate";

export default {
  darkMode: ["class"],
  content: [
    "./pages/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./app/**/*.{ts,tsx}",
    "./src/**/*.{ts,tsx}",
    "./cloud/**/*.{ts,tsx}",
    "./eval/**/*.{ts,tsx}",
  ],
  prefix: "",
  theme: {
    container: {
      center: true,
      padding: "2rem",
    },
    extend: {
      colors: {
        slate: {
          elevation1: "hsl(var(--slate-elevation-1))",
          elevation2: "hsl(var(--slate-elevation-2))",
          elevation3: "hsl(var(--slate-elevation-3))",
          elevation4: "hsl(var(--slate-elevation-4))",
          elevation5: "hsl(var(--slate-elevation-5))",
        },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        cta: {
          DEFAULT: "hsl(var(--cta))",
          foreground: "hsl(var(--cta-foreground))",
          hover: "hsl(var(--cta-hover))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        tertiary: {
          DEFAULT: "hsl(var(--tertiary))",
          foreground: "hsl(var(--tertiary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        "error-light": "var(--error-bg-light)",
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
        },
        badge: {
          success: "hsl(var(--badge-success))",
          warning: "hsl(var(--badge-warning))",
          destructive: "hsl(var(--badge-destructive))",
          terminated: "hsl(var(--badge-terminated))",
          neutral: "hsl(var(--badge-neutral))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        brand: {
          DEFAULT: "hsl(var(--brand))",
          foreground: "hsl(var(--brand-foreground))",
          soft: "hsl(var(--brand-soft))",
          cta: "hsl(var(--brand-cta))",
          "cta-foreground": "hsl(var(--brand-cta-foreground))",
        },
        studio: {
          accent: "hsl(var(--studio-accent))",
          "accent-foreground": "hsl(var(--studio-accent-foreground))",
          "accent-2": "hsl(var(--studio-accent-2))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      boxShadow: {
        sm: "var(--shadow-sm)",
        card: "var(--shadow-card)",
        "card-hover": "var(--shadow-card-hover)",
        elevated: "var(--shadow-elevated)",
        popover: "var(--shadow-popover)",
      },
      transitionTimingFunction: {
        sidebar: "cubic-bezier(0.32, 0.72, 0, 1)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        "collapsible-down": {
          from: { height: "0" },
          to: { height: "var(--radix-collapsible-content-height)" },
        },
        "collapsible-up": {
          from: { height: "var(--radix-collapsible-content-height)" },
          to: { height: "0" },
        },
        "collapsible-down-fade": {
          from: { height: "0", opacity: "0" },
          to: {
            height: "var(--radix-collapsible-content-height)",
            opacity: "1",
          },
        },
        "collapsible-up-fade": {
          from: {
            height: "var(--radix-collapsible-content-height)",
            opacity: "1",
          },
          to: { height: "0", opacity: "0" },
        },
        glow: {
          "0%, 100%": { boxShadow: "0 0 8px 2px rgba(234, 179, 8, 0.3)" },
          "50%": { boxShadow: "0 0 24px 8px rgba(234, 179, 8, 0.6)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "collapsible-down":
          "collapsible-down 0.22s cubic-bezier(0.22, 1, 0.36, 1)",
        "collapsible-up": "collapsible-up 0.22s cubic-bezier(0.22, 1, 0.36, 1)",
        "collapsible-down-fade":
          "collapsible-down-fade 0.22s cubic-bezier(0.22, 1, 0.36, 1)",
        "collapsible-up-fade":
          "collapsible-up-fade 0.22s cubic-bezier(0.22, 1, 0.36, 1)",
        glow: "glow 2.5s ease-in-out infinite",
      },
    },
  },
  plugins: [animate],
};
