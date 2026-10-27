#!/usr/bin/env python3
r"""
XLGM Cleaner - tidy up a Skater XL / XLGearModifier gear library.

What it finds
  1. Byte-identical duplicate asset packs (cause "AssetBundle ... already loaded" log errors)
  2. Duplicate mod folders in Mods\ that share the same Id (UMM only loads one, logs an error)
  3. Mesh packs that have no texture at all (nothing in Gear\, nothing embedded)
  4. Texture files in Gear\ that no mesh will ever use (no custom pack, skater, or stock template matches)
  5. Packs that share a prefix (XLGM only loads one of them) - reported only

What it does
  * Dry run by default: prints a report and writes it to a text file. Nothing is touched.
  * --apply  moves (never deletes) everything flagged into a Retired folder with a manifest.csv
  * --restore <manifest.csv>  puts everything back exactly where it came from

Requirements: Python 3.8+ on Windows. No extra packages. Close Skater XL first.
"""
import sys, os, re, io, csv, json, lzma, struct, hashlib, datetime, argparse
from collections import defaultdict

VERSION = "1.0"

# --------------------------------------------------------------------------------------
#  Windows path discovery
# --------------------------------------------------------------------------------------
def documents_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            v, _ = winreg.QueryValueEx(k, "Personal")
            p = os.path.expandvars(v)
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    for c in (os.path.expanduser("~/Documents"), os.path.expanduser("~/OneDrive/Documents")):
        if os.path.isdir(c):
            return c
    return os.path.expanduser("~/Documents")

def steam_libraries():
    libs = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            sp, _ = winreg.QueryValueEx(k, "SteamPath")
        sp = os.path.normpath(sp)
        libs.append(sp)
        vdf = os.path.join(sp, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf, encoding="utf-8", errors="replace").read()):
                libs.append(os.path.normpath(m.group(1).replace("\\\\", "\\")))
    except Exception:
        pass
    return list(dict.fromkeys(libs))

def find_game_dir():
    for lib in steam_libraries():
        g = os.path.join(lib, "steamapps", "common", "Skater XL")
        if os.path.isfile(os.path.join(g, "SkaterXL.exe")):
            return g
    return None

# --------------------------------------------------------------------------------------
#  Minimal UnityFS (AssetBundle) reader - pure python
# --------------------------------------------------------------------------------------
def _lz4_block(src, usize):
    dst = bytearray(); i = 0; n = len(src)
    while i < n:
        tok = src[i]; i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255: break
        dst += src[i:i + lit]; i += lit
        if i >= n: break
        off = src[i] | (src[i + 1] << 8); i += 2
        ml = tok & 0xF
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255: break
        ml += 4
        st = len(dst) - off
        if off >= ml:
            dst += dst[st:st + ml]
        else:
            for k in range(ml):
                dst.append(dst[st + k])
    return bytes(dst[:usize])

def _lzma(src, usize):
    """Unity LZMA block: 5-byte props + raw LZMA1 stream, no end marker. Try raw first (works on every
    liblzma), then the .lzma-alone container with a known size, then with an unknown size."""
    props = src[0]
    lc = props % 9; lp = (props // 9) % 5; pb = props // 45
    dict_size = struct.unpack("<I", src[1:5])[0]
    attempts = (
        lambda: lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA1, "dict_size": max(dict_size, 4096), "lc": lc, "lp": lp, "pb": pb}]).decompress(src[5:]),
        lambda: lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(src[:5] + struct.pack("<Q", usize) + src[5:]),
        lambda: lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(src[:5] + b"\xff" * 8 + src[5:]),
    )
    last = None
    for a in attempts:
        try:
            out = a()
            if len(out) >= usize:
                return out[:usize]
            last = ValueError("short LZMA output %d < %d" % (len(out), usize))
        except Exception as e:
            last = e
    raise last

