# skyvern-ui

Prebuilt Skyvern frontend assets for `pip install "skyvern[ui]"`.

This package is produced by the Skyvern release process after building
`skyvern-frontend` with Vite placeholder values. It should contain the generated
`skyvern_ui/dist/` directory. The Python CLI copies those assets to a writable
cache, injects local runtime values, and serves them without requiring Node or
npm on the user's machine.
