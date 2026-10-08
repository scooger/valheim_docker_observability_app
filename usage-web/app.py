#!/usr/bin/env python3
"""Valheim usage UI: sessions, NPS latency, host/container resources."""

from __future__ import annotations

import base64
import gzip
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

EVENTS_FILE = Path(os.environ.get("EVENTS_FILE", "/data/player-events.log"))
NPS_DIR = Path(os.environ.get("NPS_DIR", "/data/nps"))
METRICS_FILE = Path(os.environ.get("METRICS_FILE", "/data/metrics/metrics.jsonl"))
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
VALHEIM_CONTAINER = os.environ.get("VALHEIM_CONTAINER", "valheim-server")
HOST_PROC = Path(os.environ.get("HOST_PROC", "/host/proc"))
HOST = os.environ.get("BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
CACHE_SECONDS = float(os.environ.get("CACHE_SECONDS", "2"))
METRICS_INTERVAL = float(os.environ.get("METRICS_INTERVAL", "15"))
METRICS_KEEP = int(os.environ.get("METRICS_KEEP", "2880"))  # 15s * 2880 ≈ 12h
RESOURCE_WINDOWS_MIN = (10, 30, 90, 180, 720)
DEFAULT_WINDOW_MIN = 30
SPARK_POINTS = 480

def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


# Master switch for internet access to the usage UI (LAN is always open).
# true  → WAN: 404 while empty; auth (see USAGE_AUTH_MODE) while a player is online
# false → WAN always 404 (leave TCP 8088 unforwarded, or forward and stay closed)
USAGE_PUBLIC_ACCESS = _env_bool("USAGE_PUBLIC_ACCESS", "false")
# WAN password after a player is online:
#   server_pass → Valheim SERVER_PASS (default)
#   override    → USAGE_PASS (custom dashboard password)
#   off         → no password (session presence only)
USAGE_AUTH_MODE = os.environ.get("USAGE_AUTH_MODE", "server_pass").strip().lower()
USAGE_PASS = os.environ.get("USAGE_PASS", "").strip()
TRUST_PROXY = _env_bool("TRUST_PROXY", "false")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()
SERVER_PASS = os.environ.get("SERVER_PASS", "").strip()
DENIED_BODY = b"Not Found\n"


def _usage_password() -> str | None:
    """Password required for WAN Basic Auth, or None if auth is off / unavailable."""
    if USAGE_AUTH_MODE == "off":
        return None
    if USAGE_AUTH_MODE == "override":
        return USAGE_PASS or None
    # server_pass (default) and any unknown value
    return SERVER_PASS or None
# NPS RTT below this (ms) is treated as LAN; at/above as WAN. No client IPs available.
LAN_RTT_MS = float(os.environ.get("LAN_RTT_MS", "8"))

RE_STEAM = re.compile(r"(?:SteamID|from client|Closing socket)\s+(\d{14,})")
RE_CHARACTER = re.compile(r"Got character ZDOID from (.+?) :\s*(\d+):\d+")

_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "payload": None}
_metrics_lock = threading.Lock()
_metrics: list[dict[str, Any]] = []


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_dur(sec: int | None) -> str | None:
    if sec is None:
        return None
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m"
    return f"{m}m {s:02d}s"


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"min": None, "max": None, "avg": None, "samples": 0}
    return {
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "avg": round(sum(values) / len(values), 1),
        "samples": len(values),
    }


@dataclass
class OnlinePlayer:
    steam_id: str
    character: str | None
    connected_at: str
    handshake_at: str | None = None


@dataclass
class SessionRecord:
    id: str
    steam_id: str
    character: str | None
    connected_at: str
    disconnected_at: str | None
    duration_seconds: int | None
    short_session: bool = False
    zdoid: str | None = None
    latency_ms: dict[str, Any] = field(default_factory=dict)
    nps_peer_id: str | None = None
    network: str = "unknown"  # lan | wan | unknown (RTT heuristic)
    sock: str | None = None
    events: list[dict[str, str]] = field(default_factory=list)
    rtt_series: list[dict[str, Any]] = field(default_factory=list)


def _classify_network(latency_ms: dict[str, Any], sock: str | None = None) -> str:
    """Best-effort LAN/WAN label from NPS RTT (and non-direct sock ⇒ WAN)."""
    sock_l = (sock or "").strip().lower()
    if sock_l and sock_l not in ("direct", "none", "unknown"):
        return "wan"
    avg = latency_ms.get("avg")
    if avg is None:
        return "unknown"
    try:
        return "lan" if float(avg) < LAN_RTT_MS else "wan"
    except (TypeError, ValueError):
        return "unknown"


def _nps_event_paths(folder: Path) -> list[Path]:
    paths = list(folder.glob("events-*.jsonl")) + list(folder.glob("events-*.jsonl.gz"))
    return sorted(paths, key=lambda p: p.name)