def _decomp(kind, src, usize):
    if kind == 0: return src[:usize]
    if kind == 1: return _lzma(src, usize)
    if kind in (2, 3): return _lz4_block(src, usize)
    raise ValueError("unknown compression %d" % kind)

class _R:
    def __init__(self, d, be=True): self.d = d; self.p = 0; self.be = be
    def _u(self, f, n):
        v = struct.unpack_from((">" if self.be else "<") + f, self.d, self.p)[0]; self.p += n; return v
    def u8(self): v = self.d[self.p]; self.p += 1; return v
    def i16(self): return self._u("h", 2)
    def u16(self): return self._u("H", 2)
    def i32(self): return self._u("i", 4)
    def u32(self): return self._u("I", 4)
    def i64(self): return self._u("q", 8)
    def cstr(self):
        e = self.d.index(b"\0", self.p); s = self.d[self.p:e].decode("utf-8", "replace"); self.p = e + 1; return s
    def take(self, n): v = self.d[self.p:self.p + n]; self.p += n; return v
    def align(self, a=4): self.p = (self.p + a - 1) // a * a

def is_unityfs(path):
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"UnityFS\0"
    except Exception:
        return False

def read_bundle(path):
    data = open(path, "rb").read()
    r = _R(data)
    if r.cstr() != "UnityFS":
        raise ValueError("not a UnityFS bundle")
    ver = r.u32(); r.cstr(); r.cstr()
    r.i64(); cbi = r.u32(); ubi = r.u32(); flags = r.u32()
    if ver >= 7: r.align(16)
    bi = data[len(data) - cbi:] if flags & 0x80 else r.take(cbi)
    bi = _decomp(flags & 0x3F, bi, ubi)
    br = _R(bi); br.take(16)
    blocks = [(br.u32(), br.u32(), br.u16()) for _ in range(br.i32())]
    nodes = []
    for _ in range(br.i32()):
        off = br.i64(); sz = br.i64(); br.u32(); name = br.cstr(); nodes.append((off, sz, name))
    out = bytearray()
    for usz, csz, bfl in blocks:
        out += _decomp(bfl & 0x3F, r.take(csz), usz)
    return {name: bytes(out[off:off + sz]) for off, sz, name in nodes}

def read_serialized(data):
    """Return list of (classID, object_bytes) for a SerializedFile (no typetree needed)."""
    r = _R(data)
    r.u32(); r.u32(); version = r.u32(); doff = r.u32()
    endian = 1
    if version >= 9: endian = r.u8(); r.take(3)
    if version >= 22: r.u32(); r.i64(); doff = r.i64(); r.i64()
    r.be = bool(endian)
    if version >= 7: r.cstr()
    if version >= 8: r.i32()
    has_tt = r.u8() if version >= 13 else 1
    types = []
    for _ in range(r.i32()):
        cid = r.i32()
        if version >= 16: r.u8()
        if version >= 17: r.i16()
        if version >= 13:
            if (version < 16 and cid < 0) or (version >= 16 and cid == 114): r.take(16)
            r.take(16)
        if has_tt:
            ncount = r.i32(); sbsize = r.i32()
            r.take(ncount * (32 if version >= 19 else 24)); r.take(sbsize)
            if version >= 21: r.take(4 * r.i32())
        types.append(cid)
    objs = []
    for _ in range(r.i32()):
        r.align(4); r.i64()
        start = r.i64() if version >= 22 else r.u32()
        bsize = r.u32(); tid = r.i32()
        if version < 16:
            cid = r.i16(); r.i16()
            if version >= 15: r.u8()
        else:
            cid = types[tid]
        objs.append((cid, data[doff + start: doff + start + bsize]))
    return objs

