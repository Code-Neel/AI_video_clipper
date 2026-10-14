# AI Video Clipper

A local prototype for turning a video into short vertical clips. The inspected source includes an n8n service and external task runner, plus a Python/Flask service for Whisper transcription and OpenCV-based framing.

This repository describes only the components verified in the included source. It does not contain an exported n8n workflow, so the complete automation graph and any integrations used by that workflow cannot be reproduced from this repository.

## Problem and intended use

The project is intended to help a creator prepare short-form clips from media they are authorized to process. It is a local experimentation project, not a hosted service or production deployment.

## Implemented in the included code

- Flask health, transcription, and framing endpoints.
- CPU transcription through faster-whisper using the small model in int8 mode, with audio split into chunks of up to five minutes.
- Word-level timestamps in the transcription response.
- Face detection with OpenCV YuNet and smoothed crop positioning.
- Three framing modes: smart_crop, preserve_scene, and auto. Auto mode uses Tesseract OCR as a safeguard against cropping detected text.
- FFmpeg rendering to 1080 × 1920 H.264/AAC video.
- An n8n external task-runner image that installs yt-dlp and other runtime tools. The absent workflow export means actual yt-dlp node usage is not verified.

These code paths passed syntax and configuration checks; an end-to-end run was not performed.

## Technology stack

| Component | Role in the inspected source |
|---|---|
| n8n | Workflow UI and orchestration service. The actual workflow export is not included. |
| n8n external task runner | Separate JavaScript and Python task runners; the Python runner allows yt-dlp. |
| Python and Flask | HTTP service exposing health, transcription, and framing endpoints. |
| faster-whisper | CPU speech transcription with word timestamps. |
| FFmpeg and ffprobe | Audio extraction, duration inspection, and vertical video rendering. |
| OpenCV YuNet | Face detection used to choose a crop centre. The model file is downloaded during image build. |
| Tesseract OCR | Text detection used by auto framing to avoid some unsafe crops. |
| yt-dlp | Installed in the runner image; workflow use cannot be confirmed without the workflow export. |

## Architecture

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
    N8N -->|task broker on Docker network| Runner
    N8N --- DB
    N8N --- Media
    Runner --- Media
    Whisper --- Media
    Runner ---|/exports| Output
~~~

The Compose file also gives the Whisper service access to the shared media volume. The diagram shows configured services and mounts; it does not assert that the missing n8n workflow calls the Whisper endpoints.

## Workflow stages and orchestration

The n8n workflow export was not present during review. Google Sheets input, YouTube downloading stages, transcript-based clip selection, Qwen/Ollama calls, retries, and node-to-node error handling therefore cannot be documented as verified behavior.

The verified service interfaces are:

1. POST /transcribe accepts a JSON file_path and uses ffprobe and FFmpeg to extract audio in chunks. faster-whisper returns the detected language, duration, segments, and word timestamps.
2. POST /frame accepts file_path, start_time, end_time, output_path, and an optional framing_mode. OpenCV samples frames and detects faces. In auto mode, OCR can force preserve_scene when text may be cut off or OCR fails.
3. FFmpeg renders the selected interval as a vertical video. The service returns the requested output path and framing metadata.
4. GET /health returns a basic service status.

The Modelfile.smartdirector file in the original folder was not included because the inspected Compose and Python source do not show an Ollama service or a call to that model. Subtitle generation is also not present in the included service code.

## Docker services and storage

- n8n uses the configured n8n image, exposes its UI only on 127.0.0.1:5678, and stores its state in a Compose-managed volume.
- runner builds from Dockerfile.runner and connects to the n8n task broker over the Compose network. It shares the media volume and mounts the local output folder at /exports.
- whisper builds from WhisperDockerfile, exposes its API only on 127.0.0.1:5000, and shares the media volume at /files.
- The Compose file uses project-scoped volumes for this copy. Starting it creates new volumes under this Compose project; it does not attach to the original project's external volumes.
- Use container paths such as /files/input.mp4 for shared media. The Flask service does not enforce an /files-only path boundary; see Security.

## Whisper and language processing

The Flask service loads faster-whisper's small model on CPU with int8 computation. It probes the source duration, extracts mono 16 kHz audio chunks of up to 300 seconds with FFmpeg, and requests word timestamps from Whisper. The response contains segment and word timing data. The source code does not emit SRT or VTT subtitle files.

## Transcript selection and Qwen/Ollama

No Qwen or Ollama service/client call was found in the included files. The original folder contained a model recipe, but it was not wired into the inspected Compose setup or Python service and is omitted here. AI-based transcript clip selection is therefore not claimed as an implemented feature in this repository.

## FFmpeg rendering and subtitles

The framing endpoint produces vertical H.264/AAC output. smart_crop uses a face-based crop; preserve_scene fits the full image over a blurred background; auto chooses between those approaches and falls back to preserve_scene in several uncertain cases.

