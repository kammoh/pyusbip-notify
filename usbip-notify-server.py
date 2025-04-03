#!/usr/bin/env python3

# Copyright (c) 2025 Kamyar Mohajerani
#   USBIP server is based on [pyusbip](https://github.com/jwise/pyusbip)
#       and [this fork](https://github.com/tumayt/pyusbip) by tumayt
#     Copyright (c) 2018 Joshua Wise
#
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
import logging
import os
import struct
import threading
import time
import traceback
from functools import cached_property
from typing import Tuple

import usb
import usb.backend
from usb.backend.libusb1 import (
    CFUNCTYPE,
    POINTER,
    _Device,
    _libusb_device_handle,
    _objfinalizer,
    byref,
    c_int,
    c_uint,
    c_void_p,
    py_object,
)
from usb.core import Device
import usb1

# Configuration

USBIP_HOST = "192.168.64.1"
USBIP_PORT = 3240

NOTIFY_HOST = USBIP_HOST
NOTIFY_PORT = 65432

# set both/either to -1 to match any device
FILTER_VENDOR_ID = 0x0403  # FTDI
FILTER_PRODUCT_ID = 0x6010  # FT2232C/D/H

notify_log = logging.getLogger("Notify")

usbip_log = logging.getLogger("USBIP")

# set up logging
# set level to INFO
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="USBIP Notify Server")
    parser.add_argument(
        "--usbip-host", type=str, default=USBIP_HOST, help="Address to bind the USBIP server to"
    )
    parser.add_argument(
        "--usbip-port", type=int, default=USBIP_PORT, help="Port number for the USBIP server"
    )
    parser.add_argument(
        "--notify-host",
        type=str,
        default=NOTIFY_HOST,
        help="Address to bind the notification server to",
    )
    parser.add_argument(
        "--notify-port",
        type=int,
        default=NOTIFY_PORT,
        help="Port number for the notification server",
    )
    return parser.parse_args()


#####################
# USBIP Definitions #

USBIP_REQUEST = 0x8000
USBIP_REPLY = 0x0000

USBIP_OP_UNSPEC = 0x00
USBIP_OP_DEVINFO = 0x02
USBIP_OP_IMPORT = 0x03
USBIP_OP_EXPORT = 0x06
USBIP_OP_UNEXPORT = 0x07
USBIP_OP_DEVLIST = 0x05

USBIP_CMD_SUBMIT = 0x0001
USBIP_CMD_UNLINK = 0x0002
USBIP_RET_SUBMIT = 0x0003
USBIP_RET_UNLINK = 0x0004
USBIP_RESET_DEV = 0xFFFF

USBIP_DIR_OUT = 0
USBIP_DIR_IN = 1

USBIP_ST_OK = 0x00
USBIP_ST_NA = 0x01

USBIP_BUS_ID_SIZE = 32
USBIP_DEV_PATH_MAX = 256

USBIP_VERSION = 0x0111

USBIP_SPEED_UNKNOWN = 0
USBIP_SPEED_LOW = 1
USBIP_SPEED_FULL = 2
USBIP_SPEED_HIGH = 3
USBIP_SPEED_VARIABLE = 4

USB_RECIP_DEVICE = 0x00
USB_RECIP_INTERFACE = 0x01
USB_REQ_SET_ADDRESS = 0x05
USB_REQ_SET_CONFIGURATION = 0x09
USB_REQ_SET_INTERFACE = 0x0B

USB_ENOENT = 2
USB_EPIPE = 32


def sock_to_str(socketname) -> str:
    if isinstance(socketname, (tuple, list)):
        return ":".join(str(x) for x in socketname)
    else:
        return str(socketname)


# Global USB context
usbctx = usb1.USBContext()
usbctx.open()


class USBIPException(Exception):
    pass


class USBIPUnimplementedException(USBIPException):
    pass


class USBIPProtocolErrorException(USBIPException):
    pass


class USBIPProtocolFatalError(USBIPException):
    pass


class USBIPDevice:
    def __init__(self, devid, hnd):
        self.devid = devid
        self.hnd = hnd