def lp_strings(od):
    """Length-prefixed printable strings (int32 LE + ascii) at 4-aligned offsets, in order."""
    out = []; i = 0; n = len(od)
    while i + 4 <= n:
        ln = struct.unpack_from("<i", od, i)[0]
        if 0 < ln <= 96 and i + 4 + ln <= n:
            s = od[i + 4:i + 4 + ln]
            if all(32 <= b < 127 for b in s):
                out.append(s.decode("ascii")); i = (i + 4 + ln + 3) // 4 * 4; continue
        i += 4
    return out

BONES = ("Skater_", "LeftLeg", "RightLeg", "Armature")
NONALBEDO = re.compile(r"normal|mask|rgmtao|nrm|alpha|rough|metal|occlusion", re.I)
GEAR_CATS = {"Shirt", "Pants", "Shoes", "Hat", "Socks", "Accessories", "Hair", "Other", "Headwear", "Top", "Bottom"}

def scan_pack(path):
    """Extract XLGM metadata from one asset pack."""
    info = {"items": [], "keys": set(), "tex2d": [], "gos": [], "skater": False, "err": None}
    try:
        files = read_bundle(path)
    except Exception as e:
        info["err"] = "bundle: %s" % e; return info
    raw = set()
    for name, blob in files.items():
        if name.endswith((".resS", ".resource")): continue
        try:
            objs = read_serialized(blob)
        except Exception as e:
            info["err"] = "serialized: %s" % e; continue
        for cid, od in objs:
            if cid == 28:                                  # Texture2D
                ss = lp_strings(od[:200]); info["tex2d"].append(ss[0] if ss else "?")
            elif cid == 1:                                 # GameObject
                ss = lp_strings(od)
                if ss: info["gos"].append(ss[-1])
            elif cid == 114:                               # MonoBehaviour
                m = lp_strings(od)
                if not m or m[0].startswith(BONES): continue
                raw.update(x for x in m if not x.startswith(BONES))
                idx = [i for i, x in enumerate(m) if x.startswith("XLGearModifier/") and "/alias/" not in x]
                if idx:
                    prefix = m[idx[0]].split("/")[-1].lower()
                    alias = next((x.split("/")[-1].lower() for x in m if x.startswith("XLGearModifier/alias/")), None)
                    cat = m[idx[0] + 1] if idx[0] + 1 < len(m) else ""
                    if cat not in GEAR_CATS: info["skater"] = True
                    info["items"].append({"display": m[0], "prefix": prefix, "alias": alias, "cat": cat})
                    info["keys"].add(prefix)
                    if alias: info["keys"].add(alias)
    if not info["items"]:
        for x in raw:
            k = x.lower().replace(" ", "")
            if len(k) >= 4: info["keys"].add(k)
        info["items"].append({"display": os.path.basename(path), "prefix": "|".join(sorted(info["keys"])), "alias": None, "cat": "legacy"})
    info["emb_albedo"] = [t for t in info["tex2d"] if not NONALBEDO.search(t)]
    return info

