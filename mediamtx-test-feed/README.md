# MediaMTX Test Feed

A tiny WAMF-QA UI for publishing one local test video through an **existing, separate MediaMTX instance** to the fixed RTSP input used by Frigate/WAMF. It does not change WAMF Core, Frigate configuration or MediaMTX configuration.

## Run with Docker Compose

1. Put test videos in a host directory readable by container UID 10001. Supported extensions: `.mp4`, `.m4v`, `.mkv`, `.mov`, `.avi`, `.webm`, `.ts`, `.mts`, `.m2ts`, `.mpg`, `.mpeg` (case insensitive). Files are listed from the directory's top level; there is no upload or recursive browser.
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

The mount is read-only and must already exist. Compose binds the UI to localhost by default. For a remote Docker host, use an SSH tunnel (`ssh -L 8080:localhost:8080 user@host`). This is an unauthenticated QA tool intended for a trusted environment.

## Fixed RTSP path

`MEDIAMTX_URL` is the publisher destination, defaulting to `rtsp://mediamtx:8554/test`. **Set it to the existing test stream's path before starting if your installation uses a different path.** The repository previously contained only its introductory README, so an existing deployment URL could not be inferred.

Frigate keeps consuming its existing URL, for example `rtsp://<mediamtx-host>:8554/test`. The host can differ from the Docker-internal publisher hostname; the MediaMTX instance and path must match. Selecting another file never changes that path. Stop the old script publisher before using this UI so the two publishers do not compete. MediaMTX must already permit publishing to this path; any required credentials belong in the environment URL.

## Start, Stop and status

* **Start** validates the file, stops and waits for any previous FFmpeg publisher, then starts the chosen video from its beginning. Invalid selections leave the current feed running.
* **Stop** terminates and waits for FFmpeg, escalating to a kill if needed. Repeated Stop requests are harmless. The same cleanup runs on container shutdown, and concurrent requests are serialized.
* **Loop** repeats indefinitely using FFmpeg's input loop option. Changing the checkbox takes effect on the next Start. With Loop off, reaching the end returns the status to stopped.
* Status polls every second and reports whether the FFmpeg process is running, its current filename and active loop setting. It is not an end-to-end Frigate reception check. An early FFmpeg failure appears on the next poll; see `docker compose logs test-feed` for diagnostics.
* Video is paced in real time, converted to H.264/yuv420p and published over RTSP/TCP. Audio is omitted. Odd dimensions are padded to even dimensions. This fixed encoding supports mixed test formats without codec controls, but uses CPU and is not a bit-for-bit source replay.
* Restarting the container leaves the feed stopped until Start is pressed. There is no automatic retry after an FFmpeg failure. The container uses `unless-stopped` and an init process to reap children. Run one app instance; do not add multiple web workers or replicas.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEST_VIDEO_DIR` | Required by Compose | Existing host directory to mount read-only. |
| `VIDEO_DIR` | `/videos` | Directory inside the app/container. Compose sets this to `/videos`. |
| `MEDIAMTX_URL` | `rtsp://mediamtx:8554/test` | Fixed destination for every selected file. |
| `MEDIAMTX_NETWORK` | `mediamtx` | Existing Docker network used by the Compose example. |
| `UI_PORT` | `8080` | Host UI port; container listens on 8080. |

`GET /health` is an app liveness endpoint used by the Docker healthcheck. It does not require an active feed or verify MediaMTX. Missing mounts and unreadable directories are reported by the video list endpoint/UI.

## Local development and tests

Python 3.12 and FFmpeg with libx264 are required to publish locally. Install dependencies in a virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
VIDEO_DIR=/path/to/videos MEDIAMTX_URL=rtsp://localhost:8554/test python -m app
```

On Windows, activate `.venv\Scripts\Activate.ps1` and set variables with `$env:VIDEO_DIR` and `$env:MEDIAMTX_URL`. Tests use fake publisher processes; they do not require MediaMTX or FFmpeg. The symlink test skips on Windows if symlink creation is unavailable.

For deployment verification, start a short known video and check the unchanged RTSP URL in Frigate or a media player. Confirm natural completion without Loop, continued playback with Loop, replacement via Start, Stop and `docker compose stop` cleanup. A corrupt video or unreachable MediaMTX should produce a stopped/error state and useful FFmpeg container logs.
