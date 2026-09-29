"""WeDo RC for phones: drive the WeDo 2.0 car from any phone browser on your Wi-Fi.

This PC keeps the Bluetooth connection to the hub and serves a touch controller page
to phones on the same Wi-Fi. It drives exactly like rc_car.pyw: same turning, and the
same calibration (rc_car_calibration.json, made with Setup → Calibrate in rc_car.pyw).

Run:   python rc_car_phone.py               (uses a random free port from 8000-8999)
       python rc_car_phone.py --port 8090   (use this port)
Then press the green button on the hub, and open the printed http://<this PC>:<port>
address on your phone. (Close rc_car.pyw first: the hub takes one connection at a time.)

Safety: the phone re-sends the held buttons every ~100 ms, and this server stops the
car if the phone goes quiet for PHONE_TIMEOUT, so lifting your finger, locking the
phone or losing Wi-Fi stops the car.
"""
import asyncio
import json
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = SourceFileLoader("rc_car", os.path.join(HERE, "rc_car.pyw"))
rc = module_from_spec(spec_from_loader("rc_car", _loader))
_loader.exec_module(rc)

PORT_RANGE = (8000, 8999)   # each run uses a random free port from here (--port N picks one)
PAGE = os.path.join(HERE, "rc_car_phone.html")
PHONE_TIMEOUT = 0.35     # stop if the phone sends nothing for this long while driving (s)
DEFAULT_SPEED = 70
DIRECTIONS = {"fwd", "back", "left", "right"}
LIGHTS = [name for name, _ in rc.LIGHTS]


class Controller:
    """Owns the hub connection; turns phone button states into motor power."""

    def __init__(self):
        self.lock = threading.Lock()
        self.loop = None
        self.hub = None
        self.status = "Starting…"
        self.held = set()
        self.speed = DEFAULT_SPEED
        self.wheels = (0, 0)        # (left, right) wheel power, for the page's meters
        self.target = (0, 0)        # (port 1, port 2) power the hub should have now
        self.last_command = 0.0
        self.cal, self.cal_mtime = rc.DEFAULT_CAL, None
        self.calibrated = False
        self._reload_calibration()

    # ----- driving (called from the web server threads) -----
    def _reload_calibration(self):
        """Pick up a new calibration saved by rc_car.pyw while this runs."""
        try:
            mtime = os.path.getmtime(rc.CAL_FILE)
        except OSError:
            mtime = None
        if mtime != self.cal_mtime:
            (self.cal, self.calibrated), self.cal_mtime = rc.load_calibration(), mtime

    def drive(self, held, speed=None):
        with self.lock:
            if isinstance(speed, (int, float)):
                self.speed = int(max(rc.MIN_POWER, min(100, speed)))
            self.held = {d for d in held if d in DIRECTIONS}
            self.last_command = time.monotonic()
            self._update()

    def stop(self):
        with self.lock:
            self.held = set()
            self._update()

    def _update(self):
        self._reload_calibration()
        self.wheels = rc.wheel_powers(self.held, self.speed)
        self.target = rc.port_powers(*self.wheels, self.cal)

    def _safety(self):
        """Stop the car if the phone that's driving goes quiet (Wi-Fi drop, app switch...)."""
        while True:
            time.sleep(0.05)
            if self.held and time.monotonic() - self.last_command > PHONE_TIMEOUT:
                self.stop()

    def run_on_hub(self, make_coro):
        """Run a hub command (horn, light) on the Bluetooth loop, if connected."""
        hub, loop = self.hub, self.loop
        if hub and loop:
            asyncio.run_coroutine_threadsafe(make_coro(hub), loop)

    def state(self):
        with self.lock:
            self._reload_calibration()
        hub = self.hub   # may drop to None on the Bluetooth thread at any moment
        motors = list(dict(hub.ports).values()).count(rc.MOTOR) if hub else 0
        return {
            "connected": hub is not None,
            "status": self.status,
            "left": self.wheels[0], "right": self.wheels[1],
            "speed": self.speed, "min_speed": rc.MIN_POWER,
            "lights": [{"name": n, "color": c} for n, c in rc.LIGHTS],
            "calibrated": self.calibrated,
            "warning": "" if not hub or motors == 2 else "Plug a motor into both ports of the hub",
        }

    # ----- Bluetooth (runs on the main thread's asyncio loop) -----
    async def run(self):
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self._safety, daemon=True).start()
        try:
            while True:
                await self._connect_and_drive()
                await asyncio.sleep(1)
        finally:   # Ctrl+C: stop the motors and let the hub go
            hub, self.hub = self.hub, None
            if hub:
                try:
                    await asyncio.wait_for(hub.disconnect(), 3)
                except Exception:
                    pass

    async def _connect_and_drive(self):
        self.status = "Searching… press the green button on the hub"
        hub = rc.WeDoHub()
        try:
            await hub.connect(timeout=20)
        except RuntimeError:   # scan timed out
            self.status = "Hub not found yet: press the green button on the hub"
            return
        except Exception as e:   # Bluetooth off, adapter busy, ...
            self.status = f"Bluetooth problem: {e}"
            print(self.status)
            return
        self.hub = hub
        self.status = "Connected"
        print("Hub connected: open the page on your phone")
        try:
            await hub.led("blue")   # blue = driven from the phone
            sent = (None, None)
            while hub.client.is_connected:
                target = self.target
                if target[0] != sent[0]:
                    await hub.motor(1, target[0])
                if target[1] != sent[1]:
                    await hub.motor(2, target[1])
                sent = target
                await asyncio.sleep(0.02)
        except Exception as e:
            print(f"Hub connection lost: {e}")
        self.hub = None
        self.stop()
        self.status = "Hub disconnected: reconnecting…"
        print("Hub disconnected, searching again (press the green button)")


