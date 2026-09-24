# Dromify for Omarchy

An [Omarchy](https://omarchy.org) shell plugin that brings a
[Navidrome](https://www.navidrome.org) / Subsonic-API music library into the
bar: search, browse, and play, with transport controls, cover art, and
favourites. Free and open source; no account or subscription beyond your own
server.

It shares a name with [Dromify](https://dromify.app), my Subsonic client for
iPhone, Apple Watch, and Apple TV, but nothing else — it follows your
Omarchy theme, not the app's.

![Dromify panel](docs/screenshot.png)

## Features

- Any Subsonic-API server (Navidrome, Airsonic, Gonic, Ampache in Subsonic
  mode, ...)
- **Multiple servers** — save several by name, switch between them, rename,
  sign out, or remove
- Search artists, albums, and songs
- Browse newest albums, artists → albums → tracks, playlists, and starred
  favourites
- Play/pause, next/previous, seek, shuffle, repeat; the queue is whatever
  list you played from
- The playing track is marked in the list it came from
- Now Playing shows elapsed/remaining time and a pill for what's actually
  streaming — `FLAC`, `MP3 320`, and so on
- Star/unstar from the list or the Now Playing bar
- Cover art, cached locally
- Collapse the browsing view to just Now Playing while something's playing

  ![Collapsed to Now Playing](docs/minimized.png)
- Streams through `mpv`, so it's a normal MPRIS player: media keys,
  `playerctl`, and Omarchy's Media widget all control it
- **Cast to a UPnP/DLNA renderer** — send the stream to a network speaker
  (a Sony SRS-ZR7 and friends) instead of this machine. The renderer fetches
  the audio straight from your server; Dromify is only the control point
- One session shared across every monitor — same queue and now-playing
  wherever you open the bar
- Keyboard-driven: `j`/`k` to move, `Enter` to activate, `/` to search, `f`
  to favourite, `r` to refresh, `Esc` to go back
- Passwords live in the desktop keyring (`secret-tool` / libsecret), not in
  a config file; the password and the authenticated stream URLs are passed
  between the helpers over stdin, never on the command line
- The server name in the header links to its web UI; each settings row has
  a copy-password button for the first sign-in there

### Multiple servers

![Server settings](docs/settings.png)

The gear in the header opens settings. Each server has switch, rename,
sign-out, and remove; a form at the bottom adds another.

## Requirements

All in Arch's official repos, most already on a stock Omarchy install:

- `mpv` and [`mpv-mpris`](https://github.com/hoyon/mpv-mpris) (`omarchy pkg add mpv mpv-mpris`)
- `curl`, `jq`, `socat`
- `wl-clipboard` — ships with Omarchy; used by the copy-password button
- `secret-tool` (`libsecret`) and a running Secret Service — GNOME Keyring
  or KWallet
- `python3` — for the DLNA output and the cover-art helper; both are
  standard library only, no pip packages

The DLNA output needs nothing installed beyond `python3`: discovery, the
SOAP control point, and the optional stream bridge are all in this repo.

## Install

Install and enable this fork of Dromify with one command. Omarchy registers it
by the plugin ID `tallahootie.dromify` from `manifest.json`:

```
omarchy plugin add https://github.com/gmaxxxie/dromify-omarchy.git --enable
```

Pick a bar section when prompted (default: right). Move it later with
`omarchy bar move tallahootie.dromify --section <left|center|right>`.
To enable it again later, run `omarchy plugin enable tallahootie.dromify`.

By hand:

```
git clone https://github.com/gmaxxxie/dromify-omarchy.git \
  ~/.config/omarchy/plugins/tallahootie.dromify
omarchy plugin enable tallahootie.dromify
```

## Use

Click the music-note pill in the bar. It asks for a server name and the
URL/username/password, then remembers you — server details in
`~/.config/omarchy/dromify/config.json`, password in the keyring. Add or
switch servers from the gear icon.

**Bar pill** — left click opens the panel, right click play/pause, middle
click next.

**Panel** — click an artist/album/playlist to open it, a song to play it.
`j`/`k`/`h`/`l` or arrows to move, `Enter`/`Space` to activate, `Esc` to go
back. `/` searches, `f` favourites, `r` refreshes. Hardware media keys and
`playerctl` work too.

## Server URL: use HTTPS

The Subsonic auth scheme sends a salted token (`t`/`s`) with every request.
It's replayable, and the salt is right there for an offline crack of the
password, so it must not travel a network in the clear. Dromify therefore
**requires `https://`** for the server URL. The one exception is a **loopback**
address — `http://127.0.0.1[:port]` / `http://localhost[:port]` /
`http://[::1][:port]` — for Navidrome running on the same machine as the bar,
where there is no network to sniff.

### Getting HTTPS on a home server

Cleanest option, and it doubles as remote access — Tailscale fetches a real
Let's Encrypt cert and terminates TLS in front of Navidrome:

```
# one-time: enable HTTPS certs at https://login.tailscale.com/admin/dns
tailscale serve --bg 4533        # https://<host>.<tailnet>.ts.net -> http://127.0.0.1:4533
```

Then use `https://<your-host>.<your-tailnet>.ts.net` as the server URL — the
same address resolves on the LAN (routed directly) and from anywhere on your
tailnet, no port-forwarding, cert auto-renewed, config persists across
reboots. A reverse proxy with a Let's Encrypt cert (Caddy does this
automatically) or Navidrome's own `ND_TLSCERT` / `ND_TLSKEY` work equally well.

## Casting to a DLNA renderer

The speaker icon in the panel header opens **Output**. *This Computer* is the
default and plays through mpv exactly as before; any UPnP MediaRenderer found
on the LAN is listed below it, with **Refresh devices** to sweep again.
The same popup controls the selected output's volume. Local playback uses
mpv's 0–100 range; DLNA uses the range and step advertised by the renderer,
or native-value steps when the renderer does not publish a maximum. A renderer
without UPnP RenderingControl volume actions cannot be adjusted here; one that
only reports volume shows it as read-only.

Pick a renderer and the next track you play goes to it. Your server serves the
audio directly — the speaker does its own HTTP fetch — so nothing is
transcoded or relayed through this machine unless the renderer forces it.

Things worth knowing:

- **The panel asks the renderer what it can do.** Some devices cannot pause or
  seek while playing (a Sony SRS-ZR7 reports `Stop,Next,Previous` and nothing
  else). Those buttons are disabled rather than pretending the command worked.
- **Formats the renderer cannot play are requested as MP3.** The device's own
  `GetProtocolInfo` decides this. A ZR7 advertises DSD in its sink list and
  still rejects a DSF stream, so the known-unplayable formats are transcoded
  rather than handed over to fail. Everything it does support — FLAC, WAV,
  ALAC/M4A, MP3 — is streamed as-is.
- **HTTPS servers need the stream bridge.** The Subsonic token must not cross
  a network in the clear, so `dromify-api` requires `https://` for anything
  that is not this machine. A renderer that speaks no TLS cannot fetch an
  `https://` URL at all, so `bin/dromify-bridge` fetches it over TLS and
  re-serves it on the LAN over plain HTTP. It stays opt-out-able
  (`dromify-dlna configure --bridge off`) and only ever starts when needed.
- **The renderer keeps playing if Dromify goes away**, because it is fetching
  the stream itself. The *queue* does not: preloading the next track and
  catching the end of one is Dromify's job.

Playback continues with the panel closed, and with the shell restarted; it
stops if the renderer loses power or is switched to another input.

### If your renderer is not found

Some devices answer SSDP unreliably — a Sony SRS-ZR7 on this network responds
to roughly one multicast probe in six — so discovery sends several probes and
keeps previously found devices even when a sweep comes back empty. If a device
never shows up at all, the **scan** button (shown only when a normal sweep
found nothing) sweeps the local `/24` for a renderer whose SSDP responder is
broken:

```
bin/dromify-dlna devices --scan
```

### Media keys

Hardware media keys and `playerctl` control the **local** player, through
`mpv-mpris`. While a renderer is active mpv is stopped on purpose (a stale
MPRIS player advertising itself is worse than none), so media keys control
nothing rather than the speaker. `docs/dlna-output.md` describes what a bridge
for that would take.

### Debugging

```
DROMIFY_DLNA_DEBUG=1 bin/dromify-dlna devices
DROMIFY_DLNA_DEBUG=1 bin/dromify-dlna status
```

prints discovery traffic, the selected renderer, every SOAP action and its
result, and renderer state transitions. Passwords and authenticated stream
URLs are redacted — query values are replaced with `…`.

## Remove

```
omarchy plugin remove tallahootie.dromify
```

To also clear saved servers and cache:

```
rm -rf ~/.config/omarchy/dromify ~/.cache/dromify ~/.local/state/dromify
secret-tool clear service omarchy-dromify
```

## How it's built

Plain Quickshell QML (`Panel.qml`, `Service.qml`) over a few small helpers:

- `bin/dromify-api` — Subsonic REST client: server profiles, browsing,
  search, favourites, cover art, stream URLs.
- `bin/dromify-player` — drives one persistent `mpv` instance over its JSON
  IPC socket. mpv's playlist is the queue, so next/previous work through
  MPRIS and hardware media keys.
- `bin/dromify-dlna` (+ `lib/dlna.py`) — the UPnP/DLNA control point:
  discovery, device descriptions, AVTransport. Standard library only.
- `bin/dromify-bridge` — token-gated HTTP range proxy, started only when a
  renderer cannot fetch an `https://` stream URL itself.
- `bin/dromify-output` — routes the transport verbs to whichever backend is
  active, so the QML side never has to know which one that is.

All of them run standalone:

```
echo demo | bin/dromify-api configure Demo https://demo.navidrome.org demo
bin/dromify-api get getRandomSongs.view size=5
bin/dromify-player status
bin/dromify-output devices
```

See [docs/dlna-output.md](docs/dlna-output.md) for how the DLNA output is
put together and the measurements that shaped it.

## Licence

MIT — see [LICENSE](LICENSE).
