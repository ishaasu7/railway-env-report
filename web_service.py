#!/usr/bin/env python3
"""Stdlib-only, read-only Railway environment report service."""
from __future__ import annotations

import datetime as dt
import getpass
import html
import ipaddress
import json
import locale
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
DEFAULT_PORT = 8080
REFRESH_SECONDS = 60
MAX_OUTPUT = 64 * 1024
MAX_CONCURRENT = 16
STARTED_AT = dt.datetime.now(dt.timezone.utc)
START_MONOTONIC = time.monotonic()
CONCURRENCY = threading.BoundedSemaphore(MAX_CONCURRENT)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_text(value):
    return value.strip() if isinstance(value, str) else value


def read_text(path, limit=128 * 1024):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except (OSError, UnicodeError):
        return None


def read_first_line(path):
    text = read_text(path, 4096)
    return text.splitlines()[0].strip() if text else None


def command(argv, timeout=2.0):
    """Run a fixed argv read-only command, never through a shell, with bounds."""
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            shell=False, timeout=timeout, check=False,
        )
        return {"ok": completed.returncode == 0, "returncode": completed.returncode,
                "stdout": completed.stdout[:MAX_OUTPUT], "stderr": completed.stderr[:8192]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": type(exc).__name__}


def parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def bytes_from_proc(value):
    number = parse_int(value)
    return number * 1024 if number is not None else None


def first_existing(paths):
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def os_info():
    result = {"system": platform.system(), "release": platform.release(),
              "version": platform.version(), "architecture": platform.machine(),
              "platform": platform.platform()}
    if os.path.exists("/etc/os-release"):
        fields = {}
        for line in (read_text("/etc/os-release", 16384) or "").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                fields[key] = value.strip().strip('"')
        result["os_release"] = fields
    return result


def cgroup_limits():
    result = {}
    mount = "/sys/fs/cgroup"
    if os.path.isfile(os.path.join(mount, "memory.max")):
        mem = read_first_line(os.path.join(mount, "memory.max"))
        current = read_first_line(os.path.join(mount, "memory.current"))
        cpu_max = read_first_line(os.path.join(mount, "cpu.max"))
        result["version"] = 2
        result["memory_limit_bytes"] = None if mem in (None, "max") else parse_int(mem)
        result["memory_current_bytes"] = parse_int(current)
        if cpu_max:
            parts = cpu_max.split()
            result["cpu_max"] = {"quota": None if parts[0] == "max" else parse_int(parts[0]),
                                  "period": parse_int(parts[1]) if len(parts) > 1 else None}
        cpus = read_first_line(os.path.join(mount, "cpuset.cpus.effective"))
        if cpus:
            result["cpuset_cpus"] = cpus
    else:
        result["version"] = 1 if os.path.exists("/sys/fs/cgroup/memory/memory.limit_in_bytes") else None
        mem = read_first_line("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        current = read_first_line("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if mem:
            result["memory_limit_bytes"] = parse_int(mem)
        result["memory_current_bytes"] = parse_int(current)
        quota = read_first_line("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = read_first_line("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota or period:
            result["cpu_max"] = {"quota": parse_int(quota), "period": parse_int(period)}
    return result


def cpu_info():
    result = {"logical_cpus": os.cpu_count()}
    if hasattr(os, "process_cpu_count"):
        result["process_visible_cpus"] = os.process_cpu_count()
    data = read_text("/proc/cpuinfo", 256 * 1024)
    if data:
        model = re.search(r"^model name\s*:\s*(.+)$", data, re.MULTILINE)
        result["model"] = model.group(1).strip() if model else None
        result["host_visible_cpu_count_proc"] = len(re.findall(r"^processor\s*:", data, re.MULTILINE)) or None
    else:
        result["model"] = platform.processor() or None
    result["cgroup"] = cgroup_limits()
    try:
        result["affinity_cpus"] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        result["affinity_cpus"] = result.get("logical_cpus")
    quota = result["cgroup"].get("cpu_max", {})
    cores = result.get("affinity_cpus") or result.get("logical_cpus")
    if quota.get("quota") and quota.get("quota") > 0 and quota.get("period"):
        cores = min(cores, quota["quota"] / quota["period"]) if cores else quota["quota"] / quota["period"]
    result["effective_cores"] = cores
    result["physical_cores_host_visible"] = None
    if data:
        pairs = set()
        for block in data.split("\n\n"):
            physical = re.search(r"^physical id\s*:\s*(.+)$", block, re.MULTILINE)
            core = re.search(r"^core id\s*:\s*(.+)$", block, re.MULTILINE)
            if physical and core:
                pairs.add((physical.group(1), core.group(1)))
        result["physical_cores_host_visible"] = len(pairs) or None
    return result


def memory_info():
    result = {}
    data = read_text("/proc/meminfo", 64 * 1024)
    if data:
        for line in data.splitlines():
            match = re.match(r"^(MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree):\s+(\d+)\s+kB", line)
            if match:
                result[match.group(1) + "_bytes"] = bytes_from_proc(match.group(2))
    if not result:
        try:
            result["MemTotal_bytes"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, OSError, ValueError):
            pass
    limits = cgroup_limits()
    result["cgroup_memory_limit_bytes"] = limits.get("memory_limit_bytes")
    result["cgroup_memory_current_bytes"] = limits.get("memory_current_bytes")
    host_total = result.get("MemTotal_bytes")
    limit = limits.get("memory_limit_bytes")
    current = limits.get("memory_current_bytes")
    bounded = limit is not None and limit > 0 and (not host_total or limit <= host_total)
    total = limit if bounded else host_total
    used = current if bounded else (host_total - result.get("MemAvailable_bytes", result.get("MemFree_bytes", 0)) if host_total else None)
    result.update({"effective_total_bytes": total, "effective_used_bytes": used,
        "effective_available_bytes": max(0, total - used) if total is not None and used is not None else None,
        "effective_source": "cgroup 容器限制" if bounded else "主机可见内存",
        "swap_used_bytes": max(0, result.get("SwapTotal_bytes", 0) - result.get("SwapFree_bytes", 0))})
    return result


def disks():
    result = {"root": {}, "filesystems": []}
    try:
        usage = shutil.disk_usage("/")
        result["root"] = {"path": "/", "total_bytes": usage.total,
                           "used_bytes": usage.used, "free_bytes": usage.free,
                           "use_percent": round(usage.used / usage.total * 100, 2) if usage.total else None}
        stats = os.statvfs("/")
        if stats.f_files:
            result["root"].update({"inodes_total": stats.f_files, "inodes_free": stats.f_ffree,
                "inodes_used": stats.f_files - stats.f_ffree})
    except OSError as exc:
        result["root"] = {"error": type(exc).__name__}
    df = command(["df", "-P", "-T"])
    if df.get("ok"):
        for line in df["stdout"].splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 7 and parse_int(fields[2]) is not None:
                result["filesystems"].append({"filesystem": fields[0], "type": fields[1],
                    "blocks_1k": parse_int(fields[2]), "used_1k": parse_int(fields[3]),
                    "available_1k": parse_int(fields[4]), "use_percent": fields[5],
                    "mount": " ".join(fields[6:])})
    return result


def load_uptime():
    result = {}
    uptime = read_first_line("/proc/uptime")
    if uptime:
        try:
            result["uptime_seconds"] = float(uptime.split()[0])
            result["boot_time_utc"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=result["uptime_seconds"])).isoformat()
        except (IndexError, ValueError):
            pass
    load = read_first_line("/proc/loadavg")
    if load:
        parts = load.split()
        result["load_average"] = parts[:3]
        result["running_processes"] = parts[3] if len(parts) > 3 else None
    return result


def locale_timezone():
    # Deliberately do not inspect os.environ: no LANG, LC_ALL, TZ, or other
    # environment values are exposed.  Locale selection and /etc/localtime are
    # safe system metadata sources for this report.
    try:
        loc = locale.setlocale(locale.LC_TIME)
    except locale.Error:
        loc = None
    tz = None
    link = first_existing(["/etc/localtime"])
    if link:
        try:
            tz = os.path.realpath(link).split("/zoneinfo/", 1)[-1]
        except OSError:
            pass
    local_timestamp = dt.datetime.now().astimezone().isoformat()
    return {"locale": loc, "timezone": tz, "local_timestamp": local_timestamp,
            "utc_timestamp": utc_now()}


def cli_paths():
    names = ["python3", "python", "pip3", "pip", "git", "curl", "wget", "nginx", "apache2", "httpd",
             "docker", "podman", "systemctl", "ss", "ip", "iptables", "ufw", "psql", "mysql", "redis-server",
             "node", "npm", "bash", "tar", "unzip", "openssl", "java", "gcc"]
    version_flags = {"python3": ["--version"], "python": ["--version"], "pip3": ["--version"], "pip": ["--version"],
                     "node": ["--version"], "npm": ["--version"], "bash": ["--version"], "tar": ["--version"],
                     "unzip": ["-v"], "openssl": ["version"], "java": ["-version"], "gcc": ["--version"],
                     "ip": ["-Version"], "ss": ["-V"], "nginx": ["-v"], "apache2": ["-v"], "httpd": ["-v"]}
    result = {}
    for name in names:
        path = shutil.which(name)
        result[name] = {"path": path}
        if path:
            check = command([path] + version_flags.get(name, ["--version"]), timeout=1.5)
            lines = (check.get("stdout") or check.get("stderr") or "").strip().splitlines()
            result[name]["version"] = lines[0][:180] if check.get("ok") and lines else None
    return result


def interfaces_routes_dns():
    result = {"interfaces": [], "routes": [], "dns_nameservers": []}
    ip = shutil.which("ip")
    result["ip_command_available"] = bool(ip)
    if ip:
        addr = command([ip, "-o", "addr", "show"])
        if addr.get("ok"):
            for line in addr["stdout"].splitlines():
                fields = line.split()
                if len(fields) >= 4:
                    result["interfaces"].append({"index": fields[0], "name": fields[1], "family": fields[2], "address": fields[3].split("/", 1)[0]})
        route = command([ip, "-o", "route", "show"])
        if route.get("ok"):
            result["routes"] = [line[:500] for line in route["stdout"].splitlines()]
    else:
        dev = read_text("/proc/net/dev", 64 * 1024)
        if dev:
            for line in dev.splitlines()[2:]:
                if ":" in line and line.split(":", 1)[0].strip():
                    result["interfaces"].append({"name": line.split(":", 1)[0].strip(), "source": "/proc/net/dev"})
        route_data = read_text("/proc/net/route", 64 * 1024)
        if route_data:
            for line in route_data.splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 8:
                    try:
                        result["routes"].append({"interface": fields[0], "destination": str(ipaddress.IPv4Address(bytes.fromhex(fields[1])[::-1])),
                            "gateway": str(ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1])), "flags": fields[3],
                            "metric": parse_int(fields[6]), "mask": str(ipaddress.IPv4Address(bytes.fromhex(fields[7])[::-1])), "source": "/proc/net/route"})
                    except (ValueError, IndexError):
                        pass
    resolv = read_text("/etc/resolv.conf", 16384)
    if resolv:
        result["dns_nameservers"] = [m.group(1) for m in (re.match(r"\s*nameserver\s+(\S+)", x) for x in resolv.splitlines()) if m]
    return result


