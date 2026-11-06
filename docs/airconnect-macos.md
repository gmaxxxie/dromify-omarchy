# AirConnect bridge on macOS — Apple Music → Sony SRS-ZR7

Companion notes for casting the Mac's Apple Music (AirPlay 1) to a DLNA renderer
here: a Sony SRS-ZR7.

> The sample config and LaunchAgents in `tools/airconnect/` use placeholders
> (`__LAN_IP__`, `__HOME__`, documentation MAC/UDN values). `install.sh` fills
> in the address and the home directory; the device identity slots are yours to
> replace with what your renderer reports. Nothing in this document is part of the Dromify plugin;
`bin/dromify-dlna` covers the opposite direction (Dromify → renderer). This one
exists because the Mac has no DLNA output of its own, and the ZR7 has no AirPlay.

```
Music.app (AirPlay 1, ALAC)  ──RAOP/RTP──▶  airupnp (macOS, native)
                                                │ HTTP: audio/L16 PCM
                                                ▼
                                        SRS-ZR7 (UPnP AVTransport DMR)
```

The bridge is philippe44/AirConnect's `airupnp`. It announces itself as an
AirPlay 1 (RAOP) receiver, decodes ALAC and serves the audio to the renderer
over HTTP, so the ZR7 fetches a continuous stream the way it would fetch a
file from a DLNA server.

## Why not Docker

AirConnect needs three things a container on macOS cannot have at the same time:

| Requirement | Container reality |
| --- | --- |
| mDNS (5353/udp) announcements the Mac's Music app can see | NAT'd; multicast never crosses the VM boundary |
| SSDP (1900/udp) to discover the renderer | same |
| A LAN address the renderer can GET the audio stream from | OrbStack's `--net host` is the Linux VM's netns (`192.168.139.2`), not the LAN subnet |

Measured on this machine (OrbStack 2.2.5): a container in host mode binds
the VM's own `192.168.139.2`, discovers no renderer, and `curl` from the LAN times out.
Docker Desktop 4.34+ host networking is the same shape. The only containerised
form that could work is docker inside a *bridged* VM (Lima + socket_vmnet gets a
real DHCP lease), which costs a 2 GiB VM to replace a 10 MB process.

## Install

```sh
curl -LO https://raw.githubusercontent.com/philippe44/AirConnect/master/AirConnect-1.12.4.zip
unzip -o AirConnect-1.12.4.zip airupnp-macos-arm64-static
tools/airconnect/install.sh ./airupnp-macos-arm64-static <mac-lan-ip>
```

`install.sh` builds `~/airconnect/AirConnect.app` around the **static** binary,
ad-hoc signs it, drops `config.xml` in place and installs
`~/Library/LaunchAgents/com.maxwei.airconnect.plist` (`RunAtLoad` + `KeepAlive`).

Two macOS gates make the `.app` wrapper necessary rather than optional:

| Gate | Symptom without it | Fix |
| --- | --- | --- |
| Application Firewall (ALF) | renders fine locally, but every inbound probe from the LAN times out | a signed bundle (`codesign --force --deep -s -`) is allowed by signature |
| Local Network privacy (TCC) | launchd context discovers nothing (no SSDP), while the same binary in an `ssh` session works | one-time toggle: System Settings → Privacy & Security → Local Network → AirConnect |

The dynamic (`*-macos-arm64`, no `-static`) build aborts on macOS 27 with
`WARNING: … is loading libcrypto in an unsafe way` + `Abort trap: 6`.

## Configuration that actually works with a ZR7

`config.xml` in this directory is the deployed one; the values that matter:

| Setting | Value | Why |
| --- | --- | --- |
| `codec` | `pcm` | **The ZR7 cannot decode AirConnect's FLAC stream.** It advertises `audio/flac` in `GetProtocolInfo`, but streamed FLAC comes out silent (and at least one build sent AAC frames under an `audio/flac` header). PCM → `Content-Type: audio/L16;rate=44100;channels=2`, which the ZR7 renders. |
| `http_length` | `-1` (no content-length) | `0` (fake `2^31-1` length) makes the ZR7 probe with repeated `HEAD` → close → re-`GET`, which is audible as dropouts. `-3` (chunked) untested here. |
| `flush` | `0` | Ignore AirPlay FLUSH: the renderer then keeps its stream instead of a `STOP`→`SetAVTransportURI`→`PLAY` restart on every track change (measured: 0.62 s to first frame, no `setURI`, vs 4.5 s and a full restart). Trade-off: pause inserts silence instead of stopping. |
| `latency` | empty | `rtp:http:f`. Only needed if the renderer starves; `http` adds permanent delay. |
| `stream_type` | `broadcast` | Asks the renderer not to buffer a whole track before starting. |
| `upnp_max` | `1` | The ZR7 is `DMR-1.50` with `AVTransport:1`; no need to probe higher. |
| `binding` | `__LAN_IP__` (install.sh substitutes the real address) | Never let it autodetect: with SFM/sing-box or Tailscale running, autodetect picks `utun*` and mDNS leaves through the tunnel. |
| per-`<device>` `enabled` | one target only | Every discovered renderer gets a virtual AirPlay device by default — with two speakers that means two look-alike entries in the Music app. |

