#!/usr/bin/env bash
# Build Eureka (SM-G990B2 / r9q2, openx2) + BakaSU + SUSFS v2.3.0 and package a recovery-flashable zip.
#
# Host requirements: git curl python3 make bc bison flex perl cpio openssl
#                    + android-tools (mkbootimg, unpack_bootimg, avbtool)
# Usage:  ./bakasu/build.sh [-jN]
# Env:    WORKDIR (default: <repo>/bakasu_work)  OUT_DIR (default: <repo>/out)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="${WORKDIR:-$ROOT/bakasu_work}"
OUT_DIR="${OUT_DIR:-$ROOT/out}"
JOBS="${1:--j$(nproc)}"
export WORKDIR OUT_DIR

CLANG_URL="https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/refs/heads/main/clang-r530567.tar.gz"
EUREKA_ZIP_URL="https://github.com/saadelasfur/eureka_releases/releases/download/20251229/Eureka_Vanilla_20251229_r9q2.zip"

mkdir -p "$WORKDIR"
cd "$ROOT"

# --- BakaSU submodule (needs full history: version code = commit count)
git submodule update --init KernelSU
if [ -f "$(git -C KernelSU rev-parse --git-dir)/shallow" ]; then git -C KernelSU fetch --unshallow; fi

# --- clang r530567 (same compiler Eureka is built with)
if [ ! -x "$WORKDIR/clang/bin/clang" ]; then
    mkdir -p "$WORKDIR/clang"
    curl -fL "$CLANG_URL" | tar xz -C "$WORKDIR/clang"
fi
export PATH="$WORKDIR/clang/bin:$PATH"
clang --version | head -1

# --- Eureka Vanilla images (base for boot/vendor_boot/dtbo)
if [ ! -f "$WORKDIR/eureka_ref/x/eureka/boot.img" ]; then
    rm -rf "$WORKDIR/eureka_ref" && mkdir -p "$WORKDIR/eureka_ref"
    curl -fL -o "$WORKDIR/eureka_ref/vanilla.zip" "$EUREKA_ZIP_URL"
    unzip -q "$WORKDIR/eureka_ref/vanilla.zip" -d "$WORKDIR/eureka_ref/x"
    unpack_bootimg --boot_img "$WORKDIR/eureka_ref/x/eureka/boot.img" --out "$WORKDIR/eureka_ref/boot_unpacked" >/dev/null
    unpack_bootimg --boot_img "$WORKDIR/eureka_ref/x/eureka/vendor_boot.img" --out "$WORKDIR/eureka_ref/vb_unpacked" >/dev/null
fi

KMAKE=(make -C "$ROOT" O="$OUT_DIR" ARCH=arm64 LLVM=1 LLVM_IAS=1
       CC=clang REAL_CC=clang CROSS_COMPILE=aarch64-linux-gnu- CLANG_TRIPLE=aarch64-linux-gnu-
       DTC_EXT="$ROOT/tools/dtc" CONFIG_BUILD_ARM64_DT_OVERLAY=y CONFIG_SECTION_MISMATCH_WARN_ONLY=y)

# --- config: Eureka openx2 defconfig + BakaSU/SUSFS fragment
"${KMAKE[@]}" eureka/r9q_eur_openx2_defconfig
KCONFIG_CONFIG="$OUT_DIR/.config" scripts/kconfig/merge_config.sh -m -O "$OUT_DIR" \
    "$OUT_DIR/.config" arch/arm64/configs/bakasu_susfs.config
"${KMAKE[@]}" olddefconfig
grep -q '^CONFIG_KSU_SUSFS=y' "$OUT_DIR/.config" || { echo "KSU_SUSFS not enabled"; exit 1; }

# --- build kernel + modules
"${KMAKE[@]}" "$JOBS"

# --- package
python3 "$ROOT/bakasu/package.py"
ls -la "$WORKDIR/dist"
