#!/usr/bin/env python3
"""dlna.py - UPnP/DLNA control-point primitives for Dromify.

Small, stdlib-only helpers shared by bin/dromify-dlna (the real backend) and
tools/dlna-probe (a standalone CLI used to diagnose a renderer). Nothing here
knows about Dromify's config, queue, or UI — it is SSDP discovery, device
description parsing, and AVTransport/RenderingControl/ConnectionManager SOAP.

Design notes worth keeping:

* No third-party dependencies. The whole point is that this runs from a
  Quickshell plugin on an Omarchy box with nothing installed beyond what
  Omarchy already ships (python3 is required by dromify-api's fsafe helper).
* Every network call has a timeout and returns an error instead of raising, so
  one unreachable device can't take down a discovery sweep.
* SOAP bodies are built with explicit XML escaping. The renderer parses this
  XML, and an unescaped `&` in a track title is a real possibility.
* Response parsing tolerates whatever namespace prefix the device felt like
  using (Sony devices in particular use several).

See docs/dlna-output.md for how this fits into the plugin.
"""

from __future__ import annotations

import concurrent.futures
import html
import re
import select
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"

# Sony's own DLNA documentation searches for the AVTransport service rather
# than the device type, and at least one Sony renderer answers only that
# target. Both are searched by default.
DEFAULT_SEARCH_TARGETS = (AVTRANSPORT, MEDIA_RENDERER)

# Conventional description-document paths, for --scan. A device with a broken
# or disabled SSDP responder is still perfectly controllable if you can find
# its description; these are the paths real devices use in practice.
DESCRIPTION_PATHS = (
    "/dd.xml", "/description.xml", "/rootDesc.xml", "/DeviceDescription.xml",
    "/upnp/description.xml", "/upnp/device.xml", "/MediaRenderer.xml",
    "/MediaRenderer_SRS-ZR7.xml", "/sony/device.xml", "/info.xml",
)

# Ports worth trying during --scan: UPnP's own 1900/1400/52323, the ports
# Sony's DLNA stack uses (54380, 60151), plus the generic web ports.
SCAN_PORTS = (1900, 1400, 52323, 54380, 60151, 64321, 39520, 80, 8080, 49152, 49153)

DEFAULT_TIMEOUT = 6.0
SOAP_TIMEOUT = 20.0

USER_AGENT = "Dromify-DLNA/1.0 UPnP/1.0"


def log(msg: str) -> None:
    """Debug logging, enabled with DROMIFY_DLNA_DEBUG=1."""
    import os
    import sys

    if os.environ.get("DROMIFY_DLNA_DEBUG") == "1":
        print("dromify-dlna: %s" % msg, file=sys.stderr, flush=True)


def redact(url: str) -> str:
    """A URL safe to log: credentials and query values are stripped.

    Subsonic stream URLs carry `u`, `t` (token) and `s` (salt) — replayable
    credentials. Renderer URLs can carry user:pass@ too. Debug output must
    never leak either, so this keeps only scheme/host/path and the parameter
    names. Non-URL values (a DIDL document, say) are replaced wholesale
    rather than passed through urlsplit, which would raise on them.
    """
    if not url:
        return ""
    if not isinstance(url, str):
        return "<%s>" % type(url).__name__
    if not url.lower().startswith(("http://", "https://")):
        return "<redacted %d chars>" % len(url)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    host = parts.hostname or ""
    if parts.port:
        host = "%s:%d" % (host, parts.port)
    if parts.username:
        host = "<redacted>@%s" % host
    names = [k for k, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    query = "&".join("%s=…" % n for n in names)
    return urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path, query, "")
    )


# --- interfaces --------------------------------------------------------------


def local_ipv4_addresses() -> list[tuple[str, str]]:
    """[(interface, ipv4)] for every non-loopback IPv4 address on this host."""
    import fcntl

    out = []
    for _, iface in socket.if_nameindex():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            packed = struct.pack("256s", iface[:15].encode())
            ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])
            s.close()
        except OSError:
            continue
        if ip and not ip.startswith("127."):
            out.append((iface, ip))
    return out


