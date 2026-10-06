# spotify-mcp

A dependency-free [Model Context Protocol](https://modelcontextprotocol.io) server for Spotify. It speaks stdio JSON-RPC 2.0, uses only the Python standard library, and gives an MCP client (Claude Code, Claude Desktop, anything that speaks MCP) 36 tools for playback, queue, search, library and bulk playlist management. A small CLI is included.

> Built by Robert Lingoes with AI coding agents (Claude Code / Codex); Robert owns the architecture, requirements and review.

## Features

- **Zero dependencies.** Python 3.9+ and nothing to `pip install` to run it.
- **Bulk playlist work.** Add any number of tracks *by name or URI*; names are auto-resolved, de-duplicated against the playlist, and chunked to Spotify's 100-per-call limit (10,000-track playlist ceiling is enforced up front).
- **Safe by default.** Destructive tools (remove, empty, delete, unsave, unfollow) return a dry-run preview unless you pass `confirm=true`.
- **Robust auth.** Access tokens are minted from your refresh token, cached with `0600` permissions, refreshed under a cross-process lock, and retried once on `401`. `429` responses honor `Retry-After`.
- **Flexible inputs.** Anywhere an id is expected you can pass a raw id, a `spotify:` URI, or an `open.spotify.com` URL.

## Tools

| Group | Tools |
|---|---|
| Playback | `now_playing`, `play`, `pause`, `skip`, `previous`, `volume`, `seek`, `queue`, `devices`, `transfer` |
| Search and discovery | `search`, `resolve`, `artist_catalog`, `recommendations`, `audio_features`, `recent_tracks`, `top_tracks` |
| Playlists | `playlist_list`, `playlist_get`, `playlist_create`, `playlist_add`, `playlist_remove`, `playlist_reorder`, `playlist_rename`, `playlist_set_details`, `playlist_empty`, `playlist_delete` |
| Library and follows | `saved_tracks`, `save_tracks`, `unsave_tracks`, `saved_albums`, `save_albums`, `unsave_albums`, `followed_artists`, `follow_artists`, `unfollow_artists` |

Playback tools need Spotify Premium and an active device.

## Setup

### 1. Create a Spotify developer app

1. Go to the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and create an app.
2. Under the app's settings add this **Redirect URI** (exact match):
   ```
   http://127.0.0.1:8888/callback
   ```
3. Copy the app's **Client ID** and **Client Secret**.

### 2. Get a refresh token

```bash
export SPOTIFY_CLIENT_ID="your-client-id"
export SPOTIFY_CLIENT_SECRET="your-client-secret"
python3 auth_setup.py            # use --no-browser to just print the URL
```

`auth_setup.py` runs the standard authorization-code flow (with PKCE) against a temporary server on `127.0.0.1:8888`, then prints a refresh token once. It stores nothing on disk. Use a different port by setting `SPOTIFY_REDIRECT_URI` (and registering it in the dashboard); it must be a loopback address.

Scopes requested:

```
user-read-playback-state        user-modify-playback-state
user-read-currently-playing     user-read-recently-played
user-read-playback-position     user-top-read
playlist-read-private           playlist-read-collaborative
playlist-modify-public          playlist-modify-private
user-library-read               user-library-modify
user-follow-read                user-follow-modify
```

### 3. Configure credentials

The server reads three environment variables:

| Variable | Meaning |
|---|---|
| `SPOTIFY_CLIENT_ID` | your app's client id |
| `SPOTIFY_CLIENT_SECRET` | your app's client secret |
| `SPOTIFY_REFRESH_TOKEN` | the token printed by `auth_setup.py` |

Optional:

| Variable | Default | Meaning |
|---|---|---|
| `SPOTIFY_MCP_CONFIG_DIR` | `~/.config/spotify-mcp` | where the token cache (`tokens.json`) lives |
| `SPOTIFY_MCP_TOKEN_CACHE` | `on` | set to `off` to keep tokens in memory only |

Spotify may rotate the refresh token on refresh; the cache keeps the newest one. If you paste a new `SPOTIFY_REFRESH_TOKEN` into your config, the stale cache is ignored automatically.

Never commit these values. Keep them in your MCP client config or a secrets manager.

## Use with Claude Code

```bash
claude mcp add spotify \
  --env SPOTIFY_CLIENT_ID=your-client-id \
  --env SPOTIFY_CLIENT_SECRET=your-client-secret \
  --env SPOTIFY_REFRESH_TOKEN=your-refresh-token \
  -- python3 -m spotify_mcp
```

Run it from a checkout of this repo (so `python3 -m spotify_mcp` resolves), or `pip install .` first, which installs a `spotify-mcp` command; then the last line is simply `-- spotify-mcp`.

Equivalent `.mcp.json`:

```json
{
  "mcpServers": {
    "spotify": {
      "command": "python3",
      "args": ["-m", "spotify_mcp"],
      "cwd": "/path/to/spotify-mcp",
      "env": {
        "SPOTIFY_CLIENT_ID": "your-client-id",
        "SPOTIFY_CLIENT_SECRET": "your-client-secret",
        "SPOTIFY_REFRESH_TOKEN": "your-refresh-token"
      }
    }
  }
}
```

## Use with Claude Desktop

Add to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and restart the app:

```json
{
  "mcpServers": {
    "spotify": {
      "command": "python3",
      "args": ["-m", "spotify_mcp"],
      "cwd": "/path/to/spotify-mcp",
      "env": {
        "SPOTIFY_CLIENT_ID": "your-client-id",
        "SPOTIFY_CLIENT_SECRET": "your-client-secret",
        "SPOTIFY_REFRESH_TOKEN": "your-refresh-token"
      }
    }
  }
}
```

After `pip install .` you can replace `command`/`args`/`cwd` with `"command": "spotify-mcp"`.

## CLI

The same operations are available from the shell (`python3 -m spotify_mcp.cli ...`, or `spotify-mcp-cli` once installed):

```bash
spotify-mcp-cli now
spotify-mcp-cli search "daft punk" --limit 5
spotify-mcp-cli create "Road Trip" --desc "windows down"
spotify-mcp-cli add <playlist_id> "Blinding Lights" "Mr Brightside by The Killers"
spotify-mcp-cli add <playlist_id> --file songs.txt        # one song per line
cat songs.txt | spotify-mcp-cli add <playlist_id> --stdin
spotify-mcp-cli remove <playlist_id> "some song" --confirm
```

## Development Mode caveats

Spotify limits apps in "Development Mode", and its Web API has been changing. Depending on your app's tier, some endpoints may return `403` (for example certain playlist-write, library-save or follow calls), and `recommendations` / `audio_features` have been restricted for newer apps; those two tools return an explanatory error instead of failing hard. A `403` is passed through unchanged so you can see exactly what Spotify said. This project does not work around Spotify's access policies.

## Tests

No network, no credentials: all HTTP is mocked.

```bash
python3 -m pytest          # or: python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
