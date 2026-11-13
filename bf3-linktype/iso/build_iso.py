#!/usr/bin/env python3
"""Build bf3-eth-arm64.iso: a minimal arm64 UEFI ISO that sets every BlueField-3 to Ethernet
and powers the host off. It is booted by bf3_linktype_idrac.py through iDRAC virtual media.

The ISO contains a standalone GRUB EFI loader, the Debian 13 (trixie) arm64 kernel and an
initramfs with busybox and mstconfig (from Debian's mstflint) plus the iso/init script. Nothing
else, and no network access is needed at boot. Every package comes unmodified from the Debian
archive and is checked against the archive's signed index (gpgv) before use.

Host requirements (Debian/Ubuntu, any CPU architecture):
  apt install gpg xz-utils dpkg binutils grub-common dosfstools mtools xorriso
Optional: qemu-user-static (to smoke-test the arm64 binaries during the build).

Secure Boot must be disabled on the nodes: the GRUB image built here is not signed.
"""
import argparse
import gzip
import hashlib
import io
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = "trixie"
ARCH = "arm64"
TRIPLET = "aarch64-linux-gnu"
TOOLS = ["mstconfig", "mstflint"]      # from the mstflint package
EPOCH = 1700000000                      # fixed mtime inside the initramfs, for reproducible output


def log(msg):
    print(f"[build_iso] {msg}", flush=True)


