#!/usr/bin/env python3
"""Switch NVIDIA BlueField-3 ports from InfiniBand to Ethernet on many Dell iDRAC nodes, out of band.

On the PowerEdge XE9712 (iDRAC10 1.30.07.10, BIOS 1.1.2) iDRAC has no Redfish, BIOS or network
attribute for the BF3 link type: the setting lives in the card's own firmware (LINK_TYPE_P1 and
LINK_TYPE_P2, 1 = InfiniBand, 2 = Ethernet). This tool does everything else through iDRAC Redfish:

  check       Read-only. Per node: how the card in --slot enumerates (InfiniBand.Slot.N or
              NIC.Slot.N), port link technology, whether iDRAC exposes any network attributes
              for that slot (a newer firmware might), and the Secure Boot state.
  apply       Boot the utility ISO from image/ through iDRAC virtual media. The ISO sets the link
              type and powers the host off. Then eject, full (virtual AC) power cycle, power on.
  powercycle  Shut down, full power cycle, power on. Use this instead of apply when you set
              LINK_TYPE in-band (e.g. over SSH) and only need the power cycle out of band.
  verify      Read-only. Non-zero exit if any node's slot does not enumerate as Ethernet.

Credentials: IDRAC_USER (default root) and IDRAC_PASSWORD (prompted if unset).
"""
import argparse
import concurrent.futures as cf
import getpass
import json
import os
import re
import sys
import threading
import time

import requests

SYS = "/redfish/v1/Systems/System.Embedded.1"
CHASSIS = "/redfish/v1/Chassis/System.Embedded.1"
LINK_ATTR_RE = re.compile(r"link|vpi|proto|porttype|personality", re.I)
_print_lock = threading.Lock()


def say(node, msg):
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {node}: {msg}", flush=True)


class RedfishError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def _error_text(r):
    try:
        err = r.json().get("error", {})
    except ValueError:
        return r.text[:300]
    msgs = [m.get("Message") for m in err.get("@Message.ExtendedInfo", []) if m.get("Message")]
    return "; ".join(msgs) or err.get("message") or r.text[:300]


class Idrac:
    def __init__(self, host, user, password, verify, dry_run=False, timeout=60):
        self.name = host
        self.base = host.rstrip("/") if "://" in host else f"https://{host}"
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.verify = verify
        self.s.headers["Content-Type"] = "application/json"
        self.dry_run = dry_run
        self.timeout = timeout

    def _req(self, method, path, body=None, retries=3):
        for attempt in range(retries):
            try:
                r = self.s.request(method, self.base + path, timeout=self.timeout,
                                   data=None if body is None else json.dumps(body))
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == retries - 1:
                    raise
                time.sleep(10)
        if r.status_code >= 400:
            raise RedfishError(r.status_code, f"{method} {path} -> {r.status_code}: {_error_text(r)}")
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def get(self, path):
        return self._req("GET", path)

    def try_get(self, path):
        try:
            return self.get(path)
        except RedfishError as e:
            if e.status == 404:
                return None
            raise

    def write(self, method, path, body):
        if self.dry_run:
            say(self.name, f"DRY-RUN {method} {path} {json.dumps(body)}")
            return {}
        return self._req(method, path, body)


# ---------------------------------------------------------------- inventory (read-only)

def members(d, path):
    return [m["@odata.id"] for m in (d.try_get(path) or {}).get("Members", [])]


def slot_inventory(d, slot):
    """How the card in `slot` enumerates right now, and what link type that implies."""
    tag = f".Slot.{slot}"
    fqdds = sorted(
        u.rsplit("/", 1)[-1] for u in members(d, f"{CHASSIS}/Oem/Dell/DellPCIeFunctions")
        if tag + "-" in u and u.rsplit("/", 1)[-1].split(".")[0] in ("NIC", "InfiniBand"))
    model, ports = None, {}
    for adapter in members(d, f"{CHASSIS}/NetworkAdapters"):
        if not adapter.endswith(tag):
            continue
        model = (d.get(adapter) or {}).get("Model")
        for port in members(d, f"{adapter}/Ports"):
            p = d.get(port)
            ports[p.get("Id")] = p.get("LinkNetworkTechnology")
    if any(f.startswith("InfiniBand.") for f in fqdds) or "InfiniBand" in ports.values():
        link = "InfiniBand"
    elif ports and all(v == "Ethernet" for v in ports.values()):
        link = "Ethernet"
    else:
        link = "unknown"
    return {"fqdds": fqdds, "model": model, "ports": ports, "link": link}


