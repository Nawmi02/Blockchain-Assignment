"""
protocol.py - Application-level protocol for the P2P network.

Every control message is a JSON object sent with length-prefix framing:

    [4-byte length (big-endian unsigned int)][JSON payload (UTF-8)]

TCP is a byte stream, so the receiver first reads exactly 4 bytes to learn
the payload length, then reads exactly that many bytes.

File transfer = one "file" JSON message (metadata) followed immediately by
exactly `filesize` raw bytes on the same connection.
"""

import json
import struct

HEADER_SIZE = 4                       # bytes used for the length prefix
MAX_MESSAGE_SIZE = 10 * 1024 * 1024   # safety limit for one JSON message
CHUNK_SIZE = 64 * 1024                # 64 KB chunks for file transfer

# Message types
HELLO = "hello"
HELLO_ACK = "hello_ack"
TEXT = "text"
FILE = "file"


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def make_hello(peer_id, peer_name, port, ack=False):
    return {
        "type": HELLO_ACK if ack else HELLO,
        "peer_id": peer_id,
        "peer_name": peer_name,
        "port": port,
    }


def make_text(sender_id, sender_name, message):
    return {
        "type": TEXT,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message": message,
    }


def make_file_meta(sender_id, sender_name, filename, filesize):
    return {
        "type": FILE,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "filename": filename,
        "filesize": filesize,
    }


# ---------------------------------------------------------------------------
# Framing: encode / decode
# ---------------------------------------------------------------------------

def encode_message(message):
    """dict -> [4-byte length][JSON bytes]"""
    payload = json.dumps(message).encode("utf-8")
    return struct.pack("!I", len(payload)) + payload


def send_message(sock, message):
    sock.sendall(encode_message(message))


def recv_exact(sock, n):
    """Read exactly n bytes. One recv() may return fewer bytes than asked,
    so keep reading until we have all n bytes."""
    buffer = bytearray()
    while len(buffer) < n:
        chunk = sock.recv(n - len(buffer))
        if not chunk:
            raise ConnectionError("Peer closed the connection")
        buffer.extend(chunk)
    return bytes(buffer)


def recv_message(sock):
    """Read one framed JSON message and return it as a dict."""
    header = recv_exact(sock, HEADER_SIZE)
    (length,) = struct.unpack("!I", header)
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes")
    payload = recv_exact(sock, length)
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict) or "type" not in message:
        raise ValueError("Invalid message format")
    return message