class USBIPPending:
    def __init__(self, seqnum, device, xfer):
        self.seqnum = seqnum
        self.device = device
        self.xfer = xfer


class USBIPConnection:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.devices = {}
        self.urbs = {}
        # usbip_log.name = f"USBIP {self.peername}"

    @cached_property
    def peername(self) -> str:
        return sock_to_str(self.writer.get_extra_info("peername"))

    def say(self, msg):
        usbip_log.info(msg)

    def pack_device_desc(self, dev: usb1.USBDevice, interfaces=True):
        """Takes a usb1 device and packs it into a struct usb_device (and
        optionally, struct usb_interfaces)."""

        path = f"pyusbip/{dev.getBusNumber()}/{dev.getDeviceAddress()}"
        busid = f"{dev.getBusNumber()}-{dev.getDeviceAddress()}"
        busnum = dev.getBusNumber()
        devnum = dev.getDeviceAddress()
        speed = {
            usb1.SPEED_UNKNOWN: USBIP_SPEED_UNKNOWN,  # type: ignore
            usb1.SPEED_LOW: USBIP_SPEED_LOW,  # type: ignore
            usb1.SPEED_FULL: USBIP_SPEED_FULL,  # type: ignore
            usb1.SPEED_HIGH: USBIP_SPEED_HIGH,  # type: ignore
            usb1.SPEED_SUPER: USBIP_SPEED_HIGH,  # type: ignore
            usb1.SPEED_SUPER_PLUS: USBIP_SPEED_HIGH,  # type: ignore
        }[dev.getDeviceSpeed()]

        idVendor = dev.getVendorID()
        idProduct = dev.getProductID()
        bcdDevice = dev.getbcdDevice()

        bDeviceClass = dev.getDeviceClass()
        bDeviceSubClass = dev.getDeviceSubClass()
        bDeviceProtocol = dev.getDeviceProtocol()
        configs = list(dev.iterConfigurations())
        try:
            hnd = dev.open()
            bConfigurationValue = hnd.getConfiguration()
            hnd.close()
        except usb1.USBError:
            bConfigurationValue = configs[0].getConfigurationValue()
        bNumConfigurations = dev.getNumConfigurations()

        # Sigh, find it.
        config = configs[0]
        for _config in configs:
            if _config.getConfigurationValue() == bConfigurationValue:
                config = _config
                break
        bNumInterfaces = config.getNumInterfaces()

        data = struct.pack(
            ">256s32sIIIHHHBBBBBB",
            path.encode(),
            busid.encode(),
            busnum,
            devnum,
            speed,
            idVendor,
            idProduct,
            bcdDevice,
            bDeviceClass,
            bDeviceSubClass,
            bDeviceProtocol,
            bConfigurationValue,
            bNumConfigurations,
            bNumInterfaces,
        )

        if interfaces:
            for ifc in config.iterInterfaces():
                set = list(ifc)[0]
                data += struct.pack(
                    ">BBBB", set.getClass(), set.getSubClass(), set.getProtocol(), 0
                )

        return data

    def handle_op_devlist(self):
        devlist = usbctx.getDeviceList()

        resp = struct.pack(
            ">HHII", USBIP_VERSION, USBIP_OP_DEVLIST | USBIP_REPLY, USBIP_ST_OK, len(devlist)
        )
        for dev in devlist:
            resp += self.pack_device_desc(dev)

        self.writer.write(resp)

    def handle_op_import(self, busid):
        # We kind of do this the hard way -- rather than looking up by bus
        # id / device address, we instead just compare the string.  Life is
        # too short to extend python-libusb1.
        devlist = usbctx.getDeviceList()
        for dev in devlist:
            dev_busid = "{}-{}".format(dev.getBusNumber(), dev.getDeviceAddress())
            if busid == dev_busid:
                hnd = dev.open()
                self.say("opened device {}".format(busid))
                devid = dev.getBusNumber() << 16 | dev.getDeviceAddress()
                self.devices[devid] = USBIPDevice(devid, hnd)
                resp = struct.pack(
                    ">HHI", USBIP_VERSION, USBIP_OP_IMPORT | USBIP_REPLY, USBIP_ST_OK
                )
                resp += self.pack_device_desc(dev, interfaces=False)
                self.writer.write(resp)
                return

        self.say("device not found")
        resp = struct.pack(">HHI", USBIP_VERSION, USBIP_OP_IMPORT | USBIP_REPLY, USBIP_ST_NA)
        self.writer.write(resp)

    async def handle_urb_submit(self, seqnum, dev, direction, ep):
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (transfer_flags, buflen, start_frame, number_of_packets, interval, setup) = struct.unpack(
            op_submit, data
        )

        if number_of_packets != 0:
            raise USBIPUnimplementedException(f"ISO number_of_packets {number_of_packets}")

        if direction == USBIP_DIR_OUT:
            buf = await self.reader.readexactly(buflen)

        (bRequestType, bRequest, wValue, wIndex, wLength) = struct.unpack("<BBHHH", setup)

        # self.say("seq {:x}: ep {}, direction {}, {} bytes".format(seqnum, ep, direction, buflen))

        if ep == 0:
            # EP0 control traffic; unpack the control request.  We deal with
            # this synchronously.
            if wLength != buflen:
                raise USBIPProtocolErrorException(f"wLength {wLength} != buflen {buflen}")

            # self.say("EP0 requesttype {}, request {}".format(bRequestType, bRequest))
            fakeit = False

            if bRequestType == USB_RECIP_DEVICE and bRequest == USB_REQ_SET_ADDRESS:
                raise USBIPUnimplementedException("USB_REQ_SET_ADDRESS")
            elif bRequestType == USB_RECIP_DEVICE and bRequest == USB_REQ_SET_CONFIGURATION:
                self.say(f"set configuration: {wValue}")
                dev.hnd.setConfiguration(wValue)

                # Claim all the interfaces.
                config = None
                for _config in dev.hnd.getDevice().iterConfigurations():
                    if _config.getConfigurationValue() == wValue:
                        config = _config
                        break
                if config is not None:
                    for i in range(config.getNumInterfaces()):
                        self.say("  claim interface: {}".format(i))
                        try:
                            dev.hnd.claimInterface(i)
                        except usb1.USBError as e:
                            self.say(f"  failed to claim interface {i}: {e}")
                            # handle: usb1.USBDeviceHandle = dev.hnd
                            # print(f"releasing interface {i}")
                            # try:
                            #     handle.releaseInterface(i)
                            #     print("released interface. reclaiming")
                            #     handle.claimInterface(i)
                            # except usb1.USBError as e:
                            #     print(f"failed to release interface: {e}")

                fakeit = True
            elif bRequestType == USB_RECIP_INTERFACE and bRequest == USB_REQ_SET_INTERFACE:
                self.say("set interface alt setting: {} -> {}".format(wIndex, wValue))
                dev.hnd.claimInterface(wIndex)
                dev.hnd.setInterfaceAltSetting(wIndex, wValue)
                fakeit = True

            try:
                if direction == USBIP_DIR_IN:
                    data = dev.hnd.controlRead(bRequestType, bRequest, wValue, wIndex, wLength)
                    resp = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_RET_SUBMIT,
                        seqnum,
                        0,
                        0,
                        0,
                        # dev.devid, direction, ep,
                        0,
                        len(data),
                        0,
                        0,
                        0,
                        b"",
                    )
                    resp += data
                    # self.say("wrote response with {}/{} bytes".format(len(data), wLength))
                    self.writer.write(resp)
                else:
                    if fakeit:
                        wlen = 0
                    else:
                        wlen = dev.hnd.controlWrite(bRequestType, bRequest, wValue, wIndex, buf)
                    resp = struct.pack(
                        ">IIIIIiiiii8s", USBIP_RET_SUBMIT, seqnum, 0, 0, 0, 0, wlen, 0, 0, 0, b""
                    )
                    # self.say("wrote {}/{} bytes".format(wlen, wLength))
                    self.writer.write(resp)
            except usb1.USBError:
                resp = struct.pack(
                    ">IIIIIiiiii8s", USBIP_RET_SUBMIT, seqnum, 0, 0, 0, -USB_EPIPE, 0, 0, 0, 0, b""
                )
                # reset device ???
                self.say("[USBError] EPIPE")
                self.writer.write(resp)
        else:
            # Ok, a request on another endpoint.  These are asynchronous.
            xfer = dev.hnd.getTransfer()

            if direction == USBIP_DIR_IN:

                def callback(xfer_):
                    # self.say('callback IN seqnum {:x} status {} len {} buflen {}'.format(seqnum, xfer.getStatus(), xfer.getActualLength(), len(xfer.getBuffer())))
                    resp = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_RET_SUBMIT,
                        seqnum,
                        0,
                        0,
                        0,
                        -xfer.getStatus(),
                        xfer.getActualLength(),
                        0,
                        0,
                        0,
                        b"",
                    )
                    resp += xfer.getBuffer()[: xfer.getActualLength()]
                    self.writer.write(resp)
                    del self.urbs[seqnum]

                xfer.setBulk(ep | 0x80, buflen, callback)
                xfer.submit()
                self.urbs[seqnum] = USBIPPending(seqnum, dev, xfer)
            else:

                def callback(xfer_):
                    # self.say('callback OUT seqnum {:x} status {} '.format(seqnum, xfer.getStatus()))
                    resp = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_RET_SUBMIT,
                        seqnum,
                        0,
                        0,
                        0,
                        -xfer.getStatus(),
                        xfer.getActualLength(),
                        0,
                        0,
                        0,
                        b"",
                    )
                    self.writer.write(resp)
                    del self.urbs[seqnum]

                xfer.setBulk(ep, buf, callback)
                xfer.submit()
                self.urbs[seqnum] = USBIPPending(seqnum, dev, xfer)

    async def handle_urb_unlink(self, seqnum, dev, direction, ep):
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (sseqnum, buflen, start_frame, number_of_packets, interval, setup) = struct.unpack(
            op_submit, data
        )

        self.say("seq {:x}: UNLINK".format(sseqnum))

        if sseqnum not in self.urbs:
            rv = -USB_ENOENT
        else:
            rv = 0
            self.urbs[sseqnum].xfer.cancel()

        resp = struct.pack(">IIIIIiiiii8s", USBIP_RET_UNLINK, seqnum, 0, 0, 0, rv, 0, 0, 0, 0, b"")

    async def handle_packet(self):
        """
        Handle a USBIP packet.
        """

        # Try to read a header of some kind.  We can tell because if it's an
        # URB, the |op_common.version| is overlayed with the
        # |usbip_header_basic.command|, and so the |.version| is 0x0000;
        # otherwise, it's supposed to be 0x0106.

        try:
            data = await self.reader.readexactly(2)
        except asyncio.exceptions.IncompleteReadError:
            return False

        (version,) = struct.unpack(">H", data)
        if version == 0x0000:
            # Note that we've already trimmed the version.
            op_common = ">HIIII"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, seqnum, devid, direction, ep) = struct.unpack(op_common, data)

            if devid not in self.devices:
                raise USBIPProtocolErrorException("devid unattached {:x}".format(devid))
            dev = self.devices[devid]

            if opcode == USBIP_CMD_SUBMIT:
                await self.handle_urb_submit(seqnum, dev, direction, ep)
            elif opcode == USBIP_CMD_UNLINK:
                await self.handle_urb_unlink(seqnum, dev, direction, ep)
            elif opcode == USBIP_RESET_DEV:
                raise USBIPUnimplementedException("URB_RESET_DEV")
            else:
                raise USBIPProtocolErrorException("bad USBIP URB {:x}".format(opcode))
        elif (version & 0xFF00) == 0x0100:
            # Note that we've already trimmed the version.
            op_common = ">HI"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, status) = struct.unpack(op_common, data)

            if opcode == USBIP_OP_UNSPEC | USBIP_REQUEST:
                self.writer.write(
                    struct.pack(">HHI", version, USBIP_OP_UNSPEC | USBIP_REPLY, USBIP_ST_OK)
                )
            elif opcode == USBIP_OP_DEVINFO | USBIP_REQUEST:
                data = await self.reader.readexactly(USBIP_BUS_ID_SIZE)
                raise USBIPUnimplementedException("DEVINFO")
                # writer.write(struct.pack(">HHI", version, USBIP_OP_DEVINFO | USBIP_REPLY, USBIP_ST_NA)
            elif opcode == USBIP_OP_DEVLIST | USBIP_REQUEST:
                self.say("DEVLIST")
                # XXX: in theory, op_devlist_request has a _reserved, but they don't seem to xmit it?
                # data = await self.reader.readexactly(4) # reserved
                self.handle_op_devlist()
            elif opcode == USBIP_OP_IMPORT | USBIP_REQUEST:
                data = (await self.reader.readexactly(USBIP_BUS_ID_SIZE)).decode().rstrip("\0")
                self.say("IMPORT {}".format(data))
                self.handle_op_import(data)
            else:
                raise USBIPProtocolErrorException("bad USBIP op {:x}".format(opcode))
        else:
            raise USBIPProtocolFatalError("unsupported USBIP version {:02x}".format(version))

        return True

    async def connection(self):
        self.say("connect")

        while True:
            try:
                success = await self.handle_packet()
                await self.writer.drain()
                if not success:
                    break
            except Exception as e:
                traceback.print_exc()
                self.say(f"forcing disconnect due to exception: {e}")
                break

        self.say("disconnect")
        for i in self.devices:
            self.devices[i].hnd.close()
            self.devices[i] = None
        await self.writer.drain()
        self.writer.close()


