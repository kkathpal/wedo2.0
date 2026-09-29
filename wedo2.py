"""Minimal LEGO WeDo 2.0 Smart Hub driver over Bluetooth LE (uses bleak)."""

import asyncio
import struct

from bleak import BleakClient, BleakScanner


def _uuid(short):
    return f"0000{short}-1212-efde-1523-785feabcd123"


DEVICE_SERVICE = _uuid("1523")
BUTTON = _uuid("1526")
ATTACHED_IO = _uuid("1527")
INPUT_VALUES = _uuid("1560")
INPUT_COMMAND = _uuid("1563")
OUTPUT_COMMAND = _uuid("1565")

# Device type IDs reported when something is plugged in
MOTOR, PIEZO, LED, TILT, DISTANCE = 1, 22, 23, 34, 35
DEVICE_NAMES = {MOTOR: "motor", TILT: "tilt sensor", DISTANCE: "distance sensor"}

# Built-in ports
LED_PORT, PIEZO_PORT = 6, 5

COLORS = {
    "off": (0, 0, 0), "red": (255, 0, 0), "green": (0, 255, 0),
    "blue": (0, 0, 255), "yellow": (255, 200, 0), "purple": (160, 0, 255),
    "white": (255, 255, 255), "orange": (255, 80, 0),
}


class WeDoHub:
    def __init__(self):
        self.client = None
        self.ports = {}        # port number (1/2) -> device type id
        self.sensors = {}      # port number -> latest raw value(s)
        self.button_pressed = False
        self.on_sensor = None  # optional callback(port, device_type, values)
        self.on_button = None  # optional callback(pressed)
        self.on_disconnect = None  # optional callback()

    # ---------- connection ----------

    async def connect(self, timeout=30):
        print("Searching for WeDo 2.0 hub... press the green button on the hub.")
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: DEVICE_SERVICE in [u.lower() for u in ad.service_uuids]
            or (d.name or "").startswith("LPF2 Smart Hub"),
            timeout=timeout,
        )
        if device is None:
            raise RuntimeError("Hub not found. Press the green button and try again.")

        self.client = BleakClient(
            device, disconnected_callback=lambda _: self.on_disconnect and self.on_disconnect()
        )
        await self.client.connect()
        print(f"Connected to {device.name or device.address}")

        await self.client.start_notify(BUTTON, self._handle_button)
        await self.client.start_notify(INPUT_VALUES, self._handle_sensor)
        # The hub reports already-attached motors/sensors right after this subscribe
        await self.client.start_notify(ATTACHED_IO, self._handle_attached)
        await asyncio.sleep(0.5)

    async def disconnect(self):
        if self.client and self.client.is_connected:
            for port in list(self.ports):
                if self.ports[port] == MOTOR:
                    await self.motor(port, 0)
            await self.client.disconnect()
            print("Disconnected")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    # ---------- outputs ----------

    async def _output(self, port, command, payload):
        data = bytes([port, command, len(payload)]) + bytes(payload)
        await self.client.write_gatt_char(OUTPUT_COMMAND, data)

    async def motor(self, port, power):
        """Run motor on port 1 or 2. power: -100..100 (0 = stop)."""
        power = max(-100, min(100, int(power)))
        await self._output(port, 1, [power & 0xFF])

    async def led(self, color):
        """Set the hub light. color: a name from COLORS or an (r, g, b) tuple."""
        r, g, b = COLORS[color] if isinstance(color, str) else color
        await self._output(LED_PORT, 4, [r, g, b])

    async def beep(self, frequency=440, duration_ms=300):
        await self._output(PIEZO_PORT, 2, list(struct.pack("<HH", frequency, duration_ms)))
        await asyncio.sleep(duration_ms / 1000)

    # ---------- inputs ----------

    def tilt(self, port=None):
        """Returns (x, y) tilt angle of the tilt sensor, or None."""
        return self.sensors.get(port or self._find(TILT))

    def distance(self, port=None):
        """Returns distance sensor reading (0..10, bigger = farther), or None."""
        value = self.sensors.get(port or self._find(DISTANCE))
        return value[0] if value else None

    def _find(self, device_type):
        return next((p for p, t in self.ports.items() if t == device_type), None)

    async def _set_input_format(self, port, device_type, mode, unit, notify=True):
        data = bytes([1, 2, port, device_type, mode]) + struct.pack("<I", 1) + bytes([unit, int(notify)])
        await self.client.write_gatt_char(INPUT_COMMAND, data)

    # ---------- notification handlers ----------

    def _handle_button(self, _, data):
        self.button_pressed = bool(data[0])
        if self.on_button:
            self.on_button(self.button_pressed)

    def _handle_attached(self, _, data):
        port, attached = data[0], data[1]
        if not attached:
            self.ports.pop(port, None)
            self.sensors.pop(port, None)
            print(f"Port {port}: unplugged")
            return
        device_type = data[3]
        if device_type == LED:
            # Switch the built-in light to RGB mode so led() accepts colours
            asyncio.ensure_future(self._set_input_format(port, LED, mode=1, unit=0, notify=False))
            return
        if port not in (1, 2):
            return
        self.ports[port] = device_type
        print(f"Port {port}: {DEVICE_NAMES.get(device_type, f'device type {device_type}')} attached")
        if device_type == TILT:
            asyncio.ensure_future(self._set_input_format(port, TILT, mode=0, unit=0))
        elif device_type == DISTANCE:
            asyncio.ensure_future(self._set_input_format(port, DISTANCE, mode=0, unit=0))

    def _handle_sensor(self, _, data):
        # data = [revision, port, value bytes...]
        port = data[1]
        device_type = self.ports.get(port)
        if device_type == TILT:
            x, y = struct.unpack("<bb", bytes(data[2:4]))
            self.sensors[port] = (x, y)
        elif device_type == DISTANCE:
            self.sensors[port] = (data[2],)
        else:
            return
        if self.on_sensor:
            self.on_sensor(port, device_type, self.sensors[port])
