#!/bin/sh
# Runs once at boot inside the utility ISO (see ../README.md).
# Sets both ports of every BlueField-3 in the host to Ethernet, then powers the host off so
# bf3_linktype_idrac.py (watching PowerState through iDRAC) knows it finished.
# On any failure it leaves the host ON: the tool then times out and flags the node, and the
# messages below are on the iDRAC virtual console.
set -u
LINK_TYPE="${LINK_TYPE:-2}"   # 1 = InfiniBand, 2 = Ethernet

say()  { echo "bf3-set-eth: $*"; }
fail() { say "FAILED: $*"; say "leaving the host powered on for inspection"; exit 1; }

if command -v mlxconfig >/dev/null 2>&1; then
    TOOL=mlxconfig                          # NVIDIA MFT
    command -v mst >/dev/null 2>&1 && mst start >/dev/null 2>&1
elif command -v mstconfig >/dev/null 2>&1; then
    TOOL=mstconfig                          # mstflint (open-source MFT subset)
else
    fail "neither mlxconfig (NVIDIA MFT) nor mstconfig (mstflint) is installed in the image"
fi

# Only BlueField-3 (PCI 15b3:a2dc), function 0 of each card: LINK_TYPE_P1/P2 are per card.
# This deliberately leaves the node's other NVIDIA controllers alone (on the XE9712 the
# integrated InfiniBand controllers for the compute fabric).
BDFS=$(lspci -D -n -d 15b3:a2dc | awk '$1 ~ /\.0$/ {print $1}')
[ -n "$BDFS" ] || fail "no BlueField-3 (15b3:a2dc) found on the PCI bus"

for bdf in $BDFS; do
    say "$bdf before:"
    "$TOOL" -d "$bdf" query LINK_TYPE_P1 LINK_TYPE_P2 || fail "cannot query $bdf (host privilege restricted? Secure Boot lockdown?)"
    "$TOOL" -d "$bdf" -y set LINK_TYPE_P1="$LINK_TYPE" LINK_TYPE_P2="$LINK_TYPE" || fail "cannot set $bdf"
    say "$bdf after (see the Next Boot column):"
    "$TOOL" -d "$bdf" query LINK_TYPE_P1 LINK_TYPE_P2
done

say "done, powering off"
sync
systemctl poweroff