async def usbip_connection(reader, writer):
    conn = USBIPConnection(reader, writer)
    await conn.connection()


class _HotplugHandle(_objfinalizer.AutoFinalizedObject):
    """
    Adopted from: https://github.com/pyusb/pyusb/pull/160
    """

    def __init__(
        self,
        user_callback,
        *,
        user_data=None,
        backend=None,
        events=0x01 | 0x02,  # arrive and exit
        flags=0,  # do not emumerate on register
        vendor_id=-1,  # match any
        product_id=-1,  # match any
        dev_class=-1,  # match any
    ):
        if backend is None:
            backend = usb.backend.libusb1.get_backend()  # type: ignore

        _libusb_hotplug_callback_handle = c_int
        _libusb_hotplug_callback_fn = CFUNCTYPE(
            c_int, c_void_p, _libusb_device_handle, c_uint, py_object
        )
        backend.lib.libusb_hotplug_register_callback.argtypes = [
            c_void_p,
            c_int,
            c_int,
            c_int,
            c_int,
            c_int,
            _libusb_hotplug_callback_fn,
            py_object,
            POINTER(_libusb_hotplug_callback_handle),
        ]
        backend.lib.libusb_hotplug_deregister_callback.argtypes = [
            c_void_p,
            _libusb_hotplug_callback_handle,
        ]

        self.user_callback = user_callback
        self.__callback = _libusb_hotplug_callback_fn(self.callback)
        self.__backend = backend
        # If an object is passed directly and never stored anywhere,
        # the pointer will remain and user data might be wrong!
        # therefore we have to keep a refernce here
        self.__user_data = py_object(user_data)
        self.handle = _libusb_hotplug_callback_handle()
        backend.lib.libusb_hotplug_register_callback(
            backend.ctx,
            events,
            flags,
            vendor_id,
            product_id,
            dev_class,
            self.__callback,
            self.__user_data,
            byref(self.handle),
        )

    def callback(self, ctx, dev, evnt, user_data):
        dev = Device(_Device(dev), self.__backend)
        return self.user_callback(dev, evnt, user_data)

    def deregister(self):
        backend: usb.backend.IBackend = self.__backend
        backend.lib.libusb_hotplug_deregister_callback(backend.ctx, handle.handle)  # type: ignore

    def handle_events(self):
        return self.__backend.lib.libusb_handle_events(self.__backend.ctx)

    @property
    def backend(self):
        return self.__backend


