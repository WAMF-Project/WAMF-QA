# MediaMTX Test Feed

A tiny WAMF-QA UI for publishing one local test video through an **existing, separate MediaMTX instance** to the fixed RTSP input used by Frigate/WAMF. It does not change WAMF Core, Frigate configuration or MediaMTX configuration.

## Run with Docker Compose

1. Put test videos in a host directory readable and writable by container UID 10001. Supported extensions: `.mp4`, `.m4v`, `.mkv`, `.mov`, `.avi`, `.webm`, `.ts`, `.mts`, `.m2ts`, `.mpg`, `.mpeg` (case insensitive). Files are listed from the directory's top level; there is no recursive browser. The UI also accepts uploads of `.mp4`, `.mov` and `.mkv` files.
2. In this directory, create a local `.env` file:

   ```dotenv
   TEST_VIDEO_DIR=/absolute/path/to/test-videos
   MEDIAMTX_NETWORK=your_existing_mediamtx_docker_network
   MEDIAMTX_URL=rtsp://mediamtx:8554/test
   UI_PORT=8080
   ```

   On Windows, use a Docker Desktop-accessible path such as `C:/test-videos`.
3. Set `MEDIAMTX_NETWORK` to an existing Docker network shared with MediaMTX (`docker network ls` lists networks). The Compose example joins that network; it does not create or start MediaMTX. The default hostname `mediamtx` must resolve there, for example as the existing container's service name or network alias. If MediaMTX runs elsewhere, set its reachable hostname/IP in `MEDIAMTX_URL` and use a suitable existing network.
4. Start the UI:

   ```sh
   docker compose up --build -d
   ```

5. Open http://localhost:8080, choose a video, optionally enable Loop, and press **Start**. Use the configured `UI_PORT` if different.

The mount is writable and must already exist. Its host permissions must allow container UID 10001 to create files. Compose binds the UI to localhost by default. For a remote Docker host, use an SSH tunnel (`ssh -L 8080:localhost:8080 user@host`). This is an unauthenticated QA tool intended for a trusted environment.

## Fixed RTSP path

`MEDIAMTX_URL` is the publisher destination, defaulting to `rtsp://mediamtx:8554/test`. **Set it to the existing test stream's path before starting if your installation uses a different path.** The repository previously contained only its introductory README, so an existing deployment URL could not be inferred.

Frigate keeps consuming its existing URL, for example `rtsp://<mediamtx-host>:8554/test`. The host can differ from the Docker-internal publisher hostname; the MediaMTX instance and path must match. Selecting another file never changes that path. Stop the old script publisher before using this UI so the two publishers do not compete. MediaMTX must already permit publishing to this path; any required credentials belong in the environment URL.

## Upload a video

Use **Choose File** and **Upload** beside the video selector. Uploads are saved to `/videos` (the host `TEST_VIDEO_DIR`) and limited to 2 GiB per request. Filenames are sanitized; paths and unsupported extensions are rejected. Existing files are never overwritten. After success, the dropdown refreshes and selects the uploaded filename; uploading does not start or replace a feed. A small message reports success or errors.

After updating an existing installation, run `docker compose up --build -d test-feed` from this directory to rebuild and recreate the container with the writable mount and upload UI. Recreating the container stops any active feed.

## Start, Stop and status

The video dropdown shows file sizes in MB while selecting by filename. A compact line beneath it shows the selected video's resolution, codec, FPS and duration from FFprobe; unavailable values are shown as unknown. Metadata is fetched on selection, not on every status poll.

**Delete selected video** asks for browser confirmation and removes the file from the mounted host directory. Active videos, symbolic links and unsafe paths cannot be deleted. After deletion the list and metadata refresh, selecting another file if available. Stop the active feed before deleting its file.

The publishing mode shows **Stream copy**, **Transcoding** or **Idle**, using the publisher's actual launch decision. Selecting a different file does not change the active mode.

