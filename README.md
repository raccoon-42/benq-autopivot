# benq-autopivot

Rotates the GNOME Wayland desktop to follow a BenQ monitor's physical pivot.

## The problem

BenQ's Display Pilot 2 has an Auto Pivot feature. It does nothing on Wayland.

The monitor half works fine: it reports its orientation over DDC/CI as VCP
feature `0xAA`. But Display Pilot 2 applies the rotation through
`xcb_randr_set_crtc_config`, an X11 call. `libDpShared.so` contains no Wayland
symbols and no `zwlr_output_management` or KDE output protocol, so there is no
fallback path. Under GNOME Wayland, XWayland's RandR is a virtual screen and
Mutter ignores X clients trying to reconfigure real outputs, so the call goes
nowhere. The app's own log records it:

```
getVCP "aa" value: 2
[DpAutoPivot] set current pivot: 90
W (DpPlatformLinux.cpp:754 DpPlatform::setMonitorPivot) [DpPlatform] set crtc config failed
```

This daemon reads the same `0xAA` value and rotates through Mutter's
`org.gnome.Mutter.DisplayConfig` D-Bus API instead.

## Install

```sh
./install.sh
```

Then set `--connector` in `benq-autopivot.service` to your monitor's DRM
connector (`ls /sys/class/drm/`), and check the orientation map below.

## Calibrating

The monitor reports a small integer for its orientation; which integer means
which direction varies. Watch it, changing nothing:

```sh
systemctl --user stop benq-autopivot
./autopivot.py --watch
```

Rotate the monitor through each position, then map the values to Mutter
transforms (`0` normal, `1` 90 degrees, `2` 180, `3` 270):

```
--map 1:0,2:1,3:3
```

If the desktop lands upside down, swap the two portrait entries.

## Polling and why it costs what it does

DDC/CI is master-slave over i2c. The monitor can only answer, never initiate,
and there is no interrupt. Nothing can tell the PC the panel was turned, so the
only option is to ask repeatedly.

One poll is a 5-byte request, a mandatory ~40 ms wait for the monitor's
microcontroller, then an 11-byte reply: about 113 ms, of which ~53 ms is CPU on
NVIDIA's proprietary driver, which bit-bangs i2c in the kernel. At a 1 s
interval that is roughly 5% of one core.

That cost cannot be reduced, so the daemon just asks less often when the answer
does not matter:

| State | Interval | Why |
|---|---|---|
| Unlocked, app closed | 1 s | the only case where latency is felt |
| Session locked | 30 s | nobody pivots a monitor they are not sitting at |
| Display Pilot 2 running | 5 s | it holds the DDC bus about 30% of the time |
| Monitor asleep or absent | backs off to 30 s | avoids rescanning every i2c bus each tick |

Deciding costs far less than polling: the lock check is 0.27 ms and the process
scan 3.4 ms, against 53 ms for a DDC read. It polls immediately on unlock.

For reference, Display Pilot 2 polls this same feature every 10 seconds.

## Notes

- Turn off Display Pilot 2's own Auto Pivot, or both react and they compete for
  the DDC channel.
- Rotation is applied as a *temporary* Mutter config. Persistent configs make
  GNOME show a keep-or-revert prompt and arm a revert timer, which is wrong for
  something that reapplies itself every session. The daemon compares against
  Mutter's live state each tick, so it heals if anything resets the rotation.
- The monitor is located by EDID rather than a fixed `/dev/i2c-N`, since bus
  numbers are not stable across boots.
- It only ever *reads* from the monitor. The single thing it changes is the
  desktop rotation, through Mutter.

## Requirements

GNOME on Wayland (uses Mutter's DisplayConfig), Python 3 with PyGObject, and
read/write access to the monitor's `/dev/i2c-*` node — on a normal desktop
session systemd grants this through udev ACLs.