def get_usb_version(dev: Device) -> Tuple[int, int, int]:
    usb_bcd = dev.bcdUSB  # type: ignore
    if usb_bcd is not None:
        return ((usb_bcd & 0xFF00) >> 8, (usb_bcd & 0xF0) >> 4, usb_bcd & 0xF)
    else:
        return (0, 0, 0)


def get_usb_version_str(dev: Device) -> str:
    (major, minor, rev) = get_usb_version(dev)
    v = f"{major}.{minor}"
    if rev != 0:
        v += str(rev)
    return v


USB_EVENT_PLUGGED = 1
USB_EVENT_UNPLUGGED = 2


# Socket user
notify_socket = None


avail_devices = set()


def handle_device_event(dev: Device, event, user_data):
    global notify_socket
    global avail_devices
    dev_id = f"{dev.bus}-{dev.address}"

    if event == USB_EVENT_PLUGGED:
        event_str = "PLUGGED"
        avail_devices.add(dev_id)
    elif event == USB_EVENT_UNPLUGGED:
        event_str = "UNPLUGGED"
        if dev_id in avail_devices:
            avail_devices.remove(dev_id)
    else:
        event_str = str(event)
    usbip_log.info(
        # f">> USB {get_usb_version_str(dev)} device {dev._str()} event:{event_str}"
        f">> USB {get_usb_version_str(dev)} device {dev.idVendor:04x}:{dev.idProduct:04x} on {dev_id}, event: {event_str}"  # type: ignore
    )

    if notify_socket is not None:
        try:
            if event in (USB_EVENT_PLUGGED, USB_EVENT_UNPLUGGED):
                write_event(notify_socket, dev_id, event_str)
        except Exception as e:
            notify_log.error(f"Exception {e} during write to notify socket")
            pass
    else:
        notify_log.error("No clients connected! (notify_socket is None)")
    return 0


