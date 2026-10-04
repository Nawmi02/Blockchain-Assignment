"""
p2p_node.py - Networking layer of a peer.

Every peer = TCP Server + TCP Client
  * Server role: a background thread accept()s incoming connections.
  * Client role: connect() opens a connection to another peer.

Each connection (incoming or outgoing) gets its own receive thread, so one
peer can talk to many peers at the same time.
"""

import os
import socket
import threading
import uuid

import protocol

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")


def format_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


class PeerConnection:
    """Information about one connected remote peer."""

    def __init__(self, sock, peer_id, name, address, listen_port):
        self.sock = sock
        self.peer_id = peer_id
        self.name = name
        self.address = address          # (ip, port) of the TCP connection
        self.listen_port = listen_port  # port the remote peer listens on
        # Only one thread may write to a socket at a time, otherwise the
        # bytes of a file and a text message could get mixed together.
        self.send_lock = threading.Lock()

    def label(self):
        return f"{self.name} [{self.peer_id}]  {self.address[0]}:{self.listen_port}"


class P2PNode:
    def __init__(self, name, port, on_event=None, on_peers_changed=None):
        self.name = name
        self.port = port
        self.peer_id = uuid.uuid4().hex[:8]     # short unique ID, e.g. a83f21c4

        # Callbacks used to inform the user interface
        self.on_event = on_event or (lambda kind, text: None)
        self.on_peers_changed = on_peers_changed or (lambda: None)

        self.peers = {}                          # peer_id -> PeerConnection
        self.peers_lock = threading.Lock()
        self.server_socket = None
        self.running = False

    # ------------------------------------------------------------------
    # Server role
    # ------------------------------------------------------------------

    def start(self):
        """socket() -> bind() -> listen(), then accept() in a thread."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # On Windows SO_REUSEADDR would let two peers bind the same port.
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", self.port))
            server.listen()
        except OSError:
            server.close()
            raise

        self.server_socket = server
        self.running = True
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        self.on_event("system", f"Peer started: {self.name} [{self.peer_id}] on port {self.port}")

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None

        with self.peers_lock:
            peers = list(self.peers.values())
            self.peers.clear()
        for peer in peers:
            self._close_socket(peer.sock)

        self.on_peers_changed()
        self.on_event("system", "Peer stopped")

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                break  # server socket closed by stop()
            # A new thread per connection keeps the server free to accept more.
            threading.Thread(target=self._handle_incoming, args=(conn, addr), daemon=True).start()

    def _handle_incoming(self, conn, addr):
        """Incoming connection: wait for HELLO, reply with HELLO_ACK."""
        try:
            conn.settimeout(10)
            msg = protocol.recv_message(conn)
            if msg.get("type") != protocol.HELLO:
                raise ValueError("expected HELLO message")
            protocol.send_message(conn, protocol.make_hello(self.peer_id, self.name, self.port, ack=True))
            conn.settimeout(None)
        except Exception as e:
            self.on_event("error", f"Handshake failed with {addr[0]}:{addr[1]}: {e}")
            self._close_socket(conn)
            return

        peer = PeerConnection(conn, str(msg.get("peer_id")), str(msg.get("peer_name")),
                              addr, msg.get("port"))
        if self._register_peer(peer):
            self._receive_loop(peer)

    # ------------------------------------------------------------------
    # Client role
    # ------------------------------------------------------------------

    def connect(self, ip, port):
        """socket() -> connect(), then send HELLO and wait for HELLO_ACK.
        Blocking: the UI calls this from a background thread."""
        if not self.running:
            raise RuntimeError("Start your peer first")

        sock = socket.create_connection((ip, port), timeout=5)
        try:
            protocol.send_message(sock, protocol.make_hello(self.peer_id, self.name, self.port))
            msg = protocol.recv_message(sock)
            if msg.get("type") != protocol.HELLO_ACK:
                raise ValueError("expected HELLO_ACK message")
            sock.settimeout(None)
        except Exception:
            self._close_socket(sock)
            raise

        peer = PeerConnection(sock, str(msg.get("peer_id")), str(msg.get("peer_name")),
                              (ip, port), msg.get("port"))
        if self._register_peer(peer):
            threading.Thread(target=self._receive_loop, args=(peer,), daemon=True).start()

    # ------------------------------------------------------------------
    # Peer list management
    # ------------------------------------------------------------------

    def _register_peer(self, peer):
        if peer.peer_id == self.peer_id:
            self.on_event("error", "You cannot connect to yourself")
            self._close_socket(peer.sock)
            return False

        with self.peers_lock:
            duplicate = peer.peer_id in self.peers
            if not duplicate:
                self.peers[peer.peer_id] = peer

        if duplicate:
            self.on_event("system", f"Already connected to {peer.name} [{peer.peer_id}]")
            self._close_socket(peer.sock)
            return False

        self.on_event("system", f"Connected to {peer.name} [{peer.peer_id}] "
                                f"({peer.address[0]}:{peer.listen_port}) - HELLO handshake complete")
        self.on_peers_changed()
        return True

    def _remove_peer(self, peer, reason=None):
        with self.peers_lock:
            removed = self.peers.get(peer.peer_id) is peer
            if removed:
                del self.peers[peer.peer_id]
        self._close_socket(peer.sock)
        if removed and self.running:
            text = f"{peer.name} [{peer.peer_id}] disconnected"
            if reason:
                text += f" ({reason})"
            self.on_event("system", text)
            self.on_peers_changed()

    def get_peers(self):
        with self.peers_lock:
            return list(self.peers.values())

    def disconnect_peer(self, peer_id):
        peer = self._get_peer(peer_id)
        self._remove_peer(peer, "user disconnected")

    def _get_peer(self, peer_id):
        with self.peers_lock:
            peer = self.peers.get(peer_id)
        if peer is None:
            raise ConnectionError("Peer is no longer connected")
        return peer

    @staticmethod
    def _close_socket(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    def _receive_loop(self, peer):
        """Runs in its own thread for every connected peer."""
        reason = None
        try:
            while self.running:
                msg = protocol.recv_message(peer.sock)
                msg_type = msg.get("type")
                if msg_type == protocol.TEXT:
                    self.on_event("text_in", f"{peer.name} -> You: {msg.get('message', '')}")
                elif msg_type == protocol.FILE:
                    self._receive_file(peer, msg)
                else:
                    self.on_event("system", f"Unknown message type from {peer.name}: {msg_type}")
        except ConnectionError:
            reason = None   # normal close by the other side
        except (OSError, ValueError) as e:
            reason = str(e)
        finally:
            self._remove_peer(peer, reason)

    def _receive_file(self, peer, meta):
        # basename() stops a peer from writing outside downloads/ (e.g. "../../x")
        filename = os.path.basename(str(meta.get("filename", "")).replace("\\", "/")) or "received_file"
        filesize = meta.get("filesize")
        if not isinstance(filesize, int) or isinstance(filesize, bool) or filesize < 0:
            self.on_event("error", f"Invalid file size from {peer.name}: {filesize!r}")
            # We no longer know where the file bytes end, so the stream is unusable.
            raise ValueError("invalid file size")

        path = self._unique_download_path(filename)
        self.on_event("system", f"Receiving {filename} ({format_size(filesize)}) from {peer.name}...")

        received = 0
        try:
            with open(path, "wb") as f:
                # Use filesize to know when the file ends - never assume that
                # a single recv() returns the whole file.
                while received < filesize:
                    chunk = peer.sock.recv(min(protocol.CHUNK_SIZE, filesize - received))
                    if not chunk:
                        raise ConnectionError("peer disconnected during file transfer")
                    f.write(chunk)
                    received += len(chunk)
        except Exception:
            try:
                os.remove(path)   # do not keep a half-written file
            except OSError:
                pass
            self.on_event("error", f"File transfer of {filename} from {peer.name} failed "
                                   f"({received}/{filesize} bytes)")
            raise

        rel_path = os.path.relpath(path, BASE_DIR)
        self.on_event("file_in", f"{peer.name} -> You: File received: {os.path.basename(path)} "
                                 f"({format_size(filesize)}) saved to {rel_path}")

    @staticmethod
    def _unique_download_path(filename):
        """downloads/photo.jpg, or downloads/photo (1).jpg if it already exists."""
        base, ext = os.path.splitext(filename)
        path = os.path.join(DOWNLOAD_DIR, filename)
        counter = 1
        while os.path.exists(path):
            path = os.path.join(DOWNLOAD_DIR, f"{base} ({counter}){ext}")
            counter += 1
        return path

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_text(self, peer_id, text):
        peer = self._get_peer(peer_id)
        message = protocol.make_text(self.peer_id, self.name, text)
        try:
            with peer.send_lock:
                protocol.send_message(peer.sock, message)
        except OSError as e:
            self._remove_peer(peer, str(e))
            raise ConnectionError(f"Could not send to {peer.name}: {e}")
        return peer

    def send_file(self, peer_id, filepath):
        """Stage 1: send JSON metadata. Stage 2: send raw bytes in 64 KB chunks.
        Every file type (image, audio, video, PDF, ZIP...) is just bytes."""
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"File does not exist: {filepath}")

        peer = self._get_peer(peer_id)
        filename = os.path.basename(filepath)
        filesize = os.path.getsize(filepath)
        meta = protocol.make_file_meta(self.peer_id, self.name, filename, filesize)

        f = open(filepath, "rb")   # file errors here must not drop the peer
        try:
            with peer.send_lock, f:
                protocol.send_message(peer.sock, meta)
                sent = 0
                while True:
                    chunk = f.read(protocol.CHUNK_SIZE)
                    if not chunk:
                        break
                    peer.sock.sendall(chunk)
                    sent += len(chunk)
            if sent != filesize:
                # File changed while sending - receiver is now out of sync.
                self._remove_peer(peer, "file size changed during transfer")
                raise ValueError("File size changed while sending")
        except OSError as e:
            self._remove_peer(peer, str(e))
            raise ConnectionError(f"Could not send file to {peer.name}: {e}")
        return peer, filename, filesize
