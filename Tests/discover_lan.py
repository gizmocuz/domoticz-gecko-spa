#!/usr/bin/env python3
"""
LAN discovery + readout for in.touch 2 / 3 spas via geckolib.

This is a standalone diagnostic. It does not need any cloud account,
just UDP reachability on port 10022 between this machine and the spa.

Usage:
    pip install geckolib
    python Tests/discover_lan.py
        -> broadcasts on the LAN, lists found spas, connects to the
           first one, dumps everything visible (temps, pumps, lights,
           blowers, watercare, sensors).

Add --ip <ADDR> to skip discovery and target one spa directly:
    python Tests/discover_lan.py --ip 192.168.1.50
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

try:
    from geckolib import GeckoAsyncSpaMan, GeckoSpaEvent
except ImportError:
    sys.exit("geckolib not installed. Run: pip install geckolib")

# A stable per-installation UUID. The spa uses this to recognise repeat clients
# (it'll happily talk to anyone, but keeping the same one avoids registration
# churn on the spa side).
CLIENT_ID = "5d8f1c12-3a52-4b29-9e2c-9f3a1c0d1234"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("discover_lan")


class Probe(GeckoAsyncSpaMan):
    """Minimal SpaMan that prints events and exposes when the facade is ready."""

    def __init__(self) -> None:
        super().__init__(CLIENT_ID)
        self.facade_ready = asyncio.Event()
        self.fatal = asyncio.Event()

    async def handle_event(self, event: GeckoSpaEvent, **kwargs: Any) -> None:
        log.info("event=%s state=%s", event.name if hasattr(event, "name") else event, self.spa_state)
        if event == GeckoSpaEvent.CLIENT_FACADE_IS_READY:
            self.facade_ready.set()
        elif event in (
            GeckoSpaEvent.SPA_NOT_FOUND,
            GeckoSpaEvent.ERROR_TOO_MANY_RF_ERRORS,
            GeckoSpaEvent.ERROR_PROTOCOL_RETRY_TIME_EXCEEDED,
            GeckoSpaEvent.CONNECTION_PROTOCOL_RETRY_TIME_EXCEEDED,
        ):
            self.fatal.set()


def _fmt(obj: Any) -> str:
    try:
        return f"{obj}"
    except Exception:
        return "<?>"


async def dump_facade(probe: Probe) -> None:
    f = probe.facade
    if f is None:
        print("\nNo facade — connection did not complete.")
        return

    print("\n" + "=" * 72)
    print(f"Connected to: {probe.spa_name or f.name}  (unique_id={probe.unique_id})")
    print("=" * 72)

    wh = f.water_heater
    if wh:
        print("\n[Water heater]")
        print(f"  current_temperature : {_fmt(wh.current_temperature)} {_fmt(wh.temperature_unit)}")
        print(f"  target_temperature  : {_fmt(wh.target_temperature)}")
        print(f"  real_target         : {_fmt(wh.real_target_temperature)}")
        print(f"  min/max             : {_fmt(wh.min_temp)} .. {_fmt(wh.max_temp)}")
        print(f"  current_operation   : {_fmt(wh.current_operation)}")
        print(f"  is_on               : {_fmt(wh.is_on)}")

    if f.pumps:
        print(f"\n[Pumps] ({len(f.pumps)})")
        for i, p in enumerate(f.pumps):
            print(f"  [{i}] {p.name}: mode={_fmt(p.mode)} modes={_fmt(p.modes)} is_on={_fmt(p.is_on)} available={_fmt(p.is_available)}")

    if f.blowers:
        print(f"\n[Blowers] ({len(f.blowers)})")
        for i, b in enumerate(f.blowers):
            print(f"  [{i}] {b.name}: mode={_fmt(b.mode)} modes={_fmt(b.modes)} is_on={_fmt(b.is_on)}")

    if f.lights:
        print(f"\n[Lights] ({len(f.lights)})")
        for i, lt in enumerate(f.lights):
            print(f"  [{i}] {lt.name}: is_on={_fmt(lt.is_on)} available={_fmt(lt.is_available)}")

    if f.water_care:
        wc = f.water_care
        print("\n[Watercare]")
        print(f"  mode={_fmt(wc.mode)} modes={_fmt(wc.modes)} state={_fmt(wc.state)}")

    if f.sensors:
        print(f"\n[Sensors] ({len(f.sensors)})")
        for s in f.sensors:
            print(f"  {s.name} = {_fmt(getattr(s, 'state', '?'))}")

    if f.binary_sensors:
        print(f"\n[Binary sensors] ({len(f.binary_sensors)})")
        for s in f.binary_sensors:
            print(f"  {s.name} = {_fmt(getattr(s, 'state', '?'))}")

    print("\n(use Ctrl+C to exit)")


async def run(args: argparse.Namespace) -> int:
    async with Probe() as probe:
        if args.ip:
            # Skip discovery; we still need an identifier and a name. The library
            # accepts None for identifier/name and will read them from the spa.
            print(f"Connecting directly to {args.ip} ...")
            await probe.async_set_spa_info(args.ip, None, None)
        else:
            print("Discovering spas on the LAN (UDP broadcast, ~5s) ...")
            await probe.async_locate_spas()
            descs = probe.spa_descriptors or []
            if not descs:
                print("No spas found via broadcast. If the spa is on a different VLAN")
                print("or broadcast is blocked, re-run with --ip <SPA_IP>.")
                return 2
            print(f"Found {len(descs)} spa(s):")
            for i, d in enumerate(descs):
                print(f"  [{i}] {d.name}  ip={d.ipaddress}  id={d.identifier_as_string}")
            target = descs[args.index] if args.index < len(descs) else descs[0]
            print(f"\nUsing [{args.index}] {target.name} @ {target.ipaddress}")
            await probe.async_set_spa_info(
                target.ipaddress, target.identifier_as_string, target.name
            )

        # Wait for the facade to be ready, or a fatal event, or timeout
        try:
            done, _ = await asyncio.wait(
                {asyncio.create_task(probe.facade_ready.wait()),
                 asyncio.create_task(probe.fatal.wait())},
                timeout=args.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                print(f"\nTimed out after {args.timeout}s waiting for connection.")
                print(f"spa_state={probe.spa_state}")
                return 3
            if probe.fatal.is_set():
                print(f"\nFatal event received; spa_state={probe.spa_state}")
                return 4
        except asyncio.CancelledError:
            return 130

        await dump_facade(probe)
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="geckolib LAN discovery / readout")
    p.add_argument("--ip", help="Skip discovery; talk to this spa IP directly")
    p.add_argument("--index", type=int, default=0, help="Which discovered spa to connect to (default: 0)")
    p.add_argument("--timeout", type=int, default=45, help="Connect timeout in seconds (default: 45)")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging from geckolib")
    args = p.parse_args()
    if args.verbose:
        logging.getLogger("geckolib").setLevel(logging.DEBUG)

    # Windows Proactor loop can't send UDP broadcasts (WinError 10022).
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