def write_event(socket: asyncio.StreamWriter, dev_id, event):
    socket.write(f"{dev_id};{event};\n".encode())


async def notify_connected_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    notify_log.info(f"connection from {addr!r}")
    global notify_socket
    notify_socket = writer
    data = await reader.read(100)  # Max number of bytes to read
    # if not data:
    notify_log.info(f"received hello from client {addr!r}")
    if data != b"Client hello":
        notify_log.error(f"!!! Unexpected message from client {addr!r}: {data!r}")
        # await writer.drain()
        writer.close()
        return
    # global avail_devices
    filter_args = {}
    if isinstance(FILTER_VENDOR_ID, int) and FILTER_VENDOR_ID > 0:
        filter_args["idVendor"] = FILTER_VENDOR_ID
    if isinstance(FILTER_PRODUCT_ID, int) and FILTER_PRODUCT_ID > 0:
        filter_args["idProduct"] = FILTER_PRODUCT_ID
    usb_devices = usb.core.find(find_all=True, **filter_args)
    if usb_devices is None:
        usb_devices = []
    usb_devices = list(usb_devices)
    usbip_log.info(f"Found {len(usb_devices)} devices")
    for dev in usb_devices:
        if isinstance(dev, Device):
            dev_id = f"{dev.bus}-{dev.address}"
        write_event(notify_socket, dev_id, "PLUGGED")
    await writer.drain()
    while True:
        data = await reader.read(100)
        if not data:
            break
        usbip_log.debug(f"{addr} -> {data!r} {reader.at_eof()}")
    usbip_log.info(f"> closing connection from {addr!r}")
    writer.close()