def docker_api(path: str, timeout: float = 3.0) -> Any:
    if not Path(DOCKER_SOCK).exists():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(DOCKER_SOCK)
        except OSError as exc:
            print(f"docker sock connect failed: {exc}", flush=True)
            return None
        req = (
            f"GET {path} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        ).encode()
        sock.sendall(req)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    raw = b"".join(chunks)
    if b"\r\n\r\n" not in raw:
        return None
    header, body = raw.split(b"\r\n\r\n", 1)
    if b"200" not in header.split(b"\r\n", 1)[0] or not body:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _host_cpu_count() -> int:
    cpuinfo = HOST_PROC / "cpuinfo"
    if cpuinfo.is_file():
        try:
            n = sum(1 for line in cpuinfo.read_text().splitlines() if line.startswith("processor"))
            if n > 0:
                return n
        except OSError:
            pass
    return 0


def read_host_resources() -> dict[str, Any]:
    out: dict[str, Any] = {"available": False}
    load, mem = HOST_PROC / "loadavg", HOST_PROC / "meminfo"
    if not load.is_file() or not mem.is_file():
        return out
    try:
        parts = load.read_text().split()
        load1, load5, load15 = float(parts[0]), float(parts[1]), float(parts[2])
        info: dict[str, int] = {}
        for line in mem.read_text().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
        used_kb = max(0, total_kb - avail_kb)
        used_pct = round((used_kb / total_kb) * 100, 1) if total_kb else 0.0
        cpu_count = _host_cpu_count()
        return {
            "available": True,
            "cpu_count": cpu_count,
            "load1": load1,
            "load5": load5,
            "load15": load15,
            "mem_total_mb": round(total_kb / 1024, 1),
            "mem_used_mb": round(used_kb / 1024, 1),
            "mem_available_mb": round(avail_kb / 1024, 1),
            "mem_used_pct": used_pct,
        }
    except (OSError, ValueError, IndexError):
        return {"available": False}


def read_valheim_stats() -> dict[str, Any]:
    out: dict[str, Any] = {"available": False, "container": VALHEIM_CONTAINER}
    stats = docker_api(
        f"/containers/{VALHEIM_CONTAINER}/stats?stream=false", timeout=8.0
    )
    if not isinstance(stats, dict):
        return out
    try:
        cpu = stats.get("cpu_stats", {})
        precpu = stats.get("precpu_stats", {})
        cpu_delta = float(cpu.get("cpu_usage", {}).get("total_usage", 0)) - float(
            precpu.get("cpu_usage", {}).get("total_usage", 0)
        )
        system_delta = float(cpu.get("system_cpu_usage", 0)) - float(
            precpu.get("system_cpu_usage", 0)
        )
        online = float(
            cpu.get("online_cpus")
            or len(cpu.get("cpu_usage", {}).get("percpu_usage") or [1])
        )
        # Docker convention: 100% == one full core
        cpu_pct = 0.0
        if system_delta > 0 and cpu_delta >= 0:
            cpu_pct = round((cpu_delta / system_delta) * online * 100.0, 2)
        host_cpus = float(_host_cpu_count() or online or 1)
        cpu_cores = round(cpu_pct / 100.0, 2)
        cpu_host_pct = round(cpu_pct / host_cpus, 2) if host_cpus else 0.0
        mem = stats.get("memory_stats", {})
        usage = float(mem.get("usage", 0))
        limit = float(mem.get("limit", 0)) or 1.0
        stats_map = mem.get("stats") or {}
        cache = float(stats_map.get("inactive_file", stats_map.get("cache", 0)) or 0)
        used = max(0.0, usage - cache)
        return {
            "available": True,
            "container": VALHEIM_CONTAINER,
            "cpu_pct": cpu_pct,
            "cpu_cores": cpu_cores,
            "cpu_host_pct": cpu_host_pct,
            "online_cpus": int(online),
            "host_cpus": int(host_cpus),
            "mem_used_mb": round(used / (1024 * 1024), 1),
            "mem_limit_mb": round(limit / (1024 * 1024), 1),
            "mem_pct": round((used / limit) * 100.0, 2) if limit else 0.0,
        }
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return {"available": False, "container": VALHEIM_CONTAINER}


def sample_resources() -> dict[str, Any]:
    sample = {
        "ts": _iso_now(),
        "host": read_host_resources(),
        "valheim": read_valheim_stats(),
    }
    with _metrics_lock:
        _metrics.append(sample)
        if len(_metrics) > METRICS_KEEP:
            del _metrics[: len(_metrics) - METRICS_KEEP]
        try:
            METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with METRICS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
        except OSError:
            pass
    return sample


def load_metrics_history() -> None:
    if not METRICS_FILE.is_file():
        return
    try:
        rows = [
            json.loads(line)
            for line in METRICS_FILE.read_text(encoding="utf-8").splitlines()[-METRICS_KEEP:]
            if line.strip()
        ]
        with _metrics_lock:
            _metrics.clear()
            _metrics.extend(rows)
    except (OSError, json.JSONDecodeError):
        pass


def metrics_loop() -> None:
    while True:
        try:
            sample_resources()
        except Exception as exc:  # noqa: BLE001
            print(f"metrics sample error: {exc}", flush=True)
        time.sleep(METRICS_INTERVAL)


def parse_nps() -> dict[str, Any]:
    """Parse NetworkPerformanceSystem JSONL monitoring dumps.

    Real per-player RTT lives on ``peer`` events (fields ``rtt`` / ``rttLast``),
    keyed by character ZDOID ``uid``. Host ``batch`` self-RTT is ignored.
    """
    result: dict[str, Any] = {
        "available": NPS_DIR.is_dir(),
        "folders": [],
        "host_id": None,
        "peers": {},  # peer_id -> {rtt: [...], folder}
        "rtt_samples": [],
    }
    if not NPS_DIR.is_dir():
        return result

    folders = sorted(
        [p for p in NPS_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )[:8]
    result["folders"] = [p.name for p in folders]

    for folder in folders:
        for path in _nps_event_paths(folder):
            try:
                if path.suffix == ".gz" or path.name.endswith(".jsonl.gz"):
                    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                        lines = fh.read().splitlines()
                else:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = o.get("k")
                if kind == "session":
                    result["host_id"] = str(o.get("host") or result["host_id"] or "")
                    continue
                if kind == "peer_sock":
                    uid = str(o.get("uid") or "")
                    if uid:
                        peers = result["peers"].setdefault(
                            uid, {"peer_id": uid, "rtt": [], "folder": folder.name}
                        )
                        if o.get("sock"):
                            peers["sock"] = str(o.get("sock"))
                    continue
                if kind != "peer":
                    continue
                uid = str(o.get("uid") or "")
                if not uid:
                    continue
                if "rtt" not in o and "rttLast" not in o:
                    continue
                rtt_raw = o.get("rtt", o.get("rttLast"))
                try:
                    rtt_ms = float(rtt_raw)
                except (TypeError, ValueError):
                    continue
                sample = {
                    "peer_id": uid,
                    "rtt_ms": rtt_ms,
                    "jitter_ms": float(o["jitter"]) if o.get("jitter") is not None else None,
                    "t": o.get("t"),
                    "folder": folder.name,
                    "sock": o.get("sock"),
                }
                peers = result["peers"].setdefault(
                    uid, {"peer_id": uid, "rtt": [], "folder": folder.name}
                )
                peers["rtt"].append(rtt_ms)
                if o.get("sock"):
                    peers["sock"] = str(o.get("sock"))
                result["rtt_samples"].append(sample)

    peer_summaries = []
    for peer_id, data in result["peers"].items():
        values = [float(v) for v in data["rtt"] if v is not None]
        lat = _stats(values)
        sock = data.get("sock")
        peer_summaries.append(
            {
                "peer_id": peer_id,
                "folder": data.get("folder"),
                "latency_ms": lat,
                "sock": sock,
                "network": _classify_network(lat, sock if isinstance(sock, str) else None),
            }
        )
    result["peer_summaries"] = peer_summaries
    all_rtt = [s["rtt_ms"] for s in result["rtt_samples"] if s["rtt_ms"] is not None]
    result["latency_ms"] = _stats(all_rtt)
    return result


def _group_events_by_steam(
    events: list[dict[str, str]], char_by_steam: dict[str, str]
) -> list[dict[str, Any]]:
    """Group newest-first events by SteamID; preserve recency order of groups."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in events:
        sid = (ev.get("steam_id") or "").strip() or "unknown"
        if sid not in groups:
            groups[sid] = {
                "steam_id": None if sid == "unknown" else sid,
                "character": char_by_steam.get(sid) or ev.get("character") or None,
                "events": [],
                "count": 0,
                "last_time": ev.get("time"),
            }
            order.append(sid)
        g = groups[sid]
        if ev.get("character"):
            g["character"] = ev["character"]
        elif sid in char_by_steam:
            g["character"] = char_by_steam[sid]
        g["events"].append(ev)
        g["count"] += 1
    return [groups[sid] for sid in order]


def parse_player_events(path: Path) -> dict[str, Any]:
    online: dict[str, OnlinePlayer] = {}
    last_steam: str | None = None
    sessions: list[SessionRecord] = []
    open_sessions: dict[str, SessionRecord] = {}
    events_out: list[dict[str, str]] = []
    steam_ids: set[str] = set()
    char_by_steam: dict[str, str] = {}
    session_counter = 0

    empty = {
        "online": [],
        "sessions": [],
        "recent_events": [],
        "event_groups": [],
        "stats": {
            "online_count": 0,
            "unique_steam_ids": 0,
            "completed_sessions": 0,
            "short_sessions": 0,
            "events": 0,
            "duration_seconds": _stats([]),
        },
        "source_exists": False,
        "source_bytes": 0,
    }
    if not path.is_file():
        return empty

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    def new_session(steam: str, ts: str) -> SessionRecord:
        nonlocal session_counter
        session_counter += 1
        return SessionRecord(
            id=f"s{session_counter}",
            steam_id=steam,
            character=None,
            connected_at=ts,
            disconnected_at=None,
            duration_seconds=None,
            events=[],
        )

    for raw in lines:
        parts = raw.split("\t", 2)
        if len(parts) < 3:
            continue
        ts, tag, msg = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if tag == "disconnect" and "ClosedByPeer" in msg and "Closing socket" not in msg:
            continue

        steam = None
        m = RE_STEAM.search(msg)
        if m:
            steam = m.group(1)
            steam_ids.add(steam)

        char_name: str | None = None
        zdoid: str | None = None
        if tag == "character":
            cm = RE_CHARACTER.search(msg)
            char_name = cm.group(1).strip() if cm else None
            zdoid = cm.group(2).strip() if cm else None
            target = last_steam
            if target is None and len(online) == 1:
                target = next(iter(online))
            if target and char_name:
                steam = target
                char_by_steam[target] = char_name

        steam_id = steam or last_steam or ""
        ev = {
            "time": ts,
            "tag": tag,
            "message": msg,
            "steam_id": steam_id,
            "character": char_by_steam.get(steam_id, char_name or ""),
        }
        events_out.append(ev)

        if tag == "steam_connect" and steam:
            last_steam = steam
            online[steam] = OnlinePlayer(steam_id=steam, character=None, connected_at=ts)
            sess = new_session(steam, ts)
            sess.events.append(ev)
            open_sessions[steam] = sess
        elif tag == "handshake" and steam:
            last_steam = steam
            if steam in online:
                online[steam].handshake_at = ts
            else:
                online[steam] = OnlinePlayer(
                    steam_id=steam, character=None, connected_at=ts, handshake_at=ts
                )
            if steam not in open_sessions:
                open_sessions[steam] = new_session(steam, ts)
            open_sessions[steam].events.append(ev)
        elif tag == "character":
            target = steam or last_steam
            if target is None and len(online) == 1:
                target = next(iter(online))
            if target and char_name:
                if target in online:
                    online[target].character = char_name
                if target in open_sessions:
                    open_sessions[target].character = char_name
                    open_sessions[target].zdoid = zdoid
                    open_sessions[target].events.append(ev)
        elif tag == "disconnect" and "Closing socket" in msg and steam:
            online.pop(steam, None)
            sess = open_sessions.pop(steam, None)
            if sess is not None:
                sess.disconnected_at = ts
                sess.events.append(ev)
                start = _parse_iso(sess.connected_at)
                end = _parse_iso(ts)
                if start and end:
                    sess.duration_seconds = max(0, int((end - start).total_seconds()))
                    sess.short_session = sess.duration_seconds < 120
                sessions.append(sess)
            last_steam = None

    for sid, sess in open_sessions.items():
        if sess.character:
            char_by_steam[sid] = sess.character
        if sid not in online:
            online[sid] = OnlinePlayer(
                steam_id=sid, character=sess.character, connected_at=sess.connected_at
            )
    for sess in sessions:
        if sess.character:
            char_by_steam[sess.steam_id] = sess.character

    recent = list(reversed(events_out[-120:]))
    for ev in recent:
        sid = ev.get("steam_id") or ""
        if sid and not ev.get("character") and sid in char_by_steam:
            ev["character"] = char_by_steam[sid]

    durations = [float(s.duration_seconds) for s in sessions if s.duration_seconds is not None]
    return {
        "online": sorted(online.values(), key=lambda p: p.connected_at),
        "sessions": list(reversed(sessions[-100:])),
        "open_sessions": list(open_sessions.values()),
        "recent_events": recent,
        "event_groups": _group_events_by_steam(recent, char_by_steam),
        "stats": {
            "online_count": len(online),
            "unique_steam_ids": len(steam_ids),
            "completed_sessions": len(sessions),
            "short_sessions": sum(1 for s in sessions if s.short_session),
            "events": len(events_out),
            "duration_seconds": _stats(durations),
        },
        "source_exists": True,
        "source_bytes": path.stat().st_size,
    }


def _apply_peer_samples(sess: SessionRecord, peer_id: str, peer_samples: list[dict[str, Any]]) -> None:
    values = [float(s["rtt_ms"]) for s in peer_samples]
    socks = [str(s.get("sock")) for s in peer_samples if s.get("sock")]
    sock = socks[-1] if socks else None
    sess.nps_peer_id = peer_id
    sess.latency_ms = _stats(values)
    sess.sock = sock
    sess.network = _classify_network(sess.latency_ms, sock)
    sess.rtt_series = [
        {"rtt_ms": s["rtt_ms"], "t": s.get("t"), "folder": s.get("folder")}
        for s in peer_samples
    ][-240:]


def attach_latency(sessions: list[SessionRecord], nps: dict[str, Any]) -> None:
    """Attach NPS peer RTT to sessions via character ZDOID == peer uid."""
    samples = nps.get("rtt_samples") or []
    by_peer: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        by_peer.setdefault(sample["peer_id"], []).append(sample)
    peer_meta = {p["peer_id"]: p for p in (nps.get("peer_summaries") or [])}

    matched_peers: set[str] = set()
    unmatched: list[SessionRecord] = []

    for sess in sessions:
        sess.latency_ms = _stats([])
        sess.rtt_series = []
        sess.nps_peer_id = None
        sess.network = "unknown"
        sess.sock = None
        if sess.zdoid and sess.zdoid in by_peer:
            _apply_peer_samples(sess, sess.zdoid, by_peer[sess.zdoid])
            if not sess.sock and sess.zdoid in peer_meta:
                sess.sock = peer_meta[sess.zdoid].get("sock")
                sess.network = _classify_network(sess.latency_ms, sess.sock)
            matched_peers.add(sess.zdoid)
        else:
            unmatched.append(sess)

    leftover_peers = [pid for pid in by_peer if pid not in matched_peers]
    if len(unmatched) == 1 and len(leftover_peers) == 1:
        sess = unmatched[0]
        pid = leftover_peers[0]
        _apply_peer_samples(sess, pid, by_peer[pid])
        if not sess.sock and pid in peer_meta:
            sess.sock = peer_meta[pid].get("sock")
            sess.network = _classify_network(sess.latency_ms, sess.sock)


def _query_minutes(query: str) -> int | None:
    raw = (parse_qs(query).get("minutes") or [None])[0]
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _clamp_window(minutes: int | None) -> int:
    if minutes is None:
        return DEFAULT_WINDOW_MIN
    return min(RESOURCE_WINDOWS_MIN, key=lambda choice: (abs(choice - minutes), choice))


def _thin_samples(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(rows) <= limit or limit < 2:
        return rows
    step = (len(rows) - 1) / (limit - 1)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for i in range(limit):
        idx = min(len(rows) - 1, int(round(i * step)))
        if idx in seen:
            continue
        seen.add(idx)
        out.append(rows[idx])
    if out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


def _sample_cpu_host_pct(sample: dict[str, Any]) -> float:
    vh = sample.get("valheim") or {}
    host = sample.get("host") or {}
    if vh.get("cpu_host_pct") is not None:
        return float(vh.get("cpu_host_pct") or 0)
    cores = float(host.get("cpu_count") or vh.get("host_cpus") or vh.get("online_cpus") or 0)
    if cores <= 0:
        return 0.0
    return round(float(vh.get("cpu_pct") or 0) / cores, 2)


def _series_from(window: list[dict[str, Any]]) -> dict[str, list[float]]:
    return {
        "valheim_cpu_pct": [float((s.get("valheim") or {}).get("cpu_pct") or 0) for s in window],
        "valheim_cpu_host_pct": [_sample_cpu_host_pct(s) for s in window],
        "valheim_cpu_cores": [
            float(
                (s.get("valheim") or {}).get("cpu_cores")
                if (s.get("valheim") or {}).get("cpu_cores") is not None
                else float((s.get("valheim") or {}).get("cpu_pct") or 0) / 100.0
            )
            for s in window
        ],
        "valheim_mem_pct": [float((s.get("valheim") or {}).get("mem_pct") or 0) for s in window],
        "host_mem_pct": [float((s.get("host") or {}).get("mem_used_pct") or 0) for s in window],
        "host_load1": [float((s.get("host") or {}).get("load1") or 0) for s in window],
    }


def slice_resources(resources: dict[str, Any], minutes: int | None) -> dict[str, Any]:
    chosen = _clamp_window(minutes)
    history = list(resources.get("history") or [])
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=chosen)
    selected: list[dict[str, Any]] = []
    for sample in history:
        ts = _parse_iso(str(sample.get("ts") or ""))
        if ts is not None and ts >= cutoff:
            selected.append(sample)
    plotted = _thin_samples(selected, SPARK_POINTS)
    window_seconds = 0
    if len(selected) >= 2:
        start = _parse_iso(str(selected[0].get("ts") or ""))
        end = _parse_iso(str(selected[-1].get("ts") or ""))
        if start and end:
            window_seconds = max(0, int((end - start).total_seconds()))
    return {
        "current": resources.get("current"),
        "history": plotted,
        "series": _series_from(plotted),
        "warnings": resources.get("warnings") or [],
        "interval_seconds": resources.get("interval_seconds"),
        "samples": resources.get("samples"),
        "window_minutes": chosen,
        "window_seconds": window_seconds,
        "window_samples": len(selected),
        "windows_minutes": list(RESOURCE_WINDOWS_MIN),
    }


def resources_payload() -> dict[str, Any]:
    with _metrics_lock:
        history = list(_metrics)
    try:
        current = history[-1] if history else sample_resources()
    except Exception as exc:  # noqa: BLE001
        print(f"resources_payload sample failed: {exc}", flush=True)
        current = {
            "ts": _iso_now(),
            "host": {"available": False},
            "valheim": {"available": False},
        }
    warn = []
    vh, host = current.get("valheim") or {}, current.get("host") or {}
    if vh.get("available") and float(vh.get("cpu_host_pct") or 0) >= 70:
        warn.append("Valheim using ≥ 70% of host CPU")
    if vh.get("available") and float(vh.get("mem_pct") or 0) >= 85:
        warn.append("Valheim memory ≥ 85% of cgroup limit")
    if host.get("available") and float(host.get("mem_used_pct") or 0) >= 85:
        warn.append("Host memory ≥ 85%")
    if host.get("available") and float(host.get("load1") or 0) >= 10:
        warn.append("Host load1 ≥ 10")
    return {
        "current": current,
        "history": history,
        "series": _series_from(history),
        "warnings": warn,
        "interval_seconds": METRICS_INTERVAL,
        "samples": len(history),
        "window_minutes": None,
        "window_seconds": 0,
        "window_samples": len(history),
        "windows_minutes": list(RESOURCE_WINDOWS_MIN),
    }


def build_snapshot() -> dict[str, Any]:
    players = parse_player_events(EVENTS_FILE)
    nps = parse_nps()
    sessions: list[SessionRecord] = list(players["sessions"])
    # Include still-open sessions in the list (marked with no disconnect)
    for open_sess in players.get("open_sessions") or []:
        sessions.insert(0, open_sess)
    attach_latency(sessions, nps)

    latency_values = [
        float(s.latency_ms["avg"])
        for s in sessions
        if s.latency_ms.get("avg") is not None
    ]

    # First match wins: live/open sessions are listed before completed history
    net_by_steam: dict[str, dict[str, Any]] = {}
    for s in sessions:
        if not s.steam_id or s.steam_id in net_by_steam:
            continue
        net_by_steam[s.steam_id] = {
            "network": s.network,
            "sock": s.sock,
        }

    online_out = []
    for p in players["online"]:
        row = asdict(p)
        meta = net_by_steam.get(p.steam_id) or {}
        row["network"] = meta.get("network") or "unknown"
        row["sock"] = meta.get("sock")
        online_out.append(row)

    return {
        "generated_at": _iso_now(),
        "source": str(EVENTS_FILE),
        "source_exists": players["source_exists"],
        "source_bytes": players["source_bytes"],
        "online": online_out,
        "sessions": [
            {
                **asdict(s),
                "duration_label": _fmt_dur(s.duration_seconds),
            }
            for s in sessions
        ],
        "recent_events": players["recent_events"],
        "event_groups": players.get("event_groups") or [],
        "stats": {
            **players["stats"],
            "latency_ms": _stats(latency_values),
        },
        "nps": {
            "available": nps["available"],
            "folders": nps.get("folders") or [],
            "host_id": nps.get("host_id"),
            "peer_summaries": nps.get("peer_summaries") or [],
            "latency_ms": nps.get("latency_ms") or _stats([]),
            "sample_count": len(nps.get("rtt_samples") or []),
        },
        "resources": resources_payload(),
    }


def get_snapshot() -> dict[str, Any]:
    now = time.time()
    with _lock:
        if _cache["payload"] is not None and now - float(_cache["ts"]) < CACHE_SECONDS:
            return _cache["payload"]  # type: ignore[return-value]
        payload = build_snapshot()
        _cache["ts"] = now
        _cache["payload"] = payload
        return payload


def get_session(session_id: str) -> dict[str, Any] | None:
    snap = get_snapshot()
    for sess in snap["sessions"]:
        if sess["id"] == session_id:
            return sess
    return None


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Valheim usage</title>
  <style>
    :root {
      --bg:#12140f; --panel:#1a1f16; --ink:#e7ecd9; --muted:#9aa58a;
      --line:#2c3426; --accent:#c4a35a; --ok:#8fbf6a; --bad:#d98989; --warn:#d4a017;
    }
    *{box-sizing:border-box}
    body{
      margin:0; font-family:"Segoe UI",system-ui,sans-serif; color:var(--ink);
      background:radial-gradient(1200px 600px at 10% -10%,#24301c 0%,transparent 55%),var(--bg);
      min-height:100vh;
    }
    header{padding:1.25rem 1.5rem .85rem; border-bottom:1px solid var(--line)}
    header h1{margin:0; font-size:1.3rem}
    header p{margin:.35rem 0 0; color:var(--muted); font-size:.9rem}
    main{display:grid; gap:1rem; padding:1.1rem 1.5rem 2rem; max-width:1100px}
    section{
      background:color-mix(in srgb,var(--panel) 92%,black); border:1px solid var(--line);
      border-radius:10px; padding:1rem 1.1rem; overflow:hidden;
    }
    section h2{
      margin:0 0 .75rem; font-size:.75rem; text-transform:uppercase;
      letter-spacing:.08em; color:var(--accent);
    }
    .section-head{
      display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center;
      gap:.6rem; margin-bottom:.75rem;
    }
    .section-head h2{margin:0}
    .h-meta{
      color:var(--muted); font-weight:500; letter-spacing:0; text-transform:none; margin-left:.45rem;
    }
    .range{display:flex; flex-wrap:wrap; gap:.35rem}
    .range button{
      border:1px solid var(--line); background:#141910; color:var(--muted);
      border-radius:999px; padding:.22rem .65rem; cursor:pointer;
      font:inherit; font-size:.75rem;
    }
    .range button.on{color:var(--ink); border-color:var(--accent); background:#2a2416}
    .stats{display:flex; flex-wrap:wrap; gap:.7rem}
    .stat{
      min-width:6.5rem; padding:.6rem .75rem; border:1px solid var(--line);
      border-radius:8px; background:#141910;
    }
    .stat .n{font-size:1.25rem; font-weight:700; color:var(--ok)}
    .stat .n.warn{color:var(--warn)} .stat .n.bad{color:var(--bad)}
    .stat .l{color:var(--muted); font-size:.78rem}
    .resource-grid{display:grid; gap:.85rem; grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
    .gauge{border:1px solid var(--line); border-radius:8px; padding:.7rem .8rem; background:#141910}
    .gauge .label{color:var(--muted); font-size:.78rem}
    .gauge .value{font-size:1.15rem; font-weight:700}
    .bar{margin-top:.45rem; height:8px; border-radius:999px; background:#24301c; overflow:hidden}
    .bar>span{display:block; height:100%; border-radius:999px; background:var(--ok)}
    .bar>span.hot{background:var(--warn)} .bar>span.crit{background:var(--bad)}
    .spark-wrap{
      margin-top:.45rem; padding:.4rem .45rem .2rem;
      border-radius:8px; border:1px solid transparent;
      background:linear-gradient(180deg, rgba(36,48,28,.55), rgba(20,25,16,.35));
      transition:background .15s ease, border-color .15s ease;
    }
    .spark-wrap:has(.spark-plot[data-expandable="1"]:hover),
    .spark-wrap:has(.spark-plot[data-expandable="1"]:focus-visible){
      background:linear-gradient(180deg, rgba(48,62,36,.72), rgba(28,34,22,.5));
      border-color:#3a4434;
    }
    .spark-plot{position:relative; cursor:crosshair}
    .spark-plot[data-expandable="1"]{cursor:pointer}
    .spark{width:100%; height:48px; margin-top:0; display:block}
    .spark.lg{height:240px}
    .spark polyline{vector-effect:non-scaling-stroke}
    .chart-hint{color:var(--muted); font-size:.72rem; margin:.35rem 0 0}
    .spark-cursor{
      position:absolute; top:0; bottom:0; width:1px; left:0;
      background:rgba(231,236,217,.55); pointer-events:none; display:none;
    }
    .spark-dot{
      position:absolute; width:7px; height:7px; border-radius:50%;
      background:var(--ok); border:1px solid #e7ecd9;
      transform:translate(-50%,-50%); pointer-events:none; display:none;
    }
    .spark-tip{
      position:absolute; top:2px; left:0; transform:translateX(-50%);
      background:#1d2418; border:1px solid var(--line); border-radius:6px;
      padding:.12rem .4rem; font-size:.68rem; font-family:ui-monospace,Consolas,monospace;
      color:var(--ink); white-space:nowrap; pointer-events:none; display:none; z-index:2;
    }
    .spark-plot.on .spark-cursor,
    .spark-plot.on .spark-dot,
    .spark-plot.on .spark-tip{display:block}
    .spark-axis{
      position:relative; height:1.05rem; margin-top:1px;
      border-top:1px solid #3a4434;
    }
    .spark-axis .tick{
      position:absolute; top:0; width:1px; height:4px;
      background:#7d8b72; transform:translateX(-50%);
    }
    .spark-axis .tick span{
      position:absolute; top:5px; left:50%; transform:translateX(-50%);
      color:var(--muted); font-family:ui-monospace,Consolas,monospace;
      font-size:.62rem; font-weight:400; letter-spacing:0; white-space:nowrap;
    }
    .warnings{margin:.75rem 0 0; padding:.55rem .7rem; border-radius:8px; border:1px solid #5a4030; background:#241a10; color:#f0d9a8; font-size:.88rem}
    .table-wrap{width:100%; overflow-x:auto}
    table{width:100%; border-collapse:collapse; font-size:.9rem; min-width:640px}
    th,td{text-align:left; padding:.5rem .45rem; border-bottom:1px solid var(--line); vertical-align:top}
    th{color:var(--muted); font-weight:600; font-size:.72rem; text-transform:uppercase; letter-spacing:.05em; white-space:nowrap}
    td.char{font-weight:600; white-space:nowrap}
    td.steam,td.time,td.msg{font-family:ui-monospace,Consolas,monospace; font-size:.78rem}
    td.steam,td.time{white-space:nowrap; color:var(--muted)}
    td.msg{word-break:break-word}
    tr.short td{color:var(--bad)}
    tr.clickable{cursor:pointer}
    tr.clickable:hover td{background:#22291c}
    .badge{display:inline-block; margin-left:.35rem; padding:.05rem .4rem; border-radius:999px; border:1px solid #5a3030; color:var(--bad); font-size:.68rem; text-transform:uppercase}
    .badge.lan{border-color:#3d5a2e; color:var(--ok)}
    .badge.wan{border-color:#5a4030; color:var(--warn)}
    .badge.net{border-color:#3a4434; color:var(--muted)}
    .empty{color:var(--muted); font-style:italic; margin:0}
    .tag{display:inline-block; padding:.1rem .4rem; border-radius:999px; border:1px solid var(--line); color:var(--muted); font-size:.7rem; text-transform:uppercase; white-space:nowrap}
    .tag.character{color:var(--ok); border-color:#3d5a2e}
    .tag.steam_connect,.tag.handshake{color:var(--accent)}
    .tag.disconnect{color:var(--bad); border-color:#5a3030}
    .event-groups{display:flex; flex-direction:column; gap:.45rem}
    details.event-group{
      border:1px solid var(--line); border-radius:8px; background:#141910; overflow:hidden;
    }
    details.event-group>summary{
      list-style:none; cursor:pointer; display:flex; flex-wrap:wrap; align-items:baseline;
      gap:.45rem .85rem; padding:.55rem .75rem; user-select:none;
    }
    details.event-group>summary::-webkit-details-marker{display:none}
    details.event-group>summary::before{
      content:"▸"; color:var(--muted); font-size:.75rem; width:.85rem; flex:0 0 auto;
    }
    details.event-group[open]>summary::before{content:"▾"}
    details.event-group>summary:hover{background:#1c2318}
    details.event-group .g-char{font-weight:700}
    details.event-group .g-steam{
      font-family:ui-monospace,Consolas,monospace; font-size:.78rem; color:var(--muted);
    }
    details.event-group .g-count{
      margin-left:auto; color:var(--accent); font-size:.78rem; font-weight:600; white-space:nowrap;
    }
    details.event-group .g-when{color:var(--muted); font-size:.75rem; white-space:nowrap}
    details.event-group .group-body{padding:0 .55rem .55rem; border-top:1px solid var(--line)}
    details.event-group .group-body table{min-width:0}
    footer{padding:0 1.5rem 1.5rem; color:var(--muted); font-size:.8rem; max-width:1100px}
    a{color:var(--accent)}
    dialog{
      border:1px solid var(--line); border-radius:12px; background:#151a12; color:var(--ink);
      width:min(820px,94vw); padding:0; max-height:90vh;
    }
    dialog.chart-dlg{width:min(980px,96vw)}
    dialog::backdrop{background:rgba(0,0,0,.55)}
    .dlg-head{display:flex; justify-content:space-between; gap:1rem; padding:1rem 1.1rem; border-bottom:1px solid var(--line)}
    .dlg-head h3{margin:0; font-size:1.05rem}
    .dlg-body{padding:1rem 1.1rem 1.2rem; overflow:auto}
    .dlg-body .gauge{padding:1rem 1.1rem}
    button.close{
      border:1px solid var(--line); background:#1d2418; color:var(--ink);
      border-radius:8px; padding:.35rem .7rem; cursor:pointer;
    }
    .kv{display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:.6rem; margin-bottom:1rem}
    .kv .box{border:1px solid var(--line); border-radius:8px; padding:.55rem .7rem; background:#141910}
    .kv .box .l{color:var(--muted); font-size:.75rem}
    .kv .box .v{font-weight:700; margin-top:.15rem}
  </style>
</head>
<body>
  <header>
    <h1>Valheim usage</h1>
    <p id="subtitle">Loading…</p>
  </header>
  <main>
    <section>
      <h2>Summary</h2>
      <div class="stats" id="stats"></div>
    </section>
    <section>
      <div class="section-head">
        <h2>Resources <span class="h-meta" id="resource-window"></span></h2>
        <div class="range" id="resource-range"></div>
      </div>
      <div class="resource-grid" id="resources"></div>
      <div id="warnings"></div>
    </section>
    <section>
      <h2>Online now</h2>
      <div id="online"></div>
    </section>
    <section>
      <h2>Completed sessions <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— click a row for details</span></h2>
      <div id="sessions"></div>
    </section>
    <section>
      <h2>NPS peers (raw monitoring)</h2>
      <div id="nps"></div>
    </section>
    <section>
      <h2>Recent events <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— grouped by Steam ID</span></h2>
      <div id="events"></div>
    </section>
  </main>
  <footer>
    Sessions from player-events.log · latency from NPS NpsMonitoring JSONL ·
    <a href="/api/status">JSON</a> · LAN only
  </footer>

  <dialog id="detail">
    <div class="dlg-head">
      <h3 id="detail-title">Session</h3>
      <button class="close" id="detail-close">Close</button>
    </div>
    <div class="dlg-body" id="detail-body"></div>
  </dialog>

  <dialog id="chart-expand" class="chart-dlg">
    <div class="dlg-head">
      <h3 id="chart-expand-title">Chart</h3>
      <button class="close" id="chart-expand-close">Close</button>
    </div>
    <div class="dlg-body" id="chart-expand-body"></div>
  </dialog>

  <script>
    let DATA = null;
    const RANGES = [10, 30, 90, 180, 720];
    let RANGE_MIN = Number(localStorage.getItem("valheim-resource-minutes")) || 30;
    if (!RANGES.includes(RANGE_MIN)) RANGE_MIN = 30;
    const expandedEventGroups = new Set();
    function captureUiState(){
      document.querySelectorAll("#events details.event-group").forEach(d=>{
        const key=d.dataset.groupKey;
        if(!key) return;
        if(d.open) expandedEventGroups.add(key);
        else expandedEventGroups.delete(key);
      });
      return {scrollX: window.scrollX, scrollY: window.scrollY};
    }
    function restoreUiState(state){
      document.querySelectorAll("#events details.event-group").forEach(d=>{
        const key=d.dataset.groupKey;
        if(key && expandedEventGroups.has(key)) d.open=true;
        d.addEventListener("toggle", ()=>{
          if(!key) return;
          if(d.open) expandedEventGroups.add(key);
          else expandedEventGroups.delete(key);
        });
      });
      if(state){
        requestAnimationFrame(()=>{
          window.scrollTo(state.scrollX, state.scrollY);
        });
      }
    }
    function rangeLabel(min){
      if (min < 180) return min + "m";
      return (min / 60) + "h";
    }
    function fmtWindow(sec){
      sec = Math.max(0, Math.round(sec));
      if (sec < 90) return sec + "s";
      const mins = Math.round(sec / 60);
      if (mins < 90) return mins + " min";
      const h = Math.floor(mins / 60);
      const rem = mins % 60;
      return rem ? h + "h " + rem + "m" : h + "h";
    }
    function esc(s){
      return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }
    function fmtDur(sec){
      if(sec==null) return "—";
      const m=Math.floor(sec/60), s=sec%60;
      if(m>=60) return Math.floor(m/60)+"h "+(m%60)+"m";
      return m+"m "+String(s).padStart(2,"0")+"s";
    }
    function fmtWhen(ts){ try{return new Date(ts).toLocaleString()}catch{return ts} }
    function fmtLat(st){
      if(!st || st.samples===0 || st.avg==null) return "—";
      return `${st.min}/${st.avg}/${st.max} ms`;
    }
    function netBadge(network){
      if(network==="lan") return `<span class="badge lan" title="NPS RTT suggests LAN (low avg RTT)">LAN</span>`;
      if(network==="wan") return `<span class="badge wan" title="NPS RTT suggests WAN (higher avg RTT or non-direct sock)">WAN</span>`;
      return `<span class="badge net" title="No NPS RTT samples to classify">?</span>`;
    }
    function heat(pct){ if(pct>=85) return "crit"; if(pct>=70) return "hot"; return ""; }
    function plotBounds(times){
      if(!times || times.length<2) return null;
      const start=times[0], end=times[times.length-1];
      if(!Number.isFinite(start) || !Number.isFinite(end) || end<=start) return null;
      return {start, end};
    }
    function axisStep(spanMs){
      if(spanMs<=12*60000) return 2*60000;
      if(spanMs<=40*60000) return 5*60000;
      if(spanMs<=2*3600000) return 15*60000;
      if(spanMs<=6*3600000) return 30*60000;
      return 2*3600000;
    }
    function axisLabel(t){
      const d=new Date(t);
      const h=d.getHours()%12||12;
      return h+":"+String(d.getMinutes()).padStart(2,"0");
    }
    function axisHtml(bounds){
      if(!bounds) return "";
      const span=bounds.end-bounds.start;
      const step=axisStep(span);
      const ticks=[];
      let t=Math.ceil(bounds.start/step)*step;
      while(t<bounds.end){
        const frac=(t-bounds.start)/span;
        if(frac>0.03 && frac<0.97) ticks.push({t, frac});
        t+=step;
      }
      let lastLabel=-1;
      const marks=ticks.map(tick=>{
        const left=tick.frac*100;
        let label="";
        if(tick.frac>=0.14 && tick.frac<=0.86 && (lastLabel<0 || left-lastLabel>=24)){
          label=`<span>${esc(axisLabel(tick.t))}</span>`;
          lastLabel=left;
        }
        const title=esc(new Date(tick.t).toLocaleTimeString());
        return `<i class="tick" style="left:${left.toFixed(2)}%" title="${title}">${label}</i>`;
      }).join("");
      return `<div class="spark-axis">${marks}</div>`;
    }
    function spark(values, maxHint, times, bounds, height){
      const vals = values && values.length ? values.map(v=>Number(v)||0) : [0];
      const w=300,h=height||48,padY=2;
      const max=Math.max(maxHint||0, ...vals, 1);
      const span=bounds ? Math.max(bounds.end-bounds.start, 1) : 1;
      const xOf=(i)=>{
        if(bounds && times && times.length===vals.length && Number.isFinite(times[i])){
          return ((times[i]-bounds.start)/span)*w;
        }
        if(vals.length<2) return w;
        return (i/(vals.length-1))*w;
      };
      const pts=vals.map((v,i)=>{
        const y=h-padY-((v/max)*(h-padY*2));
        return xOf(i).toFixed(1)+","+y.toFixed(1);
      }).join(" ");
      const cls=h>48?"spark lg":"spark";
      return `<svg class="${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline fill="none" stroke="#8fbf6a" stroke-width="2" points="${pts}"/></svg>`;
    }
    function fmtSparkVal(v, format){
      if(format==="pct") return Number(v).toFixed(1)+"%";
      if(format==="load") return Number(v).toFixed(2);
      if(format==="ms") return Number(v).toFixed(0)+" ms";
      return String(v);
    }
    function nearestIdx(vals, times, bounds, frac){
      if(vals.length<1) return 0;
      if(bounds && times && times.length===vals.length){
        const target=bounds.start+frac*(bounds.end-bounds.start);
        let best=0, bestDist=Infinity;
        for(let i=0;i<times.length;i++){
          if(!Number.isFinite(times[i])) continue;
          const d=Math.abs(times[i]-target);
          if(d<bestDist){ bestDist=d; best=i; }
        }
        return best;
      }
      return Math.max(0, Math.min(vals.length-1, Math.round(frac*(vals.length-1))));
    }
    function bindSparkHovers(root){
      (root||document).querySelectorAll(".spark-plot[data-vals]").forEach(plot=>{
        if(plot.dataset.bound==="1") return;
        plot.dataset.bound="1";
        const vals=JSON.parse(plot.dataset.vals||"[]");
        const times=JSON.parse(plot.dataset.times||"[]");
        const max=Math.max(Number(plot.dataset.max)||1, 1);
        const format=plot.dataset.format||"raw";
        const padY=2;
        const cursor=plot.querySelector(".spark-cursor");
        const tip=plot.querySelector(".spark-tip");
        const dot=plot.querySelector(".spark-dot");
        const hide=()=>plot.classList.remove("on");
        plot.addEventListener("mouseleave", hide);
        plot.addEventListener("mousemove", (ev)=>{
          if(vals.length<1) return;
          const rect=plot.getBoundingClientRect();
          if(rect.width<1) return;
          const h=Number(plot.dataset.height)||rect.height||48;
          const frac=Math.max(0, Math.min(1, (ev.clientX-rect.left)/rect.width));
          const bounds=(Number.isFinite(Number(plot.dataset.start)) && Number.isFinite(Number(plot.dataset.end)))
            ? {start:Number(plot.dataset.start), end:Number(plot.dataset.end)}
            : null;
          const i=nearestIdx(vals, times, bounds, frac);
          const v=vals[i];
          const x=(bounds && Number.isFinite(times[i]))
            ? ((times[i]-bounds.start)/Math.max(bounds.end-bounds.start,1))*rect.width
            : (vals.length<2 ? rect.width : (i/(vals.length-1))*rect.width);
          const y=h-padY-((v/max)*(h-padY*2));
          const when=Number.isFinite(times[i]) ? new Date(times[i]).toLocaleTimeString() : "";
          tip.textContent=fmtSparkVal(v, format)+(when?" · "+when:"");
          cursor.style.left=x.toFixed(1)+"px";
          dot.style.left=x.toFixed(1)+"px";
          dot.style.top=y.toFixed(1)+"px";
          plot.classList.add("on");
          const tipW=tip.offsetWidth||80;
          tip.style.left=Math.max(tipW/2, Math.min(rect.width-tipW/2, x)).toFixed(1)+"px";
        });
        if(plot.dataset.expandable==="1"){
          let downX=0, downY=0;
          plot.addEventListener("mousedown", (ev)=>{ downX=ev.clientX; downY=ev.clientY; });
          plot.addEventListener("click", (ev)=>{
            if(Math.hypot(ev.clientX-downX, ev.clientY-downY)>6) return;
            openChartExpand(plot);
          });
        }
      });
    }
    function openChartExpand(sourcePlot){
      const vals=JSON.parse(sourcePlot.dataset.vals||"[]");
      if(!vals.length) return;
      const times=JSON.parse(sourcePlot.dataset.times||"[]");
      const max=Math.max(Number(sourcePlot.dataset.max)||1, 1);
      const format=sourcePlot.dataset.format||"raw";
      const gaugeEl=sourcePlot.closest(".gauge");
      const label=(gaugeEl && gaugeEl.querySelector(".label")?.textContent) || "Chart";
      const valueText=(gaugeEl && gaugeEl.querySelector(".value")?.textContent) || "";
      const dlg=document.getElementById("chart-expand");
      document.getElementById("chart-expand-title").textContent=label;
      document.getElementById("chart-expand-body").innerHTML =
        gauge(label, valueText, null, vals, max, times, format, {bar:false, height:240, expandable:false}) +
        `<p class="chart-hint">Hover for value · Esc or Close to dismiss</p>`;
      bindSparkHovers(document.getElementById("chart-expand-body"));
      if(typeof dlg.showModal==="function") dlg.showModal();
    }
    function gauge(label, valueText, pct, series, maxHint, times, format, opts){
      opts=opts||{};
      const vals=(series||[]).map(v=>Number(v)||0);
      const safeTimes=(times||[]).map(t=>Number.isFinite(t)?t:null);
      const bounds=plotBounds(safeTimes);
      const height=opts.height||48;
      const max=Math.max(maxHint||0, ...vals, 1);
      const expandable=opts.expandable!==false;
      const attrs=[
        `data-vals="${esc(JSON.stringify(vals))}"`,
        `data-times="${esc(JSON.stringify(safeTimes))}"`,
        `data-max="${max}"`,
        `data-format="${esc(format||"raw")}"`,
        `data-height="${height}"`,
      ];
      if(expandable) attrs.push(`data-expandable="1"`, `title="Click to expand"`);
      if(bounds){
        attrs.push(`data-start="${bounds.start}"`);
        attrs.push(`data-end="${bounds.end}"`);
      }
      const showBar = opts.bar!==false && pct!=null;
      const bar = showBar
        ? `<div class="bar"><span class="${heat(pct||0)}" style="width:${Math.min(100,pct||0)}%"></span></div>`
        : "";
      return `<div class="gauge"><div class="label">${esc(label)}</div><div class="value">${esc(valueText)}</div>
        ${bar}
        <div class="spark-wrap">
          <div class="spark-plot" ${attrs.join(" ")}>
            ${spark(vals, maxHint, safeTimes, bounds, height)}
            <div class="spark-cursor"></div>
            <div class="spark-dot"></div>
            <div class="spark-tip"></div>
          </div>
          ${axisHtml(bounds)}
        </div></div>`;
    }
    function rttTimes(session, series){
      if(!series || !series.length) return [];
      const gFirst=Number(series[0].t);
      const gLast=Number(series[series.length-1].t);
      const wallStart=Date.parse(session.connected_at);
      const wallEnd=session.disconnected_at
        ? Date.parse(session.disconnected_at)
        : Date.now();
      if(!Number.isFinite(gFirst) || !Number.isFinite(gLast) || !Number.isFinite(wallStart) || !Number.isFinite(wallEnd)){
        return series.map(()=>null);
      }
      if(gLast<=gFirst){
        return series.map(()=>wallStart);
      }
      return series.map(p=>{
        const t=Number(p.t);
        if(!Number.isFinite(t)) return null;
        const frac=(t-gFirst)/(gLast-gFirst);
        return wallStart + frac*(wallEnd-wallStart);
      });
    }
    function openDetail(id){
      const s=(DATA.sessions||[]).find(x=>x.id===id);
      if(!s) return;
      const dlg=document.getElementById("detail");
      document.getElementById("detail-title").textContent =
        (s.character || "Unknown") + " · " + s.steam_id;
      const lat=s.latency_ms||{};
      const seriesPts=s.rtt_series||[];
      const rtt=seriesPts.map(p=>Number(p.rtt_ms||0));
      const times=rttTimes(s, seriesPts);
      const rttChart=rtt.length
        ? gauge("RTT over session (ms)", fmtLat(lat), null, rtt, Math.max(...rtt,1), times, "ms", {bar:false})
        : `<p class="empty">No NPS RTT samples for this session yet (needs a player online while monitoring is on).</p>`;
      document.getElementById("detail-body").innerHTML = `
        <div class="kv">
          <div class="box"><div class="l">Connected</div><div class="v">${esc(fmtWhen(s.connected_at))}</div></div>
          <div class="box"><div class="l">Disconnected</div><div class="v">${esc(s.disconnected_at?fmtWhen(s.disconnected_at):"still online")}</div></div>
          <div class="box"><div class="l">Duration</div><div class="v">${esc(fmtDur(s.duration_seconds))}</div></div>
          <div class="box"><div class="l">Latency min / avg / max</div><div class="v">${esc(fmtLat(lat))}</div></div>
          <div class="box"><div class="l">Network</div><div class="v">${netBadge(s.network)}${s.sock?` <span style="color:var(--muted);font-size:.8rem">· ${esc(s.sock)}</span>`:""}</div></div>
          <div class="box"><div class="l">RTT samples</div><div class="v">${esc(lat.samples||0)}</div></div>
          <div class="box"><div class="l">NPS peer id</div><div class="v" style="font-family:ui-monospace,Consolas,monospace;font-size:.85rem">${esc(s.nps_peer_id||"—")}</div></div>
        </div>
        ${rttChart}
        <h2 style="margin:1rem 0 .5rem;font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:var(--accent)">Session events</h2>
        <div class="table-wrap"><table>
          <thead><tr><th>When</th><th>Tag</th><th>Message</th></tr></thead>
          <tbody>${(s.events||[]).map(e=>`<tr>
            <td class="time">${esc(fmtWhen(e.time))}</td>
            <td><span class="tag ${esc(e.tag)}">${esc(e.tag)}</span></td>
            <td class="msg">${esc(e.message)}</td>
          </tr>`).join("")||`<tr><td colspan="3" class="empty">No events</td></tr>`}</tbody>
        </table></div>`;
      bindSparkHovers(document.getElementById("detail-body"));
      dlg.showModal();
    }
    async function refresh(){
      const uiState=captureUiState();
      const res=await fetch("/api/status?minutes="+RANGE_MIN);
      DATA=await res.json();
      const st=DATA.stats||{}, r=DATA.resources||{}, nps=DATA.nps||{};
      const cur=r.current||{}, host=cur.host||{}, vh=cur.valheim||{}, series=r.series||{};
      document.getElementById("subtitle").textContent =
        "Updated "+fmtWhen(DATA.generated_at) +
        (nps.available ? " · NPS monitoring mounted" : " · NPS folder missing");

      const dur=st.duration_seconds||{};
      const lat=st.latency_ms||{};
      document.getElementById("stats").innerHTML = [
        ["Online", st.online_count, ""],
        ["Sessions", st.completed_sessions, ""],
        ["Short (&lt;2m)", st.short_sessions, st.short_sessions>0?"warn":""],
        ["Duration avg", dur.avg!=null?fmtDur(Math.round(dur.avg)):"—", ""],
        ["Duration min/max", (dur.min!=null?fmtDur(Math.round(dur.min)):"—")+" / "+(dur.max!=null?fmtDur(Math.round(dur.max)):"—"), ""],
        ["Latency avg", lat.avg!=null?(lat.avg+" ms"):"—", ""],
        ["Latency min/max", (lat.min!=null?lat.min:"—")+" / "+(lat.max!=null?lat.max:"—")+(lat.avg!=null?" ms":""), ""],
      ].map(([l,n,cls])=>`<div class="stat"><div class="n ${cls}">${n}</div><div class="l">${l}</div></div>`).join("");

      const cpu=Number(vh.cpu_pct||0), vmem=Number(vh.mem_pct||0), hmem=Number(host.mem_used_pct||0), load=Number(host.load1||0);
      const hostCpus=Number(host.cpu_count||vh.host_cpus||vh.online_cpus||0);
      const coresUsed=Number(vh.cpu_cores!=null?vh.cpu_cores:(cpu/100));
      const cpuHostPct=Number(vh.cpu_host_pct!=null?vh.cpu_host_pct:(hostCpus?cpu/hostCpus:0));
      const asked = Number(r.window_minutes || RANGE_MIN) * 60;
      const covered = Number(r.window_seconds || 0);
      document.getElementById("resource-window").textContent = covered < 1
        ? "waiting for samples"
        : (covered < asked * 0.85 ? fmtWindow(covered) + " collected" : "last " + fmtWindow(covered));
      document.querySelectorAll("#resource-range button").forEach(btn=>{
        btn.classList.toggle("on", Number(btn.dataset.min) === RANGE_MIN);
      });
      const times=(r.history||[]).map(s=>{
        const n=new Date(s.ts).getTime();
        return Number.isFinite(n) ? n : null;
      });
      const cpuText = vh.available
        ? `${coresUsed.toFixed(2)} / ${hostCpus||"?"} cores · ${cpuHostPct.toFixed(1)}% host`
        : "n/a";
      const loadCap = hostCpus > 0 ? hostCpus : 12;
      document.getElementById("resources").innerHTML = [
        gauge("Valheim CPU", cpuText, cpuHostPct, series.valheim_cpu_host_pct||series.valheim_cpu_pct, 100, times, "pct"),
        gauge("Valheim memory", vh.available?`${Number(vh.mem_used_mb||0).toFixed(0)} MiB (${vmem.toFixed(1)}%)`:"n/a", vmem, series.valheim_mem_pct, 100, times, "pct"),
        gauge("Host memory", host.available?`${Number(host.mem_used_mb||0).toFixed(0)} / ${Number(host.mem_total_mb||0).toFixed(0)} MiB`:"n/a", hmem, series.host_mem_pct, 100, times, "pct"),
        gauge("Host load (1m)", host.available?`${load.toFixed(2)} / ${loadCap}`:"n/a", Math.min(100,(load/loadCap)*100), series.host_load1, loadCap, times, "load"),
      ].join("");
      bindSparkHovers(document.getElementById("resources"));
      document.getElementById("warnings").innerHTML = (r.warnings||[]).length
        ? `<div class="warnings">${r.warnings.map(esc).join(" · ")}</div>` : "";

      const online=DATA.online||[];
      document.getElementById("online").innerHTML = online.length ? `
        <div class="table-wrap"><table>
          <thead><tr><th>Character</th><th>Net</th><th>Steam ID</th><th>Connected</th></tr></thead>
          <tbody>${online.map(p=>`<tr>
            <td class="char">${esc(p.character||"(joining…)")}</td>
            <td>${netBadge(p.network)}</td>
            <td class="steam">${esc(p.steam_id)}</td>
            <td class="time">${esc(fmtWhen(p.connected_at))}</td>
          </tr>`).join("")}</tbody></table></div>` : `<p class="empty">Nobody online</p>`;

      const sessions=DATA.sessions||[];
      document.getElementById("sessions").innerHTML = sessions.length ? `
        <div class="table-wrap"><table>
          <thead><tr>
            <th>Character</th><th>Net</th><th>Steam ID</th><th>Start</th><th>End</th>
            <th>Duration</th><th>Latency min/avg/max</th>
          </tr></thead>
          <tbody>${sessions.map(s=>`<tr class="clickable ${s.short_session?"short":""}" data-id="${esc(s.id)}">
            <td class="char">${esc(s.character||"—")}${s.short_session?'<span class="badge">short</span>':""}${!s.disconnected_at?'<span class="badge" style="border-color:#3d5a2e;color:var(--ok)">live</span>':""}</td>
            <td>${netBadge(s.network)}</td>
            <td class="steam">${esc(s.steam_id)}</td>
            <td class="time">${esc(fmtWhen(s.connected_at))}</td>
            <td class="time">${esc(s.disconnected_at?fmtWhen(s.disconnected_at):"—")}</td>
            <td>${esc(fmtDur(s.duration_seconds))}</td>
            <td>${esc(fmtLat(s.latency_ms))}</td>
          </tr>`).join("")}</tbody></table></div>` : `<p class="empty">No sessions yet</p>`;

      document.querySelectorAll("#sessions tr.clickable").forEach(tr=>{
        tr.addEventListener("click", ()=>openDetail(tr.dataset.id));
      });

      const peers=nps.peer_summaries||[];
      document.getElementById("nps").innerHTML = nps.available ? (
        peers.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Peer id</th><th>Net</th><th>Sock</th><th>Folder</th><th>Latency min/avg/max</th><th>Samples</th></tr></thead>
          <tbody>${peers.map(p=>`<tr>
            <td class="steam">${esc(p.peer_id)}</td>
            <td>${netBadge(p.network)}</td>
            <td class="time">${esc(p.sock||"—")}</td>
            <td class="time">${esc(p.folder||"—")}</td>
            <td>${esc(fmtLat(p.latency_ms))}</td>
            <td>${esc((p.latency_ms||{}).samples||0)}</td>
          </tr>`).join("")}</tbody></table></div>
          <p class="empty" style="margin-top:.6rem">LAN/WAN is inferred from NPS RTT (no client IPs in Valheim logs). Peer ids are ZDOIDs, not Steam IDs.</p>`
        : `<p class="empty">Monitoring is on, but no remote peer RTT samples yet. Join with a client to populate latency.</p>
           <p class="empty">Folders: ${(nps.folders||[]).map(esc).join(", ")||"—"} · samples ${esc(nps.sample_count||0)}</p>`
      ) : `<p class="empty">NpsMonitoring folder not mounted / not found</p>`;

      // Re-capture after await — user may have expanded a group while fetch was in flight
      const scrollState=captureUiState();
      uiState.scrollX=scrollState.scrollX;
      uiState.scrollY=scrollState.scrollY;
      const groups=DATA.event_groups||[];
      document.getElementById("events").innerHTML = groups.length ? `
        <div class="event-groups">${groups.map(g=>{
          const name=g.character || (g.steam_id ? "Unknown character" : "Unattributed");
          const sid=g.steam_id || "—";
          const key=g.steam_id || "unknown";
          const n=Number(g.count||(g.events||[]).length||0);
          const isOpen=expandedEventGroups.has(key);
          const sessMatch=(DATA.sessions||[]).find(s=>s.steam_id===g.steam_id);
          return `<details class="event-group" data-group-key="${esc(key)}"${isOpen?" open":""}>
            <summary>
              <span class="g-char">${esc(name)}</span>
              ${netBadge(sessMatch && sessMatch.network)}
              <span class="g-steam">${esc(sid)}</span>
              <span class="g-when">last ${esc(fmtWhen(g.last_time))}</span>
              <span class="g-count">${n} event${n===1?"":"s"}</span>
            </summary>
            <div class="group-body table-wrap"><table>
              <thead><tr><th>When</th><th>Tag</th><th>Message</th></tr></thead>
              <tbody>${(g.events||[]).map(e=>`<tr>
                <td class="time">${esc(fmtWhen(e.time))}</td>
                <td><span class="tag ${esc(e.tag)}">${esc(e.tag)}</span></td>
                <td class="msg">${esc(e.message)}</td>
              </tr>`).join("")}</tbody>
            </table></div>
          </details>`;
        }).join("")}</div>` : `<p class="empty">No events yet</p>`;
      restoreUiState(uiState);
    }
    document.getElementById("detail-close").onclick=()=>document.getElementById("detail").close();
    document.getElementById("chart-expand-close").onclick=()=>document.getElementById("chart-expand").close();
    document.getElementById("chart-expand").addEventListener("click", (ev)=>{
      if(ev.target===document.getElementById("chart-expand")) document.getElementById("chart-expand").close();
    });
    const rangeEl=document.getElementById("resource-range");
    rangeEl.innerHTML = RANGES.map(min=>`<button type="button" data-min="${min}">${rangeLabel(min)}</button>`).join("");
    rangeEl.addEventListener("click", (ev)=>{
      const btn=ev.target.closest("button");
      if(!btn) return;
      RANGE_MIN=Number(btn.dataset.min);
      localStorage.setItem("valheim-resource-minutes", String(RANGE_MIN));
      refresh();
    });
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    if TRUST_PROXY:
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        real = handler.headers.get("X-Real-IP", "").strip()
        if real:
            return real
    return str(handler.client_address[0])


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def _has_active_valheim_session() -> bool:
    try:
        online = get_snapshot().get("online") or []
    except Exception:  # noqa: BLE001
        return False
    return len(online) > 0


def _token_allows(handler: BaseHTTPRequestHandler, query: str) -> bool:
    if not ACCESS_TOKEN:
        return False
    qs = parse_qs(query)
    for key in ("token", "access_token"):
        vals = qs.get(key) or []
        if vals and vals[0] == ACCESS_TOKEN:
            return True
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {ACCESS_TOKEN}":
        return True
    if handler.headers.get("X-Access-Token", "") == ACCESS_TOKEN:
        return True
    return False


def _basic_password_ok(handler: BaseHTTPRequestHandler, expected: str) -> bool:
    """HTTP Basic Auth: any username, password must match expected."""
    if not expected:
        return False
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError, IndexError):
        return False
    if ":" not in raw:
        return False
    _user, password = raw.split(":", 1)
    return hmac.compare_digest(password, expected)


def access_decision(handler: BaseHTTPRequestHandler, query: str = "") -> str:
    """Return allow | challenge | deny. LAN is always allow.

    When USAGE_PUBLIC_ACCESS is off, WAN is always denied.
    When on: WAN is stealth-denied while no Valheim player is online; once a
    player is online, apply USAGE_AUTH_MODE (server_pass / override / off).
    """
    if _is_private_ip(_client_ip(handler)):
        return "allow"
    if _token_allows(handler, query):
        return "allow"
    if not USAGE_PUBLIC_ACCESS:
        return "deny"
    # Stealth deny while empty — do not send WWW-Authenticate
    if not _has_active_valheim_session():
        return "deny"
    expected = _usage_password()
    if USAGE_AUTH_MODE == "off":
        return "allow"
    if not expected:
        # override with empty USAGE_PASS, or server_pass with empty SERVER_PASS
        return "deny"
    if _basic_password_ok(handler, expected):
        return "allow"
    return "challenge"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        if args and str(args[1]).startswith("5"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _deny(self) -> None:
        self._send(404, DENIED_BODY, "text/plain; charset=utf-8")

    def _challenge(self) -> None:
        self._send(
            401,
            b"Authentication required\n",
            "text/plain; charset=utf-8",
            {"WWW-Authenticate": 'Basic realm="Valheim usage"'},
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            if USAGE_PUBLIC_ACCESS and not _is_private_ip(_client_ip(self)):
                self._deny()
                return
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        decision = access_decision(self, parsed.query)
        if decision == "challenge":
            self._challenge()
            return
        if decision != "allow":
            self._deny()
            return
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            snap = get_snapshot()
            body = dict(snap)
            body["resources"] = slice_resources(
                snap.get("resources") or {}, _query_minutes(parsed.query)
            )
            self._send(
                200,
                json.dumps(body, indent=2).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        if path.startswith("/api/session/"):
            sid = path.split("/api/session/", 1)[1].strip("/")
            sess = get_session(sid)
            if not sess:
                self._send(404, b'{"error":"not found"}\n', "application/json")
                return
            self._send(
                200,
                json.dumps(sess, indent=2).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")


def main() -> None:
    load_metrics_history()
    threading.Thread(target=metrics_loop, name="metrics", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"valheim-usage listening on http://{HOST}:{PORT}", flush=True)
    expected = _usage_password()
    print(
        f"events={EVENTS_FILE} nps={NPS_DIR} metrics={METRICS_FILE} "
        f"usage_public_access={USAGE_PUBLIC_ACCESS} "
        f"usage_auth_mode={USAGE_AUTH_MODE} "
        f"usage_pass={'set' if expected else ('off' if USAGE_AUTH_MODE == 'off' else 'missing')} "
        f"token={'set' if ACCESS_TOKEN else 'off'}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
