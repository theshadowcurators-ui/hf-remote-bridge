# Caller-side first fallback (separate from installation)

1. Try the connected Hugging Face direct action first, using the original attachment, exact Space, prompt, seed and parameters.
2. Only when a transfer/connector/schema failure is proven to happen **before** inference submission, call Pinokio-PC as first fallback. If remote submission may have succeeded, inspect/recover that job instead of submitting twice.
3. Start the installed app hf-remote-bridge; GET /health and /api/connector_spec.
4. receive_input_file(app="hf-remote-bridge",file=<actual ChatGPT attachment>). Then invoke_local_app(app="hf-remote-bridge",path="/api/uploads",method="POST",media_upload_path=<verified bridge-received file path>). The bridge responds with file_id and SHA-256.
5. Invoke /api/preflight using the actual live schema (no GPU), then POST /api/jobs with a stable request_id, original parameters and returned file_id; submit_job(adapter="rest") is also available when its job_spec is supported.
6. Poll job_status or GET /api/jobs/{job_id}. Do not re-submit an interrupted job. On SUCCEEDED, use get_output_media on the verified path under app/outputs to return the native file to ChatGPT.
7. ZeroGPU quota, authentication restrictions, model errors, paid requirements, and a bad visual output are not transfer failures; do not retry them automatically.
8. This README does NOT install global routing in other chats. The connected plugin must expose the app and ChatGPT must be instructed to follow this contract. No Cloudflare.
