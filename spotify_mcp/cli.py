#!/usr/bin/env python3
"""Spotify CLI: a friendly wrapper over ``spotify_mcp.client`` for humans and scripts.

Any command that takes a song list reads items from args, ``--file <path>``
(one per line), or stdin, so bulk work is a one-liner.

Examples
  spotify-mcp-cli now
  spotify-mcp-cli search "daft punk" --limit 5
  spotify-mcp-cli devices
  spotify-mcp-cli play spotify:track:<track_id>
  spotify-mcp-cli playlists
  spotify-mcp-cli create "Road Trip" --desc "windows down"
  spotify-mcp-cli add <playlist_id> "Blinding Lights" "Mr Brightside by The Killers"
  cat songs.txt | spotify-mcp-cli add <playlist_id> --stdin
  spotify-mcp-cli add <playlist_id> --file songs.txt
  spotify-mcp-cli remove <playlist_id> "some song" --confirm
  spotify-mcp-cli rename <playlist_id> "New Name"
  spotify-mcp-cli delete <playlist_id> --confirm

Credentials: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN.
"""

import argparse
import json
import os
import sys

from . import client


def _collect_items(args):
    """Gather song items from positional args, --file, and/or --stdin."""
    items = list(args.items or [])
    if getattr(args, "file", None):
        with open(os.path.expanduser(args.file)) as f:
            items += [ln.strip() for ln in f if ln.strip()]
    if getattr(args, "stdin", False) or (not sys.stdin.isatty() and not items and not getattr(args, "file", None)):
        items += [ln.strip() for ln in sys.stdin if ln.strip()]
    return items


def _out(v):
    print(json.dumps(v, indent=2, default=str) if isinstance(v, (dict, list)) else v)


def main(argv=None):
    p = argparse.ArgumentParser(prog="spotify-mcp-cli", description="Spotify CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_items_args(p_):
        p_.add_argument("items", nargs="*", help="song names or URIs")
        p_.add_argument("--file", help="read items (one per line) from a file")
        p_.add_argument("--stdin", action="store_true", help="read items from stdin")

    # reads / playback
    sub.add_parser("now")
    p_ = sub.add_parser("search"); p_.add_argument("query"); p_.add_argument("--type", default="track"); p_.add_argument("--limit", type=int, default=5); p_.add_argument("--year"); p_.add_argument("--genre")
    sub.add_parser("devices")
    p_ = sub.add_parser("play"); p_.add_argument("uri", nargs="?")
    for c in ("pause", "skip", "prev"):
        sub.add_parser(c)
    p_ = sub.add_parser("volume"); p_.add_argument("pct", type=int)
    p_ = sub.add_parser("queue"); p_.add_argument("uri")
    p_ = sub.add_parser("transfer"); p_.add_argument("device_id")
    sub.add_parser("playlists")
    p_ = sub.add_parser("get"); p_.add_argument("playlist_id"); p_.add_argument("--limit", type=int, default=100)
    p_ = sub.add_parser("artist"); p_.add_argument("artist"); p_.add_argument("--kind", default="top_tracks", choices=["top_tracks", "albums"])
    sub.add_parser("recent")
    sub.add_parser("top")
    p_ = sub.add_parser("resolve"); p_.add_argument("text")

    # writes
    p_ = sub.add_parser("create"); p_.add_argument("name"); p_.add_argument("--public", action="store_true"); p_.add_argument("--desc", default="")
    p_ = sub.add_parser("add"); p_.add_argument("playlist_id"); add_items_args(sp_); p_.add_argument("--no-dedupe", action="store_true")
    p_ = sub.add_parser("remove"); p_.add_argument("playlist_id"); add_items_args(sp_); p_.add_argument("--confirm", action="store_true")
    p_ = sub.add_parser("reorder"); p_.add_argument("playlist_id"); p_.add_argument("range_start", type=int); p_.add_argument("insert_before", type=int)
    p_ = sub.add_parser("rename"); p_.add_argument("playlist_id"); p_.add_argument("name")
    p_ = sub.add_parser("empty"); p_.add_argument("playlist_id"); p_.add_argument("--confirm", action="store_true")
    p_ = sub.add_parser("delete"); p_.add_argument("playlist_id"); p_.add_argument("--confirm", action="store_true")
    p_ = sub.add_parser("like"); add_items_args(sp_)

    a = p.parse_args(argv)
    c = a.cmd
    try:
        if c == "now": _out(client.now_playing())
        elif c == "search": _out(client.search(a.query, a.type, a.limit, a.year, a.genre))
        elif c == "devices": _out(client.get_devices())
        elif c == "play": _out(client.play(a.uri))
        elif c == "pause": _out(client.pause())
        elif c == "skip": _out(client.skip())
        elif c == "prev": _out(client.previous())
        elif c == "volume": _out(client.volume(a.pct))
        elif c == "queue": _out(client.queue(a.uri))
        elif c == "transfer": _out(client.transfer_playback(a.device_id))
        elif c == "playlists": _out(client.playlist_list())
        elif c == "get": _out(client.playlist_get(a.playlist_id, a.limit))
        elif c == "artist": _out(client.artist_catalog(a.artist, a.kind))
        elif c == "recent": _out(client.recent_tracks())
        elif c == "top": _out(client.top_tracks())
        elif c == "resolve": _out(client.resolve_uri(a.text))
        elif c == "create": _out(client.playlist_create(a.name, a.public, a.desc))
        elif c == "add": _out(client.playlist_add(a.playlist_id, _collect_items(a), dedupe=not a.no_dedupe))
        elif c == "remove": _out(client.playlist_remove(a.playlist_id, _collect_items(a), confirm=a.confirm))
        elif c == "reorder": _out(client.playlist_reorder(a.playlist_id, a.range_start, a.insert_before))
        elif c == "rename": _out(client.playlist_rename(a.playlist_id, a.name))
        elif c == "empty": _out(client.playlist_empty(a.playlist_id, confirm=a.confirm))
        elif c == "delete": _out(client.playlist_delete(a.playlist_id, confirm=a.confirm))
        elif c == "like": _out(client.save_tracks(_collect_items(a)))
    except (client.SpotifyError, client.AuthConfigError) as e:
        if isinstance(e, client.SpotifyError) and e.code == 403:
            _out({"error": "403 Forbidden: Spotify rejected this write for your app. Apps in "
                           "Development Mode are restricted on some write endpoints "
                           "(see README, 'Development Mode caveats').",
                  "detail": e.body})
            sys.exit(2)
        _out({"error": str(e)}); sys.exit(2)


if __name__ == "__main__":
    main()