The `-b <ip>` flag is repeated on the command line as well; keep both.

### Which speaker to target

Sony groups are invisible to SSDP and Cast discovery — only the Sony app shows
the group name (`SRS-ZR7 <unit> multiroom <n>` here). What matters:

| Mode | How it plays | What to target |
| --- | --- | --- |
| Stereo pair (Cast TXT `fn=… Stereo`) | one unit plays left, the other right, over a dedicated link between the units | the master; the partner never shows up in a LAN scan |
| Multiroom group | every unit mirrors the same full stereo signal | the group's master (whichever unit the Sony app names first) |
| Standalone | full stereo on that unit | that unit |

## Measured limits

- AirPlay 1 is ALAC 16/44.1: Apple Music lossless arrives at CD quality, and
  there is no AirPlay 2 multi-room sync.
- **Track switches are dominated by the sender, not the bridge.** In the log
  the sender spends 2–4.5 s pushing a 180 KB artwork plus metadata before any
  new audio arrives; the bridge cannot shorten that, it can only avoid adding a
  renderer restart on top (`flush=0`).
- **No gapless:** the ZR7's AVTransport has no `setNextAVTransportURI`.
- **Pair mode is the weak point.** With a stereo pair, the master waits for its
  partner: measured `R` (renderer read pointer) frozen for 39 s with ~32 s of
  audio buffered, while the renderer only issued `HEAD` probes. Uncoupled
  single-unit playback does not show it. Sony guarantees pair sync for its own
  sources, not for a third-party DLNA stream.
- **Chromecast is not an alternative on this speaker.** `aircast` never gets the
  Default Media Receiver to launch (`Launching receiver N` then repeated
  `Queuing PLAY`, zero HTTP GETs from the renderer); pychromecast driving the
  same device directly reports `app=None` / `media_state=UNKNOWN` for 18 s. Two
  independent clients, same result: the receiver rejects an external launch.

## Diagnostics

Both scripts parse the bridge log and print a per-event table.

```sh
tools/airconnect/measure.py ~/Library/Logs/airconnect-launchd.log
```

```
   FLUSH时刻  ->首帧音频 ->setURI ->Play ->设备GET   (秒)
    21:06:26         0.62        -      -         -
    20:33:07         5.13     4.53   0.00      0.60
```

`-` under `setURI` is the good case: `flush=0` kept the stream alive across the
track change.

```sh
tools/airconnect/stalls.py ~/Library/Logs/airconnect-launchd.log
```

```
R 冻结（播放器不消费）事件 >=3s:
  21:35:56  持续 39.0s  期间积压 3999 帧 (≈32.0 秒音频)
```

Sanity checks that separate bridge bugs from network/pairing problems:

| Check | Command | Good |
| --- | --- | --- |
| Announcement resolves | `dns-sd -L "<mac>@<name>" _raop._tcp local` | SRV target is `host.local.` — **not** `host.local.local.` (see below) |
| Renderer discovered | `grep "adding renderer" ~/Library/Logs/airconnect-launchd.log` | `adding renderer (SRS-ZR7 …) … subscribe success` |
| Renderer actually playing | `GetTransportInfo` / `GetPositionInfo` over UPnP | `PLAYING` and a `RelTime` that advances |
| LAN can reach the bridge | `nc -z <mac-ip> <http port>` from another host | open (proves ALF is happy) |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Device never appears in the Music app, but `dns-sd -B _raop._tcp` lists it | AirConnect announces `gethostname() + ".local"`; if `HostName` is unset, macOS returns `Host.local`, giving `Host.local.local.` — browsable, unresolvable | `sudo scutil --set HostName Host` (do **not** touch `LocalHostName`) |
| Connected, virtual device shows playing, no sound | codec is FLAC, or the renderer is muted/at volume 0 | set `codec=pcm`; check `RenderingControl.GetVolume` / `GetMute` |
| Audio cuts out periodically | `http_length=0` probe storms, or a paired speaker stalling the master | keep `http_length=-1`; check `stalls.py` |
| One channel only, intermittently | stereo pair: the other unit plays that channel | unpair, or stabilise the pair; see "Which speaker to target" |
| Device vanishes after a few idle minutes | the speaker enters standby and stops answering UPnP subscriptions | disable auto standby / enable network standby in Sony \| Music Center |
| Output selection is stale after a restart | the AirPlay device id is regenerated on every start (no config knob) | reselect the output once after each restart |

One more trap when testing with a plain HTTP source: the renderer has to be able
to reach it. Pushing a WAV from a Linux box whose firewall blocks inbound gives
`errorCode 716 SetAVTransportURI failed`; serve it from the Mac instead.
