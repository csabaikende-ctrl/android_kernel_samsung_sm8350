#!/usr/bin/env python3
"""
Package a BakaSU+SUSFS Eureka build for r9q2 (SM-G990B2), reusing Eureka Vanilla's images:
  boot.img        <- Eureka ramdisk/cmdline + our Image
  vendor_boot.img <- Eureka vendor ramdisk with every lib/modules/*.ko swapped for ours (+ Eureka dtb)
  dtbo.img        <- Eureka's (same source commit, untouched)
Then adds AVB hashtree footers matching Eureka's layout and builds a recovery-flashable zip.
"""
import gzip, os, shutil, subprocess, sys, zipfile, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = os.environ.get("WORKDIR", os.path.join(ROOT, "bakasu_work"))
OUT = os.environ.get("OUT_DIR", os.path.join(ROOT, "out"))
REF = os.path.join(W, "eureka_ref")
DIST = os.path.join(W, "dist")
WORK = f"{DIST}/work"
PART_SIZE = 100663296
CMDLINE = ("console=null androidboot.hardware=qcom androidboot.memcg=1 lpm_levels.sleep_disabled=1 "
           "video=vfb:640x400,bpp=32,memsize=3072000 msm_rtb.filter=0x237 service_locator.enable=1 "
           "androidboot.usbcontroller=a600000.dwc3 swiotlb=2048 loop.max_part=7 cgroup.memory=nokmem,nosocket "
           "firmware_class.path=/vendor/firmware_mnt/image printk.devkmsg=on pcie_ports=compat "
           "cpuinfo.chipname=SM8350 panic=4")
STRIP = os.path.join(W, "clang", "bin", "llvm-strip")


def run(*a):
    print("+", " ".join(a))
    subprocess.run(a, check=True)


# ---------- newc cpio rewrite (keeps every other entry byte-identical) ----------
def cpio_entries(data):
    off = 0
    while True:
        assert data[off:off + 6] == b"070701", f"bad cpio magic at {off}"
        hdr = data[off:off + 110]
        f = [int(hdr[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        namesize, filesize = f[11], f[6]
        name_end = off + 110 + namesize
        name = data[off + 110:name_end - 1].decode()
        data_off = (name_end + 3) & ~3
        data_end = data_off + filesize
        nxt = (data_end + 3) & ~3
        yield name, hdr, data[off + 110:name_end], data[data_off:data_end]
        off = nxt
        if name == "TRAILER!!!":
            return


def pad4(b):
    return b + b"\0" * ((-len(b)) % 4)


def cpio_rebuild(data, replace):
    out = bytearray()
    for name, hdr, rawname, body in cpio_entries(data):
        if name in replace:
            body = replace.pop(name)
            hdr = hdr[:54] + b"%08X" % len(body) + hdr[62:]
        rec = pad4(hdr + rawname) + pad4(body)
        out += rec
    assert not replace, f"unreplaced entries: {list(replace)}"
    out += b"\0" * ((-len(out)) % 512)
    return bytes(out)


def main():
    image = f"{OUT}/arch/arm64/boot/Image"
    assert os.path.exists(image), "build not finished: no Image"
    shutil.rmtree(DIST, ignore_errors=True)
    os.makedirs(WORK)

    # ---- collect our built modules by basename
    built = {}
    for root, _, files in os.walk(OUT):
        for fn in files:
            if fn.endswith(".ko"):
                assert fn not in built or os.path.samefile(built[fn], os.path.join(root, fn)), f"dup module {fn}"
                built[fn] = os.path.join(root, fn)

    # ---- vendor ramdisk: swap modules
    vr = gzip.decompress(open(f"{REF}/vb_unpacked/vendor_ramdisk", "rb").read())
    replace, missing = {}, []
    for name, *_ in cpio_entries(vr):
        if name.startswith("lib/modules/") and name.endswith(".ko"):
            base = os.path.basename(name)
            if base not in built:
                missing.append(base)
                continue
            tmp = f"{WORK}/{base}"
            shutil.copy(built[base], tmp)
            run(STRIP, "--strip-debug", tmp)
            replace[name] = open(tmp, "rb").read()
    if missing:
        sys.exit(f"ERROR: modules in Eureka vendor_boot not produced by our build: {missing}")
    print(f"swapping {len(replace)} modules into vendor_ramdisk")
    new_vr = cpio_rebuild(vr, replace)
    with gzip.GzipFile(f"{WORK}/vendor_ramdisk", "wb", compresslevel=9, mtime=0) as g:
        g.write(new_vr)

    # ---- boot.img (header v3)
    run("mkbootimg", "--header_version", "3", "--os_version", "11.0.0", "--os_patch_level", "2025-12",
        "--kernel", image, "--ramdisk", f"{REF}/boot_unpacked/ramdisk", "--cmdline", CMDLINE,
        "--output", f"{WORK}/boot.img")
    # ---- vendor_boot.img (header v3)
    run("mkbootimg", "--header_version", "3", "--pagesize", "0x00001000", "--base", "0x00000000",
        "--kernel_offset", "0x00008000", "--ramdisk_offset", "0x02000000", "--tags_offset", "0x01e00000",
        "--dtb_offset", "0x0000000001f00000", "--vendor_cmdline", CMDLINE, "--board", "SRPVC21A015",
        "--dtb", f"{REF}/vb_unpacked/dtb", "--vendor_ramdisk", f"{WORK}/vendor_ramdisk",
        "--vendor_boot", f"{WORK}/vendor_boot.img")

    # ---- AVB hashtree footers like Eureka's (own throwaway RSA4096 key; unlocked BL doesn't verify it)
    key = os.path.join(W, "avb_rsa4096.pem")
    if not os.path.exists(key):
        run("openssl", "genrsa", "-out", key, "4096")
    for part in ("boot", "vendor_boot"):
        run("avbtool", "add_hashtree_footer", "--image", f"{WORK}/{part}.img", "--partition_size", str(PART_SIZE),
            "--partition_name", part, "--hash_algorithm", "sha256", "--algorithm", "SHA256_RSA4096",
            "--key", key, "--do_not_generate_fec")

    # ---- zip with Eureka's installer (banner changed)
    ub = open(f"{REF}/x/META-INF/com/google/android/update-binary").read()
    ub = ub.replace('ui_print "Eureka Kernel (Vanilla)"', 'ui_print "Eureka Kernel + BakaSU + SUSFS v2.3.0"')
    ub = ub.replace('ui_print "Build date: 2025-12-29 16:07:55"',
                    f'ui_print "Build date: {datetime.datetime.now():%Y-%m-%d %H:%M:%S} (unofficial)"')
    date = datetime.date.today().strftime("%Y%m%d")
    zpath = f"{DIST}/Eureka_BakaSU-SUSFS_{date}_r9q2.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("META-INF/com/google/android/update-binary"), ub)
        z.writestr("META-INF/com/google/android/updater-script", "# Dummy file; update-binary is a shell script.\n")
        z.write(f"{WORK}/boot.img", "eureka/boot.img")
        z.write(f"{REF}/x/eureka/dtbo.img", "eureka/dtbo.img")
        z.write(f"{WORK}/vendor_boot.img", "eureka/vendor_boot.img")
    for f in ("boot.img", "vendor_boot.img"):
        shutil.copy(f"{WORK}/{f}", f"{DIST}/{f}")
    shutil.copy(f"{REF}/x/eureka/dtbo.img", f"{DIST}/dtbo.img")
    shutil.copy(image, f"{DIST}/Image")
    print("DONE:", zpath)


if __name__ == "__main__":
    main()
