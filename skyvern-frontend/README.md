<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Skyvern Frontend](#skyvern-frontend)
  - [Development](#development)
  - [Build for production](#build-for-production)
  - [Preview the production build locally](#preview-the-production-build-locally)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Skyvern Frontend

## Development

```sh
npm run dev
```

This will start the development server with hot module replacement.

## Build for production

```sh
npm run build
```

This will make a production build in the `dist` directory, ready to be served.

## Preview the production build locally

```sh
npm run preview
```

or alternatively, use the `serve` package:

```sh
npx serve@latest dist
```