def listeners():
    result = {"note": "These are local candidate listeners only; they do not establish public reachability.", "endpoints": []}
    ss = shutil.which("ss")
    if ss:
        for proto, flag in (("tcp", "-ltn"), ("udp", "-lun")):
            data = command([ss, flag])
            if data.get("ok"):
                for line in data["stdout"].splitlines()[1:]:
                    fields = line.split()
                    if len(fields) >= 4:
                        endpoint = {"protocol": proto, "local": fields[3]}
                        if proto == "udp": endpoint["classification"] = "bound_unconnected_candidate"
                        result["endpoints"].append(endpoint)
        return result
    for proto, path in (("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6"), ("udp", "/proc/net/udp"), ("udp6", "/proc/net/udp6")):
        data = read_text(path, 128 * 1024) or ""
        for line in data.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or (proto.startswith("tcp") and fields[3] != "0A"): continue
            try:
                address_hex, port_hex = fields[1].split(":")
                if proto in ("tcp", "udp"):
                    address = str(ipaddress.IPv4Address(bytes.fromhex(address_hex)[::-1]))
                else:
                    chunks = [bytes.fromhex(address_hex[i:i + 8])[::-1] for i in range(0, 32, 8)]
                    address = str(ipaddress.IPv6Address(b"".join(chunks)))
                endpoint = {"protocol": proto, "local": "%s:%d" % (address, int(port_hex, 16))}
                if proto.startswith("udp"): endpoint["classification"] = "bound_unconnected_candidate"
                result["endpoints"].append(endpoint)
            except (ValueError, IndexError):
                pass
    return result


