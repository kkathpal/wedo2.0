# WeDo 2.0 RC Car

Drive a LEGO WeDo 2.0 car from your computer's keyboard or from your phone. The computer talks
to the WeDo Smart Hub over Bluetooth, so nothing has to be installed on the hub.

- **Single mode**: one hub, one motor per side (tank steering).
- **Dual mode**: a 4×4 truck with two hubs. The front hub drives two wheels and the rear hub
  the other two, all from the same keys.
- **Phone controller**: drive from any phone browser on the same Wi-Fi, in either mode.

## What you need

- A computer with Bluetooth: Windows 10/11 or a Mac (see [On a Mac](#on-a-mac)). Linux should
  work too.
- [Python 3](https://www.python.org/downloads/) and the `bleak` Bluetooth package:

  ```
  python -m pip install bleak
  ```

  On a Mac, use `python3` instead of `python` in all the commands here.

- One WeDo 2.0 Smart Hub (two for dual mode) with a motor in each port.

## Files

| File | What it is |
|---|---|
| `rc_car.pyw` | The RC car app (desktop window). Double-click to run without a console. |
| `rc_car_phone.py` + `rc_car_phone.html` | Phone controller: a small web server on the computer and the touch page it serves. |
| `wedo2.py` | A small WeDo 2.0 hub driver (motors, light, beeper, sensors) over Bluetooth Low Energy. |
| `demo.py` | Lights, sounds, a motor and sensor readings: a quick test that the hub works. |
| `rc_car_calibration.json` | Created when you calibrate. Holds your car's calibration and last mode. Not in git, because it's specific to your car. |

## Driving from the computer

Double-click `rc_car.pyw`, or run `python rc_car.pyw` to see error messages in a terminal.

1. Pick **Single · 1 hub** or **Dual · 2 hubs** at the top.
2. Click **Connect** on a hub card, then press the green button on that hub.
   In dual mode, connect the front hub first (its light turns white), then the rear hub (red).
3. Calibrate once (see below), then drive.

| Key | Action |
|---|---|
| ↑ ↓ / W S | forward / backward |
| ← → / A D | turn: with ↑/↓ to curve, alone to spin in place |
| Space | horn |
| Esc | stop |

The **Speed** slider sets the power, and the light buttons change the hub's colour. The car
stops if the window loses focus while you're holding a key.

## Calibration

Motors can be mounted either way round and plugged into either port, so tell the app how your
car is built. Calibration is saved and remembered.

**Single mode:** click **Calibrate…** on the hub card. Hold each arrow (or click its **Test**
button) and watch the car, then click what it actually did: Forward, Backward, Left or Right.
Click **Save**.

**Dual mode (4×4 truck):** click **Calibrate truck…**, and lift the truck so the wheels are off
the ground. For each of the four motors (front hub port 1 and 2, rear hub port 1 and 2):

1. Click **Test** (or press keys 1–4). That one motor turns for a second.
2. Click which wheel turned (Front L, Front R, Rear L, Rear R) and which way it rolled
   (Forward or Backward).

Then hold the arrow keys to check: on ↑ all four wheels should roll forward. Click **Save**.
In dual mode the truck only drives while both hubs are connected, and if either hub disconnects,
both stop.

## Driving from a phone

1. Close `rc_car.pyw` first: a hub can only be connected to one program at a time.
2. In a terminal in this folder, run:

   ```
   python rc_car_phone.py
   ```

3. On your phone (on the same Wi-Fi), open the `http://…:<port>` address the server prints.
   The first time, allow Python through the Windows firewall for private networks.
4. Pick **Single** or **Dual · 4×4 truck** at the top of the page. It starts in the mode the
   desktop app used last.
5. Press the green button on the hub:
   - **Single:** the hub's light turns blue when connected.
   - **Dual:** press the **front** hub's button first (its light turns white), then the **rear**
     hub's (red). The page shows each hub's status and what to press next.

The server picks a random free port (8000–8999) each time. To always use the same address, give
one: `python rc_car_phone.py --port 8090`. Add `--dual` or `--single` to choose the starting mode.

The phone page has a d-pad (hold two arrows to curve), STOP, HORN, a speed slider and light
colours. It uses the same steering and calibration as the desktop app (in dual mode, the truck
calibration), so calibrate in the desktop app first. The controls are dimmed until every hub the
mode needs is connected. If you lift your finger, lock the phone or lose Wi-Fi, the car stops
within about a third of a second. In dual mode, if either hub disconnects, both stop.

## On a Mac

- **Install Python from [python.org](https://www.python.org/downloads/macos/).** Apple's built-in
  `/usr/bin/python3` comes with an old Tk that can show blank windows. With Homebrew Python, also
  run `brew install python-tk`.
- **Run from Terminal:** `python3 rc_car.pyw`. On a Mac, double-clicking a `.pyw` file doesn't
  start it without a console, as it does on Windows.
- **Allow Bluetooth.** The first time, macOS asks whether Terminal (or VS Code, if you run it
  from there) may use Bluetooth. Click Allow. If you said no, the hub is never found: turn it on in
  System Settings → Privacy & Security → Bluetooth, then restart the app.
- **Phone controller:** if macOS asks whether Python may accept incoming network connections,
  click Allow, or phones can't reach it.

Holding a key works the same as on Windows, and the app scales its text to match.

## Troubleshooting

- **Nothing happens when I double-click `rc_car.pyw`**: run `python rc_car.pyw` in a terminal
  to see the error. If `bleak` is missing, a message box tells you how to install it.
- **"Hub not found"**: press the hub's green button right after clicking Connect (its light
  blinks while it's looking). Check the computer's Bluetooth is on, and that no other program or
  tablet is connected to the hub. On a Mac, check Terminal is allowed to use Bluetooth (see
  [On a Mac](#on-a-mac)).
- **The car goes the wrong way**: calibrate again.
- **A wheel doesn't turn on curves**: WeDo motors stall at low power. Raise `MIN_POWER` near the
  top of `rc_car.pyw` (for example to 45).
- **Phone: "Can't reach the PC"**: check `rc_car_phone.py` is still running and that the phone
  is on the same Wi-Fi. Allow Python through the firewall.
- **Phone: "Can't use port …"**: something else is using the port you gave with `--port`. Pick
  another, or leave out `--port`.

## How it works

The WeDo 2.0 Smart Hub only speaks Bluetooth Low Energy. `wedo2.py` uses the `bleak` library to
find the hub by its LEGO service ID, then writes short commands to the hub's
characteristics, such as `[port, 1, 1, power]` to run a motor at power −100 to 100.
The desktop app draws its window with Tkinter and runs Bluetooth on an asyncio loop in the
background. The phone controller is a plain Python web server that turns the page's held
buttons into motor commands.