def network_attribute_registries(d, slot):
    """{registry id: [attribute names]} for every per-port network attribute registry of `slot`."""
    out = {}
    for u in members(d, "/redfish/v1/Registries"):
        rid = u.rsplit("/", 1)[-1]
        if not rid.startswith("NetworkAttributesRegistry_") or f".Slot.{slot}-" not in rid:
            continue
        loc = ((d.get(u) or {}).get("Location") or [{}])[0].get("Uri")
        reg = d.try_get(loc) if loc else None
        out[rid] = [a["AttributeName"] for a in (reg or {}).get("RegistryEntries", {}).get("Attributes", [])]
    return out


# ---------------------------------------------------------------- power helpers

def reset(d, reset_type):
    say(d.name, f"ComputerSystem.Reset {reset_type}")
    d.write("POST", f"{SYS}/Actions/ComputerSystem.Reset", {"ResetType": reset_type})


def power_state(d):
    try:
        return d.get(SYS).get("PowerState")
    except (requests.ConnectionError, requests.Timeout, RedfishError):
        return None  # iDRAC busy or briefly unreachable (e.g. during the virtual AC cycle)


def wait_power(d, state, timeout, poll=15):
    end = time.time() + timeout
    while time.time() < end:
        if power_state(d) == state:
            return True
        time.sleep(poll)
    return False


def full_power_cycle(d, a, host_off=False):
    if not host_off and power_state(d) != "Off":
        reset(d, "GracefulShutdown")
        if not d.dry_run and not wait_power(d, "Off", a.shutdown_timeout):
            say(d.name, "graceful shutdown timed out, forcing off")
            reset(d, "ForceOff")
            if not wait_power(d, "Off", 180):
                raise RuntimeError("host did not power off")
    say(d.name, "Chassis.Reset FullPowerCycle (virtual AC) - this is what makes the card load the new link type")
    d.write("POST", f"{CHASSIS}/Actions/Chassis.Reset", {"ResetType": "FullPowerCycle"})
    if d.dry_run:
        return
    time.sleep(a.ac_wait)
    end = time.time() + 900
    while power_state(d) is None:  # wait for iDRAC to answer again
        if time.time() > end:
            raise RuntimeError("iDRAC did not come back after the full power cycle")
        time.sleep(15)
    if not a.leave_off and power_state(d) != "On":
        reset(d, "On")


# ---------------------------------------------------------------- commands

def cmd_check(d, a):
    sysinfo = d.get(SYS)
    bios = (d.try_get(f"{SYS}/Bios") or {}).get("Attributes", {})
    mgr = d.get("/redfish/v1/Managers/iDRAC.Embedded.1")
    inv = slot_inventory(d, a.slot)
    regs = network_attribute_registries(d, a.slot)
    n_attrs = sum(len(v) for v in regs.values())
    candidates = sorted({n for v in regs.values() for n in v if LINK_ATTR_RE.search(n)})
    return {
        "ok": True,
        "summary": (f"link={inv['link']} model={inv['model']} fqdds={','.join(inv['fqdds']) or '-'} "
                    f"ports={inv['ports'] or '-'} | iDRAC {mgr.get('FirmwareVersion')} "
                    f"BIOS {sysinfo.get('BiosVersion')} power={sysinfo.get('PowerState')} "
                    f"SecureBoot={bios.get('SecureBoot')} | net-attr registries={len(regs)} "
                    f"attrs={n_attrs}" + (f" CANDIDATES={candidates}" if candidates else "")),
    }


