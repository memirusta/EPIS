# EPIS server deployment

## Local desktop mode

`npm.cmd run tauri dev` starts the Vite UI and the Tauri process. Tauri then
starts `Layer-2/src/server/local_server.py` when no server is already listening
at the configured local address. The child server is stopped with the desktop
process.

The Python runtime can be selected with `EPIS_PYTHON`. Without that variable,
the desktop app checks the standard per-user Python 3.11-3.14 locations and then
`python.exe` / `py.exe` on `PATH`. The server reads keys from, in order:

1. `EPIS_KEYS_FILE`
2. `%LOCALAPPDATA%/EPIS/keys.env`
3. `D:/EPIS/Layer-3/keys.env`
4. the repository's `Layer-3/keys.env`

The API keys stay in the Python server process and are never returned to the
frontend.

## Heroku mode

The repository root contains `Procfile`, `.python-version`, and `app.json`.
Heroku must receive these config vars:

- `OPENAI_API_KEY`
- `EPIS_SERVER_TOKEN` (generated automatically when creating from `app.json`)
- `OPENAI_ADMIN_KEY` (optional; enables organization Usage and Costs)

`app.json` also selects cloud-safe defaults: one web process, no local PC tools,
no raw hot-conversation persistence, and `/tmp` for transient SQLite metadata.
Heroku dyno files are ephemeral; long-term memory must not use this filesystem.
A durable database adapter is required before cloud memory is enabled.

Start command:

```text
uvicorn server.app:app --app-dir Layer-2/src --host 0.0.0.0 --port $PORT --workers 1
```

Health check: `https://<app-name>.herokuapp.com/health`

## Connecting the desktop app to Heroku

The desktop defaults to `ws://127.0.0.1:8000/ws`. To select the hosted server,
set these variables before launching it:

```powershell
$env:EPIS_SERVER_URL = "wss://<app-name>.herokuapp.com/ws"
$env:EPIS_SERVER_TOKEN = "<the Heroku EPIS_SERVER_TOKEN value>"
npm.cmd run tauri dev
```

For installed builds, the same values can be persisted in the Tauri app config
directory as `server.json` (on Windows this is normally under
`%APPDATA%/com.epis.desktop/`):

```json
{
  "url": "wss://<app-name>.herokuapp.com/ws",
  "token": "<the Heroku EPIS_SERVER_TOKEN value>"
}
```

The token is sent as a WebSocket subprotocol header, not as a URL query value.
Hosted mode rejects unauthenticated connections.