# --------------------------------------------------------------------------------------
#  Stock template ids (texture prefixes the base game understands)
# --------------------------------------------------------------------------------------
BUILTIN_STOCK = set("""
bwhoodie bwpants bwshoes bwshoesnewbalance508 espants esshirt esshoes fhairbraidedponytail fhairshortponytail
fhairstraightbun fhatdad fpantsdenim fshirt fshoes fshoescariumacatibapro fshoesdchyde fshoesdckalisvulc
fshoesemericalacedromero fshoesemericawinog6 fshoesesaccel fshoesesaccelslim fshoesesaccelslimplus
fshoesetniescallicut fshoesetniesmarana fshoeslakainewport fshoeslakaisheffield fshoesnewbalance1010
fshoesnewbalance306 fshoesnewbalance508 fshoesnewbalance913 fshoesnewbalancefoy306 fshoesnikejanoski
fshoesvanskylewalker2 mhairafrolong mhairafroshort mhaircounterpart mhaircurlylong mhaircurlyshort mhairdreads
mhairpompadour mhairsidepart mhairspikt mhairwavy mhat5panel mhatbeaniecuffed mhatbucket mhatdad mhatsnapback
mhattampa2022 mhoodie mjacketcoach mjeanbaggy mpants mpantsdenim mpantssweat mshirt mshirtbuttonlong mshirtlong
mshoes mshoescariumacatibapro mshoesdchyde mshoesdchyde2 mshoesdckalisvulc mshoesemericalacedromero
mshoesemericaspankyg6 mshoesemericawinog6 mshoesesaccel mshoesesaccelslim mshoesesaccelslimplus mshoesessilo
mshoesetniescallicut mshoesetniesfader mshoesetniesmarana mshoeslakaicambridge mshoeslakainewport
mshoeslakaisheffield mshoesnewbalance1010 mshoesnewbalance255 mshoesnewbalance288s mshoesnewbalance306
mshoesnewbalance508 mshoesnewbalance913 mshoesnewbalancefoy306 mshoesnikejanoski mshoesvanskylewalker2
mshortsdenim mshortskhaki msweater mtanktop tabonnet tapants tashirt tashoes tashoesesaccel tlpants tlshoes
tlsweater malebody femalebody malehead femalehead evansmithbody evansmithhead tiagolemosbody tiagolemoshead
tomastabody tomastahead brandonwestgatebody brandonwestgatehead deck griptape trucks trucksindependent
trucksthunder trucksventure wheels
""".split())

def stock_templates(game_dir):
    stock = set(BUILTIN_STOCK)
    if game_dir:
        aa = os.path.join(game_dir, "SkaterXL_Data", "StreamingAssets", "aa", "Windows", "StandaloneWindows64")
        if os.path.isdir(aa):
            for fn in os.listdir(aa):
                if fn.startswith("gear_assets_") and fn.endswith(".bundle"):
                    t = fn[len("gear_assets_"):-len(".bundle")].split("_")[0].lower()
                    if t and "." not in t and len(t) >= 4:
                        stock.add(t)
    return stock

# --------------------------------------------------------------------------------------
#  Matching
# --------------------------------------------------------------------------------------
ALWAYS_KEEP_DIRS = ("decks", "grips", "trucks", "wheels", "skateshop")
TEX_EXT = (".png", ".jpg", ".jpeg")

def matches(stem, key, loose=False):
    """'<key>_name', '<key>.normal', '<key>...Normal' (shared map), '<key>-x', '<key> x'."""
    if stem == key: return True
    if stem.startswith(key) and len(stem) > len(key) and stem[len(key)] in "_.- ": return True
    return loose and stem.startswith(key)

def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

