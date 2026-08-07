# M4-0 OpenReel Compatibility Verification

## Candidate scope

- Upstream: [Augani/openreel-video](https://github.com/Augani/openreel-video)
- Upstream version: `0.1.1` beta
- Audited commit: `8459024d4c82ee16a2e14537553884a623ae9c4e`
- Project-file schema: `1.0.0`
- License: MIT
- Acceptance status: `IN_DEVELOPMENT`

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

## Explicit boundaries

- The adapter does not fabricate browser `FileSystemFileHandle` objects or
  embed potentially multi-gigabyte media blobs in JSON.
- Cross-origin access to an Aether media URL still depends on the deployment's
  origin and CORS policy; users can always relink a local source file.
- OpenReel stays outside Aether's credentials and persistence boundary.
- Aether's Canonical Timeline and video-use/FFmpeg render stay authoritative.
- A production OpenReel URL is not claimed until an operator explicitly
  configures and verifies one.