def lan_address_for(host: str) -> str:
    """The local address this host would use to reach `host` ("" if unknown).

    Used to decide whether a server URL is renderer-reachable at all, and to
    build the bridge's advertised address without hardcoding an interface.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 9))  # UDP connect: no packets, just a route lookup
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


# --- SSDP discovery ----------------------------------------------------------


def parse_headers(text: str) -> dict[str, str]:
    """HTTP-ish header block (SSDP response or NOTIFY) -> upper-cased dict."""
    headers: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().upper()] = value.strip()
    return headers


def ssdp_search(
    search_target: str = MEDIA_RENDERER,
    mx: int = 3,
    timeout: float | None = None,
    interface_ips: list[str] | None = None,
    rounds: int = 1,
) -> list[dict[str, str]]:
    """M-SEARCH every interface at once; returns raw header dicts.

    All the sockets send together and are then drained together with one
    `select`, so the cost is a single collection window rather than
    interfaces x rounds x window. That matters: measured on this network, a
    Sony SRS-ZR7's reply can arrive several seconds after the probe while the
    other renderer answers immediately, so a per-socket sequential drain either
    waits out the slow one for every socket or cuts it off.

    `rounds` still repeats the probe (a device that drops one M-SEARCH gets
    another), but the repeats are pipelined into the same window — send round
    2's probe while still listening for round 1's answer. That is what makes
    "repeat the probe" affordable: the original implementation paid
    `timeout` seconds per round per interface, which is where discovery's
    ~45s came from.
    """
    if timeout is None:
        timeout = mx + 2
    if interface_ips is None:
        interface_ips = [ip for _, ip in local_ipv4_addresses()]

    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: %s:%d\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: %d\r\n"
        "ST: %s\r\n"
        "\r\n" % (SSDP_ADDR, SSDP_PORT, mx, search_target)
    ).encode()

    sockets: dict[int, socket.socket] = {}
    results: list[dict[str, str]] = []
    try:
        for iface_ip in interface_ips:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((iface_ip, 0))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(iface_ip))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
                sock.setblocking(False)
                sockets[sock.fileno()] = sock
            except OSError as exc:
                log("SSDP on %s failed: %s" % (iface_ip, exc))

        owners = {fd: sock.getsockname()[0] for fd, sock in sockets.items()}
        deadline = time.time() + timeout
        for round_index in range(max(1, rounds)):
            for sock in sockets.values():
                try:
                    sock.sendto(message, (SSDP_ADDR, SSDP_PORT))
                except OSError as exc:
                    log("SSDP send failed: %s" % exc)
            # A short gap between rounds, not a full window: the answers to
            # every round are collected in the loop below.
            if round_index + 1 < rounds:
                time.sleep(0.3)

        while sockets and time.time() < deadline:
            try:
                ready, _, _ = select.select(list(sockets), [], [], 0.5)
            except OSError:
                break
            for fd in ready:
                sock = sockets[fd]
                try:
                    data, addr = sock.recvfrom(65535)
                except OSError:
                    continue
                text = data.decode("utf-8", "replace")
                if not text.startswith("HTTP/1.1 200"):
                    continue
                headers = parse_headers(text)
                headers["_source"] = addr[0]
                headers["_interface"] = owners.get(fd, "")
                log("SSDP response from %s: ST=%s USN=%s LOCATION=%s"
                    % (addr[0], headers.get("ST", ""), headers.get("USN", ""),
                       redact(headers.get("LOCATION", ""))))
                results.append(headers)
        return results
    finally:
        for sock in sockets.values():
            sock.close()


def unicast_search(
    hosts: list[str],
    search_target: str = MEDIA_RENDERER,
    mx: int = 2,
    timeout: float = 3.0,
    interface_ips: list[str] | None = None,
) -> list[dict[str, str]]:
    """M-SEARCH sent directly to `hosts` instead of the multicast group.

    Some renderers (and some access points with multicast filtering on) only
    answer a unicast M-SEARCH. Costs one UDP packet per known host, and is
    what makes discovery work on networks where the multicast sweep is
    silently dropped.
    """
    if interface_ips is None:
        interface_ips = [ip for _, ip in local_ipv4_addresses()]
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: %s:%d\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: %d\r\n"
        "ST: %s\r\n"
        "\r\n" % (SSDP_ADDR, SSDP_PORT, mx, search_target)
    ).encode()

    results: list[dict[str, str]] = []
    for iface_ip in interface_ips:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((iface_ip, 0))
            sock.settimeout(timeout)
            for host in hosts:
                try:
                    sock.sendto(message, (host, SSDP_PORT))
                except OSError as exc:
                    log("unicast M-SEARCH to %s failed: %s" % (host, exc))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:
                    break
                text = data.decode("utf-8", "replace")
                if not text.startswith("HTTP/1.1 200"):
                    continue
                headers = parse_headers(text)
                headers["_source"] = addr[0]
                headers["_interface"] = iface_ip
                results.append(headers)
        except OSError as exc:
            log("unicast SSDP on %s failed: %s" % (iface_ip, exc))
        finally:
            sock.close()
    return results


def unicast_probe(host: str, port: int, message: bytes, timeout: float = 1.5,
                  bind_ip: str = "") -> tuple[int, str]:
    """Send one raw UDP probe to host:port and wait for one reply.

    Used by the fallback scan: a renderer with a broken SSDP responder still
    answers a direct M-SEARCH on its own HTTP port on some devices, and this
    is how we find out without opening a raw socket.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if bind_ip:
            sock.bind((bind_ip, 0))
        sock.settimeout(timeout)
        sock.sendto(message, (host, port))
        data, addr = sock.recvfrom(65535)
        return 1, data.decode("utf-8", "replace")
    except OSError:
        return 0, ""
    finally:
        sock.close()


def http_probe(host: str, port: int, path: str, timeout: float = 1.5) -> tuple[int, str]:
    """GET one candidate description path; returns (status, body)."""
    return http_get("http://%s:%d%s" % (host, port, path), timeout=timeout,
                    max_bytes=256 * 1024)


