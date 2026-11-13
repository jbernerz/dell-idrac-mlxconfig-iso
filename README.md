# BlueField-3: InfiniBand → Ethernet through Dell iDRAC

Tools for switching the ports of NVIDIA BlueField-3 DPUs from InfiniBand to Ethernet on Dell
PowerEdge servers, **out of band** and across many nodes at once, using only each node's iDRAC.
They were written for the PowerEdge XE9712 (iDRAC10), where the BF3 sits in PCIe slot 2.

## Why this exists

The BF3's link type is not a BIOS or iDRAC setting. It is stored in the card's own firmware as
`LINK_TYPE_P1` / `LINK_TYPE_P2` (1 = InfiniBand, 2 = Ethernet), so it can't be changed with a
Redfish BIOS PATCH. On the XE9712 (iDRAC10 1.30.07.10, BIOS 1.1.2), iDRAC exposes no attribute
for it:
- None of the 498 BIOS settings controls it.
- The per-port network attribute registry is empty.

The value has to be written from the host with `mstconfig`/`mlxconfig`, and the card picks it up
after a full power cycle.

## What's here

| Path | Purpose |
|---|---|
| [`bf3-linktype/iso/`](bf3-linktype/iso/) | `build_iso.py` builds a small arm64 UEFI boot ISO (~57 MiB). It contains GRUB, a Debian 13 kernel, busybox and `mstconfig`. The ISO finds every BF3 by PCI ID, sets both ports to Ethernet, checks the result and powers the host off. On failure it stays on with a console shell. |
| [`bf3-linktype/bf3_linktype_idrac.py`](bf3-linktype/bf3_linktype_idrac.py) | Fleet tool driven by iDRAC Redfish. `check` and `verify` are read-only. `apply` boots the ISO through virtual media, waits for it to power off, then does a full power cycle. `powercycle` does only the power cycle. |
| [`bf3-linktype/README.md`](bf3-linktype/README.md) | Full documentation: procedure, options, caveats and what has been tested. |
| [`dist/`](dist/) | Prebuilt `bf3-eth-arm64.iso` (sets both ports to Ethernet), with its `.sha256` and package `.manifest`. |
| [`gb300-debug-iso/`](gb300-debug-iso/) | Separate builder for a **GB300 debug live ISO** (Ubuntu 24.04 arm64) with NVIDIA's GB300 NVL72 release 1.0.6 host stack: GPU driver 580.126.20, DOCA-Host 3.2.1-044413, MFT, DCGM, nvfwupd, nvdebug and general debug tools. The ISO itself contains NVIDIA-licensed software, so it isn't published here; see its README. |

## Quick start

```bash
# 1. Get the ISO. Either use the prebuilt one and check it:
(cd dist && sha256sum -c bf3-eth-arm64.iso.sha256)
#    or build a fresh one on any Debian/Ubuntu host (x86 or arm64):
#    sudo apt install gpg xz-utils dpkg binutils grub-common dosfstools mtools xorriso qemu-user-static
#    bf3-linktype/iso/build_iso.py -o bf3-eth-arm64.iso

# 2. Host the ISO on an HTTP server the iDRACs can reach, list the iDRACs in nodes.txt, then:
pip install requests
export IDRAC_USER=root IDRAC_PASSWORD='...'
bf3-linktype/bf3_linktype_idrac.py check  -n nodes.txt --insecure
bf3-linktype/bf3_linktype_idrac.py apply  -n canary.txt --insecure --iso-url http://<server>/bf3-eth-arm64.iso
bf3-linktype/bf3_linktype_idrac.py verify -n canary.txt --insecure
# then the remaining nodes with -n nodes.txt
```

You don't need to know any PCI, IP or MAC addresses in advance. The ISO finds the BF3 at boot by
its PCI ID (`15b3:a2dc`) and doesn't use the network. The only inputs are the iDRAC addresses and
the ISO's URL.

## Requirements and caveats

- **Secure Boot must be disabled** on the nodes, because the ISO's GRUB is unsigned.
  `check` shows the Secure Boot state per node.
- **The process is disruptive.** Each node reboots into the ISO and is then fully
  power-cycled. Drain the nodes first.
- **Try it on one canary node first.** The ISO and the tool have been tested in QEMU (arm64
  UEFI) and against Dell's published XE9712 Redfish mockup, **not yet on real XE9712/BF3
  hardware**.
- **Two GRUB menu entries help with manual checks** on the canary, from the iDRAC virtual
  console:
  - **Query only** makes no changes and opens a shell.
  - **Self-test** checks the tools, then powers off.

## Notes

- The ISO contains unmodified Debian packages (Linux, GRUB, busybox, mstflint), which are under
  their own licences, mostly GPL. The exact versions are listed in
  [`dist/bf3-eth-arm64.iso.manifest`](dist/bf3-eth-arm64.iso.manifest), and the corresponding
  sources are permanently available from the Debian archive and
  [snapshot.debian.org](https://snapshot.debian.org/).
- **The prebuilt ISO in `dist/` is a snapshot** (Debian 13, kernel 6.12.107, mstflint 4.31,
  built 2026-11-13). It does not pick up later Debian updates, so run `build_iso.py` for a
  current one. It is the exact image that was boot-tested in QEMU (sha256 `d5f28ced…b734`).
- Built using Dell's [iDRAC-Redfish-Scripting](https://github.com/dell/iDRAC-Redfish-Scripting)
  XE9712 mockup and NVIDIA's BlueField-3 documentation.