def collect_egress():
    url = "https://api.ipify.org?format=json"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "railway-env-report/1"}, method="GET")
        with urllib.request.urlopen(request, timeout=3.0, context=ssl.create_default_context()) as response:
            parsed = json.loads(response.read(4096).decode("utf-8", "replace"))
            value = parsed.get("ip") if isinstance(parsed, dict) else None
            try: ipaddress.ip_address(value)
            except (ValueError, TypeError): value = None
            if not value: return {"ok": False, "error": "unavailable", "service": url}
            return {"ok": True, "ip": value, "service": url}
    except Exception:
        return {"ok": False, "error": "unavailable", "service": url}


def port_from_env():
    for key in ("PORT", "SERVER_PORT"):
        value = os.environ.get(key, "").strip()
        if value.isdigit() and 1 <= int(value) <= 65535:
            return int(value)
    return DEFAULT_PORT


_ACTIVITY_PREVIOUS = {}


def activity_sample(effective_cores):
    """Nonblocking interval counters; first sample intentionally has no rate."""
    global _ACTIVITY_PREVIOUS
    now = time.monotonic()
    current = {"monotonic": now, "interfaces": {}}
    cpu = read_first_line("/proc/stat")
    if cpu and cpu.startswith("cpu "):
        try:
            values = [int(x) for x in cpu.split()[1:9]]
            current["host_total"] = sum(values)
            current["host_idle"] = values[3] + (values[4] if len(values) > 4 else 0)
        except (ValueError, IndexError):
            pass
    stat = read_text("/sys/fs/cgroup/cpu.stat", 8192) or ""
    match = re.search(r"^usage_usec\s+(\d+)", stat, re.MULTILINE)
    if match:
        current["container_cpu_seconds"] = int(match.group(1)) / 1000000
    else:
        value = parse_int(read_first_line("/sys/fs/cgroup/cpuacct/cpuacct.usage"))
        if value is not None:
            current["container_cpu_seconds"] = value / 1000000000
    for line in (read_text("/proc/net/dev", 65536) or "").splitlines()[2:]:
        if ":" not in line:
            continue
        name, counters = line.split(":", 1)
        fields = counters.split()
        if len(fields) >= 16:
            try:
                current["interfaces"][name.strip()] = {"rx_bytes": int(fields[0]), "tx_bytes": int(fields[8]),
                    "rx_packets": int(fields[1]), "tx_packets": int(fields[9]),
                    "rx_errors": int(fields[2]), "tx_errors": int(fields[10])}
            except ValueError:
                pass
    previous = _ACTIVITY_PREVIOUS
    elapsed = now - previous.get("monotonic", now)
    result = {"usage_percent": None, "host_usage_percent": None, "sample_interval_seconds": round(elapsed, 3) if elapsed > 0 else None,
              "interface_counters": []}
    if elapsed > 0:
        if "container_cpu_seconds" in current and "container_cpu_seconds" in previous and effective_cores:
            delta = current["container_cpu_seconds"] - previous["container_cpu_seconds"]
            if delta >= 0:
                result["usage_percent"] = round(delta / elapsed / effective_cores * 100, 2)
        if "host_total" in current and "host_total" in previous:
            total = current["host_total"] - previous["host_total"]
            idle = current["host_idle"] - previous["host_idle"]
            if total > 0:
                result["host_usage_percent"] = round(max(0, min(100, (total - idle) / total * 100)), 2)
    for name, values in current["interfaces"].items():
        entry = {"name": name, **values, "rx_bytes_per_second": None, "tx_bytes_per_second": None}
        old = previous.get("interfaces", {}).get(name)
        if old and elapsed > 0:
            for direction in ("rx", "tx"):
                delta = values[direction + "_bytes"] - old[direction + "_bytes"]
                if delta >= 0:
                    entry[direction + "_bytes_per_second"] = round(delta / elapsed, 2)
        result["interface_counters"].append(entry)
    _ACTIVITY_PREVIOUS = current
    return result


