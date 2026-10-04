"""
main.py - Tkinter user interface for the P2P network.

Run:  python main.py

Network work happens in background threads. Those threads never touch the
widgets directly; they put events into a queue and the Tk main loop reads
the queue every 100 ms (Tkinter is not thread-safe).
"""

import ipaddress
import os
import queue
import socket
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText

from p2p_node import P2PNode, format_size


def get_local_ip():
    """LAN IP of this computer (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def parse_port(text):
    try:
        port = int(text.strip())
    except ValueError:
        raise ValueError(f"Invalid port: '{text}' (must be a number)")
    if not 1024 <= port <= 65535:
        raise ValueError(f"Invalid port: {port} (use 1024-65535)")
    return port


def parse_ip(text):
    ip = text.strip()
    if ip.lower() == "localhost":
        return "127.0.0.1"
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        raise ValueError(f"Invalid IP address: '{ip}'")
    return ip


class P2PApp:
    def __init__(self, root):
        self.root = root
        self.node = None
        self.events = queue.Queue()
        self.peer_ids = []          # peer_id for each row of the listbox

        root.title("UAP P2P Network")
        root.geometry("1000x680")
        root.minsize(800, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._set_running(False)
        self.root.after(100, self._process_events)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        # Simple, standard buttons with strong status colors
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure("TButton", padding=(10, 6), font=("Segoe UI", 10, "bold"))
        style.configure("Success.TButton", background="#2e7d32", foreground="#ffffff",
                        bordercolor="#2e7d32", focuscolor="#2e7d32")
        style.map("Success.TButton",
                  background=[("active", "#43a047"), ("pressed", "#2e7d32"), ("disabled", "#b7d7b8")],
                  foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#6d7d6e")],
                  bordercolor=[("active", "#43a047"), ("pressed", "#2e7d32"), ("disabled", "#b7d7b8")])

        style.configure("Danger.TButton", background="#d32f2f", foreground="#ffffff",
                        bordercolor="#d32f2f", focuscolor="#d32f2f")
        style.map("Danger.TButton",
                  background=[("active", "#ef5350"), ("pressed", "#c62828"), ("disabled", "#d9d9d9")],
                  foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#7a7a7a")],
                  bordercolor=[("active", "#ef5350"), ("pressed", "#c62828"), ("disabled", "#d9d9d9")])

        style.configure("Neutral.TButton", background="#5f6368", foreground="#ffffff",
                        bordercolor="#5f6368", focuscolor="#5f6368")
        style.map("Neutral.TButton",
                  background=[("active", "#7a7d80"), ("pressed", "#4d5155"), ("disabled", "#d9d9d9")],
                  foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#7a7a7a")],
                  bordercolor=[("active", "#7a7d80"), ("pressed", "#4d5155"), ("disabled", "#d9d9d9")])

        # My Peer
        my = ttk.LabelFrame(self.root, text="My Peer")
        my.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(my, text="Name:").grid(row=0, column=0, **pad)
        self.name_entry = ttk.Entry(my, width=18)
        self.name_entry.insert(0, "Alice")
        self.name_entry.grid(row=0, column=1, **pad)
        ttk.Label(my, text="Port:").grid(row=0, column=2, **pad)
        self.port_entry = ttk.Entry(my, width=8)
        self.port_entry.insert(0, "5000")
        self.port_entry.grid(row=0, column=3, **pad)
        self.start_btn = ttk.Button(my, text="Start Peer", command=self.start_peer, style="Success.TButton")
        self.start_btn.grid(row=0, column=4, **pad)
        self.stop_btn = ttk.Button(my, text="Stop", command=self.stop_peer, style="Danger.TButton")
        self.stop_btn.grid(row=0, column=5, **pad)
        self.info_label = ttk.Label(my, text=f"Not running  |  Your LAN IP: {get_local_ip()}")
        self.info_label.grid(row=1, column=0, columnspan=6, sticky="w", **pad)

        # Connect to Another Peer
        conn = ttk.LabelFrame(self.root, text="Connect to Another Peer")
        conn.pack(fill="x", padx=8, pady=4)
        ttk.Label(conn, text="IP:").grid(row=0, column=0, **pad)
        self.ip_entry = ttk.Entry(conn, width=18)
        self.ip_entry.insert(0, "127.0.0.1")
        self.ip_entry.grid(row=0, column=1, **pad)
        ttk.Label(conn, text="Port:").grid(row=0, column=2, **pad)
        self.remote_port_entry = ttk.Entry(conn, width=8)
        self.remote_port_entry.insert(0, "5001")
        self.remote_port_entry.grid(row=0, column=3, **pad)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.connect_peer, style="Success.TButton")
        self.connect_btn.grid(row=0, column=4, **pad)

        # Connected peers + log
        middle = ttk.Frame(self.root)
        middle.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.LabelFrame(middle, text="Connected Peers")
        left.pack(side="left", fill="y")
        self.peer_list = tk.Listbox(left, width=42, exportselection=False, activestyle="none")
        self.peer_list.pack(fill="both", expand=True, padx=4, pady=(4, 2))
        self.disconnect_btn = ttk.Button(left, text="Disconnect", command=self.disconnect_selected_peer, style="Danger.TButton")
        self.disconnect_btn.pack(fill="x", padx=4, pady=(0, 4))

        right = ttk.LabelFrame(middle, text="Messages / Events")
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.log = ScrolledText(right, state="disabled", wrap="word", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        self.log.tag_config("system", foreground="#555555")
        self.log.tag_config("error", foreground="#c62828")
        self.log.tag_config("text_in", foreground="#1b5e20")
        self.log.tag_config("text_out", foreground="#0d47a1")
        self.log.tag_config("file_in", foreground="#6a1b9a")
        self.log.tag_config("file_out", foreground="#6a1b9a")

        # Send text
        text_frame = ttk.LabelFrame(self.root, text="Send Text")
        text_frame.pack(fill="x", padx=8, pady=4)
        self.msg_entry = ttk.Entry(text_frame)
        self.msg_entry.pack(side="left", fill="x", expand=True, **pad)
        self.msg_entry.bind("<Return>", lambda e: self.send_text())
        self.send_btn = ttk.Button(text_frame, text="Send", command=self.send_text, style="Neutral.TButton")
        self.send_btn.pack(side="right", **pad)

        # Send file
        file_frame = ttk.LabelFrame(self.root, text="Send File")
        file_frame.pack(fill="x", padx=8, pady=4)
        ttk.Label(file_frame, text="Text, image, audio, video, PDF, ZIP, etc.").pack(side="left", **pad)
        self.file_btn = ttk.Button(file_frame, text="Choose File & Send", command=self.send_file, style="Neutral.TButton")
        self.file_btn.pack(side="right", **pad)

        # Status bar
        self.status = ttk.Label(self.root, text="Ready", relief="sunken", anchor="w")
        self.status.pack(fill="x", side="bottom")

    def _set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.name_entry.config(state="disabled" if running else "normal")
        self.port_entry.config(state="disabled" if running else "normal")
        self.connect_btn.config(state="normal" if running else "disabled")
        self.disconnect_btn.config(state="normal" if running and bool(self._selected_peer_id()) else "disabled")
        self.send_btn.config(state="normal" if running and bool(self._selected_peer_id()) else "disabled")
        self.file_btn.config(state="normal" if running and bool(self._selected_peer_id()) else "disabled")

    # ------------------------------------------------------------------
    # Thread-safe event handling
    # ------------------------------------------------------------------

    def _emit(self, kind, text):
        """Can be called from any thread."""
        self.events.put(("event", kind, text))

    def _peers_changed(self):
        self.events.put(("peers",))

    def _process_events(self):
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] == "event":
                    self._log(item[1], item[2])
                else:
                    self._refresh_peers()
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)

    def _log(self, kind, text):
        prefix = {"system": "[SYSTEM] ", "error": "[ERROR] "}.get(kind, "")
        self.log.config(state="normal")
        self.log.insert("end", prefix + text + "\n", kind)
        self.log.see("end")
        self.log.config(state="disabled")
        self.status.config(text=prefix + text)

    def _refresh_peers(self):
        selected = self._selected_peer_id()
        self.peer_list.delete(0, "end")
        self.peer_ids = []
        if self.node:
            for peer in self.node.get_peers():
                self.peer_list.insert("end", peer.label())
                self.peer_ids.append(peer.peer_id)
        if selected in self.peer_ids:
            self.peer_list.selection_set(self.peer_ids.index(selected))
        elif len(self.peer_ids) == 1:
            self.peer_list.selection_set(0)   # convenient default

        has_selection = bool(self.node and self._selected_peer_id())
        self.send_btn.config(state="normal" if has_selection else "disabled")
        self.file_btn.config(state="normal" if has_selection else "disabled")
        self.disconnect_btn.config(state="normal" if has_selection else "disabled")

    def disconnect_selected_peer(self):
        if not self.node:
            return
        peer_id = self._selected_peer_id()
        if not peer_id:
            self._log("error", "Select a peer to disconnect")
            return
        try:
            self.node.disconnect_peer(peer_id)
        except Exception as e:
            self._log("error", f"Could not disconnect peer: {e}")
        else:
            self._refresh_peers()

    def _selected_peer_id(self):
        sel = self.peer_list.curselection()
        if not sel or sel[0] >= len(self.peer_ids):
            return None
        return self.peer_ids[sel[0]]

    def _run_in_background(self, func):
        threading.Thread(target=func, daemon=True).start()

    # ------------------------------------------------------------------
    # Button actions
    # ------------------------------------------------------------------

    def start_peer(self):
        name = self.name_entry.get().strip()
        if not name:
            self._log("error", "Please enter a peer name")
            return
        try:
            port = parse_port(self.port_entry.get())
        except ValueError as e:
            self._log("error", str(e))
            return

        node = P2PNode(name, port, on_event=self._emit, on_peers_changed=self._peers_changed)
        try:
            node.start()
        except OSError as e:
            self._log("error", f"Could not start peer on port {port}: {e}")
            return

        self.node = node
        self._set_running(True)
        self.info_label.config(text=f"{name}  |  ID: {node.peer_id}  |  Port: {port}  |  "
                                    f"Your LAN IP: {get_local_ip()}")
        self.root.title(f"UAP P2P Network - {name} ({port})")

    def stop_peer(self):
        if self.node:
            self.node.stop()
            self.node = None
        self._set_running(False)
        self._refresh_peers()
        self.info_label.config(text=f"Not running  |  Your LAN IP: {get_local_ip()}")
        self.root.title("UAP P2P Network")

    def connect_peer(self):
        if not self.node:
            self._log("error", "Start your peer first")
            return
        try:
            ip = parse_ip(self.ip_entry.get())
            port = parse_port(self.remote_port_entry.get())
        except ValueError as e:
            self._log("error", str(e))
            return

        node = self.node
        self._emit("system", f"Connecting to {ip}:{port}...")

        def task():
            try:
                node.connect(ip, port)
            except ConnectionRefusedError:
                self._emit("error", f"Connection failed: Connection refused "
                                    f"(is a peer running on {ip}:{port}?)")
            except socket.timeout:
                self._emit("error", f"Connection failed: timed out ({ip}:{port} not reachable)")
            except Exception as e:
                self._emit("error", f"Connection failed: {e}")

        self._run_in_background(task)

    def send_text(self):
        text = self.msg_entry.get().strip()
        if not text:
            return
        if not self.node:
            self._log("error", "Start your peer first")
            return
        peer_id = self._selected_peer_id()
        if not peer_id:
            self._log("error", "Select a peer from the Connected Peers list first")
            return

        self.msg_entry.delete(0, "end")
        node = self.node

        def task():
            try:
                peer = node.send_text(peer_id, text)
                self._emit("text_out", f"You -> {peer.name}: {text}")
            except Exception as e:
                self._emit("error", f"Message not sent: {e}")

        self._run_in_background(task)

    def send_file(self):
        if not self.node:
            self._log("error", "Start your peer first")
            return
        peer_id = self._selected_peer_id()
        if not peer_id:
            self._log("error", "Select a peer from the Connected Peers list first")
            return

        path = filedialog.askopenfilename(title="Choose a file to send")
        if not path:
            return
        if not os.path.isfile(path):
            self._log("error", f"File does not exist: {path}")
            return

        node = self.node
        name = os.path.basename(path)
        self._log("system", f"Sending {name} ({format_size(os.path.getsize(path))})...")

        def task():
            try:
                peer, filename, size = node.send_file(peer_id, path)
                self._emit("file_out", f"You -> {peer.name}: File sent: {filename} ({format_size(size)})")
            except Exception as e:
                self._emit("error", f"File not sent: {e}")

        self._run_in_background(task)

    def on_close(self):
        if self.node:
            self.node.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    P2PApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
