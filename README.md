# HF Remote Bridge

Local-only Pinokio gateway for Hugging Face Gradio Spaces. Designed as the **first fallback** after a confirmed direct ChatGPT→Hugging Face connector/transfer failure. No Cloudflare, no publicly exposed PC port, no local model weights.

## Safety and boundaries

- The gateway binds to `127.0.0.1` and accepts Hugging Face Space identifiers (owner/name), not arbitrary URLs.
- Uploaded files remain on the PC; `gradio_client` sends them directly to the chosen Space.
- Stable `request_id` prevents the same job from being submitted twice. Interrupted jobs are **not** auto-resubmitted.
- Quota exhaustion, paid requests, authentication errors and model inference failures are **not** transfer failures.
- Raw media, tokens, .env files and SQLite job state are excluded from GitHub.
- A caller must first attempt the direct HF integration and confirm the remote job was **not submitted** before selecting the Pinokio fallback.
- Installing a Pinokio launcher alone cannot enforce routing in other ChatGPT conversations; the connected plugin must know to invoke it.

## Installation

This repository is a Pinokio app launcher. From ChatGPT, inspect its exact GitHub commit with the Pinokio-PC connector, record review and use the approved `install_app` action. Start the installed app, then check `GET /health` and `GET /api/connector_spec`. The service is API-only and does not run models locally.

## REST interface

- `POST /api/uploads`: multipart `file`, produces `file_id` and SHA-256.
- `POST /api/jobs`: `{space,api_name,request_id,file_ids,parameters,expect_extensions}`, returns persistent `job_id`.
- `GET /api/jobs/{job_id}`: job status and verified media metadata.
- `GET /api/jobs/{job_id}/events`: stage history.
- `GET /health`, `GET /api/capabilities`, `GET /api/connector_spec`.

Run `python -m pytest -q tests` after installing dependencies. Every generated MP4 must pass `ffprobe` before the gateway reports SUCCEEDED.

**Status**: Source code can be published and locally tested without invoking remote GPU generation. The ChatGPT→Pinokio→HF→ChatGPT end-to-end path is only confirmed after an actual permitted test and native output transfer.
