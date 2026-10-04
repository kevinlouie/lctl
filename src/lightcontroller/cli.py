import argparse
import asyncio
import logging
import sys

from .ble_controller import BLELight, LightEffect, run_sync, scan_for_lights, toggle_light
from .call_detector import DEFAULT_MIC_APPS, DetectionConfig, detect_calls
from .service import MeetingAwareLightService, ServiceConfig

DEFAULT_BRIGHTNESS = 80
DEFAULT_COLOR_TEMP = 4000  # Neutral white
DEFAULT_SCAN_TIMEOUT = 10.0
MIN_POLL_SECONDS = 0.5  # Each poll forks `ps`/`pactl`/`pw-dump`; avoid a hot loop


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _poll_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from None
    if not (MIN_POLL_SECONDS <= seconds < float("inf")):
        raise argparse.ArgumentTypeError(f"must be a finite number of at least {MIN_POLL_SECONDS} seconds")
    return seconds


def _detection_config(args: argparse.Namespace) -> DetectionConfig:
    return DetectionConfig(
        camera=args.camera,
        mic=args.mic,
        mic_apps=DEFAULT_MIC_APPS + tuple(args.mic_apps),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Control Ulanzi panel lights over BLE.")
    parser.add_argument("--address", help="BLE MAC address of the light")
    parser.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="Send real BLE writes (default: dry-run/log only)",
    )
    parser.add_argument(
        "--brightness",
        type=int,
        default=None,
        help="Optional brightness level (0-100) applied to supported commands",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.set_defaults(dry_run=True)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("on", help="Turn the light on")

    subparsers.add_parser("off", help="Turn the light off")

    # Call-detection options shared by `auto` and `detect`.
    detection_parser = argparse.ArgumentParser(add_help=False)
    detection_parser.add_argument(
        "--no-camera",
        dest="camera",
        action="store_false",
        help="Don't treat an in-use camera as a call",
    )
    detection_parser.add_argument(
        "--no-mic",
        dest="mic",
        action="store_false",
        help="Don't treat a meeting app's open microphone stream as a call",
    )
    detection_parser.add_argument(
        "--mic-app",
        dest="mic_apps",
        action="append",
        default=[],
        metavar="NAME",
        help="Also count microphone streams from this app (binary or application name; repeatable)",
    )

    auto_parser = subparsers.add_parser(
        "auto",
        parents=[detection_parser],
        help="Run a background loop that mirrors meeting activity to the light",
    )
    auto_parser.add_argument(
        "--poll-seconds",
        type=_poll_seconds,
        default=5.0,
        help=f"Seconds between meeting checks (default: 5.0, minimum: {MIN_POLL_SECONDS})",
    )

    subparsers.add_parser(
        "detect",
        parents=[detection_parser],
        help="Print what call detection currently sees (no BLE; exit 0 if in a call, 1 if not)",
    )

    color_temp_parser = subparsers.add_parser(
        "color-temp",
        help="Set color temperature in Kelvin (2700-6500)",
    )
    color_temp_parser.add_argument(
        "kelvin",
        type=int,
        help="Color temperature in Kelvin (2700=warm white, 6500=cool white)",
    )

    effect_parser = subparsers.add_parser(
        "effect",
        help="Set a light effect",
    )
    effect_parser.add_argument(
        "effect_name",
        choices=["flash", "tv", "candle", "strobe1", "strobe2", "strobe3"],
        help="Effect name",
    )

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan for lights on the network",
    )
    scan_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_SCAN_TIMEOUT,
        help=f"Scan duration in seconds (default: {DEFAULT_SCAN_TIMEOUT})",
    )

    args = parser.parse_args()

    # Validate that --address is provided for commands that need it
    commands_requiring_address = {"on", "off", "auto", "color-temp", "effect"}
    if args.command in commands_requiring_address and not args.address:
        parser.error(f"--address is required for the '{args.command}' command")
    _configure_logging(args.verbose)

    if args.command in {"on", "off"}:
        on = args.command == "on"
        brightness = args.brightness if args.brightness is not None else (DEFAULT_BRIGHTNESS if on else None)
        run_sync(toggle_light(args.address, on=on, brightness=brightness, dry_run=args.dry_run))
    elif args.command == "auto":
        brightness = args.brightness if args.brightness is not None else DEFAULT_BRIGHTNESS
        config = ServiceConfig(
            address=args.address,
            brightness=brightness,
            poll_seconds=args.poll_seconds,
            dry_run=args.dry_run,
            detection=_detection_config(args),
        )
        service = MeetingAwareLightService(config)
        try:
            asyncio.run(service.run_forever())
        except KeyboardInterrupt:
            pass
    elif args.command == "detect":
        detections = detect_calls(_detection_config(args), find_all=True)
        if not detections:
            print("No call detected.")
            sys.exit(1)
        for detection in detections:
            print(detection.source)
            # Browser command lines run to kilobytes; the start identifies the process.
            print(f"    {detection.process_line[:160]}")
    elif args.command == "color-temp":
        async def set_color_temp():
            async with BLELight(address=args.address, dry_run=args.dry_run) as light:
                await light.set_color_temperature(args.kelvin)
                if args.brightness is not None:
                    await light.set_brightness(args.brightness)
        run_sync(set_color_temp())
    elif args.command == "effect":
        effect_map = {
            "flash": LightEffect.FLASH,
            "tv": LightEffect.TV_SCREEN,
            "candle": LightEffect.CANDLE,
            "strobe1": LightEffect.STROBE_1,
            "strobe2": LightEffect.STROBE_2,
            "strobe3": LightEffect.STROBE_3,
        }
        async def set_effect():
            async with BLELight(address=args.address, dry_run=args.dry_run) as light:
                await light.set_effect(effect_map[args.effect_name])
        run_sync(set_effect())
    elif args.command == "scan":
        async def do_scan():
            lights = await scan_for_lights(timeout=args.timeout)
            if not lights:
                print("No lights found.")
                print("\nTips:")
                print("  - Make sure your light is powered on")
                print("  - Try moving closer to the light")
                print("  - Try increasing --timeout (e.g., --timeout 20)")
                return

            print(f"\nFound {len(lights)} light(s):\n")
            for i, light in enumerate(lights, 1):
                print(f"  {i}. {light.name}")
                print(f"     Address: {light.address}")
                print(f"     Signal:  {light.rssi} dBm")
                print()

            if len(lights) == 1:
                print("Use this address to control the light:")
                print(f"  lctl --address {lights[0].address} --execute on")
            else:
                print("Use an address from above to control a specific light:")
                print(f"  lctl --address <ADDRESS> --execute on")

        run_sync(do_scan())


if __name__ == "__main__":
    main()
