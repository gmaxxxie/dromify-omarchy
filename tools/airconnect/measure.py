#!/usr/bin/env python3
"""Quantify AirPlay track-switch latency from an AirConnect log.

Reports, per AirPlay FLUSH: time to the first audio frame, to the renderer's
SetAVTransportURI, to Play, and to the renderer's first HTTP GET.

Usage: python3 measure.py [log path]   (default: ~/Library/Logs/airconnect-launchd.log)
"""
import os
import re
import sys
import datetime

p = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/Library/Logs/airconnect-launchd.log")
ts = re.compile(r"^\[(\d\d:\d\d:\d\d\.\d+)\]")
def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)
events = []
for line in open(p, errors="ignore"):
    m = ts.match(line)
    if not m:
        continue
    t = secs(m.group(1))
    if "received FLUSH" in line:
        events.append((t, "flush"))
    elif ("1st accepted packet" in line) or ("fill [level" in line) or ("drain [level" in line):
        events.append((t, "audio"))
    elif "uPNP setURI" in line and "AVTSetURI" not in line:
        events.append((t, "seturi"))
    elif "AVTPlay" in line:
        events.append((t, "deviceplay"))
    elif "received GET" in line:
        events.append((t, "device_get"))
print("   FLUSH时刻  ->首帧音频 ->setURI ->Play ->设备GET   (秒)")
n = 0
for i, (t, k) in enumerate(events):
    if k != "flush":
        continue
    def nxt(kind, after):
        for t2, k2 in events[i:]:
            if k2 == kind and t2 >= after:
                return t2
        return None
    au = nxt("audio", t + 0.001)
    su = nxt("seturi", t + 0.001)
    pl = nxt("deviceplay", su if su else t)
    g = nxt("device_get", pl if pl else (su if su else t))
    n += 1
    f = lambda a, b: "%.2f" % (b - a) if (a is not None and b is not None) else "-"
    print("%12s %10s %8s %6s %9s" % (str(datetime.timedelta(seconds=int(t))), f(t, au), f(t, su) if su else "-", f(su, pl), f(pl, g)))
print("\n共 %d 次 FLUSH 事件" % n)
