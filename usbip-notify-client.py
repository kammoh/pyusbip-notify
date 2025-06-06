#!/usr/bin/env python3

# Copyright (c) Kamyar Mohajerani 2025
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import os
import re
import subprocess
import time
from typing import Optional

SOCKET_HOST = "192.168.64.1"
SOCKET_PORT = 65432

MAX_FAILURES = 1_000


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="USBIP Notify Client")
    parser.add_argument("--host", type=str, default=SOCKET_HOST, help="Notify server host address")
    parser.add_argument("--port", type=int, default=SOCKET_PORT, help="Notify server port number")
    parser.add_argument(
        "--max-failures",
        type=int,
        default=MAX_FAILURES,
        help="Maximum number of failures before exiting",
    )
    parser.add_argument(
        "--no-modprobe",
        action="store_true",
        help="Do not run modprobe vhci-hcd",
    )
    args = parser.parse_args()
    return args


def run_command(*cmd, get_stdout=False, check=False):
    print(f"> running {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE if get_stdout else None, check=check)
        print(f">  `{' '.join(cmd[:2])}` returned {result.returncode}")
        return result
    except FileNotFoundError as e:
        print(f"Failed to run {' '.join(cmd)}. {e.filename} command was not found!")
        return None


def run_command_get_output(*cmd, check=True):
    res = run_command(*cmd, get_stdout=True, check=check)
    if res is None:
        return (None, -999)
    return (res.stdout.decode(), res.returncode)


def list_ports():
    (output, ret) = run_command_get_output("usbip", "port")

    if output and ret == 0:
        return parse_ports(output)
    return None
    # TODO parse output


class ConnectionClosedError(Exception):
    pass


def parse_ports(output):
    ports = dict()

    for port_match in re.finditer(
        r"Port (\d+): <([^>]+)> at ([^\n]+)\n (.+) \(([\da-fA-F]+):([\da-fA-F]+)\)\s+(\d+-\d+) -> usbip://([\d\.]+):(\d+)/(\d+-\d+)\s+",
        output,
        flags=re.MULTILINE | re.DOTALL,
    ):
        dev_id = port_match.group(10)
        ports[dev_id] = {
            "port": (port_match.group(1)),
            "status": port_match.group(2),
            "speed": port_match.group(3),
            "vendor_id": port_match.group(5),
            "product_id": port_match.group(6),
            "server_ip": port_match.group(8),
            "server_port": port_match.group(9),
            "dev_id": dev_id,
        }

    return ports


class NotifyClient(asyncio.Protocol):
    message = "Client hello"

    def __init__(self) -> None:
        self.transport: Optional[asyncio.Transport] = None

    def connection_made(self, transport: asyncio.Transport):  # type: ignore
        print(f"* Connected to {transport.get_extra_info('peername')}")
        self.transport = transport
        assert self.transport is not None
        self.transport.write(self.message.encode())

    def data_received(self, data):
        data_str = data.decode()
        [dev_id, event, sep] = data_str.split(";")
        if sep != "\n":
            print(f"[ERROR] received unexpected message seperator: {sep}")
        print(f"** received: event '{event}' for device '{dev_id}'")
        if event == "PLUGGED":
            # Check dev_id is in the list
            (list_output, returncode) = run_command_get_output("usbip", "list", "-r", SOCKET_HOST)
            print(list_output)
            if list_output and returncode == 0 and dev_id in list_output:
                run_command("usbip", "attach", "-r", SOCKET_HOST, "-b", dev_id)  # check = True?
            else:
                print(f"[ERROR] device ID `{dev_id}` was not found in the device list!!!!")
        elif event == "UNPLUGGED":
            ports = list_ports()
            if ports is None:
                print("[ERROR] failed to list ports")
                port = 0
            else:
                print(f"ports:\n{ports}")
                # TODO get port for dev_id
                port = ports.get(dev_id, {}).get("port", 0)
            run_command("usbip", "detach", "-p", str(port))

    def connection_lost(self, exc):
        print("* server closed the connection")
        asyncio.get_event_loop().stop()
        if exc is not None:
            print(f"[ERROR] {exc}")
            raise exc
        else:
            raise ConnectionClosedError()


test_output = """
Imported USB devices
====================
Port 00: <Port in Use> at High Speed(480Mbps)
       Future Technology Devices International, Ltd : FT2232C/D/H Dual UART/FIFO IC (0403:6010)
       5-1 -> usbip://192.168.64.1:3240/1-1
           -> remote bus/dev 001/001
Port 01: <Port in Use> at High Speed(480Mbps)
       Future Technology Devices International, Ltd : FT2232C/D/H Dual UART/FIFO IC (0403:6010)
       5-2 -> usbip://192.168.64.1:3240/1-2
           -> remote bus/dev 001/002
"""

# print(parse_ports(test_output))

if __name__ == "__main__":
    args = parse_args()
    SOCKET_HOST = args.host
    SOCKET_PORT = args.port
    MAX_FAILURES = args.max_failures

    # check if running as root
    if os.geteuid() != 0:
        print("This script must be run as root")
        exit(1)

    if not args.no_modprobe:
        run_command("modprobe", "vhci-hcd", check=False)

    num_failures = 0
    had_success = False

    loop = asyncio.new_event_loop()
    print(f"* Connecting to server {SOCKET_HOST}:{SOCKET_PORT}")


    while True:
        try:
            coro = loop.create_connection(NotifyClient, SOCKET_HOST, SOCKET_PORT)
            loop.run_until_complete(coro)
            loop.run_forever()
        except KeyboardInterrupt:
            print("\nExiting...")
            exit(0)
        except ConnectionRefusedError as e:
            print(f"[ERROR] Connection refused: {e}")
            num_failures += 1
            max_fails = MAX_FAILURES if had_success else 10
            if num_failures >= max_fails:
                print(f"[ERROR] Giving up connecting after {num_failures} attempts")
                loop.close()
                exit(1)
            t = min(20, num_failures)
            print(f"Waiting {t} seconds before retrying...")
            time.sleep(t)
            continue
        except ConnectionClosedError:
            num_failures += 1
            if num_failures >= MAX_FAILURES:
                print(f"[ERROR] Giving up reconnecting after {num_failures} attempts")
                loop.close()
                exit(1)
            t = min(15, num_failures // 3 + 2)
            print(f"Waiting {t} seconds before reconnecting...")
            time.sleep(t)
            continue
        else:
            had_success = True
