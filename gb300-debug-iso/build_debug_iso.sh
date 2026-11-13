#!/usr/bin/env bash
# build_debug_iso.sh - build gb300-debug-arm64.iso, an Ubuntu 24.04 arm64 live ISO for debugging
# NVIDIA GB300 NVL72 compute trays (Grace, Blackwell Ultra GPUs, ConnectX-8, BlueField-3).
# It carries the host software stack of NVIDIA's GB300 NVL72 release 1.0.6 (see versions.env):
# kernel 6.17.0-1008-nvidia-64k, GPU driver/IMEX 580.126.20 (open modules), DOCA-Host
# 3.2.1-044413 with its kernel modules and MFT, DCGM 4.5.0, nvfwupd 2.0.8, nvdebug 2.0.0.
#
# Run as root on a Debian/Ubuntu host. An arm64 host runs everything natively; an x86_64 host
# uses qemu-user binfmt emulation, where the DKMS kernel-module builds take a long time.
#
# Usage: sudo ./build_debug_iso.sh [STAGE...]
#   STAGE, in order (default: all of them): keys rootfs drivers nvdebug customize image
#   Each stage works on $WORK/rootfs, so a failed stage can be re-run on its own.
#
# Environment:
#   WORK=./build                  work directory (needs about 20 GB)
#   OUT=./gb300-debug-arm64.iso   output ISO
#   WITH_DCGM_DIAG=0              1 = also install datacenter-gpu-manager-4-cuda13 for `dcgmi diag`
#                                 (about 1.8 GB unpacked; makes the ISO much larger)
#   SSH_AUTHORIZED_KEYS=          file of public keys allowed to SSH in as root
#   BUILD_CA_CERT=                extra CA certificate trusted only during the build (e.g. a
#                                 TLS-inspecting proxy); removed before the image is made
#
# Host packages: mmdebstrap squashfs-tools xorriso grub-common mtools dosfstools gpg curl git
#   plus ubuntu-keyring on Debian hosts and qemu-user-static on x86_64 hosts.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=versions.env
. "$HERE/versions.env"
WORK=$(realpath -m "${WORK:-build}")
OUT=$(realpath -m "${OUT:-gb300-debug-arm64.iso}")
ROOT=$WORK/rootfs
KEYS=$WORK/keys
KVER=$KERNEL_VERSION
WITH_DCGM_DIAG=${WITH_DCGM_DIAG:-0}
BUILD_CA_CERT=${BUILD_CA_CERT:-}
SSH_AUTHORIZED_KEYS=${SSH_AUTHORIZED_KEYS:-}

BASE_PKGS=(
    # boot, live system (apt is Priority: important in Ubuntu, so minbase leaves it out)
    apt gpgv systemd systemd-sysv systemd-resolved systemd-timesyncd udev dbus kmod casper initramfs-tools zstd
    "linux-image-$KVER" "linux-modules-$KVER" "linux-headers-$KVER" "linux-tools-$KVER" grub-efi-arm64-bin
    # access and networking
    sudo openssh-server openssh-client ca-certificates tzdata iproute2 iputils-ping netbase ethtool
    lldpd tcpdump bind9-dnsutils netcat-openbsd socat curl wget rsync traceroute mtr-tiny iperf3
    nfs-common cifs-utils
    # hardware and system debugging
    pciutils usbutils lshw dmidecode ipmitool lm-sensors smartmontools nvme-cli rasdaemon edac-utils
    numactl hwloc-nox stress-ng fio sysstat lsof strace kexec-tools efibootmgr
    gdisk parted dosfstools exfatprogs
    # shell and utilities
    bash-completion less vim-tiny nano tmux screen jq file pv xz-utils unzip zip git
    # nvdebug runtime
    python3 python3-venv python3-pip sshpass
)