# --------------------------------------------------------------------------------------
#  Main analysis
# --------------------------------------------------------------------------------------
def analyse(gear_dir, packs_dir, mods_dir, game_dir, log, skip_dir=None):
    findings = {"dup_packs": [], "dup_mods": [], "orphan_packs": [], "orphan_textures": [],
                "dup_prefixes": {}, "kept_review": [], "errors": []}
    skip = os.path.normcase(os.path.abspath(skip_dir)) if skip_dir else None
    def walk(top):
        for root, dirs, files in os.walk(top):
            if skip:
                dirs[:] = [d for d in dirs if os.path.normcase(os.path.abspath(os.path.join(root, d))) != skip]
            yield root, dirs, files

    # ---- packs ----
    pack_files = []
    for root, _, files in walk(packs_dir):
        for fn in files:
            p = os.path.join(root, fn)
            if is_unityfs(p):
                pack_files.append(p)
    log("Scanning %d asset packs in %s" % (len(pack_files), packs_dir))
    packs = {}
    by_hash = defaultdict(list)
    for i, p in enumerate(sorted(pack_files, key=str.lower)):
        packs[p] = scan_pack(p)
        by_hash[(os.path.getsize(p), md5(p))].append(p)
        if packs[p]["err"]:
            findings["errors"].append((os.path.relpath(p, packs_dir), packs[p]["err"]))
    COPYISH = re.compile(r"(\s-\s*copy|\(\d+\)|\bcopy\b)", re.I)
    for (_, h), group in by_hash.items():
        if len(group) > 1:
            # keep: a name that doesn't look like a copy, then the top-level one, then the more descriptive (longer) name
            group = sorted(group, key=lambda x: (bool(COPYISH.search(os.path.basename(x))),
                                                 x.count(os.sep), -len(os.path.basename(x)), x.lower()))
            keep, extras = group[0], group[1:]
            for e in extras:
                findings["dup_packs"].append((e, "byte-identical copy of '%s'" % os.path.basename(keep)))
    dup_set = {e for e, _ in findings["dup_packs"]}

    # ---- mods with duplicate Id ----
    if mods_dir and os.path.isdir(mods_dir):
        ids = defaultdict(list)
        for d in sorted(os.listdir(mods_dir)):
            info = os.path.join(mods_dir, d, "Info.json")
            if not os.path.isfile(info):
                info = os.path.join(mods_dir, d, "info.json")
            if os.path.isfile(info):
                try:
                    j = json.load(open(info, encoding="utf-8-sig"))
                    ids[j.get("Id", "")].append(os.path.join(mods_dir, d))
                except Exception:
                    pass
        def vkey(d):
            try:
                v = json.load(open(os.path.join(d, "Info.json") if os.path.isfile(os.path.join(d, "Info.json")) else os.path.join(d, "info.json"), encoding="utf-8-sig")).get("Version", "0")
            except Exception:
                v = "0"
            return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", str(v)))
        for mid, dirs in ids.items():
            if mid and len(dirs) > 1:
                # keep: plain folder name (not "-2-0-1" / "copy"), then highest Version, then newest
                dirs = sorted(dirs, key=lambda x: (bool(COPYISH.search(os.path.basename(x))) or bool(re.search(r"[-_ ]\d+([-_.]\d+)+$", os.path.basename(x))),
                                                   tuple(-n for n in vkey(x)), -os.path.getmtime(x)))
                for extra in dirs[1:]:
                    findings["dup_mods"].append((extra, "same mod Id '%s' as '%s' (UMM only loads one)" % (mid, os.path.basename(dirs[0]))))

    # ---- keys ----
    custom_keys = set(); skater_keys = set()
    for p, info in packs.items():
        if p in dup_set: continue
        custom_keys |= info["keys"]
        if info["skater"]: skater_keys |= info["keys"]
    stock = stock_templates(game_dir)
    log("Known keys: %d custom, %d skater, %d stock templates%s" % (
        len(custom_keys), len(skater_keys), len(stock), "" if game_dir else " (game folder not found, using built-in list)"))

    # ---- textures ----
    textures = []
    for root, _, files in walk(gear_dir):
        for fn in files:
            if fn.lower().endswith(TEX_EXT):
                textures.append(os.path.join(root, fn))
    log("Scanning %d textures in %s" % (len(textures), gear_dir))
    stems = []
    for t in textures:
        rel = os.path.relpath(t, gear_dir)
        stem = os.path.splitext(os.path.basename(t))[0].lower()
        stems.append(stem)
        top = rel.split(os.sep)[0].lower()
        if top in ALWAYS_KEEP_DIRS:
            continue
        if any(matches(stem, k) for k in custom_keys): continue
        if any(matches(stem, k, True) for k in skater_keys): continue
        if any(matches(stem, k) for k in stock): continue
        findings["orphan_textures"].append((t, "no mesh (custom pack, skater, or stock template) matches its prefix"))

    # ---- orphan packs ----
    for p, info in packs.items():
        if p in dup_set or info["err"] or info["skater"]:
            continue
        if any(matches(s, k) for k in info["keys"] for s in stems):
            continue
        if info["emb_albedo"]:
            continue
        based = sorted({k for g in info["gos"] for k in stock if len(k) >= 6 and g.lower().startswith(k)})
        if based:
            findings["kept_review"].append((p, "no textures of its own, but mesh is based on stock %s and may inherit stock textures" % based))
            continue
        why = "no texture for prefix/alias %s" % ", ".join(sorted(info["keys"]))
        if info["tex2d"]:
            why += " (embedded maps are not colour textures: %s)" % ", ".join(info["tex2d"][:3])
        findings["orphan_packs"].append((p, why))

    # ---- duplicate prefixes ----
    pref = defaultdict(set)
    for p, info in packs.items():
        if p in dup_set: continue
        for it in info["items"]:
            if it["cat"] != "legacy":
                pref[it["prefix"]].add(os.path.relpath(p, packs_dir))
    findings["dup_prefixes"] = {k: sorted(v) for k, v in pref.items() if len(v) > 1}
    findings["counts"] = {"packs": len(pack_files), "textures": len(textures)}
    return findings

