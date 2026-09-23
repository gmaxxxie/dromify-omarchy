# Dromify DLNA output — architecture

Design notes for the optional UPnP/DLNA output backend. The local (mpv) path is
unchanged; everything here is additive.

## The existing playback chain

```
Panel.qml (bar widget, one per monitor)
  └── Service.qml (one shared instance, "service" kind plugin)
        ├── bin/dromify-api      Subsonic/Navidrome REST client
        └── bin/dromify-player   mpv over its JSON IPC socket
                                  └── mpv-mpris  →  MPRIS (media keys, playerctl)
```

| Question | Answer |
| --- | --- |
| Who builds the Navidrome stream URL? | `bin/dromify-api url/urls <endpoint> id=…` (`build_url`), a fresh salt+token per call. Service.qml calls `dromify-api urls stream.view <ids…>`. |
| Who controls playback? | `bin/dromify-player`, one always-idle mpv instance, driven over `$XDG_RUNTIME_DIR/dromify-mpv.sock`. |
| Where does the queue live? | **In mpv's own playlist.** `load-queue` hands mpv every track up front (per-entry `force-media-title`), and `status` reports mpv's `playlist-pos`/`playlist-count` back. Service.qml mirrors that into `queue`/`queueIndex` — mpv is the single authority for "what's playing", so in-app buttons, `playerctl`, and mpv's own auto-advance all stay in sync through one source of truth. |
| How does the UI call the player? | `Service.qml` fires one-shot `Process` objects (`playerCtlProcess`, `playQueueProcess`, `statusPollProcess`) and polls `dromify-player status` every 800 ms while a queue is loaded. |
| Where do stream URLs travel? | On **stdin**, never argv (`/proc/<pid>/cmdline` is world-readable and the URL carries a replayable salt+token). |

Two properties of that design are worth keeping in the DLNA backend:

1. **One authority for the queue.** The backend owns it and reports an index;
   Service.qml follows, exactly like it follows mpv's `playlist-pos`.
2. **Secrets never in argv.** Stream URLs and passwords go over stdin.

## Where DLNA plugs in

```
Service.qml
  └── bin/dromify-output ──┬── "local" → bin/dromify-player   (unchanged)
                           └── "dlna"  → bin/dromify-dlna     (new)
                                            ├── SSDP discovery (socket + multicast)
                                            ├── device description (HTTP + XML)
                                            ├── AVTransport / RenderingControl SOAP
                                            └── bin/dromify-bridge (optional HTTP range proxy)
```

`bin/dromify-output` is a thin router: it forwards its argv/stdin to whichever
backend the current output selection names, and its stdout is that backend's
stdout. Service.qml therefore keeps calling the same verbs (`load-queue`,
`next`, `status`, …) and only learns about "which backend" through a new
`output` field in the status JSON. A future Chromecast/AirPlay backend is a new
`dromify-output` target and nothing else.

`dromify-dlna` holds the queue in its own state file for the same reason mpv
holds it: next/previous/auto-advance need a queue, and the panel should not be
the thing that decides what plays next.

## Stream URLs: why the renderer must fetch them itself

The renderer does its own HTTP GET against Navidrome, so the URL has to be
reachable *from the renderer*:

- Never `localhost`/`127.0.0.1`. The address comes from the configured server
  URL (`dromify-api`), which for a server on this machine is loopback and
  therefore unusable by a renderer — that case needs a LAN-visible address, and
  is reported as such rather than silently mistranslated.
- **HTTPS is a real problem for the SRS-ZR7.** Measured on this network: the
  ZR7 rejects `https://…` stream URLs with SOAP fault 501 (Action Failed) and
  plays the identical file over `http://…`. Its own `GetProtocolInfo` sink list
  contains no TLS-capable transport, and Sony's documentation for the device
  family says the same.
- `bin/dromify-bridge` exists for exactly that case: a small, opt-in,
  token-gated HTTP range proxy that fetches the authenticated Navidrome URL
  itself and re-serves it over plain HTTP on the LAN interface. It is used
  **only** when a direct fetch cannot work (https + a renderer that refuses
  TLS), never as a default path.

## Renderer state and the panel

The renderer is the authority in DLNA mode too: `dromify-dlna status` returns
`state`, `position`, `duration`, and the *index* it resolved by matching the
renderer's `TrackURI` against its queue. Service.qml mirrors it the same way it
mirrors mpv's `playlist-pos`, including scrobbling on a track change.

Polling is adaptive (playing 1 s, paused 3 s, stopped/idle 8 s) rather than a
fixed 800 ms, because every poll is two SOAP round-trips to the device.

Capabilities are *read from the device* (`GetCurrentTransportActions`), not
assumed: the SRS-ZR7 advertises `Stop,Next,Previous` while playing — no Pause,
no Seek — so the panel disables those controls instead of pretending a command
worked.