def live_endpoints(hosts: list[str],
                   ports=(80, 8080, 54380, 52323, 60151, 1400, 1400, 1900),
                   timeout: float = 0.35, workers: int = 256
                   ) -> list[tuple[str, int]]:
    """[(host, port)] for every host:port that accepts a TCP connect.

    Two reasons this runs first: a dead address otherwise costs a full
    timeout on every subsequent probe (a 254-host sweep becomes minutes
    instead of seconds), and knowing *which* port is open means phase 2 only
    has to probe ports that exist. TCP connect rather than ICMP because it
    needs no privileges and works against devices that ignore ping.
    """
    def alive(host: str) -> list[tuple[str, int]]:
        open_ports = []
        for port in ports:
            sock = socket.socket()
            sock.settimeout(timeout)
            try:
                if sock.connect_ex((host, port)) == 0:
                    open_ports.append((host, port))
            except OSError:
                pass
            finally:
                sock.close()
        return open_ports

    found: list[tuple[str, int]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(alive, hosts):
            found.extend(result)
    return found


def scan_hosts(hosts: list[str], ports=SCAN_PORTS,
               paths=DESCRIPTION_PATHS, timeout: float = 1.0,
               workers: int = 128, live_only: bool = True) -> list[dict]:
    """Fallback discovery for a device that does not answer SSDP.

    Phase 1 narrows the subnet to hosts that answer a TCP connect; phase 2
    probes each live host with a direct M-SEARCH on the candidate ports and a
    GET of the conventional description paths. Anything that parses as a
    device description advertising AVTransport is returned in the same shape
    as `discover()`.

    Explicitly user-driven (the panel's "Scan this network" action): it is a
    burst of connections across the subnet, so it never runs implicitly.
    """
    msearch = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: %s:%d\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: %s\r\n"
        "\r\n" % (SSDP_ADDR, SSDP_PORT, MEDIA_RENDERER)
    ).encode()

    if live_only:
        endpoints = live_endpoints(hosts)
        log("scan: %d of %d host(s) answered a connect" % (
            len({host for host, _ in endpoints}), len(hosts)))
        # UDP-only devices answer nothing on TCP, so they'd be invisible to
        # the liveness filter; keep a UDP probe for those, but only on the
        # ports UPnP actually uses.
        tasks = list(endpoints)
        for host in hosts:
            for port in (SSDP_PORT, 1400):
                if (host, port) not in tasks:
                    tasks.append((host, port))
    else:
        tasks = [(host, port) for host in hosts for port in ports]

    locations: set[str] = set()
    lock = threading.Lock()

    def note(location: str) -> None:
        with lock:
            locations.add(location)

    def probe_port(host: str, port: int) -> None:
        # A direct M-SEARCH on the device's own port: some renderers answer
        # unicast on their HTTP port but never on the multicast group.
        for _target in DEFAULT_SEARCH_TARGETS:
            count, text = unicast_probe(host, port, msearch, timeout=timeout)
            if count and text.startswith("HTTP/1.1 200"):
                headers = parse_headers(text)
                if headers.get("LOCATION"):
                    note(headers["LOCATION"])
                    return
        # A description document served from an unconventional path.
        status, body = http_probe(host, port, "/", timeout=timeout)
        if status == 200 and "<device" in body[:8000] and "AVTransport" in body:
            note("http://%s:%d/" % (host, port))
            return
        for path in paths:
            status, body = http_probe(host, port, path, timeout=timeout)
            if status == 200 and "<device" in body[:8000] and "AVTransport" in body:
                note("http://%s:%d%s" % (host, port, path))
                return

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda args: probe_port(*args), tasks))

    devices: dict[str, dict] = {}
    for location in sorted(locations):
        info = fetch_description(location)
        if not info or AVTRANSPORT not in info["services"]:
            continue
        device = describe_device(info)
        key = dedupe_key(device)
        if key in devices:
            continue
        devices[key] = device
    log("scan found %d renderer(s) across %d host(s)" % (len(devices), len(hosts)))
    return list(devices.values())


def ssdp_listen(
    timeout: float = 6.0,
    interface_ips: list[str] | None = None,
    quiet_after: float = 1.5,
) -> list[dict[str, str]]:
    """Collect SSDP NOTIFY (ssdp:alive) advertisements.

    The third discovery path: devices announce themselves unprompted, and a
    renderer that ignores M-SEARCH entirely can still be found this way. Needs
    to bind :1900, which fails if another daemon already holds it — treated as
    "no results", never as fatal.

    Stops early once something has been heard *and* the group has gone quiet
    for `quiet_after` seconds: an SSDP announce arrives in bursts, so waiting
    out the whole window after the burst is pure latency on the panel's
    critical path. `timeout` stays as the hard ceiling for a silent network.
    """
    if interface_ips is None:
        interface_ips = [ip for _, ip in local_ipv4_addresses()]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", SSDP_PORT))
        for iface_ip in interface_ips:
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR),
                                   socket.inet_aton(iface_ip))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as exc:
                log("cannot join %s on %s: %s" % (SSDP_ADDR, iface_ip, exc))
        sock.settimeout(0.5)
        results: list[dict[str, str]] = []
        deadline = time.time() + timeout
        quiet_until = 0.0
        while time.time() < deadline:
            if results and time.time() >= quiet_until:
                break
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            if not text.startswith("NOTIFY"):
                continue
            headers = parse_headers(text)
            if headers.get("NTS") != "ssdp:alive":
                continue
            headers["_source"] = addr[0]
            results.append(headers)
            quiet_until = time.time() + quiet_after
        return results
    except OSError as exc:
        log("SSDP NOTIFY listen unavailable: %s" % exc)
        return []
    finally:
        sock.close()


# --- device description ------------------------------------------------------


