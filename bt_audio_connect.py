# type: ignore
"""
Bluetooth Audio Playback Connection Manager

Connects to Bluetooth audio devices via Windows AudioPlaybackConnection API.
Supports device listing, interactive selection, auto-reconnect, graceful
shutdown, live "Now Playing" display, and keyboard playback controls.

Usage:
    python bt_audio_connect.py                  # Interactive device picker
    python bt_audio_connect.py --list           # List available devices
    python bt_audio_connect.py --device 0       # Connect to device by index
    python bt_audio_connect.py --name "Speaker" # Connect by partial name match
    python bt_audio_connect.py --auto-reconnect # Reconnect on disconnect
    python bt_audio_connect.py --now-playing    # Show track info + controls
"""

import asyncio
import argparse
import ctypes
import msvcrt
import signal
import sys

import time
from enum import IntEnum
from datetime import datetime

from winsdk.windows.media.audio import AudioPlaybackConnection
from winsdk.windows.media.audio import AudioPlaybackConnectionOpenResultStatus
from winsdk.windows.devices.enumeration import DeviceInformation


# ── ANSI helpers (Windows Terminal supports these) ───────────────────────────

class Colors:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    CYAN    = "\033[96m"
    MAGENTA = "\033[95m"
    BLUE    = "\033[94m"

def _c(text, color):
    return f"{color}{text}{Colors.RESET}"

def ok(msg):      print(_c("  ✓ ", Colors.GREEN)  + msg)
def warn(msg):    print(_c("  ⚠ ", Colors.YELLOW) + msg)
def err(msg):     print(_c("  ✗ ", Colors.RED)     + msg)
def info(msg):    print(_c("  ℹ ", Colors.CYAN)    + msg)
def heading(msg): print(f"\n{_c(msg, Colors.BOLD + Colors.MAGENTA)}")


# ── Enums ────────────────────────────────────────────────────────────────────

class ConnectionState(IntEnum):
    """Mirrors AudioPlaybackConnectionState."""
    CLOSED = 0
    OPENED = 1


class PlaybackStatus(IntEnum):
    """Mirrors GlobalSystemMediaTransportControlsSessionPlaybackStatus."""
    CLOSED   = 0
    OPENED   = 1
    CHANGING = 2
    STOPPED  = 3
    PLAYING  = 4
    PAUSED   = 5


_PLAYBACK_ICONS = {
    PlaybackStatus.PLAYING:  "▶ ",
    PlaybackStatus.PAUSED:   "⏸ ",
    PlaybackStatus.STOPPED:  "⏹ ",
    PlaybackStatus.CHANGING: "⟳ ",
}


# ── Virtual-key codes for media simulation ──────────────────────────────────

_VK_VOLUME_DOWN      = 0xAE
_VK_VOLUME_UP        = 0xAF
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3
_KEYEVENTF_KEYUP     = 0x0002


# ── Globals ──────────────────────────────────────────────────────────────────

_shutdown_event: asyncio.Event = None
_connection = None
_auto_reconnect = False
_reconnect_requested: asyncio.Event = None
_connected_since: float | None = None
_loop: asyncio.AbstractEventLoop = None


# ── Time helpers ─────────────────────────────────────────────────────────────

def _elapsed() -> str:
    """Human-readable duration since connection was established."""
    if _connected_since is None:
        return ""
    secs = int(time.monotonic() - _connected_since)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:  parts.append(f"{h}h")
    if m:  parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ── Audio-connection callbacks ───────────────────────────────────────────────

def _state_changed(sender, args):
    """Callback invoked by the AudioPlaybackConnection on state transitions."""
    global _connected_since

    if sender.state == ConnectionState.OPENED:
        _connected_since = time.monotonic()
        ok(f"[{_timestamp()}] State → {_c('Connected', Colors.GREEN)}")
    else:
        duration = _elapsed()
        _connected_since = None
        warn(f"[{_timestamp()}] State → {_c('Disconnected', Colors.YELLOW)}"
             + (f"  (was connected for {duration})" if duration else ""))
        # Trigger reconnect if enabled
        if _auto_reconnect and _reconnect_requested is not None:
            _reconnect_requested.set()





# ── Playback controls ───────────────────────────────────────────────────────

def _send_key(vk_code):
    """Simulate a press of a hardware key."""
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, _KEYEVENTF_KEYUP, 0)

def _toggle_play_pause():
    _send_key(_VK_MEDIA_PLAY_PAUSE)

def _skip_previous():
    _send_key(_VK_MEDIA_PREV_TRACK)

def _skip_next():
    _send_key(_VK_MEDIA_NEXT_TRACK)

def _volume_up():
    _send_key(_VK_VOLUME_UP)

def _volume_down():
    _send_key(_VK_VOLUME_DOWN)