API additions: `GET /api/videos` includes a `sizes` map of filename to bytes alongside the existing `files` list; `GET /api/videos/<filename>` returns metadata; `DELETE /api/videos/<filename>` deletes an inactive video; `/api/status` includes `mode`. Metadata fields are `width`, `height`, `codec`, `fps` and `duration` (seconds), with `null` for unavailable values.

* **Start** validates the file, stops and waits for any previous FFmpeg publisher, then starts the chosen video from its beginning. Invalid selections leave the current feed running.
* **Stop** terminates and waits for FFmpeg, escalating to a kill if needed. Repeated Stop requests are harmless. The same cleanup runs on container shutdown, and concurrent requests are serialized.
* **Loop** repeats indefinitely using FFmpeg's input loop option. Changing the checkbox takes effect on the next Start. With Loop off, reaching the end returns the status to stopped.
* Status polls every second and reports whether the FFmpeg process is running, its current filename and active loop setting. It is not an end-to-end Frigate reception check. An early FFmpeg failure appears on the next poll; see `docker compose logs test-feed` for diagnostics.
* Video is paced in real time and published over RTSP/TCP, with audio omitted. On each Start, FFprobe checks the first video stream (the stream that is published). H.264 with positive integer dimensions, no known incompatible pixel format and no attached-picture flag uses `-c:v copy`, avoiding video decoding and encoding and reducing CPU use. Selection is based on stream metadata, not the filename extension; compatible H.264 MP4 files can be copied directly.
* Unknown or missing pixel format, profile and level do not prevent stream copy. Known pixel formats other than `yuv420p`, non-H.264 codecs, attached pictures, failed probes, malformed metadata or missing/invalid dimensions use the existing `libx264` path with the `veryfast` preset, `zerolatency` tuning and `yuv420p` output. When transcoding, odd dimensions are padded to even dimensions. Probes time out after 10 seconds and fall back to transcoding. Container logs report **H.264 stream copy** or **libx264 transcoding** for each launch. Selection is automatic, with no codec controls in the UI. Both paths retain real-time pacing, Loop behaviour and the fixed MediaMTX destination. A successful metadata probe does not guarantee that a damaged file will play; FFmpeg failures still appear in status and logs.
* Restarting the container leaves the feed stopped until Start is pressed. There is no automatic retry after an FFmpeg failure. The container uses `unless-stopped` and an init process to reap children. Run one app instance; do not add multiple web workers or replicas.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEST_VIDEO_DIR` | Required by Compose | Existing host directory to mount read/write. |
| `VIDEO_DIR` | `/videos` | Directory inside the app/container. Compose sets this to `/videos`. |
| `MEDIAMTX_URL` | `rtsp://mediamtx:8554/test` | Fixed destination for every selected file. |
| `MEDIAMTX_NETWORK` | `mediamtx` | Existing Docker network used by the Compose example. |
| `UI_PORT` | `8080` | Host UI port; container listens on 8080. |

`GET /health` is an app liveness endpoint used by the Docker healthcheck. It does not require an active feed or verify MediaMTX. Missing mounts and unreadable directories are reported by the video list endpoint/UI.

## Local development and tests

Python 3.12 and FFmpeg with libx264 and FFprobe are required to publish locally (the Docker image includes both executables). Install dependencies in a virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
node --test tests/*.js
VIDEO_DIR=/path/to/videos MEDIAMTX_URL=rtsp://localhost:8554/test python -m app
```

On Windows, activate `.venv\Scripts\Activate.ps1` and set variables with `$env:VIDEO_DIR` and `$env:MEDIAMTX_URL`. Tests use fake publisher processes and probe results; they do not require MediaMTX, FFmpeg or FFprobe. They cover copy/transcode selection and probe fallbacks. The symlink test skips on Windows if symlink creation is unavailable.

For deployment verification, start a short known video and check the unchanged RTSP URL in Frigate or a media player. Confirm natural completion without Loop, continued playback with Loop, replacement via Start, Stop and `docker compose stop` cleanup. A corrupt video or unreachable MediaMTX should produce a stopped/error state and useful FFmpeg container logs.