log() { printf '\n[%s] === %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- chroot helpers

MOUNTED=0
mount_chroot() {
    [ "$MOUNTED" = 1 ] && return
    [ -d "$ROOT/etc" ] || die "$ROOT does not exist; run the rootfs stage first"
    mount -t proc proc "$ROOT/proc"
    mount -t sysfs sysfs "$ROOT/sys"
    mount --bind /dev "$ROOT/dev"
    mount --bind /dev/pts "$ROOT/dev/pts"
    mount -t tmpfs tmpfs "$ROOT/run"
    rm -f "$ROOT/etc/resolv.conf"
    cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
    MOUNTED=1
    trap umount_chroot EXIT
}

umount_chroot() {
    local m
    native_cc_off
    [ -d "$ROOT/usr/local/bin" ] && fake_uname_off
    for m in run dev/pts dev sys proc; do
        if mountpoint -q "$ROOT/$m"; then umount -l "$ROOT/$m"; fi
    done
    MOUNTED=0
}

# On x86_64 build hosts the chroot's compiler runs under qemu emulation, which makes the DKMS
# builds take hours. If the host has the same GCC and binutils build as the chroot (Ubuntu's
# gcc-13-aarch64-linux-gnu / binutils-aarch64-linux-gnu: the same compiler, built to run on
# x86_64), bind-mount its cc1, as and ld over the chroot's copies for the drivers stage. The
# output is the same arm64 code. NATIVE_CC=0 turns this off.
NATIVE_CC=${NATIVE_CC:-auto}
NATIVE_CC_MOUNTS=()
native_cc_on() {
    [ "$(uname -m)" = x86_64 ] && [ "$NATIVE_CC" != 0 ] || return 0
    [ ${#NATIVE_CC_MOUNTS[@]} -eq 0 ] || return 0                              # already on
    in_chroot sh -c 'command -v gcc-13 && command -v as' > /dev/null || return 0  # no compiler yet
    local cc1=/usr/libexec/gcc-cross/aarch64-linux-gnu/13/cc1 host_cc chroot_cc host_as chroot_as
    if [ ! -x "$cc1" ] || [ ! -x /usr/bin/aarch64-linux-gnu-as ] || [ ! -x /usr/bin/aarch64-linux-gnu-ld.bfd ]; then
        echo "  compiling under emulation (apt install gcc-13-aarch64-linux-gnu binutils-aarch64-linux-gnu to compile natively)"
        return 0
    fi
    host_cc=$(aarch64-linux-gnu-gcc-13 --version | head -1 | sed 's/^[^(]*//')
    chroot_cc=$(in_chroot gcc-13 --version | head -1 | sed 's/^[^(]*//')
    host_as=$(aarch64-linux-gnu-as --version | head -1)
    chroot_as=$(in_chroot as --version | head -1)
    if [ "$host_cc" != "$chroot_cc" ] || [ "$host_as" != "$chroot_as" ]; then
        echo "  compiling under emulation: host cross toolchain ($host_cc, $host_as) differs from the chroot's ($chroot_cc, $chroot_as)"
        return 0
    fi
    if [ -e "$ROOT/lib64" ] || [ -e "$ROOT/usr/lib/x86_64-linux-gnu" ]; then
        echo "  compiling under emulation: $ROOT/lib64 or usr/lib/x86_64-linux-gnu already exists"
        return 0
    fi
    mkdir "$ROOT/lib64" "$ROOT/usr/lib/x86_64-linux-gnu"
    mount --bind /usr/lib64 "$ROOT/lib64"
    mount --bind /usr/lib/x86_64-linux-gnu "$ROOT/usr/lib/x86_64-linux-gnu"
    mount --bind "$cc1" "$ROOT/usr/libexec/gcc/aarch64-linux-gnu/13/cc1"
    mount --bind /usr/bin/aarch64-linux-gnu-as "$ROOT/usr/bin/aarch64-linux-gnu-as"
    mount --bind /usr/bin/aarch64-linux-gnu-ld.bfd "$ROOT/usr/bin/aarch64-linux-gnu-ld.bfd"
    NATIVE_CC_MOUNTS=(usr/bin/aarch64-linux-gnu-ld.bfd usr/bin/aarch64-linux-gnu-as
        usr/libexec/gcc/aarch64-linux-gnu/13/cc1 usr/lib/x86_64-linux-gnu lib64)
    echo "  compiling natively with the host's $host_cc cross compiler"
}

native_cc_off() {
    local m
    [ ${#NATIVE_CC_MOUNTS[@]} -gt 0 ] || return 0
    for m in "${NATIVE_CC_MOUNTS[@]}"; do
        if mountpoint -q "$ROOT/$m"; then umount -l "$ROOT/$m"; fi
    done
    rmdir "$ROOT/usr/lib/x86_64-linux-gnu" "$ROOT/lib64"
    NATIVE_CC_MOUNTS=()
}

# DKMS and the DOCA configure script ask `uname -r` which kernel they build for; in a chroot that
# is the build host's kernel. For the drivers stage, make the chroot's uname report the ISO kernel.
fake_uname_on() {
    cat > "$ROOT/usr/local/bin/uname" <<EOF
#!/bin/sh
# Build-time only (build_debug_iso.sh): report the ISO kernel $KVER instead of the build host's.
/usr/bin/uname "\$@" | sed "s/\$(/usr/bin/uname -r)/$KVER/g"
EOF
    chmod 755 "$ROOT/usr/local/bin/uname"
    [ "$(in_chroot uname -r)" = "$KVER" ] || die "uname wrapper does not report $KVER"
}

fake_uname_off() { rm -f "$ROOT/usr/local/bin/uname"; }

in_chroot() {
    env -u PIP_CONFIG_FILE -u NODE_EXTRA_CA_CERTS \
        LC_ALL=C.UTF-8 LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive \
        SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
        REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
        PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        chroot "$ROOT" "$@"
}

apt_in() { in_chroot apt-get -y -o Dpkg::Options::=--force-confold -o APT::Install-Recommends=false "$@"; }

install_build_ca() {
    [ -n "$BUILD_CA_CERT" ] || return 0
    install -D -m644 "$BUILD_CA_CERT" "$ROOT/usr/local/share/ca-certificates/zz-build-only.crt"
    in_chroot update-ca-certificates > /dev/null
}

remove_build_ca() {
    rm -f "$ROOT/usr/local/share/ca-certificates/zz-build-only.crt"
    in_chroot update-ca-certificates --fresh > /dev/null
}

pkg_version() { in_chroot dpkg-query -W -f='${Version}' "$1" 2>/dev/null || true; }

expect_version() { # package expected-version-prefix
    local v; v=$(pkg_version "$1")
    case "$v" in "$2"*) printf '  %-34s %s\n' "$1" "$v" ;; *) die "$1 is '$v', expected $2" ;; esac
}

# ---------------------------------------------------------------- stages

key_fpr() { gpg --show-keys --with-colons "$1" 2>/dev/null | awk -F: '/^fpr/ {print $10}' | sort -u; }

stage_keys() {
    log "keys: NVIDIA CUDA and DOCA repository keys"
    local tmp; tmp=$(mktemp -d)
    mkdir -p "$KEYS"
    curl -fsSL -o "$tmp/cuda-keyring.deb" "$CUDA_REPO/$CUDA_KEYRING_DEB"
    dpkg-deb -x "$tmp/cuda-keyring.deb" "$tmp/cuda"
    cp "$tmp/cuda/usr/share/keyrings/cuda-archive-keyring.gpg" "$KEYS/cuda-archive-keyring.gpg"
    [ "$(key_fpr "$KEYS/cuda-archive-keyring.gpg")" = "$CUDA_KEY_FPR" ] || die "CUDA key fingerprint is not $CUDA_KEY_FPR"
    curl -fsSL -o "$tmp/doca.asc" "$DOCA_KEY_URL"
    [ "$(key_fpr "$tmp/doca.asc")" = "$DOCA_KEY_FPR" ] || die "DOCA key fingerprint is not $DOCA_KEY_FPR"
    gpg --dearmor < "$tmp/doca.asc" > "$KEYS/nvidia-doca.gpg"
    rm -rf "$tmp"
    echo "  CUDA repository key $CUDA_KEY_FPR"
    echo "  DOCA repository key $DOCA_KEY_FPR"
}

stage_rootfs() {
    log "rootfs: Ubuntu $UBUNTU_SUITE arm64 with kernel $KVER"
    local keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg comps="main restricted universe multiverse"
    [ -f "$keyring" ] || die "$keyring missing (apt install ubuntu-keyring)"
    if [ "$(uname -m)" != aarch64 ] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
        die "this host cannot run arm64 binaries: install qemu-user-static and enable its binfmt handler"
    fi
    umount_chroot
    rm -rf "$ROOT"
    mkdir -p "$WORK"
    mmdebstrap --arch=arm64 --variant=minbase --mode=root --format=directory --keyring="$keyring" \
        --aptopt='APT::Install-Recommends "false"' \
        --dpkgopt='path-exclude=/usr/share/doc/*' --dpkgopt='path-include=/usr/share/doc/*/copyright' \
        --dpkgopt='path-exclude=/usr/share/man/*' --dpkgopt='path-exclude=/usr/share/info/*' \
        --dpkgopt='path-exclude=/usr/share/locale/*' --dpkgopt='path-include=/usr/share/locale/locale.alias' \
        --include="$(IFS=,; echo "${BASE_PKGS[*]}")" \
        "$UBUNTU_SUITE" "$ROOT" \
        "deb $UBUNTU_MIRROR $UBUNTU_SUITE $comps" \
        "deb $UBUNTU_MIRROR $UBUNTU_SUITE-updates $comps" \
        "deb $UBUNTU_MIRROR $UBUNTU_SUITE-security $comps"
    [ -e "$ROOT/boot/vmlinuz-$KVER" ] || die "kernel $KVER was not installed"
}

stage_drivers() {
    log "drivers: DOCA-Host $DOCA_VERSION, NVIDIA $NVIDIA_VERSION (DKMS for $KVER), DCGM, nvfwupd"
    [ -f "$KEYS/nvidia-doca.gpg" ] || stage_keys
    mount_chroot
    install_build_ca
    printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
    chmod 755 "$ROOT/usr/sbin/policy-rc.d"
    install -d "$ROOT/etc/apt/keyrings"
    install -m644 "$KEYS/cuda-archive-keyring.gpg" "$KEYS/nvidia-doca.gpg" "$ROOT/etc/apt/keyrings/"
    echo "deb [signed-by=/etc/apt/keyrings/cuda-archive-keyring.gpg] $CUDA_REPO/ /" \
        > "$ROOT/etc/apt/sources.list.d/nvidia-cuda.list"
    echo "deb [signed-by=/etc/apt/keyrings/nvidia-doca.gpg] $DOCA_REPO/ ./" \
        > "$ROOT/etc/apt/sources.list.d/nvidia-doca.list"
    cat > "$ROOT/etc/apt/preferences.d/gb300-baseline" <<EOF
# Pin the NVIDIA GB300 NVL72 1.0.6 host software versions (see versions.env in the build tree).
Package: nvidia-* libnvidia-* libxnvctrl*
Pin: version $NVIDIA_VERSION-*
Pin-Priority: 1001

Package: datacenter-gpu-manager-4-*
Pin: version $DCGM_VERSION
Pin-Priority: 1001

Package: nvfwupd
Pin: version $NVFWUPD_VERSION
Pin-Priority: 1001

# MFT must come from the DOCA repo (it matches kernel-mft-dkms); the CUDA repo carries newer ones.
Package: mft mft-* kernel-mft-dkms
Pin: origin "linux.mellanox.com"
Pin-Priority: 1001
EOF
    apt_in update

    # Point uname at the ISO kernel and (x86_64 hosts) swap in the native cross compiler before
    # anything is compiled. On a fresh chroot gcc only arrives with dkms, hence native_cc_on twice.
    fake_uname_on
    native_cc_on
    in_chroot dpkg --configure -a   # finish packages left half-configured by an earlier failed run
    apt_in install dkms
    native_cc_on

    # DOCA first: the DOCA-OFED mlx5/RDMA modules (nvidia-peermem builds against them) and the
    # MFT access driver. doca-ofed-userspace is the userspace half of the doca-ofed profile.
    apt_in install --allow-downgrades mlnx-ofed-kernel-dkms kernel-mft-dkms mft doca-ofed-userspace
    local nv=(
        "nvidia-headless-$NVIDIA_BRANCH-open" "nvidia-utils-$NVIDIA_BRANCH" nvidia-imex libnvidia-nscq
        nvidia-persistenced nvidia-modprobe datacenter-gpu-manager-4-core nvfwupd
    )
    [ "$WITH_DCGM_DIAG" = 1 ] && nv+=(datacenter-gpu-manager-4-cuda13)
    apt_in install "${nv[@]}"

    # Package scripts build for whichever kernels they find; make sure everything is built for KVER.
    in_chroot dkms autoinstall -k "$KVER"
    log "drivers: checking versions"
    in_chroot dkms status -k "$KVER"
    local m
    for m in "nvidia/$NVIDIA_VERSION" mlnx-ofed-kernel kernel-mft-dkms; do
        in_chroot dkms status -k "$KVER" | grep "^$m" | grep -q ': installed' \
            || die "DKMS module $m is not installed for $KVER"
    done
    [ "$(in_chroot modinfo -k "$KVER" -F version nvidia)" = "$NVIDIA_VERSION" ] || die "nvidia.ko is not $NVIDIA_VERSION"
    in_chroot modinfo -k "$KVER" -F filename mlx5_core | grep -q /updates/ || die "mlx5_core is not the DOCA build"
    expect_version "nvidia-dkms-$NVIDIA_BRANCH-open" "$NVIDIA_VERSION-"
    expect_version nvidia-imex "$NVIDIA_VERSION-"
    expect_version doca-ofed-userspace "$DOCA_VERSION"
    expect_version mft 4.34.1-12
    expect_version datacenter-gpu-manager-4-core "$DCGM_VERSION"
    expect_version nvfwupd "$NVFWUPD_VERSION"
    in_chroot sh -c 'command -v nvidia-smi && command -v mst && command -v dcgmi && command -v nvfwupd' > /dev/null \
        || die "nvidia-smi, mst, dcgmi or nvfwupd missing"
    native_cc_off
    fake_uname_off
    in_chroot update-initramfs -u -k "$KVER"
}

stage_nvdebug() {
    log "nvdebug: open-nvdebug at $NVDEBUG_COMMIT"
    local src=$WORK/src/open-nvdebug
    if [ "$(git -C "$src" rev-parse HEAD 2>/dev/null)" != "$NVDEBUG_COMMIT" ]; then
        rm -rf "$src"
        mkdir -p "$src"
        git -C "$src" init -q
        git -C "$src" fetch -q --depth 1 "$NVDEBUG_REPO" "$NVDEBUG_COMMIT"
        git -C "$src" -c advice.detachedHead=false checkout -q FETCH_HEAD
    fi
    [ "$(git -C "$src" rev-parse HEAD)" = "$NVDEBUG_COMMIT" ] || die "open-nvdebug is not at $NVDEBUG_COMMIT"
    mount_chroot
    install_build_ca
    rm -rf "$ROOT/opt/nvdebug"
    mkdir -p "$ROOT/opt/nvdebug/app"
    git -C "$src" archive HEAD | tar -x -C "$ROOT/opt/nvdebug/app"
    in_chroot python3 -m venv /opt/nvdebug/venv
    in_chroot /opt/nvdebug/venv/bin/pip install -q --no-cache-dir --disable-pip-version-check \
        -r /opt/nvdebug/app/requirements.txt
    in_chroot /opt/nvdebug/venv/bin/pip freeze --disable-pip-version-check > "$ROOT/opt/nvdebug/requirements.lock"
    cat > "$ROOT/usr/local/bin/nvdebug" <<'EOF'
#!/bin/sh
# nvdebug 2.0.0 (open-nvdebug, /opt/nvdebug/app). Documentation: /opt/nvdebug/app/README.md
PYTHONPATH=/opt/nvdebug/app${PYTHONPATH:+:$PYTHONPATH} exec /opt/nvdebug/venv/bin/python -m src.tool.main "$@"
EOF
    chmod 755 "$ROOT/usr/local/bin/nvdebug"
    in_chroot nvdebug --version
}

stage_customize() {
    log "customize: services, access, banner, cleanup"
    mount_chroot
    cp -a "$HERE/overlay/." "$ROOT/"
    chmod 755 "$ROOT"/usr/local/sbin/* "$ROOT/etc/update-motd.d/10-gb300-debug"
    find "$ROOT/etc/update-motd.d" -type f ! -name 10-gb300-debug -delete
    echo gb300-debug > "$ROOT/etc/hostname"
    printf '127.0.0.1 localhost\n127.0.1.1 gb300-debug\n::1 localhost ip6-localhost ip6-loopback\n' > "$ROOT/etc/hosts"
    if [ -n "$SSH_AUTHORIZED_KEYS" ]; then
        install -d -m700 "$ROOT/root/.ssh"
        install -m600 "$SSH_AUTHORIZED_KEYS" "$ROOT/root/.ssh/authorized_keys"
    fi
    in_chroot systemctl enable systemd-networkd.service systemd-resolved.service
    in_chroot systemctl disable systemd-networkd-wait-online.service 2> /dev/null || true
    in_chroot systemctl mask apt-daily.timer apt-daily-upgrade.timer motd-news.timer > /dev/null 2>&1 || true

    local dcgm nvdbg
    dcgm=$(pkg_version datacenter-gpu-manager-4-core)
    nvdbg=$(PYTHONPATH=/opt/nvdebug/app in_chroot /opt/nvdebug/venv/bin/python -c \
        'from src.tool.version import __version__ as v; print(v)' 2> /dev/null || echo "2.0.0")
    cat > "$ROOT/etc/gb300-debug-release" <<EOF
GB300 debug live ISO - NVIDIA GB300 NVL72 release 1.0.6 host software, built $(date -u +%Y-%m-%d)
  Ubuntu 24.04, kernel $KVER
  GPU driver $(pkg_version "nvidia-dkms-$NVIDIA_BRANCH-open") (open), IMEX $(pkg_version nvidia-imex)
  DOCA-Host $(pkg_version doca-ofed-userspace), MFT $(pkg_version mft), DOCA-OFED kernel $(pkg_version mlnx-ofed-kernel-dkms)
  DCGM ${dcgm#1:}, nvfwupd $(pkg_version nvfwupd), nvdebug $nvdbg (open-nvdebug)
EOF
    cat "$ROOT/etc/gb300-debug-release"

    remove_build_ca
    fake_uname_off
    rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT"/etc/ssh/ssh_host_*
    : > "$ROOT/etc/machine-id"
    rm -f "$ROOT/var/lib/dbus/machine-id"
    rm -rf "$ROOT"/var/lib/dkms/*/*/build
    apt_in clean
    rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/var/cache/apt/*.bin "$ROOT"/root/.cache "$ROOT"/tmp/* "$ROOT"/var/tmp/*
    find "$ROOT/var/log" -type f -exec truncate -s 0 {} +
    in_chroot update-initramfs -u -k "$KVER"
    umount_chroot
    ln -sf ../run/systemd/resolve/stub-resolv.conf "$ROOT/etc/resolv.conf"
}

stage_image() {
    log "image: squashfs and ISO"
    umount_chroot
    local iso=$WORK/iso marker label cmd
    rm -rf "$iso"
    mkdir -p "$iso/casper" "$iso/.disk" "$iso/boot"
    cp "$ROOT/boot/vmlinuz-$KVER" "$iso/casper/vmlinuz"
    cp "$ROOT/boot/initrd.img-$KVER" "$iso/casper/initrd"
    dpkg-query --admindir="$ROOT/var/lib/dpkg" -W > "$iso/casper/filesystem.manifest"
    du -sx --block-size=1 "$ROOT" | cut -f1 > "$iso/casper/filesystem.size"
    mksquashfs "$ROOT" "$iso/casper/filesystem.squashfs" -noappend -comp zstd -Xcompression-level 19 \
        -b 1M -e "boot/initrd.img-$KVER" -e "boot/initrd.img-$KVER.old-dkms"
    marker="gb300-debug-$(sha256sum "$iso/casper/initrd" | cut -c1-16)"
    echo "GB300 debug live ISO $(date -u +%Y-%m-%d)" > "$iso/.disk/info"
    touch "$iso/.disk/$marker"
    cp "$ROOT/etc/gb300-debug-release" "$iso/README.txt"

    cmd="boot=casper noautologin noprompt"
    label="GB300 debug: kernel $KVER, GPU driver $NVIDIA_VERSION, DOCA $DOCA_VERSION"
    cat > "$WORK/grub.cfg" <<EOF
set timeout=10
set default=0
search --no-floppy --file --set=root /.disk/$marker

menuentry "$label" {
    linux /casper/vmlinuz $cmd console=ttyAMA0,115200 console=tty0 ---
    initrd /casper/initrd
}
menuentry "GB300 debug, copy the image to RAM first (virtual media can be detached)" {
    linux /casper/vmlinuz $cmd toram console=ttyAMA0,115200 console=tty0 ---
    initrd /casper/initrd
}
menuentry "GB300 debug, without the NVIDIA GPU driver (if it hangs the boot)" {
    linux /casper/vmlinuz $cmd modprobe.blacklist=nvidia,nvidia_uvm,nvidia_modeset,nvidia_drm,nvidia_peermem console=ttyAMA0,115200 console=tty0 ---
    initrd /casper/initrd
}
menuentry "GB300 debug, serial console (ttyAMA0) as the main console" {
    linux /casper/vmlinuz $cmd console=tty0 console=ttyAMA0,115200 ---
    initrd /casper/initrd
}
EOF
    grub-mkstandalone -O arm64-efi -d "$ROOT/usr/lib/grub/arm64-efi" -o "$WORK/BOOTAA64.EFI" \
        --locales= --fonts= --themes= "boot/grub/grub.cfg=$WORK/grub.cfg"
    local esp=$iso/boot/efiboot.img
    mkfs.fat -C -n GB300-EFI "$esp" $(( $(stat -c %s "$WORK/BOOTAA64.EFI") / 1024 + 2048 )) > /dev/null
    mmd -i "$esp" ::/EFI ::/EFI/BOOT
    mcopy -i "$esp" "$WORK/BOOTAA64.EFI" ::/EFI/BOOT/BOOTAA64.EFI
    xorriso -as mkisofs -quiet -iso-level 3 -r -J -joliet-long -V GB300DEBUG -o "$OUT" \
        -e boot/efiboot.img -no-emul-boot -append_partition 2 0xef "$esp" -partition_cyl_align all "$iso"
    (cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
    cp "$iso/casper/filesystem.manifest" "$OUT.manifest"
    log "done: $OUT ($(du -h "$OUT" | cut -f1))"
    cat "$OUT.sha256"
}

STAGES=("$@")
[ ${#STAGES[@]} -gt 0 ] || STAGES=(keys rootfs drivers nvdebug customize image)
[ "$(id -u)" -eq 0 ] || die "run as root (mmdebstrap, chroot and mounts need it)"
for s in "${STAGES[@]}"; do
    case "$s" in
        keys | rootfs | drivers | nvdebug | customize | image) "stage_$s" ;;
        *) die "unknown stage '$s' (stages: keys rootfs drivers nvdebug customize image)" ;;
    esac
done
