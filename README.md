# Master AI Video Director — Pinokio local backend (v0.1.1)

This archive is **SOURCE READY, NOT INSTALLED ON YOUR PC**. Uses Python 3.10+ stdlib only (no model weights, no npm, no cloud subscription). A Pinokio launcher is provided but must be installed into `D:\YT\Pinokio\api\master-ai-video-director-backend`; do not place it under an unrelated app. This package does not change ClipPilot or any existing app.

## Install options

1. **Pinokio:** extract the archive as `D:\YT\Pinokio\api\master-ai-video-director-backend`. Refresh Pinokio; choose **Start Backend**. This package is not installed by ChatGPT and is not yet verified on the target PC.
2. **Windows testing:** extract anywhere outside protected directories, run `START_WINDOWS.bat` with a Python 3.10+ installation. For verification, use `VERIFY_WINDOWS.bat`.

Backend listens on `127.0.0.1:8768`, never `0.0.0.0`. By default data persists outside the app in `%USERPROFILE%\Documents\Master_AI_Video_Director_Data`; set `MAVD_DATA_DIR` to select a different persistent location. The app cannot accept public calls without deliberate reconfiguration. Do not turn on Pinokio Home Server or internet sharing.

## API
- `GET /health`: unauthenticated, no sensitive data.
- Authenticated with bearer token from `<data-root>/config/auth_token.txt` (do not post this secret in ChatGPT): `GET /v1/status`, `/v1/inventory`, `/v1/retention/plan`, `/v1/verify`.
- Authenticated POST JSON: `/v1/projects`, `/v1/generations`, `/v1/generations/finalize`, `/v1/queue`, `/v1/queue/receipt`, `/v1/retention/commit`.
- Authenticated POST binary `/v1/media?project_id=<id>&generation_id=<id>&role=RAW_MP4`; supports `video/mp4`, `image/png`, `image/jpeg`, `audio/wav`; max 500MB. Caller must supply a verified approved file.

### Privacy and current limits
The existing Pinokio connector does **not yet have a secure automated token-to-tool handoff**. Do not expose your token in chat, do not spoof an app API, do not claim full plugin-to-backend integration just because the process listens locally. The backend can run on PC and be tested locally; connector integration is a separate step requiring an approved authenticated bridge.

## Retention
- At most **5 project archives**, each with **latest 10 generated versions**.
- Raw+final MP4+QC image within generation count as **one** version; file media is deduplicated by SHA-256. Legacy model/skill knowledge is separate.
- An additional server-side integrity scan refuses pruning if any file SHA-256 differs, regardless of client flags.

`retention-plan` shows what is eligible without deleting anything. Actual `prune` uses last index and checks file hashes. High-confidence receipts report deleted hashes and size; no silent removal.
- `SAVED_VERIFIED` / `DELETED_VERIFIED` need evidence. Offline queue operations are idempotent and retain `PENDING` / `FAILED` status until serviced.
- Never equate a temporary CDN URL to locally archived media. Keep SHA-256, original approvals and concise tombstone reports after removing old byte-heavy generations.

## Verification
`python -m unittest discover -s tests -v` checks storage, dedupe, pruning, 50-generation cap, operations, audit chain, and offline reports. The plugin additionally checks remote Library backup independently.

**Automatic cleanup trigger:** `POST /v1/generations/finalize` after all required media roles have been uploaded and SHA-256 readback confirmed. This atomically marks the generation complete, then prunes retired versions with verified receipts. Creating a pending generation does NOT evict old media.