"""Detect renderer "not consuming audio" stalls in an AirConnect log.

The bridge logs `fill [level:N] [W:x R:y]`; a growing W with a frozen R means
the renderer stopped reading the stream (its own buffer full, or a paired
speaker it is waiting for). Sustained stalls are the audible dropouts.

Usage: python3 stalls.py [log path]   (default: ~/Library/Logs/airconnect-launchd.log)
"""
import os
import re
import sys
import datetime

p = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/Library/Logs/airconnect-launchd.log")
pat = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\.(\d+)\].*level:(\d+)\] \[W:(\d+) R:(\d+)\]")
def t(h,m,s,ms): return int(h)*3600+int(m)*60+int(s)+int(ms)/1000.0
rows=[]
for line in open(p, errors="ignore"):
    m=pat.match(line)
    if m: rows.append((t(*map(int,m.groups()[:4])), int(m.group(5)), int(m.group(6)), int(m.group(7))))
print("采样点:", len(rows))
stalls=[]; cur=None
for i in range(1,len(rows)):
    prev, now = rows[i-1], rows[i]
    if now[3]==prev[3] and now[2]>prev[2]:   # R 不动而 W 增长
        if cur is None: cur=[prev[0], prev[2], prev[3]]
        cur[1]=now[2]
    else:
        if cur and now[0]-cur[0] >= 3: stalls.append((cur[0], now[0]-cur[0], cur[1]-cur[2]))
        cur=None
if cur: stalls.append((cur[0], rows[-1][0]-cur[0], cur[1]-cur[2]))
print("\nR 冻结（播放器不消费）事件 >=3s:")
for st,dur,wpk in stalls:
    print("  %s  持续 %5.1fs  期间积压 %d 帧 (≈%.1f 秒音频)" % (datetime.timedelta(seconds=int(st)), dur, wpk, wpk*0.008))
print("\n共 %d 次" % len(stalls))
