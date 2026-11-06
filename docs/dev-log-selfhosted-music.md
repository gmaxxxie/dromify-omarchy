# Dev log — a self-hosted music stack behind Dromify

Working notes for the setup Dromify runs against on this LAN: a Navidrome
server on a desktop Mac, plus the AirPlay→DLNA bridge that feeds the same
speaker from the Mac's own Apple Music. Addresses and device identities are
written as placeholders on purpose (`<mac-lan-ip>`, `<renderer-ip>`,
`<client>`, `__HOME__`).

Related: [`dlna-output.md`](dlna-output.md) (Dromify's own DLNA backend) and
[`airconnect-macos.md`](airconnect-macos.md) (the AirPlay in / DLNA out bridge).

## Shape

```
Navidrome (Docker, Mac)          Dromify (two Omarchy boxes)
  :4533  ── ssh -L 127.0.0.1:4533 ──▶ plugin ──▶ mpv (local)
              │
              └─ also plain http on the LAN for phones / anything without the
                 https requirement

Music.app (Mac) ── AirPlay 1 ──▶ airupnp ── HTTP/PCM ──▶ Sony SRS-ZR7
```

The two halves are independent: Dromify does not go through AirConnect, and
AirConnect does not know about Navidrome. They just happen to share a speaker.

## Navidrome on the Mac (Docker)

The previous server lived on a Debian box that was rebuilt; the Mac replaced it.
Layout follows the machine's existing convention of one directory per service:

```
/Volumes/Data/navidrome/
  docker-compose.yml
  data/            # navidrome.db + caches, ~160 MB for ~5k tracks
```

```yaml
services:
  navidrome:
    image: deluan/navidrome:latest
    container_name: navidrome
    restart: unless-stopped
    ports: ["4533:4533"]
    environment:
      ND_LOGLEVEL: info
      ND_SCANINTERVAL: 1h        # NOT ND_SCANSCHEDULE — see below
      ND_SESSIONTIMEOUT: 24h
      ND_ENABLESHARING: "false"
    volumes:
      - /Volumes/<music-disk>/media:/music:ro
      - ./data:/data
```

Notes worth keeping:

- **`ND_SCANSCHEDULE` does not exist in 0.64.** Setting it is silently
  ignored and the log says `Periodic scan is DISABLED`; the real key is
  `ND_SCANINTERVAL` (a Go duration, e.g. `1h`). Verifying with
  `docker exec … env | grep SCAN` proves nothing — the variable arrives, but
  only the binary knows whether it read it.
- **First admin user needs a pty.** `navidrome user create` prompts for a
  password and aborts with `inappropriate ioctl for device` when stdin is a
  pipe:
  `printf '%s\n%s\n' "$PW" "$PW" | script -q /dev/null docker exec -i -t navidrome /app/navidrome user create -u <user> -a --name '<name>'`.
- **Pulling images from an ssh session on macOS fails**:
  `error getting credentials … keychain cannot be accessed because the current
  session does not allow user interaction`, and pointing `DOCKER_CONFIG` at an
  empty `{"auths":{}}` does not help (the helper is still invoked). Transfer
  instead: on a Linux box `crane pull --platform linux/arm64 <image> out.tar`
  then `ssh mac 'docker load -i …'`. `docker pull --platform` + `docker save`
  fails there with `unable to create manifests file … NotFound`.
- First full scan of ~5.4k tracks on an exFAT USB disk: **4m45s**; incremental
  scans after a restart: ~2s.

## The https rule, and why the clients use a tunnel

`bin/dromify-api` refuses plain `http://` for anything that is not loopback,
because the Subsonic salt+token is replayable and would cross the wire in the
clear. A home Navidrome reachable at `http://<mac-lan-ip>:4533` therefore cannot
be configured directly.

Two ways out; the tunnel was chosen because it needs no certificate:

```ini
# ~/.config/systemd/user/dromify-navidrome-tunnel.service
[Service]
ExecStart=/usr/bin/ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:4533:127.0.0.1:4533 <user>@<mac-lan-ip>
Restart=always
```

Then configure the profile against `http://127.0.0.1:4533`. The hop is inside
ssh, so the token never crosses the LAN in the clear.

The alternative is real TLS on the server (`tailscale serve` once the tailnet
has HTTPS enabled, or Caddy with a trusted cert) and then the profile points at
`https://…` with no tunnel at all. That is the better long-term shape if
several devices are involved; it requires the tunnel-free clients to be on the
tailnet.

## Client installs (Omarchy)

```sh
git clone -b <branch> https://github.com/gmaxxxie/dromify-omarchy.git \
  ~/.config/omarchy/plugins/tallahootie.dromify
chmod +x ~/.config/omarchy/plugins/tallahootie.dromify/bin/*
printf '%s\n' "$PW" | ~/.config/omarchy/plugins/tallahootie.dromify/bin/dromify-api \
  configure "Navidrome" http://127.0.0.1:4533 <user>
omarchy plugin enable tallahootie.dromify right
omarchy restart shell
```

- **Remote installs from another machine need `OMARCHY_PATH=/usr/share/omarchy`.**
  Over a non-interactive ssh session `omarchy plugin enable` half-fails
  (`find: '/shell/plugins': No such file or directory`, `OMARCHY_PATH is not
  set`) and the plugin silently never reaches the bar, with a success-looking
  exit code. Confirm with `omarchy plugin list` (`disabled` → `enabled`) **and**
  the id appearing under `/bar/layout/<section>[n]/id` in
  `~/.config/omarchy/shell.json`.
- `omarchy restart shell` is required; QML is not hot-reloaded. A healthy load
  shows either `DEBUG qml: <plugin> Service LOADED` or a plugin-specific debug
  line in `journalctl --user`. `WARN quickshell.qmlscanner: Module path … is
  outside of the config folder` is normal, not an error.
- End-to-end check without touching the UI:

  ```sh
  bin/dromify-api ping                                   # {"ok":true}
  bin/dromify-api url stream.view id=<id> | xargs mpv --no-video --end=5
  ```

  mpv exiting 0 means tunnel, credentials and streaming all work.

## Open items

- Library currently includes a large long-form audio tree next to the music;
  narrowing the mount to the album folders is a one-line compose change plus a
  full rescan.
- If a second Mac-side consumer appears (phones, TVs), move the server to real
  TLS and drop the per-client tunnels.
- The AirPlay bridge still targets a grouped speaker; see
  [`airconnect-macos.md`](airconnect-macos.md) for what grouping does to
  playback stability.
