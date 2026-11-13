# BF3 InfiniBand → Ethernet, out of band through iDRAC

Two pieces for switching the NVIDIA BlueField-3 in **slot 2** of Dell PowerEdge XE9712 nodes
from InfiniBand to Ethernet, driven entirely through each node's iDRAC:

| Path | What it is |
|---|---|
| [`iso/`](iso/) | Builds `bf3-eth-arm64.iso`: a ~57 MiB arm64 UEFI boot ISO that sets every BF3 to Ethernet and powers the host off |
| [`bf3_linktype_idrac.py`](bf3_linktype_idrac.py) | Fleet tool: boots that ISO on many nodes through iDRAC virtual media, then full-power-cycles them; also `check` / `verify` |

## Why not a single Redfish PATCH

Taken from Dell's XE9712 iDRAC10 1.30.07.10 mockup (BIOS 1.1.2):

- **No BIOS setting.** None of the 498 BIOS settings controls the link type. For slot 2 there is
  only `Slot2` (Enabled / BootDriverDisabled) and `Slot2Bif` (lane split, read-only).
  The HGX baseboard BIOS has no settings at all.
- **No NIC setting.** `NetworkAttributesRegistry_NIC.Slot.2-*` is empty, so Dell's usual path
  (`.../Oem/Dell/DellNetworkAttributes/<port>/Settings`, or a Server Configuration Profile import)
  has nothing to set.
- **The mockup can't show whether `NetDevFuncType` is writable.** Its BF3 is already Ethernet, and
  its `NetDevFuncCapabilities` list only Disabled/Ethernet. On an InfiniBand-mode node, it is
  worth trying `NetDevFuncType` on `.../NetworkDeviceFunctions/<port>/Settings` first. If iDRAC
  accepts it, you don't need the ISO.

The link type is stored in the card's firmware as `LINK_TYPE_P1` / `LINK_TYPE_P2`
(1 = InfiniBand, 2 = Ethernet). It is written from the host with `mstconfig`/`mlxconfig`, and the
card loads the new value after a full power cycle.

## The ISO

```bash
sudo apt install gpg xz-utils dpkg binutils grub-common dosfstools mtools xorriso qemu-user-static
cd iso && ./build_iso.py            # -> bf3-eth-arm64.iso (+ .sha256, .manifest)
```

It contains no full OS: just a standalone GRUB EFI loader, the Debian 13 arm64 kernel, and an
initramfs with busybox, `mstconfig` (Debian's `mstflint` 4.31) and [`iso/init`](iso/init).
Every package is downloaded from the Debian archive unmodified. Each one is checked against the
archive's GPG-signed index before use. The build runs on any Debian/Ubuntu host, x86 included.

On boot, the default menu entry starts after 5 s and does the following:

1. Finds every BlueField-3 by PCI ID `15b3:a2dc`. The integrated InfiniBand controllers of the
   compute fabric have different IDs and are left alone.
2. Shows the current `LINK_TYPE_P1/P2`.
3. Runs `mstconfig -y set LINK_TYPE_P1=2 LINK_TYPE_P2=2`.
4. Re-queries and checks that both ports show `ETH(2)`.
5. **Powers the host off.** The fleet tool treats this as success.

On any failure it prints the reason, **leaves the host on** and opens a shell. Everything appears
on the iDRAC virtual console.

The GRUB menu has two more entries for trying it by hand on a canary node:
- **Show current link type only** (no changes, then a shell).
- **Self-test** (checks the tools run, lists NVIDIA devices, then powers off).

Build options: `--link-type 1` makes an IB ISO. `--extra-cmdline`, `--console`, `--timeout` and
`--default-entry` change the GRUB menu.

**Secure Boot must be disabled** on the nodes, because the GRUB image is unsigned. It is
Disabled in the mockup; `check` shows it per node.

## Fleet procedure

```bash
pip install requests
export IDRAC_USER=root IDRAC_PASSWORD='...'
# nodes.txt: one iDRAC IP or hostname per line

./bf3_linktype_idrac.py check -n nodes.txt --insecure        # read-only survey

# Put bf3-eth-arm64.iso on an HTTP server the iDRACs can reach, then do one canary node:
./bf3_linktype_idrac.py apply  -n canary.txt --insecure --iso-url http://10.10.0.5/bf3-eth-arm64.iso --dry-run
./bf3_linktype_idrac.py apply  -n canary.txt --insecure --iso-url http://10.10.0.5/bf3-eth-arm64.iso
./bf3_linktype_idrac.py verify -n canary.txt --insecure      # once the host has finished booting

# Then the rest (5 at a time by default; nodes already on Ethernet are skipped):
./bf3_linktype_idrac.py apply  -n nodes.txt --insecure --iso-url http://10.10.0.5/bf3-eth-arm64.iso
./bf3_linktype_idrac.py verify -n nodes.txt --insecure
```

`apply` runs these steps on each node:
1. `VirtualMedia.InsertMedia`, then a one-time boot from `Cd`.
2. Restarts the host. The ISO sets the link type and powers off.
3. The tool sees `PowerState = Off` and ejects the media.
4. `Chassis.Reset FullPowerCycle` (a virtual AC cycle), then power on.

If the ISO doesn't power the node off within `--iso-timeout`, the node is reported as failed,
with the media still attached so you can look at the console.

`--insecure` skips TLS checks for self-signed iDRAC certificates; use `--ca-bundle` instead if you
have the CA. If the nodes already run Linux with SSH, you can run `mstconfig`/`mlxconfig` in-band
and use only `./bf3_linktype_idrac.py powercycle`.

## Caveats

- **Disruptive.** Each node is rebooted into the ISO and then fully power-cycled. Drain the nodes
  first.
- **Host privilege.** If the BF3 restricts host privileges (`mstprivhost`/`mlxprivhost`), the set
  fails. The node then stays on and is flagged, and the change has to be made from the BF3's Arm
  side.
- **Both ports.** Both ports are set to the same type. NVIDIA doesn't clearly document mixed
  IB/ETH on one card.
- **`verify` reads iDRAC's inventory,** which refreshes after POST. An IB-mode card shows up as
  `InfiniBand.Slot.2-*`, an Ethernet one as `NIC.Slot.2-*` with `LinkNetworkTechnology = Ethernet`.
- **Licensing.** The ISO redistributes unmodified Debian packages (Linux, GRUB, busybox,
  mstflint; GPL). Exact versions are in the `.manifest` file, and the matching sources are in the
  Debian archive.

## What has been tested

**ISO**, in QEMU on an arm64 `virt` machine with AAVMF UEFI and Secure Boot off:
- It boots both as a USB device and as a pure El Torito CD (partition tables wiped).
- The self-test entry runs `mstconfig` and powers off.
- The default entry, with no BF3 present, reports `FAILED: no BlueField-3 found`, stays on and
  opens a shell. This was checked on the framebuffer console.
- The result check after `set` was tested against sample `mstconfig` output, using the image's
  own busybox.
- The arm64 `mstconfig`, `mstflint` and busybox were smoke-tested under qemu-user during the build.
- `mstflint` 4.31's parameter database defines `LINK_TYPE` (IB=1/ETH=2) and knows BlueField-3.

**Fleet tool:** `check`, `verify` and `--dry-run` were run against the XE9712 mockup through a
local stub. A real `apply` against the stub was also run, to check the requests it sends and its
timeout handling.

**Not tested:** anything on real XE9712/BF3 hardware, including whether iDRAC's virtual CD boot,
the `mstconfig` write over PCI config space and `FullPowerCycle` behave as expected there. Do the
canary node first.