# ── Keyboard input ───────────────────────────────────────────────────────────

def _read_key_blocking() -> str | None:
    """
    Poll msvcrt.kbhit() for up to ~200 ms.
    Returns the character if a key was pressed, otherwise None.
    Runs inside the default ThreadPoolExecutor via run_in_executor.
    """
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            return msvcrt.getwch()
        time.sleep(0.05)
    return None


async def _keyboard_loop():
    """
    Async loop: offloads blocking key-reads to the executor, then dispatches
    playback commands directly on the event loop thread.
    """
    loop = asyncio.get_running_loop()
    while not _shutdown_event.is_set():
        ch = await loop.run_in_executor(None, _read_key_blocking)
        if ch is None:
            continue
        if ch == "p":
            _toggle_play_pause()
        elif ch == ",":
            _skip_previous()
        elif ch == ".":
            _skip_next()
        elif ch == "0":
            _volume_up()
        elif ch == "9":
            _volume_down()
        elif ch == "\x03":  # Ctrl+C
            _shutdown_event.set()
            break


# ── Device helpers ───────────────────────────────────────────────────────────

async def _enumerate_devices() -> list:
    """Return a list of AudioPlaybackConnection-compatible devices."""
    selector = AudioPlaybackConnection.get_device_selector()
    devices = await DeviceInformation.find_all_async(selector, [])
    return list(devices)


def _print_device_table(devices):
    """Pretty-print the device list."""
    if not devices:
        warn("No Bluetooth audio devices found.")
        return
    heading("Available Devices")
    for i, d in enumerate(devices):
        idx  = _c(f"[{i}]", Colors.BLUE)
        name = _c(d.name, Colors.BOLD)
        print(f"  {idx}  {name}")
        print(f"       {_c('ID:', Colors.DIM)} {d.id}")
    print()


def _pick_device_interactive(devices):
    """Prompt the user to pick a device by index."""
    while True:
        try:
            raw = input(_c("  → Select device index: ", Colors.CYAN)).strip()
            idx = int(raw)
            if 0 <= idx < len(devices):
                return devices[idx]
            err(f"Index out of range (0–{len(devices) - 1})")
        except ValueError:
            err("Please enter a valid number.")
        except (EOFError, KeyboardInterrupt):
            print()
            return None