Subtitle generation, subtitle styling, and subtitle burn-in are not included in the inspected code.

## Prerequisites

- Docker Desktop with Docker Compose support.
- Network access for container images and model downloads during first build/run.
- Enough CPU and disk space for the Whisper model and media being processed.
- Media that you have permission to use.

No GPU is configured. Model weights and example videos are not stored in this repository.

## Setup and startup

1. Clone the repository.
2. Copy .env.example to .env.
3. Replace the placeholder value in .env with a long, randomly generated runner token. Do not commit .env or share the token.
4. Run docker compose config -q to check the configuration.
5. Run docker compose up --build to start the local services.

The first build downloads the YuNet model. The first transcription may also need to download Whisper model data. Starting the stack creates new project-scoped Docker volumes for this copy. The n8n service will start without the original automation because no workflow export is included.

## Environment variables

| Variable | Purpose | Value |
|---|---|---|
| N8N_RUNNERS_AUTH_TOKEN | Shared n8n and task-runner authentication token | Generate locally; never commit the real value. |

Other runner settings are defined in docker-compose.yml and n8n-task-runners.json. The task runner currently allows all Python standard-library modules and allows the yt-dlp package; review and narrow this configuration before running workflows from untrusted sources.

## Safe configuration

- Keep the stack on localhost. The published host ports are bound to 127.0.0.1.
- The Flask endpoints have no authentication and accept file paths from requests. Do not forward port 5000 to a LAN, reverse proxy, or public network without adding authentication and strict path validation.
- Import only trusted n8n workflows. Narrow the Python standard-library allowlist for the specific workflow before using this runner beyond local experimentation.
- Keep .env, media, model files, databases, logs, and generated videos out of Git.
- Do not reuse or delete volumes belonging to another project. This copy uses new Compose-managed volumes.
- The setup instructions do not include a destructive Docker cleanup command.

## Troubleshooting and limitations

- A missing environment variable causes Compose configuration to fail. Confirm .env exists and replace its placeholder.
- A missing video path usually means the file is not available inside the container at the path supplied in the request. Shared media should be under /files.
- Image builds require network access to fetch packages and the YuNet model.
- CPU transcription may be slow for long videos.
- OCR can miss stylized, small, or low-contrast text. Auto mode is a safeguard, not a guarantee.
- There is no included n8n workflow, requirements lockfile, automated test suite, or end-to-end verification.
- Docker image/package references are not all pinned to immutable digests, and the runner uses a beta image tag.

## Demo and screenshots

No screenshots, sample media, or demo link were included. To prepare a demonstration, use a short video you own or are licensed to process, and review frames for private information and third-party rights before sharing.

## Testing and verification

The prepared copy was checked with Python syntax parsing, JSON parsing for the task-runner file, and Docker Compose configuration validation. These checks did not start containers or exercise the HTTP endpoints. No end-to-end workflow test was possible because the n8n workflow export is absent.

## Security and responsible use

This is a local prototype. The API does not authenticate requests or restrict file paths to a safe directory. Keep the service bound to localhost and add authentication and path validation before any network exposure. The task-runner allowlist is broad, and workflows should be treated as executable code.

Process only media you own or are authorized to use, and follow the relevant platform terms. Do not assume that downloading or republishing a video is permitted just because it is publicly accessible.

## Third-party licensing and attribution

No project LICENSE is included because the project owner has not selected one. The absence of a LICENSE does not grant permission to reuse this project's code.

- n8n Community Edition uses the Sustainable Use License; review the [official license](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) and [n8n license guidance](https://github.com/n8n-io/n8n-docs/blob/main/docs/n8n-community-license/README.md).
- faster-whisper is distributed under the [MIT License](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE).
- The YuNet model download directory provides an [MIT license](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE). The model itself is downloaded during the Whisper image build and is not committed here.
- yt-dlp's source project is under the [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE); its releases may include third-party components with separate notices.
- A complete transitive software bill of materials and license audit was not possible because this project has no dependency lockfile. Review the licenses of the actual resolved container packages before redistribution.

## Future improvements

- Export and sanitize the actual n8n workflow so orchestration and integrations can be verified.
- Add request authentication and strict input/output path validation to the Flask service.
- Narrow the task-runner import allowlist based on the workflow.
- Pin container images, Python packages, and model downloads to reviewed versions or digests.
- Add endpoint and end-to-end tests, plus a dependency/license inventory.
- Add subtitle rendering and model-assisted clip selection only after those features are implemented and testable.

## Contribution guidance

This repository is a work in progress. Keep secrets, private workflow exports, personal data, videos, model weights, databases, and generated output out of contributions. Include the evidence for any new feature claims, and do not add a project license without the owner's decision.