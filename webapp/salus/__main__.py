"""Start the app:  python -m salus [--fake]"""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m salus",
        description="Run the Salus web app on this computer, then open the address it prints.",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="use a pretend board, so the app can be worked on without the robot",
    )
    parser.add_argument(
        "--board-port",
        metavar="PORT",
        help="the board's serial port, e.g. /dev/cu.usbmodem1101 or COM3 "
        "(default: the first board found on USB; `atech ports` lists them)",
    )
    parser.add_argument("--http-port", type=int, default=8000, help="port for the web page (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="restart when the Python code changes")
    args = parser.parse_args()

    # Passed through the environment so they survive the restarts --reload causes.
    if args.fake:
        os.environ["SALUS_BOARD"] = "fake"
    if args.board_port:
        os.environ["SALUS_BOARD_PORT"] = args.board_port

    # 127.0.0.1 means only this computer can reach the app. Do not change it to
    # 0.0.0.0 to open it to the network: the app has no login, and it will hold
    # health data.
    uvicorn.run("salus.main:app", host="127.0.0.1", port=args.http_port, reload=args.reload)


if __name__ == "__main__":
    main()
