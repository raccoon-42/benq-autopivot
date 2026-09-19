#!/usr/bin/env python3
"""Rotate the desktop to follow a BenQ monitor's physical pivot, under Wayland.

Display Pilot 2 reads the same orientation value we do (DDC/CI VCP 0xAA) but
applies the rotation through xcb_randr_set_crtc_config, which does nothing on a
Wayland session.  This polls the monitor directly and rotates through Mutter's
DisplayConfig D-Bus API instead.

Leave Display Pilot 2's own Auto Pivot switch off, or the two will both react.
"""

import argparse
import fcntl
import os
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

I2C_SLAVE = 0x0703
DDC_ADDR = 0x37
EDID_ADDR = 0x50
VCP_ORIENTATION = 0xAA

EDID_MAGIC = b"\x00\xff\xff\xff\xff\xff\xff\x00"

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
OBJ_PATH = "/org/gnome/Mutter/DisplayConfig"

METHOD_VERIFY, METHOD_TEMPORARY, METHOD_PERSISTENT = 0, 1, 2

# VCP 0xAA value -> Mutter transform (0=normal, 1=90, 2=180, 3=270).
DEFAULT_MAP = {1: 0, 2: 1, 3: 3}

LOCK_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "benq-autopivot.i2c.lock"
)


def log(msg):
    print(msg, flush=True)


class I2CLock:
    """Serialise our own DDC access; Display Pilot 2 does not take this lock."""

    def __enter__(self):
        self.fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def read_edid(path):
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.ioctl(fd, I2C_SLAVE, EDID_ADDR)
        os.write(fd, b"\x00")
        edid = os.read(fd, 128)
    except OSError:
        return None
    finally:
        os.close(fd)
    return edid if len(edid) == 128 and edid.startswith(EDID_MAGIC) else None


def edid_id(edid):
    mfg = (edid[8] << 8) | edid[9]
    vendor = "".join(chr(ord("A") - 1 + ((mfg >> s) & 0x1F)) for s in (10, 5, 0))
    return vendor, (edid[10] | (edid[11] << 8))


def find_bus(vendor, product):
    """Locate the i2c bus for a monitor by EDID; bus numbers are not stable."""
    for entry in sorted(os.listdir("/dev")):
        if not entry.startswith("i2c-"):
            continue
        path = "/dev/" + entry
        edid = read_edid(path)
        if edid and edid_id(edid) == (vendor, product):
            return path
    return None


def get_vcp(path, code, attempts=3):
    for attempt in range(attempts):
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return None
        try:
            fcntl.ioctl(fd, I2C_SLAVE, DDC_ADDR)
            req = bytes([0x51, 0x82, 0x01, code])
            chk = (DDC_ADDR << 1)
            for b in req:
                chk ^= b
            os.write(fd, req + bytes([chk]))
            time.sleep(0.06)
            r = os.read(fd, 11)
        except OSError:
            r = b""
        finally:
            os.close(fd)

        if len(r) == 11 and r[2] == 0x02 and r[3] == 0x00 and r[4] == code:
            verify = 0x50
            for b in r[:10]:
                verify ^= b
            if verify == r[10]:
                return (r[8] << 8) | r[9]
        time.sleep(0.2 * (attempt + 1))
    return None


APP_NAMES = ("Display Pilot 2", "Display_Pilot_2", "AppRun.wrapped")