# --------------------------------------------------------------------------------------
#  Report / apply / restore
# --------------------------------------------------------------------------------------
def build_report(f, gear_dir, packs_dir, mods_dir):
    L = []
    def sec(title, rows, base):
        L.append(""); L.append("== %s (%d) ==" % (title, len(rows)))
        for path, why in rows:
            L.append("  %-60s %s" % (os.path.relpath(path, base) if base else path, why))
    sec("Duplicate asset packs (byte-identical) -> MOVE", f["dup_packs"], packs_dir)
    sec("Duplicate mod folders (same Id) -> MOVE", f["dup_mods"], mods_dir)
    sec("Mesh packs with no texture -> MOVE", f["orphan_packs"], packs_dir)
    sec("Textures with no mesh -> MOVE", f["orphan_textures"], gear_dir)
    sec("Kept, but check yourself", f["kept_review"], packs_dir)
    sec("Packs that could not be read (left alone)", f["errors"], None)
    L.append(""); L.append("== Packs sharing one prefix (XLGM loads only one; nothing moved) (%d) ==" % len(f["dup_prefixes"]))
    for k, v in f["dup_prefixes"].items():
        L.append("  %-30s %s" % (k, "  |  ".join(v)))
    total = len(f["dup_packs"]) + len(f["dup_mods"]) + len(f["orphan_packs"]) + len(f["orphan_textures"])
    L.append(""); L.append("Scanned %d packs and %d textures. %d items would be moved." % (f["counts"]["packs"], f["counts"]["textures"], total))
    return "\n".join(L), total

