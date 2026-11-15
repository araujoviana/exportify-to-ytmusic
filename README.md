# exportify-to-ytmusic

Copies Spotify playlists to YouTube Music. Free, no song limit, and it doesn't need a Spotify developer app (those now require Premium).

Made from necessity 🫩

## Install

```sh
uv tool install git+https://github.com/araujoviana/exportify-to-ytmusic
```

## Use

1. Export your playlists as CSV at [exportify.app](https://exportify.app).
2. Log in to YouTube Music:
   - Open music.youtube.com in a private window and sign in.
   - Press F12, go to Network, filter by `browse`, and click Library.
   - Click a POST `browse` request and copy its request headers (Firefox: turn on "Raw").
   - Close the private window without signing out, then run:
     ```sh
     exportify-to-ytmusic login
     ```
     Paste the headers, press Enter, then Ctrl+D.
3. Import:
   ```sh
   exportify-to-ytmusic import my_playlist.csv
   ```

Playlists are created as private. Songs it can't find are listed in `<playlist>.missing.txt`.

If it stops (login expired, Ctrl+C, no internet), run the same command again and it picks up where it left off.

## Notes

- Your login file holds your YouTube cookies. Don't share it.
- This uses [ytmusicapi](https://github.com/sigma67/ytmusicapi), which is unofficial, so YouTube changes can break it.

MIT license.