def _localname(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _absolute(base: str, url: str) -> str:
    if not url:
        return ""
    return url if url.startswith("http") else urllib.parse.urljoin(base, url)


def http_get(url: str, timeout: float = DEFAULT_TIMEOUT, max_bytes: int = 512 * 1024):
    """GET returning (status, body). Never raises; (None, error) on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                return None, "response too large"
            return resp.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(max_bytes).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return None, "%s: %s" % (type(exc).__name__, exc)


def fetch_description(location: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """Download and parse a UPnP device description into a flat dict."""
    status, body = http_get(location, timeout=timeout)
    if status != 200:
        log("device description %s failed: %s" % (redact(location), body or status))
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        log("device description %s is not XML: %s" % (redact(location), exc))
        return None

    def text_of(name: str) -> str:
        for el in root.iter():
            if _localname(el.tag) == name and el.text:
                return el.text.strip()
        return ""

    base = location
    match = re.match(r"^(https?://[^/]+)", location)
    if match:
        base = match.group(1)

    services: dict[str, dict[str, str]] = {}
    for svc in root.iter():
        if _localname(svc.tag) != "service":
            continue
        fields = {_localname(child.tag): (child.text or "").strip() for child in svc}
        stype = fields.get("serviceType", "")
        if not stype:
            continue
        services[stype] = {
            "serviceId": fields.get("serviceId", ""),
            "controlURL": _absolute(base, fields.get("controlURL", "")),
            "eventSubURL": _absolute(base, fields.get("eventSubURL", "")),
            "SCPDURL": _absolute(base, fields.get("SCPDURL", "")),
        }

    return {
        "location": location,
        "friendlyName": text_of("friendlyName"),
        "manufacturer": text_of("manufacturer"),
        "modelName": text_of("modelName"),
        "modelNumber": text_of("modelNumber"),
        "modelDescription": text_of("modelDescription"),
        "udn": text_of("UDN"),
        "deviceType": text_of("deviceType"),
        "services": services,
    }


def discover(
    timeout: float = 6.0,
    known_hosts: list[str] | None = None,
    search_targets: tuple[str, ...] = DEFAULT_SEARCH_TARGETS,
    rounds: int = 3,
    budget: float = 0.0,
) -> list[dict]:
    """Find MediaRenderers on the LAN and describe them.

    Three probes, merged by UDN: multicast M-SEARCH, unicast M-SEARCH to
    `known_hosts`, and a passive SSDP NOTIFY listen. Any one of them finding a
    device is enough — which is the point, since real networks drop at least
    one of the three.

    Only devices whose description actually advertises an AVTransport service
    are returned: a `MediaRenderer` without AVTransport can't be played to,
    and pretending otherwise would put an unusable row in the output picker.
    """
    interface_ips = [ip for _, ip in local_ipv4_addresses()]
    log("interfaces: %s" % ", ".join(interface_ips))

    # The three probes run concurrently: sequentially they'd cost the sum of
    # their timeouts (20s+ on a real network), and a user clicking "Refresh"
    # should not wait that long. Each probe is independent and total, so a
    # failure in one is just an empty result.
    candidates: dict[str, dict[str, str]] = {}
    collected: list[tuple[str, list[dict[str, str]]]] = []
    lock = threading.Lock()

    def run(kind: str, fn) -> None:
        try:
            found = fn()
        except Exception as exc:  # noqa: BLE001 - a probe must never kill discovery
            log("%s probe failed: %s: %s" % (kind, type(exc).__name__, exc))
            found = []
        with lock:
            collected.append((kind, found))

    probes = [("multicast-%s" % iface_ip,
               (lambda ip=iface_ip: [
                   headers
                   for target in search_targets
                   for headers in ssdp_search(target, mx=max(1, int(timeout) - 2),
                                              timeout=timeout,
                                              interface_ips=[ip],
                                              rounds=rounds)
               ]))
              for iface_ip in interface_ips]
    if known_hosts:
        probes += [("unicast-%s" % iface_ip,
                    (lambda ip=iface_ip: [
                        headers
                        for target in search_targets
                        for headers in unicast_search(known_hosts, target,
                                                      timeout=min(3.0, timeout),
                                                      interface_ips=[ip])
                    ]))
                   for iface_ip in interface_ips]
    probes.append(("notify", lambda: ssdp_listen(timeout=timeout,
                                                 interface_ips=interface_ips)))

    threads = [threading.Thread(target=run, args=probe, daemon=True)
               for probe in probes]
    for thread in threads:
        thread.start()
    # Bounded join. The probes are independently time-limited, but a socket
    # can still sit in a syscall (a device that keeps broadcasting means
    # ssdp_listen's quiet window never expires), and discovery sits on the
    # panel's critical path — it must not be able to hang the UI. Anything
    # still running when the budget expires is abandoned rather than waited
    # on; daemon threads die with the process.
    budget = budget or max(3.0, timeout + 2.0)
    for thread in threads:
        thread.join(timeout=budget)
    stragglers = [t for t in threads if t.is_alive()]
    if stragglers:
        log("%d probe(s) still running after %.0fs; continuing without them"
            % (len(stragglers), budget))

    for kind, found in collected:
        for headers in found:
            if kind == "notify":
                nt = headers.get("NT", "")
                if nt not in search_targets and nt != "upnp:rootdevice" and \
                        nt not in headers.get("USN", ""):
                    continue
            key = headers.get("USN") or headers.get("LOCATION", "")
            candidates.setdefault(key, headers)

    log("%d SSDP candidate(s)" % len(candidates))

    # Descriptions are fetched in parallel. Sequentially, one unreachable
    # candidate costs a full timeout before the next is even tried — measured
    # here as most of discovery's wall time, and it lands squarely on the
    # panel's critical path.
    seen_locations: list[str] = []
    for headers in candidates.values():
        location = headers.get("LOCATION", "")
        if location and location not in seen_locations:
            seen_locations.append(location)

    devices: dict[str, dict] = {}
    if seen_locations:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(seen_locations))) as pool:
            for info in pool.map(fetch_description, seen_locations):
                if not info:
                    continue
                if AVTRANSPORT not in info["services"]:
                    log("skipping %s: no AVTransport service"
                        % (info.get("friendlyName") or info.get("location", "")))
                    continue
                device = describe_device(info)
                key = dedupe_key(device)
                devices.setdefault(key, device)
    log("discovered %d renderer(s)" % len(devices))
    return list(devices.values())


def dedupe_key(device: dict) -> str:
    """A stable identity for a renderer, for de-duplicating discovery results.

    A UDN alone is not enough: some Sony devices advertise one UUID in their
    SSDP messages (derived from the MAC) and report a different one in their
    own device description, so a single speaker shows up twice — once under
    its SSDP name and once under its generic model name. The description's
    location (host + port + path) identifies one control endpoint
    unambiguously, so that is preferred, falling back to the UDN.
    """
    location = device.get("location", "")
    if location:
        return location
    return device.get("udn", "")


def describe_device(info: dict) -> dict:
    """Flatten a parsed description into the shape the rest of Dromify uses.

    `name` prefers the friendlyName; some devices report a generic string
    there ("Sony Audio") while the model carries the real product name, so
    fall back to that when friendlyName is generic or missing.
    """
    name = info.get("friendlyName", "")
    model = info.get("modelName", "")
    manufacturer = info.get("manufacturer", "")
    # Some devices put a generic brand string in friendlyName ("Sony Audio",
    # "MediaRenderer") while the model carries the real product name, which
    # makes two renderers from one brand indistinguishable in the picker.
    # The test is "the friendlyName is a prefix of, or contained in, the
    # manufacturer or model" — i.e. it names the brand rather than this
    # device — which needs no per-vendor table:
    #   friendlyName "Sony Audio" + manufacturer "Sony Corporation" -> generic
    #   friendlyName "SRS-ZR7 maxxie2" + model "SRS-ZR7"            -> specific
    #   friendlyName "Living Room"                                  -> specific
    def is_generic(candidate: str) -> bool:
        """Whether this friendlyName names the brand rather than the device.

        Two signals, no vendor table:

        * The friendlyName does not mention the model. A user-chosen name
          almost always does not either, which is why the brand-word test
          below has to pass as well.
        * Its first word is the manufacturer's first word — "Sony Audio" for
          a "Sony Corporation" device. That is the shape of a stock
          brand-and-category default, as opposed to a name somebody set.
        """
        if not candidate:
            return True
        low = candidate.lower().strip()
        if low in ("audio", "mediarenderer", "media renderer", "upnp av"):
            return True
        if model and model.lower() in low:
            return False
        if manufacturer:
            first = manufacturer.lower().split()[0]
            if low.split()[0] == first:
                return True
        return False

    if is_generic(name) and model:
        name = model
    services = info.get("services", {})
    return {
        "name": name or info.get("udn", "Unknown renderer"),
        "manufacturer": info.get("manufacturer", ""),
        "model": model or info.get("modelNumber", ""),
        "modelDescription": info.get("modelDescription", ""),
        "udn": info.get("udn", ""),
        "location": info.get("location", ""),
        "avTransportControlURL": services.get(AVTRANSPORT, {}).get("controlURL", ""),
        "avTransportEventURL": services.get(AVTRANSPORT, {}).get("eventSubURL", ""),
        "renderingControlURL": services.get(RENDERING_CONTROL, {}).get("controlURL", ""),
        "renderingControlSCPDURL": services.get(RENDERING_CONTROL, {}).get("SCPDURL", ""),
        "connectionManagerURL": services.get(CONNECTION_MANAGER, {}).get("controlURL", ""),
        "services": sorted(services.keys()),
    }


# --- SOAP --------------------------------------------------------------------


def _xml_escape(value: str) -> str:
    return html.escape(str(value), quote=True)


def soap_call(control_url: str, service_type: str, action: str,
              args: dict, timeout: float = SOAP_TIMEOUT) -> tuple[bool, dict]:
    """POST one SOAP action. Returns (ok, fields-or-error-dict).

    On a SOAP fault the returned dict carries `errorCode`/`errorDescription`
    (UPnP's own error vocabulary, e.g. 701 = Transition not available, 716 =
    Action failed) — callers surface those instead of a generic failure,
    because the renderer's answer is the useful diagnostic.
    """
    body = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        "<u:%s xmlns:u=\"%s\">" % (action, service_type)
    )
    for key, value in args.items():
        body += "<%s>%s</%s>" % (key, _xml_escape(value), key)
    body += "</u:%s></s:Body></s:Envelope>" % action

    req = urllib.request.Request(control_url, data=body.encode("utf-8"), headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"%s#%s"' % (service_type, action),
        "User-Agent": USER_AGENT,
    })
    log("SOAP %s -> %s %s" % (action, redact(control_url),
                              {k: (redact(v) if k in ("CurrentURI", "NextURI") else v)
                               for k, v in args.items()}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(256 * 1024).decode("utf-8", "replace")
            log("SOAP %s <- HTTP %s" % (action, resp.status))
            return True, soap_fields(text)
    except urllib.error.HTTPError as exc:
        text = exc.read(64 * 1024).decode("utf-8", "replace")
        fields = soap_fields(text)
        log("SOAP %s <- HTTP %s %s" % (action, exc.code,
                                       fields.get("errorCode", "")))
        return False, fields or {"errorCode": str(exc.code),
                                 "errorDescription": "HTTP %s" % exc.code}
    except Exception as exc:  # noqa: BLE001
        log("SOAP %s <- %s: %s" % (action, type(exc).__name__, exc))
        return False, {"errorCode": "transport",
                       "errorDescription": "%s: %s" % (type(exc).__name__, exc)}


def soap_fields(text: str) -> dict[str, str]:
    """Every leaf element of a SOAP response, by local tag name."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for el in root.iter():
        if len(el) == 0 and el.text is not None:
            out[_localname(el.tag)] = el.text
    return out


# --- AVTransport -------------------------------------------------------------


class Renderer:
    """A discovered renderer's control endpoints, with the actions we use."""

    def __init__(self, device: dict):
        self.device = device
        self.avt = device.get("avTransportControlURL", "")
        self.rc = device.get("renderingControlURL", "")
        self.cm = device.get("connectionManagerURL", "")

    @property
    def name(self) -> str:
        return self.device.get("name", "")

    def _avt(self, action: str, **args) -> tuple[bool, dict]:
        if not self.avt:
            return False, {"errorCode": "no-avtransport",
                           "errorDescription": "renderer has no AVTransport control URL"}
        return soap_call(self.avt, AVTRANSPORT, action, args)

    def set_uri(self, uri: str, metadata: str) -> tuple[bool, dict]:
        return self._avt("SetAVTransportURI", InstanceID=0,
                         CurrentURI=uri, CurrentURIMetaData=metadata)

    def set_next_uri(self, uri: str, metadata: str) -> tuple[bool, dict]:
        return self._avt("SetNextAVTransportURI", InstanceID=0,
                         NextURI=uri, NextURIMetaData=metadata)

    def play(self) -> tuple[bool, dict]:
        return self._avt("Play", InstanceID=0, Speed=1)

    def pause(self) -> tuple[bool, dict]:
        return self._avt("Pause", InstanceID=0)

    def stop(self) -> tuple[bool, dict]:
        return self._avt("Stop", InstanceID=0)

    def next(self) -> tuple[bool, dict]:
        return self._avt("Next", InstanceID=0)

    def previous(self) -> tuple[bool, dict]:
        return self._avt("Previous", InstanceID=0)

    def seek(self, seconds: int) -> tuple[bool, dict]:
        return self._avt("Seek", InstanceID=0, Unit="REL_TIME",
                         Target=format_hms(seconds))

    def transport_info(self) -> dict:
        ok, fields = self._avt("GetTransportInfo", InstanceID=0)
        return fields if ok else {}

    def position_info(self) -> dict:
        ok, fields = self._avt("GetPositionInfo", InstanceID=0)
        return fields if ok else {}

    def media_info(self) -> dict:
        ok, fields = self._avt("GetMediaInfo", InstanceID=0)
        return fields if ok else {}

    def current_actions(self) -> list[str]:
        """What the device says it can do *right now*.

        Read rather than assumed: the SRS-ZR7 answers `Stop,Next,Previous`
        while playing, so Pause/Seek are genuinely unavailable there and the
        panel greys them out instead of firing commands that fault.
        """
        ok, fields = self._avt("GetCurrentTransportActions", InstanceID=0)
        if not ok:
            return []
        return [a.strip() for a in fields.get("Actions", "").split(",") if a.strip()]

    def protocol_info(self) -> list[str]:
        """The device's advertised Sink protocols (ConnectionManager)."""
        if not self.cm:
            return []
        ok, fields = soap_call(self.cm, CONNECTION_MANAGER, "GetProtocolInfo", {})
        if not ok:
            return []
        return [p for p in fields.get("Sink", "").split(",") if p]

    def volume(self) -> int | None:
        if not self.rc:
            return None
        ok, fields = soap_call(self.rc, RENDERING_CONTROL, "GetVolume",
                               {"InstanceID": 0, "Channel": "Master"})
        if not ok or "CurrentVolume" not in fields:
            return None
        try:
            return int(fields["CurrentVolume"])
        except ValueError:
            return None

    def _rendering_control_scpd(self) -> ET.Element | None:
        scpd_url = self.device.get("renderingControlSCPDURL", "")
        if not scpd_url and self.device.get("location"):
            info = fetch_description(self.device["location"], timeout=3.0)
            if info:
                scpd_url = info.get("services", {}).get(RENDERING_CONTROL, {}).get(
                    "SCPDURL", "")
        if not scpd_url:
            return None

        status, body = http_get(scpd_url, timeout=3.0, max_bytes=256 * 1024)
        if status != 200:
            return None
        try:
            return ET.fromstring(body)
        except ET.ParseError:
            return None

    def volume_info(self) -> dict | None:
        """Return volume actions and the declared Master range, if available.

        UPnP RenderingControl volume units are vendor-defined. Read the
        `GetVolume` state variable's range and step from the service
        description instead of assuming every renderer uses 0–100. Some
        devices omit the maximum; callers can still offer relative steps.
        """
        root = self._rendering_control_scpd()
        if root is None:
            return None

        actions: set[str] = set()
        related_name = ""
        for action in root.iter():
            if _localname(action.tag) != "action":
                continue
            action_name = next((
                (child.text or "").strip()
                for child in action if _localname(child.tag) == "name"
            ), "")
            if action_name:
                actions.add(action_name)
            if action_name != "GetVolume":
                continue
            for arg in action.iter():
                if _localname(arg.tag) != "argument":
                    continue
                fields = {
                    _localname(child.tag): (child.text or "").strip()
                    for child in arg
                }
                if fields.get("name") == "CurrentVolume":
                    related_name = fields.get("relatedStateVariable", "")
                    break

        get_supported = "GetVolume" in actions and bool(related_name)
        value_range = None
        for variable in root.iter():
            if _localname(variable.tag) != "stateVariable":
                continue
            name = next((
                (child.text or "").strip()
                for child in variable if _localname(child.tag) == "name"
            ), "")
            if name != related_name or not related_name:
                continue
            allowed_range = next((
                child for child in variable
                if _localname(child.tag) == "allowedValueRange"
            ), None)
            if allowed_range is None:
                break
            fields = {
                _localname(child.tag): (child.text or "").strip()
                for child in allowed_range
            }
            try:
                minimum = int(fields.get("minimum", "0"))
                maximum = int(fields["maximum"]) if fields.get("maximum") else None
                step = int(fields.get("step", "1"))
            except ValueError:
                return None
            if (minimum < 0 or minimum > 65535 or step < 1 or step > 65535
                    or (maximum is not None
                        and (maximum < minimum or maximum > 65535))):
                break
            value_range = (minimum, maximum, step)
            break
        return {
            "get": get_supported,
            "set": "SetVolume" in actions,
            "range": value_range,
        }

    def volume_range(self) -> tuple[int, int | None, int] | None:
        """Return the renderer's declared Master volume range, if available."""
        info = self.volume_info()
        return info["range"] if info else None

    def set_volume(self, value: int) -> bool:
        if not self.rc:
            return False
        ok, _ = soap_call(self.rc, RENDERING_CONTROL, "SetVolume",
                          {"InstanceID": 0, "Channel": "Master",
                           "DesiredVolume": int(value)})
        return ok


# --- time formatting ---------------------------------------------------------


def sane_duration(reported, known) -> float:
    """Pick between the renderer's reported duration and the one we queued.

    A transcoded stream has no Content-Length (Navidrome sends it chunked), so
    renderers report nonsense for it — measured on a ZR7: `596:31:23` for a
    4-minute track. The queue's own duration comes from the Subsonic API and
    is correct, so it wins whenever the renderer's answer is implausible.
    """
    reported_seconds = parse_hms(reported or "")
    try:
        known_seconds = float(known or 0)
    except (TypeError, ValueError):
        known_seconds = 0.0
    if known_seconds <= 0:
        return reported_seconds
    if reported_seconds <= 0:
        return known_seconds
    if abs(reported_seconds - known_seconds) > max(30.0, 0.2 * known_seconds):
        return known_seconds
    return reported_seconds


def format_hms(seconds) -> str:
    """Seconds -> "H:MM:SS" (UPnP's REL_TIME / duration format)."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "0:00:00"
    total = max(0, total)
    return "%d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


def parse_hms(text: str) -> float:
    """UPnP "H:MM:SS" / "H:MM:SS.mmm" -> seconds (0.0 if unparseable)."""
    if not text:
        return 0.0
    parts = text.strip().split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return max(0.0, seconds)


# --- DIDL-Lite ---------------------------------------------------------------

# Fallback MIME table, used only when the Subsonic API gave us no
# `contentType`. The API's own value is preferred because it is what the
# server will actually put on the wire — measured against Navidrome 0.64:
# `.dsf` -> audio/x-dsf, `.ape` -> audio/x-monkeys-audio, `.wav` ->
# audio/x-wav, `.m4a` -> audio/mp4. Guessing "audio/dsd" for a .dsf (which is
# what the extension suggests) makes the renderer reject the item, because it
# validates the response against the MIME we declared.
_MIME_BY_SUFFIX = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/vnd.dlna.adts",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "wma": "audio/x-ms-wma",
    "aif": "audio/aiff",
    "aiff": "audio/aiff",
    "dsf": "audio/dsd",
    "dff": "audio/dsd",
}

# A PN is only claimed for the formats where the DLNA profile is unambiguous
# and lossless (no transcoding surprises). Guessing a wrong PN is worse than
# sending none: some renderers reject the item outright.
_PROFILE_BY_MIME = {
    "audio/mpeg": "MP3",
    "audio/L16": "LPCM",
}


def guess_mime(song: dict) -> str:
    """The MIME type the server will actually send for this song.

    `contentType` from the Subsonic API wins when present: it is the server's
    own answer, and the renderer validates what it receives against the MIME
    we declared in the DIDL, so being right here is what makes playback work
    at all. The suffix table is only a fallback for a server that omits it.
    """
    content_type = str(song.get("contentType") or "").strip().lower()
    if content_type.startswith("audio/") or content_type.startswith("video/"):
        return content_type
    suffix = str(song.get("suffix") or "").lower()
    return _MIME_BY_SUFFIX.get(suffix, "audio/mpeg")


# The one format every DLNA renderer is required to accept, and the one
# Navidrome can transcode anything into (`?format=mp3`).
FALLBACK_MIME = "audio/mpeg"


def renderer_accepts_mime(sink_protocols: list[str], mime: str) -> bool:
    """Whether the renderer's ConnectionManager sink list covers `mime`.

    Absence of a sink list means "unknown", not "no": an empty list returns
    True so an uncooperative device is attempted rather than pre-emptively
    transcoded.
    """
    if not sink_protocols:
        return True
    wanted = mime.strip().lower()
    for entry in sink_protocols:
        # http-get:*:audio/flac:DLNA.ORG_PN=FLAC;... — third field is the MIME
        fields = entry.split(":")
        if len(fields) < 3:
            continue
        advertised = fields[2].strip().lower()
        if advertised in (wanted, "*"):
            return True
    return False


# Formats outside the DLNA baseline (MP3, LPCM/WAV, AAC, WMA) that renderers
# routinely advertise in GetProtocolInfo and then fail to actually play.
# Measured: a Sony SRS-ZR7 lists `http-get:*:audio/dsd:*` in its sink list and
# still rejects a DSF stream with SOAP fault 501. Sony's own DLNA format table
# for this class of device agrees — FLAC, ALAC, WAV/LPCM, AIFF, MP3, AAC and
# WMA are the supported set, and DSD/APE are not on it. Keyed by MIME, not by
# device model: the rule is "not in the DLNA baseline and seen to fail", which
# any renderer's sink list can be wrong about.
TRANSCODE_ALWAYS = {
    "audio/dsd", "audio/x-dsd", "audio/dff", "audio/x-dff",
    "audio/x-monkeys-audio", "audio/ape",
    "audio/x-wavpack", "audio/x-tta", "audio/x-tak",
}


def transcode_url(url: str) -> str:
    """Ask Navidrome for this stream transcoded to MP3 (`?format=mp3`).

    Subsonic's documented parameter, and the one Navidrome implements: it
    replaces the whole media response, so the URL stays a single ranged GET
    with the existing salt+token — no extra server state, no second hop.
    """
    if not url:
        return url
    separator = "&" if "?" in url else "?"
    if "format=" in url:
        return url
    return "%s%sformat=mp3" % (url, separator)


def needs_transcode(song: dict, sink_protocols: list[str]) -> bool:
    """Whether this track must be asked for as MP3 instead of as-is.

    Two independent reasons, either of which is enough: the renderer's own
    sink list does not cover the MIME we would declare, or the format is one
    of the ones renderers advertise but cannot play (see TRANSCODE_ALWAYS).
    Handing over a stream the device will reject is not "lossless" — it is
    silence — so the fallback is Navidrome's MP3 transcode.
    """
    mime = guess_mime(song)
    if mime in TRANSCODE_ALWAYS:
        return True
    return not renderer_accepts_mime(sink_protocols, mime)


def protocol_info_for(song: dict, mime: str = "") -> str:
    """A `protocolInfo` string for this song's stream, for the DIDL `<res>`.

    The third field is a comma-separated list of DLNA profile/flag
    attributes; `*` there means "no constraints" and is universally accepted.
    We add the standard flags plus `DLNA.ORG_OP=01` (byte-seek supported) so
    renderers know they may range-request, which they will do.

    `mime` overrides the guess, for a track that was asked for as a
    transcode (`?format=mp3`) — the declared MIME must match what the server
    actually sends or the renderer rejects the item.
    """
    mime = mime or guess_mime(song)
    profile = _PROFILE_BY_MIME.get(mime, "")
    parts = ["DLNA.ORG_OP=01", "DLNA.ORG_CI=0",
             "DLNA.ORG_FLAGS=01700000000000000000000000000000"]
    if profile:
        parts.insert(0, "DLNA.ORG_PN=%s" % profile)
    return "http-get:*:%s:%s" % (mime, ";".join(parts))


def didl_lite(uri: str, song: dict, duration_seconds=None, art_url: str = "",
              mime: str = "") -> str:
    """DIDL-Lite metadata for one track, ready to put in CurrentURIMetaData.

    Only the fields we actually know are emitted. A renderer that displays
    metadata shows title/artist/album; one that validates `res` needs the
    protocolInfo to match what it will really fetch.
    """
    def esc(value) -> str:
        return html.escape(str(value or ""), quote=True)

    title = song.get("title") or song.get("name") or "Unknown track"
    artist = song.get("artist") or ""
    album = song.get("album") or ""
    duration = duration_seconds if duration_seconds is not None else song.get("duration")
    res_attrs = ['protocolInfo="%s"' % esc(protocol_info_for(song, mime))]
    if duration:
        res_attrs.append('duration="%s"' % format_hms(duration))
    if song.get("size"):
        try:
            res_attrs.append('size="%d"' % int(song["size"]))
        except (TypeError, ValueError):
            pass

    parts = [
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"',
        ' xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"',
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"',
        ' xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">',
        '<item id="%s" parentID="0" restricted="1">' % esc(song.get("id") or "1"),
        "<dc:title>%s</dc:title>" % esc(title),
    ]
    if artist:
        parts.append("<dc:creator>%s</dc:creator>" % esc(artist))
        parts.append("<upnp:artist>%s</upnp:artist>" % esc(artist))
    if album:
        parts.append("<upnp:album>%s</upnp:album>" % esc(album))
    if song.get("track"):
        parts.append("<upnp:originalTrackNumber>%s</upnp:originalTrackNumber>"
                     % esc(song["track"]))
    if art_url:
        parts.append('<upnp:albumArtURI dlna:profileID="JPEG_TN">%s</upnp:albumArtURI>'
                     % esc(art_url))
    parts.append('<upnp:class>object.item.audioItem.musicTrack</upnp:class>')
    parts.append("<res %s>%s</res>" % (" ".join(res_attrs), esc(uri)))
    parts.append("</item></DIDL-Lite>")
    return "".join(parts)
