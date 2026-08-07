# M2-0 Real Media and video-use Verification

## Candidate scope

- Branch: `agent/m2-0-real-media-video-use`
- Upstream: [browser-use/video-use](https://github.com/browser-use/video-use)
- Upstream version: `0.1.0`
- Pinned commit: `92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66`
- License: MIT
- Acceptance status: `IN_REVIEW`

This record describes a candidate milestone. It must not be changed to
`ACCEPTED` until its pull request is merged and all three GitHub Actions jobs
have passed for the exact accepted head commit.

## Implemented path

1. The browser uploads a real media file through the same-origin API.
2. The API streams the file to the internal `video-use` Sidecar.
3. The Sidecar assigns a generated media ID, stores the file in its named
   volume, and runs `ffprobe` before accepting it.
4. A project video track is translated into a validated EDL whose sources are
   generated media IDs rather than caller-controlled filesystem paths.
5. The Sidecar invokes the pinned upstream `helpers/render.py` using an
   argument array, not a shell command.
6. Real job state flows back through API SSE. A completed render exposes a
   same-origin MP4 download URL.

The Sidecar also exposes the pinned upstream timeline-view and transcription
helpers. Transcription reports `configured: false` until an
`ELEVENLABS_API_KEY` is present; it does not return a fabricated transcript.

## Security and persistence boundaries

- The Sidecar is not published on a host port and is reachable only on the
  Compose network.
- Its container runs as UID `10001`, not root.
- Project, media, and job IDs have a strict character allowlist.
- Supported media extensions and maximum upload bytes are bounded.
- Render grade values are an allowlisted enum; arbitrary FFmpeg filter strings
  are not accepted through the API.
- The upstream source is checked out at one full commit SHA during image build.
- Media and job records use the dedicated `video-use-media` named volume.
- No provider credentials are stored in the repository.

## Local verification completed

| Gate | Result |
| --- | --- |
| API tests | 15 passed |
| Worker tests | 21 passed, including real FFmpeg proxy/audio/probe |
| Sidecar tests | 2 passed, including upload/probe/job/artifact flow |
| JavaScript tests | 12 passed |
| ESLint | Passed |
| TypeScript and Vite production build | Passed |
| Direct pinned-upstream render | Passed |
| `git diff --check` | Passed |

The direct upstream test generated a one-second H.264/AAC source, uploaded it,
submitted a draft EDL to the exact pinned `video-use` checkout, and downloaded
a 114,676-byte MP4. `ffprobe` reported H.264 video, AAC audio, 1280×960 output,
and 0.832 seconds duration.

## Pending authoritative CI evidence

- Playwright was not locally executable because the browser download endpoint
  returned a certificate-time error. The GitHub Actions Playwright job remains
  required and may not be waived.
- Docker is not installed in the local execution environment. The GitHub
  Actions Docker job must build the non-root Sidecar, verify the pinned commit,
  execute a real upstream render, probe all health endpoints, and upload logs.

## Explicitly not accepted by M2-0

- OpenCut editor embedding.
- OpenReel fallback adapter.
- A configured ElevenLabs account or successful paid transcription request.
- A configured MoneyPrinterTurbo production generation account.
- Public DNS, TLS, production host deployment, or external launch.
- The still-mocked `AIProviderInterface` cartoon style-transfer methods.
