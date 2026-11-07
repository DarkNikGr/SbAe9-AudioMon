# SPDX-License-Identifier: GPL-2.0-only
"""The PipeWire graph: watched with `pw-dump --monitor`, changed only with `pw-link`."""

import codecs
import json
import os
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

from sbae9kit import ae9

from .core import APP_NAME, CHANNEL_ORDER, child_cmd


class Graph:
    """Keeps a live copy of the PipeWire object list from `pw-dump --monitor`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.objects = {}
        self.version = 0
        self.error = None
        self.down_since = None      # monotonic time pw-dump lost PipeWire, None while connected
        self.restarts = 0           # times PipeWire came back after being lost
        self._stopping = False
        self._proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return self.version, list(self.objects.values())

    def _apply(self, arr):
        with self.lock:
            for o in arr if isinstance(arr, list) else [arr]:
                if not isinstance(o, dict) or "id" not in o:
                    continue
                if o.get("info") is None:
                    self.objects.pop(o["id"], None)
                else:
                    self.objects[o["id"]] = o
            if self.down_since is not None and self.objects:
                self.down_since = None
                self.restarts += 1
            self.version += 1

    def _run(self):
        dec = json.JSONDecoder()
        while not self._stopping:
            try:
                proc = subprocess.Popen(child_cmd(["pw-dump", "--monitor", "--no-colors"]),
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                self.error = "pw-dump not found (install pipewire-utils)"
                return
            self._proc = proc
            with self.lock:
                self.objects = {}
                self.version += 1
            utf8 = codecs.getincrementaldecoder("utf-8")("replace")
            buf = ""
            fd = proc.stdout.fileno()
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                buf += utf8.decode(chunk)
                pos = 0
                while True:
                    while pos < len(buf) and buf[pos].isspace():
                        pos += 1
                    if pos >= len(buf):
                        break
                    try:
                        arr, pos = dec.raw_decode(buf, pos)
                    except json.JSONDecodeError:
                        break  # incomplete; wait for more
                    self._apply(arr)
                buf = buf[pos:]
                if len(buf) > 64 << 20:  # garbage we can't parse; start over
                    buf = ""
            proc.wait()
            if not self._stopping:
                # PipeWire went away (restarted or stopped): every meter and
                # link is gone with it. Try again until it's back.
                with self.lock:
                    if self.down_since is None:
                        self.down_since = time.monotonic()
                time.sleep(1.0)

    def stop(self):
        self._stopping = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


class Snapshot:
    """Nodes, ports and links parsed from a Graph snapshot."""

    def __init__(self, objects):
        self.nodes, self.ports, self.links = {}, {}, {}
        for o in objects:
            t = o.get("type", "")
            info = o.get("info") or {}
            props = info.get("props") or {}
            if t.endswith(":Node"):
                name = props.get("node.name", "")
                self.nodes[o["id"]] = SimpleNamespace(
                    id=o["id"], name=name, props=props,
                    desc=props.get("node.description") or props.get("node.nick") or name,
                    media_class=props.get("media.class", ""),
                    state=info.get("state", ""), error=info.get("error"),
                    params=info.get("params") or {})
            elif t.endswith(":Port"):
                direction = info.get("direction") or props.get("port.direction") or ""
                self.ports[o["id"]] = SimpleNamespace(
                    id=o["id"], node=props.get("node.id"), name=props.get("port.name", ""),
                    out=direction.startswith("out"), channel=props.get("audio.channel", ""),
                    monitor=bool(props.get("port.monitor")))
            elif t.endswith(":Link"):
                self.links[o["id"]] = SimpleNamespace(
                    id=o["id"], out_node=info.get("output-node-id"), out_port=info.get("output-port-id"),
                    in_node=info.get("input-node-id"), in_port=info.get("input-port-id"))
        self.node_ports = {}
        for p in self.ports.values():
            self.node_ports.setdefault(p.node, []).append(p)

    def by_name(self, name):
        for n in self.nodes.values():
            if n.name == name:
                return n
        return None

    def out_ports(self, node, monitor=False):
        ps = [p for p in self.node_ports.get(node.id, []) if p.out and p.monitor == monitor]
        return sorted(ps, key=lambda p: (CHANNEL_ORDER.index(p.channel)
                                         if p.channel in CHANNEL_ORDER else 99, p.id))


def is_ours(n):
    """One of the AE-9 apps' own nodes (our meters), never the card."""
    return n.props.get("application.name") == APP_NAME or ae9.is_app_node(n.props)


def is_ae9(n):
    """Shared detection (sbae9kit.ae9): by name, never PCI address or card number."""
    return ae9.is_ae9(n.props)


def find_ae9(snap):
    """Locate the AE-9 nodes. Returns dict key -> node (or None)."""
    found = {"monitor": None, "optical_out": None, "wuh": None, "mic": None,
             "optical": None, "rear_rca": None}
    for n in sorted(snap.nodes.values(), key=lambda n: n.id):
        if is_ours(n) or not is_ae9(n):
            continue
        name = n.name.lower()
        if n.media_class == "Audio/Sink":
            if "ae9_optical" in name or "optical" in n.desc.lower():
                found["optical_out"] = found["optical_out"] or n
            elif found["monitor"] is None or ("analog" in name and "analog" not in found["monitor"].name):
                found["monitor"] = n
        elif n.media_class == "Audio/Source":
            kind = ae9.input_kind(n.props)
            if kind == "what_u_hear":
                found["wuh"] = found["wuh"] or n
            elif kind == "optical_in":
                found["optical"] = found["optical"] or n
            elif kind == "rear_rca":
                found["rear_rca"] = found["rear_rca"] or n
            elif found["mic"] is None or ("analog" in name and "analog" not in found["mic"].name):
                found["mic"] = n
    return found


class Linker:
    """Runs pw-link commands on a worker thread so the UI never blocks."""

    def __init__(self):
        self.q = queue.Queue()
        self.failures = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def link(self, out_port, in_port):
        self.q.put(["pw-link", "--wait", str(out_port), str(in_port)])

    def unlink(self, link_id):
        self.q.put(["pw-link", "--disconnect", str(link_id)])

    def _run(self):
        while True:
            cmd = self.q.get()
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if r.returncode:
                    self.failures.put(f"{' '.join(cmd)}: {r.stderr.strip() or r.returncode}")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.failures.put(f"{' '.join(cmd)}: {e}")
