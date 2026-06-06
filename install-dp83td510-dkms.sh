#!/bin/sh
#
# This script builds dp83td510 out-of-tree (mainline source at the matching
# kernel SERIES) and installs it via DKMS, so it survives reboots & kernel
# upgrades.
#
# Usage:
#   sudo ./install-dp83td510-dkms.sh
#
# Overrides (env vars):
#   DP83_VER=1.0            DKMS package version
#   DP83_KSERIES=6.12       kernel series used to derive the source branch rpi-X.Y.y
#   DP83_SRC_URL=<url>      direct URL to dp83td510.c (non-RPi host: point it at
#                           the mainline torvalds tag matching your kernel)
#
# Hardware support:
#   - Raspberry Pi 3B+  : TESTED & working (RPiOS Bookworm, kernel 6.12.25 aarch64).
#   - Raspberry Pi 4    : very likely works (same headers/flavor scheme, untested).
#   - Raspberry Pi 5    : TBD — different kernel flavor (linux-headers-rpi-2712);
#                         the headers guard below will stop with a clear error if
#                         the right package isn't found. Not yet validated.
#   - Other Linux hosts : use DP83_SRC_URL to point at the matching kernel source.
set -eu

PKG=dp83td510
VER="${DP83_VER:-1.0}"
KVER="$(uname -r)"
KSERIES="${DP83_KSERIES:-$(echo "$KVER" | grep -oE '^[0-9]+\.[0-9]+')}"
SRC_URL="${DP83_SRC_URL:-https://raw.githubusercontent.com/raspberrypi/linux/rpi-${KSERIES}.y/drivers/net/phy/dp83td510.c}"
SRCDIR="/usr/src/${PKG}-${VER}"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo $0)"; exit 1; }

echo ">>> kernel=$KVER  series=$KSERIES  DKMS package=$PKG/$VER"
echo ">>> source = $SRC_URL"

# 1. dependencies (dkms + headers + toolchain)
echo ">>> 1/5 dependencies"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq || true
  apt-get install -y dkms build-essential curl || true
  if [ ! -d "/lib/modules/${KVER}/build" ]; then
    # Versioned package first (resolves directly on current Bookworm), then the
    # legacy RPi metapackage as a fallback.
    apt-get install -y "linux-headers-${KVER}" \
      || apt-get install -y raspberrypi-kernel-headers || true
  fi
else
  echo "    (no apt: install dkms + kernel headers + gcc/make manually)"
fi
[ -d "/lib/modules/${KVER}/build" ] \
  || { echo "ERROR: kernel headers missing (/lib/modules/${KVER}/build)"; exit 1; }

# 2. DKMS source tree (source + Makefile + dkms.conf)
echo ">>> 2/5 source tree $SRCDIR"
mkdir -p "$SRCDIR"
curl -fsSL "$SRC_URL" -o "$SRCDIR/${PKG}.c"
grep -q "DP83TD510" "$SRCDIR/${PKG}.c" \
  || { echo "ERROR: downloaded source is invalid (no DP83TD510 inside)"; exit 1; }
echo "obj-m := ${PKG}.o" > "$SRCDIR/Makefile"
# NB: \${kernelver}/\${dkms_tree}/... stay LITERAL -> DKMS substitutes them at
# build time; ${PKG}/${VER} are substituted here by the shell.
cat > "$SRCDIR/dkms.conf" <<EOF
PACKAGE_NAME="${PKG}"
PACKAGE_VERSION="${VER}"
AUTOINSTALL="yes"
BUILT_MODULE_NAME[0]="${PKG}"
DEST_MODULE_LOCATION[0]="/updates/dkms"
MAKE[0]="make -C /lib/modules/\${kernelver}/build M=\${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/build modules"
CLEAN="make -C /lib/modules/\${kernelver}/build M=\${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/build clean"
EOF

# 3. (re)install via DKMS — idempotent
echo ">>> 3/5 dkms add/build/install"
dkms remove -m "$PKG" -v "$VER" --all >/dev/null 2>&1 || true
dkms add    -m "$PKG" -v "$VER"
dkms build  -m "$PKG" -v "$VER"
dkms install -m "$PKG" -v "$VER"

# 4. load at boot (and right now)
echo ">>> 4/5 autoload"
echo "$PKG" > "/etc/modules-load.d/${PKG}.conf"
depmod -a
modprobe "$PKG" 2>/dev/null || true

# 5. verification
echo ">>> 5/5 verification"
dkms status -m "$PKG"
modinfo "$PKG" 2>/dev/null | grep -E '^filename|^vermagic' || true
echo
echo ">>> OK. Plug in the SPE dongle, then:"
echo "      dmesg | grep -i DP83TD510   # must show 'attached PHY driver'"
echo "      ethtool <ethN>              # must show 10baseT1L/Full"
echo ">>> If the dongle was already plugged in BEFORE (PHY fell back to Generic PHY):"
echo "      unplug/replug the dongle so dp83td510 takes over the PHY."