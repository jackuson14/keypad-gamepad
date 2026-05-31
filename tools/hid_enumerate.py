"""
hid_enumerate.py - list every HID interface this PC exposes, grouped by VID:PID.

Why this exists: the wired M1 V5 HE is known (3151:5030), but the 2.4GHz dongle
may present a DIFFERENT PID and/or a different interface layout. This tool just
*enumerates* (it never opens or writes to a device, no admin needed), so you can
plug in the dongle, run it, and read off the dongle's real VID/PID and which of
its interfaces look like the vendor config / vendor input the depth protocol needs.

    py tools/hid_enumerate.py              # list everything, highlight VID 0x3151
    py tools/hid_enumerate.py --vid 0x3151 # only this vendor
    py tools/hid_enumerate.py --json       # machine-readable dump

Read the verdict line under each device:
  - "HE-candidate" = has a 0xFFFF/0x02 config interface AND a vendor input one;
    this is what the wired keyboard looks like. Try it with:
        py stage1_probe.py --vid 0x.... --pid 0x....
  - "no vendor config interface" = the depth protocol almost certainly isn't here
    (e.g. the dongle may expose only a plain HID keyboard interface when idle).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# hidapi.dll is vendored in the project root; register it before importing hid.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if hasattr(os, "add_dll_directory") and os.path.isdir(_ROOT):
    os.add_dll_directory(_ROOT)

import hid  # noqa: E402

KNOWN = {
    (0x3151, 0x5030): "MonsGeek M1 V5 HE (wired) [verified]",
    (0x3151, 0x503A): "MonsGeek M1 V5 HE (2.4GHz dongle) [listed, unverified]",
}


def hx(v: int) -> str:
    return f"0x{v:04x}"


def _is_config(i: dict) -> bool:
    return i.get("usage_page") == 0xFFFF and i.get("usage") == 0x02


def _is_vendor_input(i: dict) -> bool:
    up = i.get("usage_page", 0)
    return (up & 0xFF00) == 0xFF00 and not _is_config(i)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Enumerate HID interfaces, grouped by VID:PID (read-only).")
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=0,
                    help="filter to this vendor id (e.g. 0x3151)")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=0,
                    help="filter to this product id (requires --vid)")
    ap.add_argument("--json", action="store_true", help="machine-readable dump")
    args = ap.parse_args()

    devs = list(hid.enumerate(args.vid, args.pid))

    groups: dict[tuple[int, int], list[dict]] = {}
    for d in devs:
        groups.setdefault((d["vendor_id"], d["product_id"]), []).append(d)

    if args.json:
        out = []
        for (vid, pid), ifaces in groups.items():
            out.append({
                "vid": hx(vid), "pid": hx(pid),
                "known": KNOWN.get((vid, pid)),
                "product": ifaces[0].get("product_string"),
                "manufacturer": ifaces[0].get("manufacturer_string"),
                "he_candidate": any(_is_config(i) for i in ifaces)
                                and any(_is_vendor_input(i) for i in ifaces),
                "interfaces": [{
                    "interface_number": i.get("interface_number"),
                    "usage_page": hx(i.get("usage_page", 0)),
                    "usage": hx(i.get("usage", 0)),
                    "release_number": hx(i.get("release_number", 0)),
                } for i in ifaces],
            })
        print(json.dumps(out, indent=2))
        return 0

    # known / VID 0x3151 first, then everything else
    def rank(k: tuple[int, int]):
        if k in KNOWN:
            return (0,) + k
        if k[0] == 0x3151:
            return (1,) + k
        return (2,) + k

    print(f"[hid_enumerate] {len(devs)} interface(s) across {len(groups)} device(s)\n")
    for key in sorted(groups, key=rank):
        vid, pid = key
        ifaces = groups[key]
        label = KNOWN.get(key, "")
        prod = ifaces[0].get("product_string") or "?"
        manu = ifaces[0].get("manufacturer_string") or "?"
        star = "   <<< VID 0x3151" if vid == 0x3151 else ""
        print(f"=== {hx(vid)}:{hx(pid)}  {manu} / {prod}  {label}{star}")
        for i in sorted(ifaces, key=lambda x: (x.get("interface_number", -1),
                                               x.get("usage_page", 0), x.get("usage", 0))):
            up, us = i.get("usage_page", 0), i.get("usage", 0)
            tag = "  [config FFFF/02]" if _is_config(i) else \
                  "  [vendor input]" if _is_vendor_input(i) else ""
            print(f"    if{str(i.get('interface_number', '?')):>3}  "
                  f"usage_page={hx(up)}  usage={hx(us)}  rel={hx(i.get('release_number', 0))}{tag}")
        has_cfg = any(_is_config(i) for i in ifaces)
        has_vin = any(_is_vendor_input(i) for i in ifaces)
        verdict = ("HE-candidate (FFFF/02 config + vendor input present) -> try "
                   f"`py stage1_probe.py --vid {hx(vid)} --pid {hx(pid)}`") if (has_cfg and has_vin) else \
                  "has FFFF/02 config but NO vendor input interface" if has_cfg else \
                  "no vendor config (FFFF/02) -> depth protocol unlikely on this interface set"
        print(f"    -> {verdict}\n")

    if not any(k[0] == 0x3151 for k in groups):
        print("No VID 0x3151 device found. If testing the dongle, make sure the dongle is\n"
              "plugged in AND the keyboard is powered on / paired (a 2.4GHz keyboard often\n"
              "presents its vendor interface only once it's awake and connected to the dongle).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