def collect_report(egress):
    began = time.monotonic()
    cpu = cpu_info()
    activity = activity_sample(cpu.get("effective_cores"))
    cpu.update({key: activity[key] for key in ("usage_percent", "host_usage_percent", "sample_interval_seconds")})
    report = {
        "report": {"name": "railway-environment", "schema_version": 1, "presentation_version": 2, "generated_at_utc": utc_now()},
        "os": os_info(),
        "kernel": {"system": platform.system(), "release": platform.release(), "version": platform.version()},
        "architecture": platform.machine(), "hostname": socket.gethostname(),
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                    "executable": sys.executable, "prefix": sys.prefix},
        "user_identity": {"uid": getattr(os, "getuid", lambda: None)(), "gid": getattr(os, "getgid", lambda: None)(),
                          "username": safe_text(getpass.getuser())},
        "memory": memory_info(), "cpu": cpu, "disks": disks(), "load_and_uptime": load_uptime(),
        "locale_timezone_time": locale_timezone(), "common_cli": cli_paths(),
        "network": {"local_interfaces_routes_dns": interfaces_routes_dns(), "local_listeners": listeners(),
                     "public_egress_ip": egress,
                     "interface_counters": activity["interface_counters"],
                     "port_allocation": {"service_port": port_from_env(), "source": "现有启动配置（不是可达性检测）",
                         "platform_allocated_port": None, "platform_allocation_source": None,
                         "public_url": None,
                         "external_reachability": "采集器不探测外部入站；本地监听与分配端口不能证明公网可达。HTTPS 页面由平台网关提供。"}},
        "limitations_and_notes": [
            "Read-only inventory; no configuration, services, files, or packages are modified.",
            "Only selected non-secret metadata is collected; environment variables, process arguments, private configuration contents, and tokens are not enumerated.",
            "Container cgroup limits may differ from host-visible CPU and RAM values; unavailable fields are omitted or null.",
            "Load average, uptime, and root filesystem statistics may be host-visible or shared values in a container; panel allocations are not necessarily the same as df filesystem size.",
            "Local listener entries are candidate ports only and do not prove public reachability or firewall exposure.",
            "The public egress-IP check is a single HTTPS request to api.ipify.org with a short timeout; its result may be unavailable due to network policy.",
            "Some commands, procfs paths, filesystem types, and networking data are platform- or permission-dependent.",
        ],
    }
    report["report"]["collector_duration_seconds"] = round(time.monotonic() - began, 3)
    return report


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.egress = collect_egress()
        self.report = collect_report(self.egress)
        self.refreshed = utc_now()
        self.last_error = None

    def refresh(self):
        try:
            report = collect_report(self.egress)
            with self.lock:
                self.report, self.refreshed, self.last_error = report, utc_now(), None
        except Exception:
            with self.lock: self.last_error = "refresh_failed"

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.report, ensure_ascii=False)), self.refreshed, self.last_error


STATE = State()


def refresh_loop():
    while True:
        time.sleep(REFRESH_SECONDS)
        STATE.refresh()


"""Server-rendered, stdlib-only Chinese dashboard for Railway environment reports."""

import datetime as _dt
import html as _html
import json as _json
import math as _math
from typing import Any



_REFRESH_SECONDS = 60
_SOURCE_REPOSITORY_URL = 'https://github.com/ishaasu7/railway-env-report'


def _e(value: Any) -> str:
    """Escape every rendered value, including values placed in attributes."""
    if value is None:
        value = "—"
    return _html.escape(str(value), quote=True)


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(obj, dict) and key in obj and obj[key] is not None:
            return obj[key]
    return default


