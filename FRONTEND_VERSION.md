# Frontend Version Display

This document explains how the Skyvern frontend version information is captured and displayed.

## Overview

The Skyvern frontend now displays version information in the Settings page, including:
- **Version**: Application version (from package.json or environment)
- **Git SHA**: First 7 characters of the Git commit hash
- **Build Time**: ISO 8601 timestamp of when the image was built

**Note**: The version information card is only displayed if a valid Git SHA is available. If the build doesn't include version information, the card will be hidden.

## How It Works

### 1. Build Time Version Injection

The version information is automatically captured during the Docker image build:

- `VITE_GIT_SHA`: Git commit hash (7 characters) - extracted from `.git` directory
- `VITE_BUILD_TIME`: UTC timestamp in ISO 8601 format - generated at build time
- `VITE_APP_VERSION`: SDK version from `skyvern-ts/client/src/version.ts` - extracted automatically

The Dockerfile (`Dockerfile.ui`) handles this automatically during the build:
1. Copies `.git` directory and `skyvern-ts/client/src/version.ts`
2. Extracts the `SDK_VERSION` using grep/sed
3. Gets the current Git SHA using `git rev-parse --short=7 HEAD`
4. Gets the current UTC timestamp using `date -u`
5. Sets these as environment variables that Vite picks up during `npm run build`
6. Cleans up temporary files

### 2. Frontend Code

**`skyvern-frontend/src/util/version.ts`**: Exports version constants
```typescript
export const FRONTEND_VERSION = {
  gitSha: import.meta.env.VITE_GIT_SHA || 'unknown',
  buildTime: import.meta.env.VITE_BUILD_TIME || 'unknown',
  version: import.meta.env.VITE_APP_VERSION || 'unknown', // From skyvern-ts/client/src/version.ts
  isAvailable: gitSha !== 'unknown', // Only show if Git SHA is available
};
```

**`skyvern-frontend/src/routes/settings/Settings.tsx`**: Displays version info in a card
- Shows all three version fields
- Styled with monospace font for easy reading

## Building with Version Info

The Dockerfile automatically extracts version information during the build process. No special scripts or build arguments are needed!

### Simple Build

```bash
docker compose build skyvern-ui
```

That's it! The Dockerfile will automatically:
1. Extract the SDK version from `skyvern-ts/client/src/version.ts`
2. Capture the current Git SHA (7 characters)
3. Generate the current UTC timestamp
4. Inject these into the build as environment variables

### Alternative: Direct Docker Build

```bash
docker build -f Dockerfile.ui -t skyvern-ui:latest .
```

This also works and automatically captures version info.

## CI/CD Integration

For automated builds (GitHub Actions, etc.), the build works automatically:

```yaml
- name: Build UI with version
  run: docker compose build skyvern-ui
```

The Dockerfile will automatically extract version info from the repository. No environment variables needed!

## Viewing Version Information

1. Build and start Skyvern:
   ```bash
   docker compose build skyvern-ui
   docker compose up -d
   ```
2. Navigate to the UI: http://localhost:8080
3. Go to **Settings** page (left sidebar)
4. Look for the **Version Information** card

The card will display:
- Version: 1.0.19 (from `skyvern-ts/client/src/version.ts`)
- Git SHA: abc1234 (first 7 characters of your commit hash)
- Build Time: 2026-02-20T12:34:56Z

**Note**: If using the pre-built ECR image without rebuilding (`image: public.ecr.aws/skyvern/skyvern-ui:latest`), the Version Information card will not be displayed unless that image was built with version info.

## Files Modified

- `skyvern-frontend/src/util/version.ts` - New file for version constants
- `skyvern-frontend/src/routes/settings/Settings.tsx` - Added version display card (conditionally shown)
- `Dockerfile.ui` - Added automatic version extraction during build
- `docker-compose.yml` - Switched to local build (commented out ECR image)

## Version Source

The application version displayed is automatically sourced from `skyvern-ts/client/src/version.ts`:
```typescript
export const SDK_VERSION = "1.0.19";
```

This ensures the frontend version always matches the SDK version. When you update the SDK version, the frontend will automatically reflect the change on the next build.

## Notes

- The Version Information card is only displayed if a valid Git SHA is available
- Git SHA is automatically truncated to first 7 characters
- Version is automatically extracted from `skyvern-ts/client/src/version.ts`
- Version info is baked into the built JavaScript at build time (not runtime)
- The Dockerfile requires `.git` directory to be present for Git SHA extraction
- If building in an environment without `.git` (like some CI systems), the Git SHA will show as "unknown" and the card will be hidden
- Using the pre-built ECR image without rebuilding will not show version information