handle = _HotplugHandle(
    handle_device_event, vendor_id=FILTER_VENDOR_ID, product_id=FILTER_PRODUCT_ID
)


def get_socket_name(server: asyncio.Server) -> str:
    assert server is not None
    return sock_to_str(server.sockets[0].getsockname())


async def start_notify_server():
    # try:
    server = await asyncio.start_server(notify_connected_cb, NOTIFY_HOST, NOTIFY_PORT)
    # except OSError as e:
    #     notify_log.critical(f"start_server failed: {e.strerror}")
    #     return
    async with server:
        notify_log.info(f"Serving on {get_socket_name(server)}")
        await server.serve_forever()


def usb_event_loop(handle: _HotplugHandle, stop_event: threading.Event):
    while not stop_event.is_set():
        r = handle.handle_events()
        if r != 0:
            usbip_log.error(f"[usb_event_loop] libusb_handle_events returned {r}")
            return


async def usb_event_handler(handle, stop_event):
    usbip_log.info("Handling USB events...")
    return await asyncio.to_thread(usb_event_loop, handle, stop_event)


async def uspip_server():
    usbip_log.debug(f"Starting server on {USBIP_HOST}:{USBIP_PORT}")
    server = await asyncio.start_server(usbip_connection, USBIP_HOST, USBIP_PORT)
    async with server:
        usbip_log.info(f"Serving on {get_socket_name(server)}")
        return await server.serve_forever()


