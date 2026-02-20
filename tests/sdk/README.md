# Skyvern SDK Tests

Test suite for Skyvern Python and TypeScript SDKs with shared HTML fixtures in `web/`.

## Python SDK

**Location:** `tests/sdk/python_sdk/`

### Prerequisites
- Requires `.env` with `SKYVERN_API_KEY`
- Browser fixture auto-launches on port 9222
- Web server fixture auto-starts on port 9010

### Running Tests

```bash
# Run all tests
pytest tests/sdk/python_sdk/

# Run specific test file
pytest tests/sdk/python_sdk/test_sdk_simple_actions.py

# Run specific test
pytest tests/sdk/python_sdk/test_sdk_simple_actions.py::test_clicks
```

---

## TypeScript SDK

**Location:** `tests/sdk/typescript_sdk/`

### Prerequisites
- Requires `.env` with `SKYVERN_API_KEY` — copy from the repo root: `cp .env tests/sdk/typescript_sdk/.env`
- Requires the Skyvern server running: `skyvern run server`
- Requires Chrome/Chromium with CDP on `localhost:9222` (see below)
- Web server auto-starts via `run-test.js`

**Launch Chromium with CDP:**
```bash
# Find your Playwright Chromium path
ls ~/Library/Caches/ms-playwright/

# Then launch it (adjust the chromium-XXXX version to match yours)
~/Library/Caches/ms-playwright/chromium-XXXX/chrome-mac/Chromium.app/Contents/MacOS/Chromium \
  --remote-debugging-port=9222 \
  --user-data-dir=~/tmp/chrome-playwright \
  about:blank
```

### Running Tests

```bash
cd tests/sdk/typescript_sdk

# First time setup
npm install

# Run specific test
npm test test_simple_actions.ts testClicks

# Run all tests in a file
npm test test_simple_actions.ts all
```
