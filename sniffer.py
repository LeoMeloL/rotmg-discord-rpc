"""Passive capture of the game's server->client traffic via Npcap (scapy).

Nothing is injected into the game or the connection: we only read copies of
the packets the network card already sees. Each TCP connection to port 2050 is
reassembled in order and fed to a StreamDecoder. A connection must be observed
from its handshake (SYN-ACK) so the RC4 state lines up; connections already
open when the tool starts are skipped until the next map change.
"""
from __future__ import annotations

import logging
import os
import queue
import struct
import threading
import time

from scapy.all import IP, TCP, AsyncSniffer, conf

from protocol import StreamDecoder

log = logging.getLogger("sniffer")

GAME_PORT = 2050
MAX_PENDING_BYTES = 4 * 1024 * 1024


class Flow:
    def __init__(self, next_seq: int):
        self.next_seq = next_seq
        self.pending: dict[int, bytes] = {}
        self.pending_bytes = 0
        self.decoder = StreamDecoder()


def _seq_diff(a: int, b: int) -> int:
    """a - b in 32-bit sequence space."""
    d = (a - b) & 0xFFFFFFFF
    return d - 0x100000000 if d & 0x80000000 else d


class Sniffer:
    """Calls on_packet(flow_key, packet_id, body) and on_flow_closed(flow_key)."""

    def __init__(self, on_packet, on_flow_closed, ifaces: list[str] | None = None):
        self.on_packet = on_packet
        self.on_flow_closed = on_flow_closed
        self.ifaces = ifaces or self._default_ifaces()
        self.flows: dict[tuple, Flow] = {}
        self.queue: queue.Queue = queue.Queue(maxsize=50_000)
        self.last_traffic = 0.0
        # Set RPC_DUMP=<file> to save decrypted packets ([u8 id][u32 len][body]) for debugging.
        dump = os.environ.get("RPC_DUMP")
        self.dump_file = open(dump, "ab", buffering=0) if dump else None
        self._sniffer: AsyncSniffer | None = None
        self._worker = threading.Thread(target=self._run, name="flow-worker", daemon=True)

    @staticmethod
    def _default_ifaces() -> list[str]:
        names = []
        for iface in conf.ifaces.values():
            ips = iface.ips.get(4) or []
            if any(ip and not ip.startswith(("127.", "169.254.")) for ip in ips):
                names.append(iface.name)
        return names

    def start(self):
        log.info("Capturing on: %s", ", ".join(self.ifaces))
        self._worker.start()
        self._sniffer = AsyncSniffer(iface=self.ifaces, filter=f"tcp src port {GAME_PORT}",
                                     prn=self._capture, store=False)
        self._sniffer.start()

    def stop(self):
        if self._sniffer:
            self._sniffer.stop()

    # Runs on scapy's capture thread: keep it cheap.
    def _capture(self, pkt):
        if IP not in pkt or TCP not in pkt:
            return
        ip, tcp = pkt[IP], pkt[TCP]
        key = (ip.src, tcp.sport, ip.dst, tcp.dport)
        # bytes(tcp.payload) also contains Ethernet padding (zeros added to short
        # frames such as bare ACKs); the IP header says how much is real data.
        payload = bytes(tcp.payload)
        if ip.len:  # 0 when the NIC offloads segmentation
            payload = payload[:max(0, ip.len - ip.ihl * 4 - tcp.dataofs * 4)]
        try:
            self.queue.put_nowait((key, int(tcp.flags), tcp.seq, payload))
        except queue.Full:
            log.warning("capture queue full, dropping packet")

    def _run(self):
        while True:
            key, flags, seq, payload = self.queue.get()
            try:
                self._handle(key, flags, seq, payload)
            except Exception:
                log.exception("flow handling failed")

    def _drop(self, key, reason: str):
        if self.flows.pop(key, None) is not None:
            log.info("Connection %s:%s closed (%s)", key[0], key[1], reason)
            self.on_flow_closed(key)

    def _handle(self, key, flags: int, seq: int, payload: bytes):
        SYN, FIN, RST = 0x02, 0x01, 0x04
        if flags & SYN:
            self.flows[key] = Flow((seq + 1) & 0xFFFFFFFF)
            log.info("New game connection %s:%s -> %s:%s", *key)
            return
        if payload:
            self.last_traffic = time.time()
        flow = self.flows.get(key)
        if flow is None:
            return
        if payload:
            self._add_segment(key, flow, seq, payload)
        if flags & (FIN | RST) and key in self.flows:
            self._drop(key, "FIN/RST")

    def _add_segment(self, key, flow: Flow, seq: int, payload: bytes):
        d = _seq_diff(seq, flow.next_seq)
        if d < 0:  # retransmission / overlap
            if -d >= len(payload):
                return
            payload = payload[-d:]
            d = 0
        if d > 0:
            if seq not in flow.pending:
                flow.pending[seq] = payload
                flow.pending_bytes += len(payload)
                if flow.pending_bytes > MAX_PENDING_BYTES:
                    self._drop(key, "lost segment, waiting for next map change")
            return
        self._deliver(key, flow, payload)
        while flow.pending and key in self.flows:
            nxt = flow.pending.pop(flow.next_seq, None)
            if nxt is not None:
                flow.pending_bytes -= len(nxt)
                self._deliver(key, flow, nxt)
                continue
            # Segments that start behind next_seq (overlapping retransmits).
            progressed = False
            for s in [s for s in flow.pending if _seq_diff(s, flow.next_seq) < 0]:
                data = flow.pending.pop(s)
                flow.pending_bytes -= len(data)
                over = _seq_diff((s + len(data)) & 0xFFFFFFFF, flow.next_seq)
                if over > 0:
                    self._deliver(key, flow, data[-over:])
                    progressed = True
                    break
            if not progressed:
                return

    def _deliver(self, key, flow: Flow, data: bytes):
        flow.next_seq = (flow.next_seq + len(data)) & 0xFFFFFFFF
        try:
            packets = list(flow.decoder.feed(data))
        except ValueError as e:
            self._drop(key, str(e))
            return
        for pid, body in packets:
            if self.dump_file:
                self.dump_file.write(struct.pack("<BI", pid, len(body)) + body)
            try:
                self.on_packet(key, pid, body)
            except Exception:
                log.exception("failed to handle packet id %d", pid)