def _num(value: Any) -> float | None:
    try:
        result = float(value)
        return result if _math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _bytes(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    amount = abs(number)
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        number /= 1024.0
        amount /= 1024.0
        index += 1
    precision = 0 if abs(number) >= 100 else 1
    return f"{number:,.{precision}f} {units[index]}"


def _percent(value: Any) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:.1f}%"


def _duration(value: Any) -> str:
    seconds = _num(value)
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours or days:
        parts.append(f"{hours} 小时")
    if minutes or hours or days:
        parts.append(f"{minutes} 分钟")
    if not parts:
        parts.append(f"{secs} 秒")
    return " ".join(parts)


def _time(value: Any) -> str:
    if value is None:
        return "—"
    raw = str(value)
    try:
        parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        shanghai = parsed.astimezone(_dt.timezone(_dt.timedelta(hours=8)))
        return f"{shanghai:%Y-%m-%d %H:%M:%S}（北京时间 UTC+08:00）"
    except (TypeError, ValueError, OverflowError):
        return raw


def _raw_time(value: Any) -> str:
    return "—" if value is None else str(value)


def _cell(value: Any, cls: str = "") -> str:
    klass = f' class="{_e(cls)}"' if cls else ""
    return f"<td{klass}>{_e(value)}</td>"