controller = Controller()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse lets a second copy take a port that's already in use
    # (and phones then reach either one), so there a busy port must fail instead.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # A phone closing the tab or switching apps mid-request is normal; don't print tracebacks.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:   # read each time so edits show up on refresh
                self._send(f.read(), "text/html; charset=utf-8")
        elif self.path == "/state":
            self._json(controller.state())
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_error(400, "bad json")
        if self.path == "/drive":
            controller.drive(data.get("held", []), data.get("speed"))
        elif self.path == "/stop":
            controller.stop()
        elif self.path == "/horn":
            controller.run_on_hub(lambda hub: hub.beep(392, 250))
        elif self.path == "/light":
            if data.get("name") in LIGHTS:
                controller.run_on_hub(lambda hub: hub.led(data["name"]))
        else:
            return self.send_error(404)
        self._json(controller.state())

    def log_message(self, *args):   # keep the console quiet (10 requests a second)
        pass


def lan_addresses():
    """This PC's addresses a phone on the same network could reach."""
    found = []
    try:   # the interface the default route uses (no packets are sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip not in found and not ip.startswith("127."):
            found.append(ip)
    return found


def port_from_args():
    """The number after --port on the command line, or None."""
    args = sys.argv[1:]
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
        sys.exit("Use:  python rc_car_phone.py --port 8090")
    return None


def start_server(wanted=None):
    """The web server on the `wanted` port, or on a random free port in PORT_RANGE.
    Trying to open the server is the check, so no other program can take the port
    between checking and using it."""
    if wanted:
        try:
            return Server(("0.0.0.0", wanted), Handler)
        except OSError as e:
            sys.exit(f"Can't use port {wanted} ({e.strerror or e}).\n"
                     f"Something else is using it: pick another, or leave out --port for a random free one.")
    ports = list(range(PORT_RANGE[0], PORT_RANGE[1] + 1))
    random.shuffle(ports)
    for port in ports:
        try:
            return Server(("0.0.0.0", port), Handler)
        except OSError:
            continue   # in use: try another
    sys.exit(f"No free port between {PORT_RANGE[0]} and {PORT_RANGE[1]}.")


def main():
    server = start_server(port_from_args())
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("\nWeDo RC phone controller")
    for ip in lan_addresses():
        print(f"  open on your phone:  http://{ip}:{port}")
    print("  (phone must be on the same Wi-Fi; allow Python through the firewall if asked)")
    print("  Press the green button on the hub to connect.  Ctrl+C to quit.")
    try:
        # Bluetooth on the main thread: bleak is happiest there on Windows
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        pass
    finally:
        print("stopping…")
        server.shutdown()


if __name__ == "__main__":
    main()