def run(*cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def fetch(url, dest=None):
    log(f"GET {url}")
    with urllib.request.urlopen(url, timeout=300) as r:
        data = r.read()
    if dest:
        with open(dest, "wb") as f:
            f.write(data)
    return data


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- Debian archive, verified

def gpgv_verified_text(inrelease_path, keyring):
    """Return the signed text of an InRelease file, or None if no valid signature from `keyring`."""
    with tempfile.NamedTemporaryFile() as out:
        p = subprocess.run(["gpgv", "--status-fd", "1", "--keyring", keyring, "--output", out.name,
                            inrelease_path], capture_output=True, text=True)
        status = p.stdout
        if "[GNUPG:] VALIDSIG" not in status or "[GNUPG:] BADSIG" in status:
            return None
        return open(out.name, encoding="utf-8").read()


def release_hash(release_text, path):
    in_sha = False
    for line in release_text.splitlines():
        if line.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha and line.startswith(" "):
            h, _, p = line.split()
            if p == path:
                return h
        elif in_sha:
            in_sha = False
    raise SystemExit(f"{path} not listed in Release")


def parse_packages(text):
    pkgs = {}
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if line[:1] in (" ", "\t") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            fields[k] = v.strip()
        if "Package" in fields:
            pkgs.setdefault(fields["Package"], fields)
    return pkgs


class Archive:
    def __init__(self, mirror, workdir, keyring):
        self.mirror = mirror.rstrip("/")
        self.cache = os.path.join(workdir, "debs")
        os.makedirs(self.cache, exist_ok=True)
        self.release = self._verified_release(SUITE, keyring, workdir)
        self.pkgs = self._packages(self.release, f"main/binary-{ARCH}")

    def _inrelease(self, suite, workdir):
        path = os.path.join(workdir, f"{suite}.InRelease")
        fetch(f"{self.mirror}/dists/{suite}/InRelease", path)
        return path

    def _packages(self, release, component):
        return parse_packages(self._index(release, SUITE, component))

    def _verified_release(self, suite, keyring, workdir):
        text = gpgv_verified_text(self._inrelease(suite, workdir), keyring)
        if text:
            log(f"{suite} index signature verified with {keyring}")
            return text
        # The host keyring may predate Debian 13 (e.g. Ubuntu 24.04). Bootstrap the current
        # Debian keyring from bookworm, whose signature the host keyring can verify.
        log(f"{keyring} cannot verify {suite}; fetching the Debian keyring via bookworm")
        old = gpgv_verified_text(self._inrelease("bookworm", workdir), keyring)
        if not old:
            raise SystemExit(f"cannot verify the Debian archive with {keyring}; pass --keyring")
        allp = parse_packages(self._index(old, "bookworm", "main/binary-all"))
        deb = self._deb(allp["debian-archive-keyring"])
        kdir = os.path.join(workdir, "keyring")
        run("dpkg-deb", "-x", deb, kdir)
        newkr = os.path.join(kdir, "usr/share/keyrings/debian-archive-keyring.gpg")
        text = gpgv_verified_text(os.path.join(workdir, f"{suite}.InRelease"), newkr)
        if not text:
            raise SystemExit(f"cannot verify {suite} even with the bookworm-provided keyring")
        log(f"{suite} index signature verified with the Debian keyring from bookworm")
        return text

    def _index(self, release_text, suite, component):
        rel = f"{component}/Packages.xz"
        data = fetch(f"{self.mirror}/dists/{suite}/{rel}")
        if sha256(data) != release_hash(release_text, rel):
            raise SystemExit(f"checksum mismatch for {suite}/{rel}")
        return lzma.decompress(data).decode("utf-8")

    def _deb(self, p):
        dest = os.path.join(self.cache, os.path.basename(p["Filename"]))
        if not (os.path.exists(dest) and sha256(open(dest, "rb").read()) == p["SHA256"]):
            data = fetch(f"{self.mirror}/{p['Filename']}")
            if sha256(data) != p["SHA256"]:
                raise SystemExit(f"checksum mismatch for {p['Filename']}")
            with open(dest, "wb") as f:
                f.write(data)
        return dest

    def deb(self, name):
        return self._deb(self.pkgs[name])

    def closure(self, roots, follow):
        """Package names reachable from `roots` through Depends, following only names `follow` accepts."""
        out, todo = [], list(roots)
        while todo:
            name = todo.pop()
            if name in out:
                continue
            if name not in self.pkgs:
                raise SystemExit(f"package {name} not found in {SUITE}/{ARCH}")
            out.append(name)
            p = self.pkgs[name]
            for field in ("Pre-Depends", "Depends"):
                for alt in filter(None, (s.strip() for s in p.get(field, "").split(","))):
                    choices = [re.split(r"[\s(:]", a.strip(), maxsplit=1)[0] for a in alt.split("|")]
                    pick = next((c for c in choices if c in self.pkgs), None)
                    if pick and follow(pick) and pick not in out:
                        todo.append(pick)
        return out


# ---------------------------------------------------------------- ELF helpers (cross-arch, via readelf)

def elf_needed(path):
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
    return re.findall(r"\(NEEDED\).*\[(.+?)\]", out)


def elf_interp(path):
    out = subprocess.run(["readelf", "-l", path], capture_output=True, text=True).stdout
    m = re.search(r"Requesting program interpreter: (\S+?)\]", out)
    return m.group(1) if m else None


def is_elf(path):
    with open(path, "rb") as f:
        return f.read(4) == b"\x7fELF"


def resolve_in(sysroot, path):
    """Follow symlinks of an absolute in-image path inside `sysroot` (merged-/usr aware)."""
    candidates = [path] + ([("/usr" + path)] if not path.startswith("/usr/") else [])
    for cand in candidates:
        p = os.path.join(sysroot, cand.lstrip("/"))
        for _ in range(40):
            if not os.path.islink(p):
                break
            t = os.readlink(p)
            p = os.path.join(sysroot, t.lstrip("/")) if t.startswith("/") else os.path.join(os.path.dirname(p), t)
        if os.path.isfile(p):
            return p
    return None


def find_lib(sysroot, soname):
    for d in (f"/usr/lib/{TRIPLET}", f"/lib/{TRIPLET}", "/usr/lib", "/lib"):
        hit = resolve_in(sysroot, f"{d}/{soname}")
        if hit:
            return hit
    return None


# ---------------------------------------------------------------- initramfs (newc cpio, written directly)

class Cpio:
    def __init__(self):
        self.buf = io.BytesIO()
        self.ino = 1

    def _entry(self, name, mode, data=b"", rdev=(0, 0)):
        name_b = name.encode() + b"\0"
        hdr = "070701" + "".join(f"{v:08x}" for v in (
            self.ino, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, EPOCH, len(data),
            0, 0, rdev[0], rdev[1], len(name_b), 0))
        self.ino += 1
        self.buf.write(hdr.encode() + name_b)
        self.buf.write(b"\0" * (-(110 + len(name_b)) % 4))
        self.buf.write(data)
        self.buf.write(b"\0" * (-len(data) % 4))

    def add_tree(self, root):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            rel = os.path.relpath(dirpath, root)
            if rel != ".":
                self._entry(rel, stat.S_IFDIR | 0o755)
            for fn in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
                full = os.path.join(dirpath, fn)
                name = os.path.normpath(os.path.join(rel, fn))
                if os.path.islink(full):
                    self._entry(name, stat.S_IFLNK | 0o777, os.readlink(full).encode())
                else:
                    perm = 0o755 if os.stat(full).st_mode & 0o111 else 0o644
                    self._entry(name, stat.S_IFREG | perm, open(full, "rb").read())

    def add_chardev(self, name, major, minor, perm=0o600):
        self._entry(name, stat.S_IFCHR | perm, rdev=(major, minor))

    def gz(self):
        self._entry("TRAILER!!!", 0)
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9, mtime=0) as g:
            g.write(self.buf.getvalue())
        return out.getvalue()