def usb_added(fd, events):
    usbip_log.info("adding fd {} for {}".format(fd, events))
    loop = asyncio.get_running_loop()
    loop.add_reader(fd, lambda: usbctx.handleEventsTimeout())


def usb_removed(fd, events):
    usbip_log.info("removing fd {} for {}".format(fd, events))
    loop = asyncio.get_running_loop()
    loop.remove_reader(fd)


async def main(stop_event: threading.Event):
    for fd, events in usbctx.getPollFDList():
        usb_added(fd, events)
    usbctx.setPollFDNotifiers(usb_added, usb_removed)
    try:
        async with asyncio.TaskGroup() as tg:
            usbip_task = tg.create_task(uspip_server())

            assert usbip_task is not None
            usbip_task.add_done_callback(lambda _: stop_event.set())
            try:
                await asyncio.sleep(0.1)
            except asyncio.exceptions.CancelledError:
                return
            notif_server_task = tg.create_task(start_notify_server())
            assert notif_server_task is not None
            notif_server_task.add_done_callback(
                lambda _: (print("notif_server_task done"), stop_event.set(), usbip_task.cancel())
            )
            usbip_task.add_done_callback(lambda _: usbip_task.cancel())
            try:
                await asyncio.sleep(0.1)
            except asyncio.exceptions.CancelledError:
                return
            usb_task = tg.create_task(usb_event_handler(handle, stop_event))
            assert usb_task is not None
            usb_task.add_done_callback(
                lambda _: (
                    # print(f"usb_task done cancelled={usb_task.cancelled()}"),
                    stop_event.set(),
                    usbip_task.cancel(),
                    notif_server_task.cancel(),
                    handle.deregister(),
                )
            )
    except ExceptionGroup as eg:
        for e in eg.exceptions:
            if not isinstance(e, asyncio.CancelledError):
                raise e


if __name__ == "__main__":
    args = parse_args()
    USBIP_HOST = args.usbip_host
    USBIP_PORT = args.usbip_port
    NOTIFY_HOST = args.notify_host
    NOTIFY_PORT = args.notify_port

    # check running as root
    if os.geteuid() != 0:
        usbip_log.critical("This script must be run as root.")
        exit(1)

    stop_event = threading.Event()

    def cleanup_and_exit(exit_val):
        usbip_log.info("Cleaning up and exiting...")
        stop_event.set()
        try:
            handle.deregister()
            handle.deregister()
        except Exception as e:
            notify_log.error(f"Error during deregister: {e}")
            pass
        try:
            asyncio.get_event_loop().stop()
        except RuntimeError:
            notify_log.debug("Event loop already stopped")
            pass
        usbctx.setPollFDNotifiers(None, None)
        usbctx.close()
        exit(exit_val)

    while True:
        try:
            asyncio.run(main(stop_event))
        except KeyboardInterrupt:
            usbip_log.info("KeyboardInterrupt, exiting...")
            cleanup_and_exit(0)
        except OSError as e:
            usbip_log.info("%s", e.strerror)
            cleanup_and_exit(0)
        except USBIPProtocolErrorException as e:
            usbip_log.error(f"[__main__] USBIPProtocolError: {e}")
        finally:
            stop_event.set()
        time.sleep(3)
