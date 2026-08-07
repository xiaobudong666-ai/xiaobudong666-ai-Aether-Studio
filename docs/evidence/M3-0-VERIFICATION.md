# M3-0 OpenCut Compatibility Core Verification

## Candidate scope

- Official rewrite: [opencut-app/opencut](https://github.com/opencut-app/opencut)
- Audited rewrite commit: `400f097becba5db0fbc305d5a65348cb81c20356`
- Compatibility source: [opencut-app/opencut-classic](https://github.com/opencut-app/opencut-classic)
- Audited Classic commit: `cf5e79e919144200294fb9fed22a222592a0aeea`
- Pinned package: `opencut-wasm@0.2.10`
- Package integrity: `sha512-dy+Z9SWwpjLjgmTAMQoMMIUmdbUk9OXoWXLoacl9xT9TCrekIGSeMM0F7bJ1H3VJwwUxbGasaVibvn5AfmeZrg==`
- License: MIT
- Acceptance status: `IN_DEVELOPMENT`

The official rewrite states that its Editor API, plugin system, MCP server, and
headless mode are forthcoming. The official Classic repository states that it
is archived and no longer maintained. M3 therefore uses the published OpenCut
Rust/WASM core without deploying the archived application.

## Implemented path

1. `@aether/editor` converts exact RationalTime values into OpenCut's 120,000
   ticks-per-second MediaTime representation.
2. Frame alignment and `HH:MM:SS:FF` formatting call the actual
   `opencut-wasm` exports rather than reimplementing OpenCut behavior.
3. A Canonical Timeline project is translated into a Classic v31 scene with a
   main video track, overlay video tracks, audio tracks, clip trims, and a media
   manifest.
4. The workbench downloads the result as an explicit
   `aether-opencut-compat/v1` snapshot.
5. Vite loads the 3,037,900-byte WASM asset through a separate dynamic chunk;
   the initial workbench JavaScript remains about 68 KB before gzip.

## Local verification completed

| Gate | Result |
| --- | --- |
| OpenCut adapter tests | 2 passed against the real WASM package |
| All JavaScript tests | 14 passed |
| TypeScript and Vite production build | Passed |
| ESLint | Passed |
| `git diff --check` | Passed |

## Explicit boundaries

- The snapshot does not copy media bytes into OpenCut's private IndexedDB.
- It is not represented as a one-click bidirectional import into an API that
  OpenCut has not released.
- OpenCut Classic is not built, served, or granted access to Aether credentials.
- Aether's Canonical Timeline remains authoritative; final video rendering
  remains in the isolated video-use/FFmpeg path.
- Full editor embedding can be reconsidered only after the official rewrite
  publishes a stable, versioned Editor or plugin API.
