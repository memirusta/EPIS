# Spotify Web API setup for EPIS

EPIS uses Spotify Authorization Code with PKCE. No Spotify client secret is
required or stored.

1. Create an app in the Spotify Developer Dashboard.
2. Add this exact Redirect URI:
   `http://127.0.0.1:8765/callback`
3. Copy the app Client ID.
4. On the Windows device that runs `cloud_device_agent.py`, set:

   ```powershell
   [Environment]::SetEnvironmentVariable(
     "EPIS_SPOTIFY_CLIENT_ID",
     "<YOUR_CLIENT_ID>",
     "User"
   )
   ```

5. Open a new terminal / restart the EPIS device agent.
6. Ask EPIS to connect Spotify. EPIS opens the Spotify authorization page.
7. After granting access, ask EPIS to check the Spotify connection (or retry
   the original request). Tokens are encrypted with the current Windows user's
   DPAPI and stored outside the repository under `%LOCALAPPDATA%\EPIS`.

Optional redirect override:

```powershell
[Environment]::SetEnvironmentVariable(
  "EPIS_SPOTIFY_REDIRECT_URI",
  "http://127.0.0.1:8765/callback",
  "User"
)
```

The redirect must remain an explicit loopback IP literal; do not use
`localhost`.

## Playback flow

For a named track, Luna should use:

`spotify_search_tracks` -> optional `spotify_devices` -> `spotify_play_track`

Track and device selections use short-lived opaque references. Raw Spotify
device IDs and track URIs stay inside the local device process.
