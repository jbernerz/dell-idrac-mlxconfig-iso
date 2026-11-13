# BF3 InfiniBand → Ethernet, out of band through iDRAC

Fleet tool for switching the NVIDIA BlueField-3 in **slot 2** of Dell PowerEdge XE9712 nodes from
InfiniBand to Ethernet, driven entirely through each node's iDRAC.

## Why it isn't a single Redfish PATCH

Taken from Dell's XE9712 iDRAC10 1.30.07.10 mockup (BIOS 1.1.2):

- **No BIOS setting.** None of the 498 BIOS settings controls the link type. For slot 2 there is
  only `Slot2` (Enabled / BootDriverDisabled) and `Slot2Bif` (lane split, read-only).
  The HGX baseboard BIOS has no settings at all.
- **No NIC setting.** `NetworkAttributesRegistry_NIC.Slot.2-*` is empty, so Dell's usual path
  (`.../Oem/Dell/DellNetworkAttributes/<port>/Settings`, or a Server Configuration Profile import)
  has nothing to set.
- **The Redfish properties are read-only.** `NetDevFuncCapabilities` lists only
  Disabled/Ethernet, and the Dell fields `VPISupport`, `Protocol` and `NicMode` are read-only.

The link type is stored in the card's firmware as `LINK_TYPE_P1` / `LINK_TYPE_P2`
(1 = InfiniBand, 2 = Ethernet). Only `mlxconfig` (NVIDIA MFT) or `mstconfig` (mstflint) running on
the host, or on the BF3's own Arm OS, can change it. The card loads the new value after a full
power cycle.

## How the tool does it out of band

`bf3_linktype_idrac.py apply` does the following on each node, several nodes at a time:

1. Mounts a small arm64 utility ISO as iDRAC virtual media (`VirtualMedia.InsertMedia`).
2. Sets a one-time boot from it (`Boot.BootSourceOverrideTarget = Cd`, `Once`) and resets the host.
3. The ISO runs [`image/bf3-set-eth.sh`](image/bf3-set-eth.sh). The script touches **only**
   BlueField-3 devices (PCI `15b3:a2dc`), so the integrated InfiniBand controllers of the compute
   fabric are left alone. It sets `LINK_TYPE_P1=2 LINK_TYPE_P2=2` and then **powers the host off**.
4. The tool sees `PowerState = Off`, ejects the media and issues
   `Chassis.Reset {"ResetType":"FullPowerCycle"}` (a virtual AC cycle). It then powers the host on.

If the script fails, it leaves the host **on**. The tool then times out and flags that node, and
the error is visible on the iDRAC virtual console.

## Procedure

```bash
pip install requests
export IDRAC_USER=root IDRAC_PASSWORD='...'
cat > nodes.txt <<EOF
10.10.0.11
10.10.0.12   # one iDRAC IP or hostname per line
EOF

# 1. Read-only survey. Shows the current link type, firmware, Secure Boot, and whether a newer
#    iDRAC exposes link-type attributes (look for "attrs=" > 0 or CANDIDATES=[...]).
./bf3_linktype_idrac.py check -n nodes.txt --insecure

# 2. Build the utility ISO (see below) and put it on an HTTP server the iDRACs can reach.

# 3. Canary: one node first, printing the Redfish writes without sending them, then for real.
./bf3_linktype_idrac.py apply -n canary.txt --insecure --iso-url http://10.10.0.5/bf3-eth.iso --dry-run
./bf3_linktype_idrac.py apply -n canary.txt --insecure --iso-url http://10.10.0.5/bf3-eth.iso
./bf3_linktype_idrac.py verify -n canary.txt --insecure     # after the host has finished POST

# 4. The rest, 5 at a time (--parallel). Nodes already on Ethernet are skipped.
./bf3_linktype_idrac.py apply  -n nodes.txt --insecure --iso-url http://10.10.0.5/bf3-eth.iso
./bf3_linktype_idrac.py verify -n nodes.txt --insecure
```

`--insecure` skips TLS checks for self-signed iDRAC certificates; use `--ca-bundle` instead if
you have the CA. Run `--help` for timeouts, `--graceful` (soft restart instead of a forced one),
`--leave-off`, `--slot` and `--vm-slot`.

**Already have SSH to a running OS on the nodes?** Then it's simpler to set the value in-band
(the `mlxconfig ... set` line from `bf3-set-eth.sh`, run with Ansible or similar) and use only the
out-of-band power cycle:
`./bf3_linktype_idrac.py powercycle -n nodes.txt --insecure`.

## The utility ISO

Any **arm64 UEFI** Linux live image works (Grace CPUs, so not x86), provided it contains
`mstflint` or NVIDIA MFT plus `pciutils` and runs `bf3-set-eth.sh` once at boot. If your
provisioning system already has an arm64 rescue/PXE image, add the two files from `image/` to it.

A starting point with Debian live-build. This recipe is **untested**: build it on an arm64 host,
and boot-test it on the canary.

```bash
sudo apt install live-build
mkdir bf3-live && cd bf3-live
lb config --architectures arm64 --distribution trixie \
  --archive-areas "main contrib non-free-firmware" \
  --binary-images iso-hybrid --bootloaders grub-efi
echo "mstflint pciutils" > config/package-lists/bf3.list.chroot
install -D -m 0755 ../image/bf3-set-eth.sh      config/includes.chroot/usr/local/sbin/bf3-set-eth.sh
install -D -m 0644 ../image/bf3-set-eth.service config/includes.chroot/etc/systemd/system/bf3-set-eth.service
mkdir -p config/hooks/normal
printf '#!/bin/sh\nsystemctl enable bf3-set-eth.service\n' > config/hooks/normal/9000-bf3.hook.chroot
chmod +x config/hooks/normal/9000-bf3.hook.chroot
sudo lb build            # -> live-image-arm64.hybrid.iso
```

If the distro's `mstflint` doesn't recognise the BF3, install NVIDIA MFT for arm64 in the image
instead. The script uses `mlxconfig` when it is present.

## Caveats

- **Disruptive.** Each node is rebooted into the ISO and then fully power-cycled. Drain the nodes
  first.
- **Secure Boot.** It is Disabled in the mockup. If it is enabled on your nodes, an unsigned ISO
  won't boot. A Secure Boot kernel may also apply lockdown, which can block the PCI config-space
  access the tools need. `check` shows the Secure Boot state.
- **Host privilege.** If the BF3 was set to restrict host privileges (`mlxprivhost`), the host
  cannot change its configuration. The change then has to be made from the BF3's Arm OS.
- **Both ports.** The script sets both ports to the same type. NVIDIA doesn't clearly document
  mixed IB/ETH on one card.
- **verify reads iDRAC's inventory,** which refreshes after POST. Give a freshly booted node a few
  minutes before trusting the result. An IB-mode card shows up as `InfiniBand.Slot.2-*`, an
  Ethernet one as `NIC.Slot.2-*` with `LinkNetworkTechnology = Ethernet`.
- **Not documented by Dell for the XE9712.** `Cd` one-time boot and `FullPowerCycle` are both
  listed by this iDRAC, but the full sequence hasn't been run on XE9712 hardware. Do the canary
  node first.

## What has been tested

`check`, `verify`, and `apply`/`powercycle` in `--dry-run` were run against the XE9712 iDRAC10
1.30.07.10 mockup served by a local stub. A real `apply` against the stub was also run, to check
the Redfish writes it sends and its timeout handling. Nothing has been run on real hardware, and
the ISO recipe has not been built.
