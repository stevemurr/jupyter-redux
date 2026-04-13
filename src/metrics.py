"""Host-wide CPU and memory metrics read directly from /proc.

The backend container's /proc is not rewritten by docker — MemTotal,
MemAvailable, and /proc/stat all reflect the host, not the container's
cgroup. So we can just read them directly without any bind mounts
or pid namespace sharing.
"""

from __future__ import annotations

import threading

_cpu_lock = threading.Lock()
_last_cpu_total = 0
_last_cpu_idle = 0


def get_host_cpu_pct() -> float:
    """CPU usage across all cores since the last call, normalized to 0–100.

    /proc/stat's first line is the aggregated `cpu` counters in jiffies.
    The first call returns 0 (no prior sample to diff against); every
    call after that reports real utilization.
    """
    global _last_cpu_total, _last_cpu_idle
    try:
        with open("/proc/stat") as f:
            line = f.readline()
    except OSError:
        return 0.0

    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return 0.0
    values = [int(v) for v in parts[1:]]
    # /proc/stat columns: user, nice, system, idle, iowait, irq,
    # softirq, steal, guest, guest_nice. Treat idle + iowait as "idle".
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)

    with _cpu_lock:
        prev_total = _last_cpu_total
        prev_idle = _last_cpu_idle
        _last_cpu_total = total
        _last_cpu_idle = idle

    delta_total = total - prev_total
    delta_idle = idle - prev_idle
    if delta_total <= 0 or prev_total == 0:
        return 0.0
    return max(0.0, (1.0 - delta_idle / delta_total) * 100.0)


def get_host_memory() -> dict[str, int] | None:
    """Return {used_bytes, total_bytes} parsed from /proc/meminfo.

    `used` = MemTotal − MemAvailable. MemAvailable already accounts
    for reclaimable cache, so this matches how `free -h` reports
    "used" (not the older MemFree-based formula that undercounts).
    """
    try:
        fields: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                tokens = rest.strip().split()
                if not tokens:
                    continue
                try:
                    fields[key] = int(tokens[0]) * 1024  # kB → bytes
                except ValueError:
                    continue
    except OSError:
        return None

    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", 0)
    if total == 0:
        return None
    return {"used": total - available, "total": total}