def app_running():
    """True while Display Pilot 2 is up.

    It polls the same DDC channel we do, roughly ten times a second, and the
    monitor serves one request at a time.  Competing with it makes both sides
    retry -- measurably so: the app's own log showed its retry rate go from
    17% to 59% once this daemon started.  Back off and let it have the bus.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if any(comm.startswith(n[:15]) for n in APP_NAMES):
            return True
    return False


class ScreenSaver:
    """Whether the session is locked.

    Nobody pivots a monitor they are not sitting at, and the lock screen is up
    for most of a day.  A lock check costs 0.27 ms against 53 ms for a DDC
    poll, so trading one for the other is nearly free.
    """

    def __init__(self, bus):
        self.bus = bus

    def locked(self):
        try:
            r = self.bus.call_sync(
                "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                "org.gnome.ScreenSaver", "GetActive", None, None,
                Gio.DBusCallFlags.NONE, 1000, None,
            )
        except GLib.Error:
            return False       # not GNOME, or the service is not up
        return bool(r.unpack()[0])


class Mutter:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def state(self):
        r = self.bus.call_sync(
            BUS_NAME, OBJ_PATH, BUS_NAME, "GetCurrentState",
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        )
        return r.unpack()

    def current_mode(self, monitors, connector):
        for (conn, _v, _p, _s), modes, _props in monitors:
            if conn != connector:
                continue
            for mode in modes:
                if mode[6].get("is-current"):
                    return mode[0]
        return None

    def apply(self, connector, transform, method=METHOD_TEMPORARY):
        serial, monitors, logical, _props = self.state()
        mode_id = self.current_mode(monitors, connector)
        if mode_id is None:
            raise RuntimeError("%s has no current mode" % connector)

        changed = False
        entries = []
        for x, y, scale, cur_transform, primary, mons, _lprops in logical:
            connectors = [m[0] for m in mons]
            want = transform if connector in connectors else cur_transform
            if connector in connectors and want != cur_transform:
                changed = True
            entries.append(GLib.Variant(
                "(iiduba(ssa{sv}))",
                (x, y, scale, want, primary,
                 [(m[0], self.current_mode(monitors, m[0]) or mode_id, {})
                  for m in mons]),
            ))

        if not changed and method != METHOD_VERIFY:
            return False

        args = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                            (serial, method, entries, {}))
        self.bus.call_sync(BUS_NAME, OBJ_PATH, BUS_NAME, "ApplyMonitorsConfig",
                           args, None, Gio.DBusCallFlags.NONE, -1, None)
        return True

    def transform_of(self, connector):
        _serial, _monitors, logical, _props = self.state()
        for _x, _y, _scale, transform, _primary, mons, _lprops in logical:
            if connector in [m[0] for m in mons]:
                return transform
        return None


def parse_map(text):
    mapping = {}
    for pair in text.split(","):
        k, _, v = pair.partition(":")
        mapping[int(k.strip())] = int(v.strip())
    return mapping


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connector", default="DP-3", help="DRM connector to rotate")
    ap.add_argument("--vendor", default="BNQ", help="EDID vendor id of the monitor")
    ap.add_argument("--product", default="0x80bf", help="EDID product id")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="poll seconds; each poll costs ~53 ms of CPU because "
                         "NVIDIA bit-bangs DDC in the kernel")
    ap.add_argument("--idle-interval", type=float, default=30.0,
                    help="poll seconds while the session is locked")
    ap.add_argument("--busy-interval", type=float, default=5.0,
                    help="poll seconds while Display Pilot 2 is running, which "
                         "hammers the same DDC channel")
    ap.add_argument("--map", default="1:0,2:1,3:3",
                    help="VCP 0xAA value:Mutter transform pairs")
    ap.add_argument("--watch", action="store_true",
                    help="print the orientation value, change nothing")
    ap.add_argument("--once", action="store_true", help="apply once and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask Mutter to verify the config instead of applying it")
    ap.add_argument("--persistent", action="store_true",
                    help="save to monitors.xml; makes GNOME prompt to keep or revert")
    args = ap.parse_args()

    product = int(args.product, 0)
    mapping = parse_map(args.map)

    with I2CLock():
        bus_path = find_bus(args.vendor, product)
    if not bus_path:
        log("monitor %s %#06x not found on any i2c bus" % (args.vendor, product))
        return 1
    log("monitor on %s, connector %s, map %s" % (bus_path, args.connector, mapping))

    if args.watch:
        last = object()
        while True:
            with I2CLock():
                value = get_vcp(bus_path, VCP_ORIENTATION)
            if value != last:
                log("VCP 0xAA = %s  -> transform %s" % (value, mapping.get(value)))
                last = value
            time.sleep(1.0)

    mutter = Mutter()
    saver = ScreenSaver(mutter.bus)
    failures = 0
    was_locked = False
    if args.dry_run:
        method = METHOD_VERIFY
    elif args.persistent:
        method = METHOD_PERSISTENT
    else:
        # Temporary: applied at once, not written to monitors.xml.  Persistent
        # configs make Mutter emit confirm-display-change, which is the GNOME
        # "keep changes / revert" dialog plus a revert timer -- unwanted for
        # something that reapplies itself every time the session starts.
        method = METHOD_TEMPORARY
    while True:
        with I2CLock():
            value = get_vcp(bus_path, VCP_ORIENTATION)

        if value is None:
            # Monitor asleep, unplugged, or DDC busy.  Re-locating it means
            # reading EDID from every i2c bus, which costs more than a normal
            # poll -- so do it rarely, and back off, or a display left asleep
            # overnight burns more CPU than one in use.
            failures += 1
            if failures % 10 == 0:
                with I2CLock():
                    if read_edid(bus_path) is None:
                        new = find_bus(args.vendor, product)
                        if new and new != bus_path:
                            log("monitor moved to %s" % new)
                            bus_path = new
                            failures = 0
        else:
            failures = 0
            transform = mapping.get(value)
            if transform is None:
                log("VCP 0xAA = %s has no mapping, ignoring" % value)
            else:
                # Compare against what Mutter actually has rather than against
                # the last value we saw: a temporary config is not written to
                # monitors.xml, so anything that reloads the display config can
                # silently put the rotation back. Re-checking heals that.
                try:
                    if mutter.transform_of(args.connector) != transform:
                        mutter.apply(args.connector, transform, method)
                        log("orientation %s -> transform %s" % (value, transform))
                except GLib.Error as e:
                    log("rotation refused by Mutter: %s" % e.message)
                except RuntimeError as e:
                    log("skipped: %s" % e)

        if args.once:
            return 0

        locked = saver.locked()
        if was_locked and not locked:
            was_locked = False
            continue          # just unlocked: check straight away
        was_locked = locked

        if failures:
            delay = min(args.interval * 2 ** min(failures, 5), 30.0)
        elif locked:
            delay = args.idle_interval
        elif app_running():
            delay = args.busy_interval
        else:
            delay = args.interval
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