def apply_moves(f, retire_root, gear_dir, packs_dir, mods_dir, log):
    import shutil
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    root = os.path.join(retire_root, stamp)
    os.makedirs(root, exist_ok=True)
    rows = []
    def mv(kind, src, base, sub, why):
        rel = os.path.relpath(src, base)
        dst = os.path.join(root, sub, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        rows.append((kind, rel, src, dst, why))
    for p, why in f["dup_packs"]:      mv("duplicate-pack", p, packs_dir, "Asset Packs", why)
    for p, why in f["dup_mods"]:       mv("duplicate-mod", p, mods_dir, "Mods", why)
    for p, why in f["orphan_packs"]:   mv("orphan-pack", p, packs_dir, "Asset Packs", why)
    for p, why in f["orphan_textures"]: mv("orphan-texture", p, gear_dir, "Gear", why)
    man = os.path.join(root, "manifest.csv")
    with open(man, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["kind", "relative_path", "moved_from", "moved_to", "reason"]); w.writerows(rows)
    log("Moved %d items to %s" % (len(rows), root))
    log("Manifest: %s   (run with --restore \"%s\" to undo)" % (man, man))
    return root

def restore(manifest, log):
    import shutil
    rows = list(csv.DictReader(open(manifest, encoding="utf-8")))
    n = 0
    for r in rows:
        if os.path.exists(r["moved_to"]):
            os.makedirs(os.path.dirname(r["moved_from"]), exist_ok=True)
            if os.path.exists(r["moved_from"]):
                log("  skip (already exists at source): %s" % r["relative_path"]); continue
            shutil.move(r["moved_to"], r["moved_from"]); n += 1
        else:
            log("  missing in retired folder: %s" % r["relative_path"])
    log("Restored %d of %d items." % (n, len(rows)))

# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="XLGM Cleaner v%s - find and retire unused Skater XL gear" % VERSION)
    ap.add_argument("--apply", action="store_true", help="move flagged items to the Retired folder (default is dry run)")
    ap.add_argument("--restore", metavar="MANIFEST", help="undo a previous --apply using its manifest.csv")
    ap.add_argument("--gear", help="Gear folder (default: Documents\\SkaterXL\\Gear)")
    ap.add_argument("--packs", help="Asset Packs folder (default: Documents\\SkaterXL\\XLGearModifier\\Asset Packs)")
    ap.add_argument("--game", help="Skater XL install folder (auto-detected from Steam)")
    ap.add_argument("--mods", help="Mods folder to check for duplicate mod Ids (default: <game>\\Mods)")
    ap.add_argument("--retire-dir", help="where to move things (default: Documents\\SkaterXL_Retired)")
    ap.add_argument("--no-prompt", action="store_true", help="never ask; just dry-run (or apply if --apply)")
    a = ap.parse_args()

    lines = []
    def log(s=""):
        print(s); lines.append(s)

    log("XLGM Cleaner v%s" % VERSION)
    if a.restore:
        restore(a.restore, log); return

    docs = documents_dir()
    gear_dir = a.gear or os.path.join(docs, "SkaterXL", "Gear")
    packs_dir = a.packs or os.path.join(docs, "SkaterXL", "XLGearModifier", "Asset Packs")
    game_dir = a.game or find_game_dir()
    mods_dir = a.mods or (os.path.join(game_dir, "Mods") if game_dir else None)
    retire_root = a.retire_dir or os.path.join(docs, "SkaterXL_Retired")

    for label, d in (("Gear", gear_dir), ("Asset Packs", packs_dir)):
        if not os.path.isdir(d):
            log("ERROR: %s folder not found: %s" % (label, d)); sys.exit(2)
    log("Game:        %s" % (game_dir or "not found (stock template list is built in)"))
    log("Mods:        %s" % (mods_dir or "not checked"))
    log("Gear:        %s" % gear_dir)
    log("Asset Packs: %s" % packs_dir)
    log("Retire to:   %s" % retire_root)
    log("")

    f = analyse(gear_dir, packs_dir, mods_dir, game_dir, log, skip_dir=retire_root)
    report, total = build_report(f, gear_dir, packs_dir, mods_dir)
    log(report)

    os.makedirs(retire_root, exist_ok=True)
    rp = os.path.join(retire_root, "report_%s.txt" % datetime.datetime.now().strftime("%Y-%m-%d_%H%M"))
    open(rp, "w", encoding="utf-8").write("\n".join(lines))
    log(""); log("Report saved: %s" % rp)

    if total == 0:
        log("Nothing to move. Library is clean."); return
    do_apply = a.apply
    if not do_apply and not a.no_prompt and sys.stdin and sys.stdin.isatty():
        log("")
        ans = input("Type MOVE to move these %d items to the Retired folder (nothing is deleted), or Enter to quit: " % total)
        do_apply = ans.strip().upper() == "MOVE"
    if do_apply:
        apply_moves(f, retire_root, gear_dir, packs_dir, mods_dir, log)
        open(rp, "a", encoding="utf-8").write("\n" + "\n".join(lines[-2:]) + "\n")
    else:
        log("Dry run only. Re-run with --apply (or answer MOVE) to move them.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