def cmd_apply(d, a):
    inv = slot_inventory(d, a.slot)
    say(d.name, f"slot {a.slot} currently {inv['link']} ({', '.join(inv['fqdds']) or 'no functions found'})")
    if inv["link"] == "Ethernet" and not a.force:
        return {"ok": True, "summary": "already Ethernet, skipped (use --force to run anyway)"}
    vm = f"{SYS}/VirtualMedia/{a.vm_slot}"
    if (d.get(vm) or {}).get("Inserted"):
        d.write("POST", f"{vm}/Actions/VirtualMedia.EjectMedia", {})
    say(d.name, f"inserting {a.iso_url} as virtual media {a.vm_slot}")
    d.write("POST", f"{vm}/Actions/VirtualMedia.InsertMedia",
            {"Image": a.iso_url, "Inserted": True, "WriteProtected": True})
    d.write("PATCH", SYS, {"Boot": {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}})
    if power_state(d) == "On":
        reset(d, "GracefulRestart" if a.graceful else "ForceRestart")
    else:
        reset(d, "On")
    if not d.dry_run:
        say(d.name, f"utility ISO booting; waiting up to {a.iso_timeout}s for it to power the host off")
        time.sleep(a.settle)
        if not wait_power(d, "Off", a.iso_timeout):
            return {"ok": False, "summary": "utility ISO did not power the host off in time - media left "
                                            "attached, look at the iDRAC virtual console"}
    d.write("POST", f"{vm}/Actions/VirtualMedia.EjectMedia", {})
    full_power_cycle(d, a, host_off=True)  # the ISO powered the host off
    if d.dry_run:
        return {"ok": True, "summary": "dry run, nothing changed"}
    return {"ok": True, "summary": "link type written and full power cycle done; run verify once the host has booted"}


def cmd_powercycle(d, a):
    full_power_cycle(d, a)
    return {"ok": True, "summary": "dry run, nothing changed" if d.dry_run else "full power cycle done"}


def cmd_verify(d, a):
    inv = slot_inventory(d, a.slot)
    return {"ok": inv["link"] == "Ethernet",
            "summary": f"link={inv['link']} fqdds={','.join(inv['fqdds']) or '-'} ports={inv['ports'] or '-'}"}


COMMANDS = {"check": cmd_check, "apply": cmd_apply, "powercycle": cmd_powercycle, "verify": cmd_verify}


def read_nodes(path):
    with open(path) as f:
        hosts = [ln.split("#", 1)[0].strip() for ln in f]
    return list(dict.fromkeys(h for h in hosts if h))  # drop blanks and duplicates, keep order


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("-n", "--nodes", required=True, help="file with one iDRAC host/IP per line (# comments ok)")
    p.add_argument("--slot", default="2", help="PCIe slot holding the BF3 (default 2, as on the XE9712)")
    p.add_argument("--iso-url", help="apply: HTTP/HTTPS/NFS/CIFS URL of the utility ISO, reachable from the iDRACs")
    p.add_argument("--vm-slot", default="1", help="iDRAC virtual media instance to use (default 1)")
    p.add_argument("--parallel", type=int, default=5, help="nodes worked on at once (default 5)")
    p.add_argument("--dry-run", action="store_true", help="print the Redfish writes instead of sending them")
    p.add_argument("--force", action="store_true", help="apply: run even if the slot already reports Ethernet")
    p.add_argument("--graceful", action="store_true", help="apply: GracefulRestart a running host instead of ForceRestart")
    p.add_argument("--leave-off", action="store_true", help="leave hosts powered off after the full power cycle")
    p.add_argument("--iso-timeout", type=int, default=1800, help="apply: seconds to wait for the ISO to power off")
    p.add_argument("--settle", type=int, default=90, help="apply: seconds to wait after reset before polling")
    p.add_argument("--ac-wait", type=int, default=60, help="seconds to wait after FullPowerCycle before polling")
    p.add_argument("--shutdown-timeout", type=int, default=600, help="seconds to wait for a graceful shutdown")
    tls = p.add_mutually_exclusive_group()
    tls.add_argument("--ca-bundle", help="CA bundle to verify iDRAC certificates")
    tls.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed iDRAC certs)")
    a = p.parse_args()

    if a.command == "apply" and not a.iso_url:
        p.error("apply needs --iso-url")
    verify = a.ca_bundle or not a.insecure
    if a.insecure:
        requests.packages.urllib3.disable_warnings()
    user = os.environ.get("IDRAC_USER", "root")
    password = os.environ.get("IDRAC_PASSWORD") or getpass.getpass(f"iDRAC password for {user}: ")
    nodes = read_nodes(a.nodes)

    def run(host):
        d = Idrac(host, user, password, verify, dry_run=a.dry_run)
        try:
            return host, COMMANDS[a.command](d, a)
        except Exception as e:  # report per node, keep going with the others
            return host, {"ok": False, "summary": f"ERROR: {e}"}

    results = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        for host, res in pool.map(run, nodes):
            results[host] = res
            say(host, ("OK   " if res["ok"] else "FAIL ") + res["summary"])

    bad = [h for h, r in results.items() if not r["ok"]]
    print(f"\n{a.command}: {len(results) - len(bad)}/{len(results)} OK" + (f"; failed: {' '.join(bad)}" if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