def _table(headers: list[str], rows: list[list[Any]], empty: str = "暂无数据") -> str:
    head = "".join(f"<th scope=\"col\">{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(_cell(v) for v in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr><td class="empty" colspan="{max(1, len(headers))}">{_e(empty)}</td></tr>'
    return f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _card(title: str, content: str, eyebrow: str = "") -> str:
    label = f'<p class="eyebrow">{_e(eyebrow)}</p>' if eyebrow else ""
    return f'<section class="card"><h2>{_e(title)}</h2>{label}{content}</section>'


def _kv(items: list[tuple[str, Any]]) -> str:
    return '<dl class="kv">' + "".join(
        f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in items
    ) + "</dl>"


def _bar(label: str, value: Any, detail: str = "") -> str:
    number = _num(value)
    width = max(0.0, min(100.0, number or 0.0))
    return (f'<div class="bar-row"><div class="bar-label"><span>{_e(label)}</span>'
            f'<strong>{_e(_percent(value))}</strong></div><div class="bar"><i style="width:{width:.1f}%"></i></div>'
            f'<small>{_e(detail)}</small></div>')


def _os_card(report: dict[str, Any]) -> str:
    osinfo = report.get("os") or {}
    release = _get(osinfo, "os_release", default={}) or {}
    pretty_os = _get(release, "PRETTY_NAME", "NAME", default=_get(osinfo, "system"))
    kernel = report.get("kernel") or {}
    items = [("操作系统", pretty_os), ("发行版版本", _get(release, "VERSION", "VERSION_ID", default="—")),
             ("内核", f"{_get(kernel, 'system', default='')} {_get(kernel, 'release', default='')}".strip()),
             ("内核版本", _get(kernel, "version")), ("架构", report.get("architecture")),
             ("主机名", report.get("hostname"))]
    runtime = report.get("python") or {}
    identity = report.get("user_identity") or {}
    items += [("Python 运行时", f"{runtime.get('implementation', 'Python')} {runtime.get('version', '—')}"),
              ("运行用户 / UID / GID", f"{identity.get('username', '—')} / {identity.get('uid', '—')} / {identity.get('gid', '—')}")]
    return _card("系统概览", _kv(items), "运行环境")


def _compute_cards(report: dict[str, Any]) -> str:
    cpu = report.get("cpu") or {}
    cg = _get(cpu, "cgroup", default={}) or {}
    effective = _get(cpu, "effective_cores", default=None)
    visible = _get(cpu, "logical_cpus", "process_visible_cpus", "host_visible_cpu_count_proc")
    cpu_items = [("CPU 型号", _get(cpu, "model")), ("有效 CPU 配额", str(effective) + " 核" if effective is not None else "未单独提供"),
                 ("主机可见逻辑 CPU", visible), ("主机可见物理核心", _get(cpu, "physical_cores_host_visible")),
                 ("容器 CPU 使用率", _percent(_get(cpu, "usage_percent")) if _get(cpu, "usage_percent") is not None else "等待首个采样间隔（约 60 秒）"), ("主机 CPU 使用率", _percent(_get(cpu, "host_usage_percent"))),
                 ("cgroup 版本", _get(cg, "version")),
                 ("CPU 配额 / 周期", f"{_get(cg, 'cpu_max', default={}).get('quota', '—')} / {_get(cg, 'cpu_max', default={}).get('period', '—')}" if isinstance(_get(cg, 'cpu_max', default={}), dict) else "—")]
    mem = report.get("memory") or {}
    total = _get(mem, "MemTotal_bytes", "total_bytes")
    avail = _get(mem, "MemAvailable_bytes", "available_bytes")
    effective_total = _get(mem, "effective_total_bytes", default=None)
    effective_used = _get(mem, "effective_used_bytes", default=None)
    effective_avail = _get(mem, "effective_available_bytes", default=None)
    limit = _get(mem, "effective_limit_bytes", "cgroup_memory_limit_bytes", default=None)
    current = _get(mem, "effective_current_bytes", "cgroup_memory_current_bytes", default=None)
    host_used = None
    if total is not None and avail is not None:
        host_used = (_num(total) - _num(avail)) / _num(total) * 100 if _num(total) else None
    if effective_total is not None:
        effective_detail = f"已用 {_bytes(effective_used)} / 有效总量 {_bytes(effective_total)}；来源 {_get(mem, 'effective_source', default='—')}"
        effective_pct = (_num(effective_used) / _num(effective_total) * 100) if _num(effective_used) is not None and _num(effective_total) else None
    else:
        effective_detail = f"当前 {_bytes(current)} / 上限 {_bytes(limit)}" if current is not None or limit is not None else "未提供容器有效上限"
        effective_pct = (_num(current) / _num(limit) * 100) if _num(current) is not None and _num(limit) else None
    mem_content = _bar("有效容器内存使用", effective_pct, effective_detail)
    mem_content += _bar("主机可见内存使用", host_used, f"总量 {_bytes(total)}；可用 {_bytes(avail)}")
    mem_content += _kv([("主机可见总量", _bytes(total)), ("有效总量", _bytes(effective_total if effective_total is not None else limit)),
                        ("有效可用", _bytes(effective_avail)), ("Swap 总量", _bytes(_get(mem, "SwapTotal_bytes"))),
                        ("Swap 已用", _bytes(_get(mem, "swap_used_bytes"))), ("Swap 可用", _bytes(_get(mem, "SwapFree_bytes")))])
    return '<div class="grid two">' + _card("CPU 资源", _kv(cpu_items), "有效配额与主机可见值分开显示") + _card("内存与 Swap", mem_content, "容器有效值优先；主机值仅作可见范围") + '</div>'


def _load_card(report: dict[str, Any], started_at: Any, service_uptime: Any) -> str:
    load = report.get("load_and_uptime") or {}
    averages = _get(load, "load_average", default=[])
    avg_text = " / ".join(str(x) for x in averages) if isinstance(averages, (list, tuple)) else averages
    boot = _get(load, "boot_time_utc")
    clock = report.get("locale_timezone_time") or {}
    content = _kv([("主机可见负载（1/5/15 分钟）", avg_text), ("可见任务（运行 / 总数）", _get(load, "running_processes")),
                   ("主机可见运行时间", _duration(_get(load, "uptime_seconds"))), ("主机可见启动时间", _time(boot)),
                   ("服务器时区", clock.get("timezone")), ("语言环境 Locale", clock.get("locale")),
                   ("服务启动", _time(started_at)), ("服务运行时间", _duration(service_uptime))])
    return _card("负载与时间", content, "时间显示为 Asia/Shanghai（北京时间）；主机数据可能共享")


def _disk_card(report: dict[str, Any]) -> str:
    disks = report.get("disks") or {}
    root = disks.get("root") or {}
    rows = []
    for fs in disks.get("filesystems", []) or []:
        rows.append([fs.get("mount"), fs.get("type"), _bytes(_num(fs.get("blocks_1k")) * 1024 if _num(fs.get("blocks_1k")) is not None else None),
                     _bytes(_num(fs.get("used_1k")) * 1024 if _num(fs.get("used_1k")) is not None else None),
                     _bytes(_num(fs.get("available_1k")) * 1024 if _num(fs.get("available_1k")) is not None else None), fs.get("use_percent")])
    if not rows and root:
        rows = [[root.get("path", "/"), "—", _bytes(root.get("total_bytes")), _bytes(root.get("used_bytes")), _bytes(root.get("free_bytes")), _percent(root.get("use_percent"))]]
    table = _table(["挂载点", "文件系统", "总量", "已用", "可用", "使用率"], rows)
    inodes = disks.get("inodes") or []
    if isinstance(inodes, dict):
        inodes = [inodes]
    if inodes:
        table += _table(["挂载点", "inode 总数", "已用", "可用", "使用率"], [[x.get("mount"), x.get("total"), x.get("used"), x.get("free"), x.get("use_percent")] for x in inodes])
    elif root and any(root.get(k) is not None for k in ("inodes_total", "inodes_used", "inodes_free")):
        table += _table(["挂载点", "inode 总数", "已用", "可用", "使用率"], [[root.get("path", "/"), root.get("inodes_total"), root.get("inodes_used"), root.get("inodes_free"), _percent(root.get("inodes_used", 0) / root.get("inodes_total") * 100) if root.get("inodes_total") else "—"]])
    return _card("磁盘与挂载点", table, "df 视角；容器中可能是共享或主机可见值")


def _network_card(report: dict[str, Any]) -> str:
    net = report.get("network") or {}
    local = net.get("local_interfaces_routes_dns") or {}
    interfaces = local.get("interfaces") or []
    rows = [[x.get("name", x.get("index")), x.get("family"), x.get("address"), x.get("source", "ip/procfs")] for x in interfaces]
    content = _table(["接口", "族", "地址", "来源"], rows)
    egress = net.get("public_egress_ip") or {}
    egress_text = egress.get("ip") if egress.get("ok") else "不可用（未声称公网可达）"
    dns = local.get("dns_nameservers") or []
    content += _kv([("公网出口 IP", egress_text), ("出口探测服务", egress.get("service")), ("DNS", ", ".join(map(str, dns)) or "—")])
    routes = local.get("routes") or []
    content += '<h3>路由</h3>' + _table(["路由条目"], [[r] for r in routes])
    counters = net.get("interface_counters") or []
    if counters:
        content += '<h3>接口计数器</h3>' + _table(["接口", "接收", "发送", "接收速率", "发送速率"], [[x.get("name"), _bytes(x.get("rx_bytes")), _bytes(x.get("tx_bytes")), (_bytes(x.get("rx_bytes_per_second")) + "/秒") if x.get("rx_bytes_per_second") is not None else "待采样", (_bytes(x.get("tx_bytes_per_second")) + "/秒") if x.get("tx_bytes_per_second") is not None else "待采样"] for x in counters])
    return _card("网络接口与出口", content, "公网出口探测不等于入站公网可达")


def _listeners_card(report: dict[str, Any]) -> str:
    net = report.get("network") or {}
    listener = net.get("local_listeners") or {}
    rows = [[x.get("protocol"), x.get("local"), x.get("classification", "本地候选监听")] for x in listener.get("endpoints", []) or []]
    content = '<p class="notice">本地监听仅表示进程在此环境绑定；不能证明防火墙、NAT 或公网入站可达。</p>' + _table(["协议", "本地端点", "分类"], rows)
    allocation = net.get("port_allocation") or {}
    if allocation:
        content += '<h3>应用端口分配</h3>' + _kv([("服务监听端口", allocation.get("service_port")), ("面板分配端口", allocation.get("platform_allocated_port")), ("来源", allocation.get("source")), ("外部可达性", allocation.get("external_reachability", "未验证")), ("配置公网 URL", allocation.get("public_url"))])
    return _card("监听与端口", content, "候选监听与已验证可达性严格区分")


def _cli_card(report: dict[str, Any]) -> str:
    cli = report.get("common_cli") or {}
    available = [[name, data.get("version") or "版本未提供", data.get("path")] for name, data in cli.items() if isinstance(data, dict) and data.get("path")]
    missing = [name for name, data in cli.items() if not isinstance(data, dict) or not data.get("path")]
    content = _table(["命令", "版本", "路径"], available)
    if missing:
        content += f'<details class="fold"><summary>未在 PATH 找到（{len(missing)}）</summary><p>{_e(", ".join(missing))}</p></details>'
    return _card("CLI 工具", content, "仅展示命令可用性与版本；不展示环境变量或进程参数")


def render_dashboard(report, refreshed, error, started_at, service_uptime) -> str:
    """Render a complete human-readable dashboard without scripts or external assets."""
    report = report if isinstance(report, dict) else {}
    generated = _get(report.get("report") or {}, "generated_at_utc")
    status = "正常" if not error else f"刷新异常：{error}"
    status_class = "ok" if not error else "warn"
    pretty = _json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    body = (_os_card(report) + _compute_cards(report) + _load_card(report, started_at, service_uptime) +
            '<div class="grid two">' + _disk_card(report) + _network_card(report) + '</div>' +
            '<div class="grid two">' + _listeners_card(report) + _cli_card(report) + '</div>')
    collector_duration = _get(report.get("report") or {}, "collector_duration_seconds")
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<meta name="color-scheme" content="dark"><title>Railway 环境报告</title>
<style>
:root{{color-scheme:dark;--bg:#08111f;--panel:#101c2e;--panel2:#13243a;--line:#263a55;--text:#e8f0fb;--muted:#91a4bd;--cyan:#58d6e6;--indigo:#8992ff;--good:#63ddb0;--warn:#ffcb70}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(1000px 500px at 15% -10%,#172d52 0,transparent 62%),var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif}}main{{max-width:1220px;margin:auto;padding:28px 18px 56px}}header{{padding:20px 0 26px;border-bottom:1px solid var(--line);margin-bottom:22px}}h1{{font-size:clamp(1.8rem,4vw,2.8rem);letter-spacing:-.03em;margin:0 0 6px}}h2{{font-size:1.08rem;margin:0 0 16px;color:#f5f8ff}}h3{{font-size:.9rem;color:var(--muted);margin:18px 0 8px}}p{{margin:7px 0}}.sub,.eyebrow,small{{color:var(--muted)}}.eyebrow{{font-size:.74rem;letter-spacing:.1em;text-transform:uppercase;margin-top:-9px}}.grid{{display:grid;gap:16px;margin:16px 0}}.grid.two{{grid-template-columns:repeat(2,minmax(0,1fr))}}.card{{background:linear-gradient(145deg,rgba(19,36,58,.96),rgba(13,26,44,.96));border:1px solid var(--line);border-radius:15px;padding:20px;box-shadow:0 12px 35px #02091455;min-width:0}}.kv{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px 18px;margin:0}}.kv div{{padding:9px 0;border-bottom:1px solid #263a554d;min-width:0}}dt{{font-size:.77rem;color:var(--muted)}}dd{{margin:2px 0 0;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse;font-size:.88rem}}.table-wrap{{overflow-x:auto}}th{{color:var(--muted);font-weight:600;text-align:left;white-space:nowrap}}td,th{{padding:9px 8px;border-bottom:1px solid #263a554d;vertical-align:top}}td{{overflow-wrap:anywhere}}.empty{{color:var(--muted);text-align:center;padding:18px}}.bar-row{{margin:12px 0 16px}}.bar-label{{display:flex;justify-content:space-between;gap:12px;margin-bottom:5px}}.bar-label strong{{color:var(--cyan)}}.bar{{height:7px;background:#07101d;border-radius:99px;overflow:hidden}}.bar i{{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--indigo),var(--cyan))}}.status{{display:inline-flex;align-items:center;gap:7px;border:1px solid #63ddb055;border-radius:99px;color:var(--good);padding:3px 10px;font-size:.8rem}}.status.warn{{color:var(--warn);border-color:#ffcb7055}}.status::before{{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}}.meta{{display:flex;flex-wrap:wrap;gap:8px 20px;color:var(--muted);font-size:.86rem;margin-top:15px}}.meta b{{color:var(--text);font-weight:500}}.links a{{color:var(--cyan);text-decoration:none;margin-right:14px}}.notice{{background:#0a1627;border-left:3px solid var(--warn);padding:9px 12px;color:#c8d5e6;border-radius:5px;font-size:.88rem}}details.fold{{margin-top:13px;border-top:1px solid var(--line);padding-top:10px}}summary{{cursor:pointer;color:var(--cyan)}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-height:600px;overflow:auto;background:#07101d;border:1px solid var(--line);border-radius:9px;padding:14px;font-size:12px;color:#c8d5e6}}footer{{color:var(--muted);font-size:.82rem;margin-top:22px}}@media(max-width:760px){{main{{padding:18px 12px 40px}}.grid.two{{grid-template-columns:1fr}}.kv{{grid-template-columns:1fr}}.card{{padding:16px}}td,th{{padding:8px 6px}}}}
</style></head><body><main><header><h1>Railway 环境报告</h1><p class="sub">只读、无认证公开报告 · 面向人阅读的运行环境摘要</p><span class="status {status_class}">{_e(status)}</span><div class="meta"><span>报告生成 <b>{_e(_time(generated))}</b></span><span>最近刷新 <b>{_e(_time(refreshed))}</b></span><span>刷新周期 <b>每 {_REFRESH_SECONDS} 秒</b></span><span>本次采集 <b>{_e(collector_duration)} 秒</b></span></div><p class="links"><a href="/report.json">查看 JSON 报告</a><a href="/healthz">健康检查 /healthz</a><a href="{_e(_SOURCE_REPOSITORY_URL)}" target="_blank" rel="noopener noreferrer">GitHub 开源代码</a></p></header>{body}<section class="card"><h2>原始 JSON</h2><details><summary>展开完整报告</summary><pre>{_e(pretty)}</pre></details></section><footer>容量按 1024 进制自动换算（1 KB = 1024 B，1 GB = 1024 MB），速率为两次采样之间的平均值；首轮显示为待采样。安全提示：本页保持只读；不展示环境变量、密钥、私有配置内容或进程参数。公网可达性不会仅因本地监听而被推断。</footer></main></body></html>'''



def render_html(report, refreshed, error):
    uptime = round(time.monotonic() - START_MONOTONIC, 3)
    service = {"started_utc": STARTED_AT.isoformat(), "uptime_seconds": uptime,
               "report_refreshed_utc": refreshed, "refresh_error": error, "public_no_auth": True}
    return render_dashboard(report | {"service": service}, refreshed, error, STARTED_AT.isoformat(), uptime)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RailwayEnv/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass

    def do_GET(self): self.dispatch(True)
    def do_HEAD(self): self.dispatch(False)

    def reject_write(self):
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    do_POST = reject_write
    do_PUT = reject_write
    do_PATCH = reject_write
    do_DELETE = reject_write
    do_OPTIONS = reject_write

    def dispatch(self, body):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                data, ctype, status = {"ok": True, "service": "railway-env-report"}, "application/json; charset=utf-8", 200
            elif path == "/report.json":
                report, refreshed, error = STATE.snapshot()
                data = report | {"service": {"started_utc": STARTED_AT.isoformat(), "uptime_seconds": round(time.monotonic() - START_MONOTONIC, 3),
                                                "report_refreshed_utc": refreshed, "refresh_error": error, "public_no_auth": True}}
                ctype, status = "application/json; charset=utf-8", 200
            elif path == "/":
                report, refreshed, error = STATE.snapshot()
                data, ctype, status = render_html(report, refreshed, error), "text/html; charset=utf-8", 200
            else:
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = data.encode("utf-8") if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            if body: self.wfile.write(payload)
        except Exception:
            try:
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass


class BoundedHTTPServer(ThreadingHTTPServer):
    def process_request_thread(self, request, client_address):
        if not CONCURRENCY.acquire(blocking=False):
            try: request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError: pass
            finally: request.close()
            return
        try: super().process_request_thread(request, client_address)
        finally: CONCURRENCY.release()


def main():
    threading.Thread(target=refresh_loop, daemon=True, name="report-refresh").start()
    server = BoundedHTTPServer((HOST, port_from_env()), Handler)
    server.daemon_threads = True
    print(f"Railway environment report listening on 0.0.0.0:{server.server_address[1]} (read-only)", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