def build_initramfs(sysroot, root, build_info):
    for d in ("bin", "sbin", "dev", "proc", "sys", "tmp", "run", "etc", "usr/bin", f"usr/lib/{TRIPLET}"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    os.symlink("usr/lib", os.path.join(root, "lib"))

    busybox = resolve_in(sysroot, "/bin/busybox")
    if not busybox or not is_elf(busybox) or elf_interp(busybox):
        raise SystemExit("busybox-static did not provide a static /bin/busybox")
    shutil.copy2(busybox, os.path.join(root, "bin/busybox"))
    shutil.copy2(os.path.join(HERE, "init"), os.path.join(root, "init"))
    os.chmod(os.path.join(root, "init"), 0o755)
    with open(os.path.join(root, "etc/bf3-eth-build"), "w") as f:
        f.write(build_info + "\n")

    elves = []
    for tool in TOOLS:
        src = resolve_in(sysroot, f"/usr/bin/{tool}")
        if not src or not is_elf(src):
            raise SystemExit(f"/usr/bin/{tool} from mstflint is not an ELF binary")
        dst = os.path.join(root, "usr/bin", tool)
        shutil.copy2(src, dst)
        elves.append(dst)
    # mstconfig's parameter databases and any other mstflint data
    for d in ("usr/share/mstflint", "etc/mstflint"):
        if os.path.isdir(os.path.join(sysroot, d)):
            shutil.copytree(os.path.join(sysroot, d), os.path.join(root, d), symlinks=True)

    interp = elf_interp(elves[0])
    src = resolve_in(sysroot, interp)
    if not src:
        raise SystemExit(f"dynamic loader {interp} not found")
    dst = os.path.join(root, ("usr" + interp if interp.startswith("/lib/") else interp).lstrip("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)

    done, todo = set(), [n for e in elves for n in elf_needed(e)]
    while todo:
        soname = todo.pop()
        if soname in done:
            continue
        done.add(soname)
        src = find_lib(sysroot, soname)
        if not src:
            raise SystemExit(f"shared library {soname} not found in the extracted packages")
        dst = os.path.join(root, f"usr/lib/{TRIPLET}", soname)
        shutil.copy2(src, dst)
        todo += elf_needed(dst)
    log(f"initramfs: busybox, {', '.join(TOOLS)}, {len(done)} shared libraries")

    c = Cpio()
    c.add_tree(root)
    c.add_chardev("dev/console", 5, 1)
    return c.gz()


# ---------------------------------------------------------------- GRUB + ISO

GRUB_CFG = """\
set timeout={timeout}
set default=0
search --no-floppy --file --set=root /.disk/{marker}
echo "BF3 link-type ISO ({info})"

menuentry "BlueField-3: set link type to {name}, then power off" {{
    linux /bf3/vmlinuz {console} bf3.mode=apply bf3.link_type={lt} {extra}
    initrd /bf3/initrd.gz
}}
menuentry "BlueField-3: show current link type only (no changes), then shell" {{
    linux /bf3/vmlinuz {console} bf3.mode=query {extra}
    initrd /bf3/initrd.gz
}}
menuentry "Self-test: check the tools run and list NVIDIA devices, then power off" {{
    linux /bf3/vmlinuz {console} bf3.mode=selftest {extra}
    initrd /bf3/initrd.gz
}}
"""


def build_grub(sysroot, workdir, cfg_text):
    moddir = os.path.join(sysroot, "usr/lib/grub/arm64-efi")
    if not os.path.isfile(os.path.join(moddir, "moddep.lst")):
        raise SystemExit("GRUB arm64-efi modules not found in the extracted packages")
    cfg = os.path.join(workdir, "grub-embedded.cfg")
    with open(cfg, "w") as f:
        f.write(cfg_text)
    efi = os.path.join(workdir, "BOOTAA64.EFI")
    run("grub-mkstandalone", "-O", "arm64-efi", "-d", moddir, "-o", efi,
        "--locales=", "--fonts=", "--themes=", f"boot/grub/grub.cfg={cfg}")
    return efi


def build_iso(efi, kernel, initrd, marker, out, workdir):
    tree = os.path.join(workdir, "iso-root")
    shutil.rmtree(tree, ignore_errors=True)
    os.makedirs(os.path.join(tree, "bf3"))
    os.makedirs(os.path.join(tree, ".disk"))
    os.makedirs(os.path.join(tree, "boot"))
    shutil.copy2(kernel, os.path.join(tree, "bf3/vmlinuz"))
    with open(os.path.join(tree, "bf3/initrd.gz"), "wb") as f:
        f.write(initrd)
    with open(os.path.join(tree, ".disk", marker), "w") as f:
        f.write("BlueField-3 link-type ISO\n")

    esp = os.path.join(tree, "boot/efiboot.img")
    kib = (os.path.getsize(efi) // 1024) + 2048
    run("mkfs.fat", "-C", "-n", "BF3ETH-EFI", esp, str(kib), stdout=subprocess.DEVNULL)
    run("mmd", "-i", esp, "::/EFI", "::/EFI/BOOT")
    run("mcopy", "-i", esp, efi, "::/EFI/BOOT/BOOTAA64.EFI")

    run("xorriso", "-as", "mkisofs", "-quiet", "-r", "-J", "-joliet-long", "-V", "BF3ETH",
        "-o", out, "-e", "boot/efiboot.img", "-no-emul-boot",
        "-append_partition", "2", "0xef", esp, "-partition_cyl_align", "all", tree)


def smoke_test(root):
    qemu = shutil.which("qemu-aarch64-static") or shutil.which("qemu-aarch64")
    if not qemu:
        log("qemu-aarch64 not installed - skipping the arm64 smoke test")
        return
    env = dict(os.environ, LD_LIBRARY_PATH=f"/usr/lib/{TRIPLET}")
    for cmd in (["/usr/bin/mstconfig", "-v"], ["/usr/bin/mstflint", "-v"]):
        p = subprocess.run([qemu, "-L", root] + [os.path.join(root, cmd[0].lstrip("/"))] + cmd[1:],
                           capture_output=True, text=True, env=env)
        if p.returncode != 0:
            raise SystemExit(f"smoke test failed: {' '.join(cmd)}\n{p.stdout}{p.stderr}")
        log(f"smoke test: {' '.join(cmd)} -> {p.stdout.strip().splitlines()[0]}")
    p = subprocess.run([qemu, os.path.join(root, "bin/busybox"), "--list"], capture_output=True, text=True)
    missing = {"sh", "mount", "setsid", "cttyhack", "poweroff", "grep", "sleep", "dmesg"} - set(p.stdout.split())
    if missing:
        raise SystemExit(f"busybox lacks applets the init script uses: {sorted(missing)}")
    log("smoke test: busybox has every applet /init uses")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="bf3-eth-arm64.iso")
    ap.add_argument("--workdir", default="build")
    ap.add_argument("--mirror", default="https://deb.debian.org/debian")
    ap.add_argument("--keyring", default="/usr/share/keyrings/debian-archive-keyring.gpg")
    ap.add_argument("--link-type", type=int, choices=(1, 2), default=2, help="2 = Ethernet (default), 1 = InfiniBand")
    ap.add_argument("--console", default="console=ttyAMA0,115200 console=tty0",
                    help="kernel console= arguments; the last one gets the script's output")
    ap.add_argument("--extra-cmdline", default="", help="extra kernel arguments for every menu entry")
    ap.add_argument("--default-entry", type=int, default=0, help="GRUB entry booted after the timeout (0 = apply)")
    ap.add_argument("--timeout", type=int, default=5, help="GRUB menu timeout in seconds")
    a = ap.parse_args()

    for tool in ("gpgv", "dpkg-deb", "readelf", "grub-mkstandalone", "mkfs.fat", "mmd", "mcopy", "xorriso"):
        if not shutil.which(tool):
            raise SystemExit(f"missing host tool: {tool} (see --help for the apt install line)")

    work = os.path.abspath(a.workdir)
    os.makedirs(work, exist_ok=True)
    arc = Archive(a.mirror, work, a.keyring)

    kernel_pkg = re.split(r"[\s(]", arc.pkgs["linux-image-arm64"]["Depends"].split(",")[0].strip(), maxsplit=1)[0]
    sets = {
        "kernel": [kernel_pkg],
        "grub": arc.closure(["grub-efi-arm64-bin"], lambda n: n.startswith("grub-efi-arm64")),
        "busybox": ["busybox-static"],
        "mstflint": arc.closure(["mstflint"], lambda n: n.startswith("lib") or n == "zlib1g"),
    }
    sysroot = os.path.join(work, "sysroot")
    shutil.rmtree(sysroot, ignore_errors=True)
    manifest = []
    for group, names in sets.items():
        for n in sorted(set(names)):
            run("dpkg-deb", "-x", arc.deb(n), sysroot)
            manifest.append(f"{n} {arc.pkgs[n]['Version']}")
    log("packages: " + ", ".join(manifest))

    kernels = sorted(f for f in os.listdir(os.path.join(sysroot, "boot")) if f.startswith("vmlinuz-"))
    if len(kernels) != 1:
        raise SystemExit(f"expected one kernel in {kernel_pkg}, found {kernels}")
    kernel = os.path.join(sysroot, "boot", kernels[0])
    info = f"Debian {SUITE} {kernels[0][len('vmlinuz-'):]}, mstflint {arc.pkgs['mstflint']['Version']}"

    root = os.path.join(work, "initramfs")
    shutil.rmtree(root, ignore_errors=True)
    initrd = build_initramfs(sysroot, root, info)
    smoke_test(root)

    marker = "bf3-eth-" + sha256(open(kernel, "rb").read() + initrd)[:16]
    cfg = GRUB_CFG.format(timeout=a.timeout, marker=marker, info=info, console=a.console,
                          lt=a.link_type, name={1: "InfiniBand", 2: "Ethernet"}[a.link_type],
                          extra=a.extra_cmdline).replace("set default=0", f"set default={a.default_entry}")
    efi = build_grub(sysroot, work, cfg)
    out = os.path.abspath(a.output)
    build_iso(efi, kernel, initrd, marker, out, work)

    digest = sha256(open(out, "rb").read())
    with open(out + ".sha256", "w") as f:
        f.write(f"{digest}  {os.path.basename(out)}\n")
    with open(out + ".manifest", "w") as f:
        f.write(f"{info}\nmarker {marker}\n" + "\n".join(manifest) + "\n")
    log(f"done: {out} ({os.path.getsize(out) // (1024 * 1024)} MiB) sha256 {digest}")


if __name__ == "__main__":
    main()
