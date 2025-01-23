# USBIP with Notify Client/Server

The [USBIP](https://docs.kernel.org/usb/usbip_protocol.html) server implementation is based on [pyusbip](https://github.com/jwise/pyusbip) by Joshua Wise and includes updates from [this fork](https://github.com/tumayt/pyusbip).

`pyusbip` is a userspace USBIP server implementation based on `python-libusb1`. (In USBIP, the 'server' has the physical USB device attached, and the 'client' has only a network cable attached; i.e., you run this if you have a USB device attached to your local machine that you'd like to be able to use on a different computer.)

It has been tested (for small values of "tested") on Mac OS X; it is, at least, sufficient to forward many FPGA boards to the programmer software. Boards tested: Digilent BASYS-3, Digilent ARTY A7, and Lattice MachXO3.

The notify client/server implementation is a simple extension to the original `pyusbip` implementation. The server sends a notification to the client when a USB device is attached or detached. The client receives the notification and will attach/detach the available device from the USBIP server.

## Usage instructions

### Server side (running on the host machine with the USB device attached)

* You may need to create a virtualenv: `virtualenv .venv`.
  * If you do, activate it: `source .venv/bin/activate`.
* Install the dependencies: `pip install -r requirements.txt`.
* As necessary on your platform, give yourself permission to access the USB device.
* Launch the server: `./usbip-notify-server.py`

### Client side

Note: client must be run as `root` to attach/detach the USB device.

* Run the client as `root`: `sudo ./usbip-notify-client.py`.

## Limitations of the included USBIP implementation (pyusbip)

From the original [README](https://github.com/jwise/pyusbip/blob/master/README.md):

`pyusbip` has many limitations.  Much of the protocol is unimplemented; `pyusbip` simply disconnects a device and drops a connection if it trips on that.  In some cases, protocol violations in USBIP can cause the Linux kernel's entire USB stack to crash (!); for instance, if `pyusbip` gets stuck transmitting half of a response URB, you may need to reboot the remote end before it'll come back to life.  Following is a list of known limitations in `pyusbip`:

* `pyusbip` gives up on a device if it can't claim all interfaces, rather than failing URBs with `-EPERM` or some such.  (This is a problem when exporting, say, a ST-Link board that also has a mass storage device, from OS X.)
* `pyusbip`'s control traffic is synchronous.
* `pyusbip` does not keep track of what type of endpoint it's talking to, and as such, always sends bulk requests, even to interrupt or isochronous endpoints.
* `pyusbip` does not implement isochronous endpoints at all.
* Error handling usually results in forcing a device disconnect, rather than doing anything reasonable.
* `UNLINK` requests may send a spurious URB reply, which could confuse some clients.
* Comments are ... sparse.
* Code architecture is suspicious.
