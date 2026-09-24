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

`bin/dromify-output` routes commands to the selected backend. For queue
commands, Service.qml sends one JSON record per track over stdin, including
the source `contentType`; the router reads the declared track count and
converts records to URL/title pairs for mpv. It passes the full records to
DLNA. Service.qml therefore keeps calling the same verbs (`load-queue`,
`next`, `status`, …) and learns about "which backend" through an `output` field
in the status JSON. Adding a backend is a new `dromify-output` target and
nothing else — no renderer-specific code in QML, no new UI framework.

If several song rows are selected while a queue command is running,
Service.qml retains the latest selection and starts it when the shared
processes are free. A stale request cannot discard the newer click.

`dromify-dlna` holds the queue in its own state file for the same reason mpv
holds one: next/previous/auto-advance need a queue, and the panel should not be
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
- `bin/dromify-bridge` exists for exactly that case: a small, token-gated HTTP
  range proxy that fetches the authenticated Navidrome URL itself over TLS and
  re-serves it over plain HTTP on the LAN interface. Every served path is a
  random 32-hex token; the upstream URL never appears in a path, a log line or
  an error, and the daemon binds one LAN address rather than `0.0.0.0`. It is
  used **only** when a direct fetch cannot work (https + a renderer that
  refuses TLS), never as a default path, and it idles out on its own.
- **A loopback tunnel is the other answer, and the one to prefer when the
  server is on the LAN.** `systemd --user` unit `dromify-tunnel.service` holds
  `ssh -L 127.0.0.1:4533:<server>:4533` open, which makes the server a loopback
  address: upstream's rule is satisfied without weakening it, and the token
  never crosses the wire in the clear because SSH is carrying it.
  **But a tunnel and DLNA are mutually exclusive in one direction:** a
  renderer fetches the stream itself, and `127.0.0.1` on a renderer means the
  renderer. So use the loopback tunnel for local (mpv) playback, and the LAN
  address — through `bin/dromify-bridge` if the server is https — for casting.

## Discovery, and why it is not one M-SEARCH

`dromify-dlna devices` runs three probes concurrently and merges what they
find: multicast M-SEARCH, unicast M-SEARCH to hosts already known (from the
cache, from the selected renderer, and from ARP), and a passive listen for
SSDP `NOTIFY` advertisements. This is not belt-and-braces; on the network this
was developed against:

- The ZR7 answers a multicast M-SEARCH roughly **one time in six**. Most
  sweeps see nothing from it while its HTTP side is perfectly responsive.
- Another renderer on the same network only ever announces itself with
  `NOTIFY` and never answers a probe.

So results **accumulate** in a device cache instead of being replaced by each
sweep — a sweep that misses a device must not make it disappear from the
picker — and `dromify-dlna devices --scan` exists for a device whose SSDP
responder is broken entirely: it sweeps the local `/24`s, narrowing to hosts
that answer a TCP connect first so the sweep takes seconds rather than minutes.

Identity is the **AVTransport control endpoint**, not the description URL and
not the UDN. A ZR7 serves two description documents (`MediaRenderer_SRS-ZR7.xml`
from its own SSDP advertisement and `MediaRenderer.xml` at a conventional path),
reports a *different* UDN in each, and points both at the same control URL.
Two records sharing a control URL are one device; two renderers sharing a host
but having their own control URLs stay apart.

## Renderer state and the panel

The renderer is the authority in DLNA mode too: `dromify-dlna status` returns
`state`, `position`, `duration`, and the *index* it resolved by matching the
`TrackURI` the device reports against its queue. Service.qml mirrors it the
same way it mirrors mpv's `playlist-pos`, including scrobbling on a track
change.

Polling is adaptive (playing 1 s, paused 3 s, stopped/idle 8 s) rather than a
fixed 800 ms, because every poll is two SOAP round-trips to the device. mpv
keeps its 800 ms locally.

Capabilities are *read from the device* (`GetCurrentTransportActions`), never
assumed:

- The ZR7 advertises `Stop,Next,Previous` while playing — no Pause, no Seek —
  so the panel disables those controls instead of firing commands that fault.
  (Its advertised set is not even stable between calls, which is a good
  argument for reading it rather than caching it.)
- Its `Previous` action returns success and does nothing, so the backend always
  drives Previous from its own queue rather than delegating.
- At the end of a stream with nothing preloaded it settles into
  `PAUSED_PLAYBACK` sitting at the duration rather than reporting `STOPPED`, so
  end-of-track is detected from position-vs-duration in either state.
- `SetAVTransportURI` intermittently faults (501/716) on an action that
  succeeds immediately after, so `start_track` retries once.
- For a transcoded stream (no `Content-Length`) `TrackDuration` comes back as
  nonsense — `596:31:23` for a 4-minute track — so the queue's own duration
  from the Subsonic API wins when the two disagree.

## Codecs

The MIME declared in the DIDL `<res>` comes from the Subsonic API's own
`contentType`, because the renderer validates what it receives against it — a
`.dsf` guessed as `audio/dsd` gets rejected even though the extension suggests
it.

A format outside the DLNA baseline that renderers advertise but cannot play
(DSD, APE) is requested from Navidrome as `?format=mp3` instead of being handed
over to fail. Measured: a ZR7 lists `audio/dsd` in its sink list and rejects a
DSF stream with fault 501. Everything it does support — FLAC, WAV, ALAC/M4A,
MP3, AAC — is streamed as-is with no intermediate decoding. When the sink list
is unknown, the original is tried first; the fallback is only taken on evidence.

## What does not survive without Dromify

The renderer fetches the stream itself, so playback continues with the panel
closed, the shell restarted, or Dromify uninstalled. The **queue** does not:
preloading the next track and catching the end of one is Dromify's job, and a
device that has stopped at the end of a stream will not start the next one on
its own.

MPRIS is the known gap. `mpv-mpris` is the local player's MPRIS face; in DLNA
mode mpv is stopped (deliberately — a stale player advertising itself would be
worse), so `playerctl` and the hardware media keys control nothing rather than
the renderer. A small MPRIS bridge that proxies to `dromify-dlna` is feasible —
`python-dbus` is already a dependency of the desktop — but it is a separate
service with its own lifecycle, and it is not part of this change.