def _find_device_by_name(devices, query: str):
    """Case-insensitive partial match on device name."""
    q = query.lower()
    matches = [d for d in devices if q in d.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        warn(f"Multiple devices match \"{query}\":")
        _print_device_table(matches)
        return _pick_device_interactive(matches)
    return None


def _print_controls_help():
    """Show the keyboard shortcut legend."""
    heading("Playback Controls")
    shortcuts = [
        ("p", "Play / Pause"),
        (",", "Previous track"),
        (".", "Next track"),
        ("9", "Volume down"),
        ("0", "Volume up"),
    ]
    for key, desc in shortcuts:
        print(f"    {_c(key, Colors.BOLD + Colors.CYAN)}  {desc}")
    print()


# ── Core logic ───────────────────────────────────────────────────────────────

async def connect(device, active_connect=True) -> int:
    """
    Create, start, and open an AudioPlaybackConnection for *device*.
    Returns 0 on success, 1 on failure.
    """
    global _connection, _connected_since

    if active_connect:
        info(f"Actively connecting to {_c(device.name, Colors.BOLD)} …")
    else:
        info(f"Opening audio transport for {_c(device.name, Colors.BOLD)} …")

    connection = AudioPlaybackConnection.try_create_from_id(device.id)
    if connection is None:
        err("Failed to create AudioPlaybackConnection (unsupported device?).")
        return 1

    _connection = connection
    connection.add_state_changed(_state_changed)

    # -- Start --
    info("Starting audio transport …")
    try:
        await connection.start_async()
    except Exception as exc:
        err(f"start_async failed: {exc}")
        return 1
    ok("Transport started.")

    if not active_connect:
        info("Waiting for the device to initiate connection...")
        return 0

    # -- Open --
    info("Opening connection …")
    try:
        result = await connection.open_async()
    except Exception as exc:
        err(f"open_async failed: {exc}")
        return 1

    if result.status == AudioPlaybackConnectionOpenResultStatus.SUCCESS:
        ok("Connection opened successfully! 🎵")
        _connected_since = time.monotonic()
        return 0
    elif result.status == AudioPlaybackConnectionOpenResultStatus.REQUEST_TIMED_OUT:
        err("Connection timed out – is the device in range and powered on?")
    elif result.status == AudioPlaybackConnectionOpenResultStatus.DENIED_BY_SYSTEM:
        err("Connection denied by the system.")
    elif result.status == AudioPlaybackConnectionOpenResultStatus.UNKNOWN_FAILURE:
        err("Connection failed with an unknown error.")
    else:
        err(f"Connection failed (status={result.status}).")
    return 1


async def run(args):
    """Main entry-point coroutine."""
    global _shutdown_event, _auto_reconnect, _reconnect_requested
    global _loop

    _shutdown_event     = asyncio.Event()
    _reconnect_requested = asyncio.Event()
    _auto_reconnect     = args.auto_reconnect
    _loop               = asyncio.get_running_loop()

    # Register Ctrl-C handler
    for sig in (signal.SIGINT, signal.SIGBREAK):
        try:
            _loop.add_signal_handler(sig, _shutdown_event.set)
        except NotImplementedError:
            # add_signal_handler is unavailable on the Windows ProactorEventLoop;
            # fall back to the standard signal module.
            signal.signal(sig, lambda *_: _shutdown_event.set())

    # ── Enumerate devices ────────────────────────────────────────────────
    heading("Bluetooth Audio Connection Manager")
    info("Scanning for devices …")

    devices = await _enumerate_devices()

    if not devices:
        err("No compatible Bluetooth audio devices found.")
        return 1

    _print_device_table(devices)

    if args.list:
        return 0  # --list mode: just print and exit

    # ── Select device ────────────────────────────────────────────────────
    device = None

    if args.name:
        device = _find_device_by_name(devices, args.name)
        if device is None:
            err(f"No device matching \"{args.name}\".")
            return 1

    elif args.device is not None:
        if 0 <= args.device < len(devices):
            device = devices[args.device]
        else:
            err(f"Device index {args.device} out of range (0–{len(devices) - 1}).")
            return 1

    elif len(devices) == 1:
        device = devices[0]
        info("Auto-selected the only available device.")

    else:
        device = _pick_device_interactive(devices)
        if device is None:
            return 1

    # ── Connect ──────────────────────────────────────────────────────────
    rc = await connect(device, active_connect=not args.listen)
    if rc != 0 and not _auto_reconnect:
        return rc

    # ── Controls legend ──────────────────────────────────────────────────
    _print_controls_help()

    if _auto_reconnect:
        info(f"Auto-reconnect is {_c('enabled', Colors.GREEN)}.")
    info(f"Press {_c('Ctrl+C', Colors.BOLD)} to disconnect and exit.\n")

    # ── Keyboard task ────────────────────────────────────────────────────
    kb_task = asyncio.create_task(_keyboard_loop())

    # ── Keep alive / reconnect loop ──────────────────────────────────────
    reconnect_delay = 3  # seconds between reconnect attempts

    while not _shutdown_event.is_set():
        reconnect_task = asyncio.create_task(_reconnect_requested.wait())
        shutdown_task  = asyncio.create_task(_shutdown_event.wait())

        done, _ = await asyncio.wait(
            {reconnect_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Clean up the losing task
        for t in (reconnect_task, shutdown_task):
            if t not in done:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        if _shutdown_event.is_set():
            break

        # Reconnect requested
        _reconnect_requested.clear()
        warn(f"Reconnecting in {reconnect_delay}s …")
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=reconnect_delay)
            break  # shutdown during delay
        except asyncio.TimeoutError:
            pass

        rc = await connect(device, active_connect=not args.listen)
        if rc != 0:
            warn("Reconnect failed – will retry on next state change.")

    # ── Cleanup ──────────────────────────────────────────────────────────
    if kb_task is not None:
        kb_task.cancel()
        try:
            await kb_task
        except asyncio.CancelledError:
            pass

    heading("Shutting down …")

    if _connection is not None:
        try:
            _connection.close()
            ok("Connection closed.")
        except Exception as exc:
            warn(f"Error closing connection: {exc}")

    ok("Goodbye! 👋")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Connect to a Bluetooth audio device via AudioPlaybackConnection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sel = p.add_mutually_exclusive_group()
    sel.add_argument(
        "-d", "--device",
        type=int,
        metavar="INDEX",
        help="Connect to device by index (shown in --list).",
    )
    sel.add_argument(
        "-n", "--name",
        type=str,
        metavar="QUERY",
        help="Connect by partial, case-insensitive name match.",
    )
    p.add_argument(
        "-l", "--list",
        action="store_true",
        help="List available devices and exit.",
    )
    p.add_argument(
        "-r", "--auto-reconnect",
        action="store_true",
        help="Automatically reconnect when the device disconnects.",
    )
    p.add_argument(
        "--listen",
        action="store_true",
        help="Wait for the phone to initiate the connection (may cause choppy audio).",
    )
    return p


def main():
    # Enable ANSI escape processing on Windows
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

    parser = build_parser()
    args = parser.parse_args()
    rc = asyncio.run(run(args))
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
