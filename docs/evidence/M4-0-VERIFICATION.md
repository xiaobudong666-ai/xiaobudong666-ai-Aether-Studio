# M4-0 OpenReel Compatibility Verification

## Accepted scope

- Upstream: [Augani/openreel-video](https://github.com/Augani/openreel-video)
- Upstream version: `0.1.1` beta
- Audited commit: `8459024d4c82ee16a2e14537553884a623ae9c4e`
- Project-file schema: `1.0.0`
- License: MIT
- Acceptance status: `ACCEPTED`
- Implementation PR: [#6](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/6)
- Accepted merge commit: `35798c63a7614cb4fa4856109d8dc2fb942450fa`
- Verified head: `ac18a613085842db268c2a6d75f63b9bd95b0efd`
- Authoritative CI: [run 31](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/31207113712)

OpenReel is a client-side editor rather than a backend editing API. Its audited
serializer writes `{ version: "1.0.0", project, metadata }` and imports missing
media blobs as placeholders. M4 targets that public project-file contract.

## Implemented path

1. Aether maps project settings, media metadata, track flags, clip source
   ranges, transforms, volume, and exact timeline duration into the OpenReel
   schema.
2. Media blobs and browser file handles are intentionally `null`; the source
   URL and placeholder fields preserve an explicit relink path.
3. The workbench downloads a `.openreel.json` file accepted by the audited
   serializer's required-field and media-reference validation rules.
4. `VITE_OPENREEL_URL` optionally enables a separate-window editor link. The
   default build exposes no third-party origin or iframe.

## Local verification completed

| Gate | Result |
| --- | --- |
| OpenReel adapter tests | 1 passed |
| All JavaScript tests | 15 passed |
| TypeScript and Vite production build | Passed |
| ESLint | Passed |
| `git diff --check` | Passed |

## Authoritative CI evidence

- Lint, build, JavaScript/WASM tests, API tests, Worker tests, and video-use
  tests passed.
- Playwright passed and uploaded its evidence artifact.
- Docker Compose built the pinned Sidecars, started a healthy same-origin stack,
  verified FFmpeg/ffprobe, executed a real video-use render, and uploaded logs.

## Explicit boundaries

- The adapter does not fabricate browser `FileSystemFileHandle` objects or
  embed potentially multi-gigabyte media blobs in JSON.
- Cross-origin access to an Aether media URL still depends on the deployment's
  origin and CORS policy; users can always relink a local source file.
- OpenReel stays outside Aether's credentials and persistence boundary.
- Aether's Canonical Timeline and video-use/FFmpeg render stay authoritative.
- A production OpenReel URL is not claimed until an operator explicitly
  configures and verifies one.
