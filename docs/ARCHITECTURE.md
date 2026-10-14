# Architecture and orchestration

## Scope

This document describes only the service wiring visible in the sanitized Compose file and the included source. There is no exported n8n workflow in the source project, so it cannot establish whether the actual workflow uses Google Sheets, yt-dlp, Whisper, Ollama/Qwen, or any particular retry/error branch.

## Service diagram

~~~mermaid
flowchart LR
    Host[Local host]
    N8N[n8n service]
    Runner[External task runner]
    Whisper[Whisper Flask service]
    DB[(Project-scoped n8n data volume)]
    Media[(Project-scoped shared media volume)]
    Output[Local output folder]

    Host -->|127.0.0.1:5678| N8N
    Host -->|127.0.0.1:5000| Whisper
    N8N -->|task broker at n8n:5679| Runner
    N8N --- DB
    N8N --- Media
    Runner --- Media
    Whisper --- Media
    Runner ---|/exports| Output
~~~

The task-runner broker URI is configured in the runner environment. The diagram does not imply that n8n calls the Whisper API; that edge requires the missing workflow export.

## Services

| Service | Build or image | Exposed interface | Persistent/shared paths |
|---|---|---|---|
| n8n | n8nio/n8n:2.38.6 | Host loopback port 5678 | n8n state at /home/node/.n8n; shared media at /files |
| runner | Dockerfile.runner, based on n8nio/runners:beta | Internal task broker connection to n8n:5679 | Shared media at /files; host output folder at /exports |
| whisper | WhisperDockerfile | Host loopback port 5000 | Shared media at /files |

The Compose copy removes fixed container names and static custom image tags so multiple project copies do not collide. Its n8n and media volumes are managed by Compose and scoped to this project name; they are separate from the original project's external volumes.

## Data paths and service responsibilities

### n8n and task runner

The n8n service publishes its editor port only on localhost. It retains n8n state in its project-scoped volume and can access the shared media volume at /files.

The runner is configured in external mode. It connects to the n8n task broker using the Docker service name n8n and port 5679. Both services receive the same N8N_RUNNERS_AUTH_TOKEN from the private .env file. The runner configuration defines JavaScript and Python workers, allows yt-dlp as an external Python package, and currently permits all Python standard-library modules.

### Whisper service

The Flask server loads faster-whisper's small model on CPU with int8 computation. POST /transcribe accepts a JSON file_path, probes media duration with ffprobe, extracts audio chunks with FFmpeg, and returns language, duration, segments, and word timestamps.

POST /frame accepts a source path, start/end timestamps, output path, and optional framing mode. OpenCV samples frames and uses YuNet face detection to estimate a crop centre. Auto mode also checks sampled frames with Tesseract OCR; when OCR fails or text appears outside the crop, it falls back to preserve_scene. FFmpeg writes the rendered vertical video to the requested path.

The service shares /files with n8n and the runner. The Compose configuration also binds the local output folder at /exports in the runner. The Flask service does not validate that request paths stay within /files; use trusted inputs and keep the service local.

## Verified flow versus missing workflow evidence

Verified from files:

1. The Compose project starts n8n, an external runner, and a Flask/Whisper service.
2. The runner contacts the n8n broker over the internal Docker network.
3. The n8n, runner, and Whisper services mount the same Compose-managed media volume at /files.
4. The runner has a host output directory mounted at /exports.
5. The Flask service implements transcription and vertical framing endpoints.

Not verified:

- A Google Sheets trigger or input schema.
- Actual yt-dlp invocation or source URL handling.
- The sequence of n8n nodes and any transcript-based clip selection logic.
- A Qwen/Ollama request or model configuration in the running stack.
- Subtitle generation, styling, or burn-in.
- Workflow retries, error routes, and cleanup behavior.

The original folder contained a Qwen-oriented Modelfile, but no matching Ollama service or model client appeared in the included Compose/Python source. It is omitted from this publication copy.

## Failure behavior visible in the service

- /transcribe relies on ffprobe and FFmpeg subprocesses. The temporary audio chunk is removed in a finally block.
- /frame rejects an unsupported mode, missing source, and invalid timestamps with client errors. Failures during analysis or rendering are returned as server errors.
- In auto mode, failed OCR or detected text outside the proposed crop leads to preserve_scene.
- No workflow-level retry or alert behavior can be described without the workflow export.