# Skyvern blog posts

This directory is the source for the Skyvern blog. Each post is a Markdown file at `blogs/<slug>.md`.

## Add a post

1. Create `blogs/<slug>.md`. Keep the file directly under `blogs/`.
2. Use a lowercase, hyphen-separated slug. The frontmatter `slug` must equal the filename stem.
3. Add images under `blogs/media/<slug>/` and reference them as `./media/<slug>/<filename>`.
4. Keep a draft at `publicationState: "draft"`, `publishedAt: null`, and `sendNewsletter: false` until it is ready.
5. Before you request review, set a new post to `publicationState: "published"`, add its UTC `publishedAt`, and set `sendNewsletter: true`.
6. Install the validator dependencies: `python3 -m pip install --no-deps markdown-it-py==4.2.0 linkify-it-py==2.0.3 mdit-py-plugins==0.6.1 mdurl==0.1.2 uc-micro-py==2.0.0`.
7. Run `python3 scripts/validate_blog_content.py` from the repository root.

Merging a new published post authorizes publication and one newsletter send. Editing an existing post does not send it again. Do not merge a draft if you expect the normal added-file publication flow.

## Frontmatter

Use UTF-8 without a byte-order mark. Put one JSON-encoded value on each top-level YAML line. Do not use multiline YAML, nested objects, aliases, anchors, tags, or implicit dates.

Keep these required keys in this order:

```yaml
---
title: "Example post"
description: "A concise search description."
excerpt: "The summary used on cards, in RSS, and in email previews."
slug: "example-post"
publicationState: "draft"
publishedAt: null
updatedAt: "2026-08-12T12:00:00.000Z"
author: "author-key"
tags: ["browser-automation"]
featureImage: "./media/example-post/cover.png"
featureImageAlt: "A useful description of the cover image"
featureImageCaption: null
sendNewsletter: false
migratedFromGhost: false
---
```

A new post can use `published` or `draft`. The `sent` state is reserved for the historical migration corpus. A new published post needs nonempty `description`, `publishedAt`, and `sendNewsletter: true`. A draft needs `publishedAt: null` and `sendNewsletter: false`.

The validator also accepts reviewed SEO parity overrides: `seoTitle`, `ogTitle`, `ogDescription`, `ogImage`, `twitterTitle`, `twitterDescription`, `twitterImage`, `twitterCard`, `twitterUrl`, `twitterSite`, `twitterLabel1`, `twitterData1`, `twitterLabel2`, and `twitterData2`. Most new posts do not need them.

## Images

A post can add at most 20 image files. Each file must be at most 5 MiB, and the post's media directory must be at most 25 MiB total. Use `.jpg`, `.jpeg`, `.png`, `.webp`, or `.gif`. Do not add SVG, video, audio, archives, fonts, executable files, symlinks, or nested media directories.

Every image must be referenced by its owning post. New posts cannot hotlink third-party images. The validator checks encoded type, dimensions, decoded pixels, and animation limits.

## Content rules

Use CommonMark or GitHub Flavored Markdown. Standard headings, paragraphs, emphasis, lists, blockquotes, fenced code, tables, task lists, links, and images are supported.

Keep raw HTML small and necessary. The validator permits a restricted set of semantic tags and attributes. New HTML cannot use scripts, styles, event handlers, active URL schemes, MDX, JSX components, imports, or exports.

Use `https:` for external links. Internal links to another blog post must use the canonical trailing slash, such as `/blog/example-post/`. Do not add credentials, protocol-relative URLs, local network destinations, local filesystem paths, or `data:`, `javascript:`, `vbscript:`, or `file:` URLs.

CI checks the complete directory on every pull request. It also protects published slugs from accidental rename or deletion.
