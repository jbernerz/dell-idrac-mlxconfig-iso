# GB300 debug ISO

An Ubuntu 24.04 arm64 live ISO for debugging NVIDIA GB300 NVL72 compute trays (Grace CPUs,
Blackwell Ultra GPUs, ConnectX-8, BlueField-3). It boots from iDRAC/BMC virtual media into a
root shell, with the GPU and network drivers and NVIDIA's debug tools at the versions of
**NVIDIA's GB300 NVL72 release 1.0.6**. Nothing is written to the node's disks.

`build_debug_iso.sh` builds it; the ISO itself is not in this repository (see
[Licensing](#licensing)).

## What's on it

Versions come from the GB300 NVL72 1.0.6 release notes (RN-11874-001_1.0.6, sections 4.2 and
5.7) and are pinned in [`versions.env`](versions.env). The build stops if any installed version
or repository key differs from what's pinned.

| Component | Version | From |
|---|---|---|
| OS / kernel | Ubuntu 24.04, `6.17.0-1008-nvidia-64k` (the DGX OS 7.4.0 kernel) | Ubuntu archive |
| GPU driver, `nvidia-smi` | 580.126.20, open kernel modules (built with DKMS for that kernel) | NVIDIA CUDA repo |
| IMEX, NSCQ | 580.126.20 | NVIDIA CUDA repo |
| DOCA-Host | 3.2.1-044413: DOCA-OFED kernel modules (mlx5, RDMA) and `doca-ofed-userspace` | NVIDIA DOCA repo (new 1.0.6 GPG key) |
| MFT | 4.34.1-12: `mst`, `flint`, `mlxconfig`, `mlxlink`, `mlxfwmanager`, plus the MFT kernel module | NVIDIA DOCA repo |
| DCGM | 4.5.0 (`dcgmi`, `nv-hostengine`) | NVIDIA CUDA repo |
| nvfwupd | 2.0.8 | NVIDIA CUDA repo |
| nvdebug | 2.0.0, as [open-nvdebug](https://github.com/NVIDIA/open-nvdebug) at the `initial_release_2.0.0` commit | GitHub (Apache-2.0) |

General tools are also included:

| Area | Tools |
|---|---|
| RDMA | `ibstat`, `ibv_devinfo`, `ibdev2netdev`, `perftest`, `ibdump`, `rshim` |
| Network | `ip`, `ethtool`, `lldpd`, `tcpdump`, `iperf3`, `mtr`, `curl` |
| Hardware | `lspci`, `lshw`, `dmidecode`, `ipmitool`, `rasdaemon`, `edac-utils`, `lm-sensors`, `smartctl`, `nvme`, `numactl`, `hwloc` |
| Load and tracing | `stress-ng`, `fio`, `perf` (`linux-tools`), `strace` |
| Other | Python 3.12, `sshpass`, NFS/CIFS clients |

Four extra commands are added:

| Command | What it does |
|---|---|
| `gb300-collect [--quick] [-o DIR]` | Writes one `.tar.gz` debug bundle. It covers `nvidia-smi -q`, NVLink and fabric state, `nvidia-bug-report.sh` and DCGM discovery, plus MST/`flint`/`mlxlink`/`mlxconfig` for every CX8 and BF3 port, RDMA state, `lspci -vvv`, `ethtool`, LLDP neighbours, RAS/EDAC errors, in-band IPMI SEL/FRU/SDR, `dmesg` and the journal. |
| `nvdebug ...` | Runs nvdebug 2.0.0, e.g. `nvdebug collect -i <BMC_IP> -u <BMC_USER>` for out-of-band collection. See `/opt/nvdebug/app/README.md`. |
| `debug-ssh-password` | Sets a random root password and allows SSH password login, until reboot. |
| login banner | Shows the versions above, the node's IP addresses and these commands. |

## Booting it

1. **Disable Secure Boot.** The GRUB loader is unsigned, and so are the DKMS-built driver
   modules.
2. **Attach the ISO** as virtual media (iDRAC: Virtual Media → map CD/DVD, or Redfish
   `VirtualMedia.InsertMedia`).
3. **Boot it once:** one-time boot from virtual CD, then reset.
4. **Pick a GRUB menu entry** (10 s timeout):

| Entry | Use it when |
|---|---|
| default | Normal debugging. |
| copy to RAM | The whole image is copied into memory first, so virtual media can be detached and a flaky virtual-media link can't break the session. |
| without the NVIDIA GPU driver | Loading the GPU driver hangs or crashes the boot. |
| serial console as main | You're on Serial-over-LAN. `ttyAMA0` is assumed; change it in the GRUB entry if the tray uses another port. |

**Access:**
- The console (iDRAC virtual console and serial) **logs in as root automatically**.
- **SSH accepts root keys only:** the ones built in with `SSH_AUTHORIZED_KEYS`, or a one-time
  password from `debug-ssh-password` run on the console.
- **Every Ethernet port uses DHCP** (IPv4/IPv6), and boot doesn't wait for any of them.

**Everything runs in RAM.** Copy bundles off (`scp`, NFS/CIFS mount, or a USB stick / iDRAC
removable disk) before rebooting.

## Building it

On a Debian or Ubuntu host, as root. An arm64 host (e.g. a Grace system) runs everything
natively. On x86_64 the arm64 parts run under qemu-user emulation, and the DKMS module builds
then take much longer.

```bash
sudo apt install mmdebstrap squashfs-tools xorriso grub-common mtools dosfstools gpg curl git \
                 qemu-user-static          # x86_64 hosts only; Debian hosts also need ubuntu-keyring
sudo ./build_debug_iso.sh                  # -> gb300-debug-arm64.iso (+ .sha256, .manifest)
```

| Variable | Default | |
|---|---|---|
| `WORK` | `./build` | Work directory (needs about 20 GB). |
| `OUT` | `./gb300-debug-arm64.iso` | Output ISO. |
| `WITH_DCGM_DIAG` | `0` | `1` adds `datacenter-gpu-manager-4-cuda13` for `dcgmi diag`. That's about 1.8 GB unpacked, so the ISO gets much larger. |
| `SSH_AUTHORIZED_KEYS` | none | Public keys allowed to SSH in as root. |
| `BUILD_CA_CERT` | none | Extra CA trusted during the build only (for a TLS-inspecting proxy). |

**Stages.** The build runs in stages: `keys rootfs drivers nvdebug customize image`. Pass stage
names to run or re-run only those, e.g. `sudo ./build_debug_iso.sh image`.

**Updating to a newer baseline.** Edit `versions.env`: kernel, driver, DOCA, DCGM, nvfwupd and
nvdebug versions, plus the repository key fingerprints.

## Licensing

The builder is in this repository; the ISO is not. The ISO contains NVIDIA software under
NVIDIA's own licence terms: the GPU driver libraries and GSP firmware, DOCA, MFT and nvfwupd.
It also contains Ubuntu packages under their usual licences.

Keep built ISOs for internal use, on your own HTTP server for virtual media, rather than
publishing them. They're also well over GitHub's 100 MB file limit.

## Status

**Work in progress:** the first full build and the QEMU boot test are still running. The
package versions, repository keys and package names have been checked against the live
NVIDIA and Ubuntu repositories. It has not been run on GB300 hardware.
