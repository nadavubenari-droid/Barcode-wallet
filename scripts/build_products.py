"""
Builds a barcode -> product table from the price lists Israeli supermarket chains
must publish by law (חוק שקיפות מחירים), for the Barcode Wallet app.

Output (in ./out):
  p/<last two digits of the barcode>.json  {"<barcode>": [name, maker, size, min, max, median, chains]}
  meta.json                                 {"updated": ..., "products": ..., "chains": [...]}
"""
import collections, datetime, gzip, io, json, os, re, statistics, sys, zipfile

from lxml import etree

# Every chain whose price lists can be downloaded from outside Israel (GitHub's runners are in the US).
# Super-Pharm, Victory, Carrefour/Yeinot Bitan, Hazi Hinam, Netiv Hesed, Het Cohen and Mahsani Ashuk
# block foreign downloads, so they are not here.
CHAINS = ["SHUFERSAL", "RAMI_LEVY", "YOHANANOF", "OSHER_AD", "TIV_TAAM", "KING_STORE", "KESHET",
          "SUPER_YUDA", "STOP_MARKET", "POLIZER", "DOR_ALON", "SALACH_DABACH", "GOOD_PHARM",
          "BAREKET", "CITY_MARKET_KIRYATGAT", "CITY_MARKET_SHOPS", "FRESH_MARKET_AND_SUPER_DOSH",
          "MAAYAN_2000", "MESHMAT_YOSEF_2", "SHEFA_BARCART_ASHEM", "SHUK_AHIR", "SUPER_SAPIR",
          "WOLT", "YELLOW", "ZOL_VEBEGADOL"]
# download folder -> (one-letter code used in the data, name shown in the app)
CHAIN_INFO = {
    "Shufersal": ("a", "שופרסל / BE"), "RamiLevy": ("b", "רמי לוי"), "Yohananof": ("c", "יוחננוף"),
    "Osherad": ("d", "אושר עד"), "TivTaam": ("e", "טיב טעם"), "KingStore": ("f", "קינג סטור"),
    "Keshet": ("g", "קשת טעמים"), "SuperYuda": ("h", "סופר יודה"), "StopMarket": ("i", "סטופ מרקט"),
    "Polizer": ("j", "פוליצר"), "DorAlon": ("k", "דור אלון / אלונית"), "SalachDabach": ("l", "סאלח דבאח"),
    "GoodPharm": ("m", "גוד פארם"), "Bareket": ("n", "ברקת"), "CityMarketKiryatGat": ("o", "סיטי מרקט"),
    "CityMarketShops": ("o", "סיטי מרקט"), "FreshMarketAndSuperDosh": ("p", "פרשמרקט / סופר דוש"),
    "Maayan2000": ("q", "מעיין 2000"), "MeshnatYosef2": ("r", "משנת יוסף"), "ShefaBarcartAshem": ("s", "שפע ברכת השם"),
    "ShukAhir": ("t", "שוק העיר"), "SuperSapir": ("u", "סופר ספיר"), "Wolt": ("v", "וולט מרקט"),
    "Yellow": ("w", "יילו (פז)"), "ZolVeBegadol": ("x", "זול ובגדול"),
}
FILES_PER_CHAIN = int(os.environ.get("FILES_PER_CHAIN", "25"))
DUMPS = "dumps"
OUT = "out"


def download():
    from il_supermarket_scarper import ScarpingTask
    task = ScarpingTask(
        enabled_scrapers=CHAINS,
        files_types=["PRICE_FULL_FILE"],
        multiprocessing=6,
        output_configuration={"output_mode": "disk", "base_storage_path": DUMPS},
        status_configuration={"database_type": "json", "base_path": os.path.join(DUMPS, "status")},
        timeout_in_seconds=60 * 30,
    )
    task.start(limit=FILES_PER_CHAIN)
    task.join()


# ---- medicines sold without a prescription, from the Ministry of Health registry ----
REGISTRY = "https://registries.health.gov.il/api/Medicines/GetMedicines"


def fetch_medicines():
    """Registrations whose packs need no prescription (otcOnly) or may be sold outside a
    pharmacy (telemOnly). A few dozen gentle requests a day."""
    import time
    import requests
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json",
                      "Origin": "https://registries.health.gov.il", "Referer": "https://registries.health.gov.il/Medicines",
                      "User-Agent": "Mozilla/5.0 (Barcode Wallet daily product list)"})
    out = {}
    for flt, kind in (("telemOnly", "gsl"), ("otcOnly", "otc")):
        page, total = 0, None
        while total is None or page * 100 < total:
            body = {"searchText": "", "pageNumber": page, "maxResult": 100, "sortBy": "tradeNameHeb",
                    "sortDirection": "asc", "includeActive": True, "includeCanceled": False, flt: True}
            try:
                r = s.post(REGISTRY, json=body, timeout=60).json()
            except Exception as e:  # noqa: BLE001
                print("medicines:", flt, "page", page, e)
                break
            if not isinstance(r, dict) or r.get("statusCode") != 200 or not r.get("returnObject"):
                print("medicines: unexpected answer", str(r)[:200])
                break
            ro = r["returnObject"]
            total = int(ro.get("totalRows") or 0)
            for m in ro.get("medicinesList") or []:
                add_medicine(m, kind, out)
            page += 1
            if page > 120:
                break
            time.sleep(1)
        print(f"medicines {flt}: {total} registrations, {len(out)} barcodes so far")
    return out


def barcodes_in(obj):
    found = set()
    for k, v in (obj or {}).items():
        if re.search(r"barc|bark", k, re.I) and v:
            for c in re.findall(r"\d{8,14}", str(v)):
                found.add(c)
    return found


def add_medicine(m, kind, out):
    name = (m.get("tradeNameHeb") or m.get("tradeName") or "").strip()
    eng = (m.get("tradeName") or "").strip()
    maker = (m.get("ownerName") or m.get("manufacturerName") or "").strip()
    comps = ", ".join(filter(None, [((c.get("componentDesc") or "") + " " + (c.get("dosage") or "")).strip()
                                    for c in (m.get("components") or [])]))
    form = (m.get("dosageFormHeb") or m.get("dosageFormE") or "").strip()
    rec_codes = barcodes_in(m)
    packs = m.get("packages") or []
    for p in packs:
        if p.get("mirsham") not in (0, "0", None):
            continue
        codes = barcodes_in(p) or (rec_codes if len(packs) == 1 else set())
        size = (p.get("packageDesc") or p.get("packDesc") or p.get("packageName") or "").strip()
        price = p.get("customerPriceWithTax") or 0
        for c in codes:
            out[c] = {"name": name if not eng or eng.lower() in name.lower() else f"{name} ({eng})",
                      "maker": maker, "size": size or form, "price": float(price) if price else None,
                      "kind": kind, "extra": comps}
    if not packs:
        for c in rec_codes:
            out[c] = {"name": name, "maker": maker, "size": form, "price": None, "kind": kind, "extra": comps}


def read_bytes(path):
    data = open(path, "rb").read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        data = z.read(z.namelist()[0])
    return data


def local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def size_text(qty, unit):
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return ""
    if q <= 0:
        return ""
    u = (unit or "").strip()
    if re.search(r"גר|גרם|gram|^g$", u, re.I) and q >= 1000:
        q, u = q / 1000, 'ק"ג'
    elif re.search(r'מ"ל|מיליליטר|מ״ל|ml', u, re.I) and q >= 1000:
        q, u = q / 1000, "ליטר"
    if re.search(r"יחיד|unit", u, re.I) and q == 1:
        return ""
    return f"{q:g} {u}".strip()


# ---- word index, so the app can find products by words read off the package ----
# (the app has the same normalisation in JavaScript: keep the two in step)
FINAL = str.maketrans("םןץףך", "מנצפכ")
UNITS = {u.translate(FINAL) for u in 'גרם גר מל ליטר ל קג ק ג יח יחידות יחידה ml g gr kg l מ סמ cm x'.split()}
SHARDS = 64


def norm_tokens(text):
    s = (text or "").lower()
    s = re.sub("[\u0591-\u05C7]", "", s)
    s = re.sub("[\"'״׳`]", "", s)
    s = s.translate(FINAL).replace("דוקטור", "דר")
    out = []
    for t in re.findall("[א-תa-z0-9]+", s):
        if len(t) < 2 or t.isdigit() or t in UNITS or re.fullmatch(r"\d+[a-zא-ת]{0,3}", t):
            continue
        out.append(t)
    return out


def shard_of(token):
    h = 0x811C9DC5
    for ch in token:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h % SHARDS


def write_index(table):
    post = collections.defaultdict(set)
    for code, row in table.items():
        for t in set(norm_tokens(row[0] + " " + (row[1] or ""))):
            post[t].add(code)
    vocab = {t: len(c) for t, c in post.items()}
    shards = collections.defaultdict(dict)
    for t, codes in post.items():
        shards[shard_of(t)][t] = sorted(codes)[:300]
    os.makedirs(os.path.join(OUT, "t"), exist_ok=True)
    for k, v in shards.items():
        with open(os.path.join(OUT, "t", f"{k}.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "vocab.json"), "w", encoding="utf-8") as fh:
        json.dump(vocab, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Index: {len(vocab)} words")


def best_name(names):
    """Chains often cut names short; prefer the fullest of the commonly used names."""
    top = names.most_common()
    floor = max(1, top[0][1] / 4)
    good = [n for n, c in top if c >= floor]
    return max(good, key=lambda n: (len(n.rstrip('"\' ')), names[n]))


def parse(chain, path, acc):
    try:
        data = read_bytes(path)
    except Exception as e:  # noqa: BLE001
        print(f"  skip {path}: {e}")
        return 0
    n = 0
    for _, el in etree.iterparse(io.BytesIO(data), events=("end",), recover=True, huge_tree=True):
        if local(el.tag) not in ("item", "product"):
            continue
        f = {local(c.tag): (c.text or "").strip() for c in el if isinstance(c.tag, str)}
        el.clear()
        code = re.sub(r"\D", "", f.get("itemcode", ""))
        if not (8 <= len(code) <= 14) or f.get("bisweighted") == "1":
            continue
        code = code.lstrip("0").zfill(13) if len(code) > 13 else code
        rec = acc[code]
        name = re.sub(r"\s+", " ", f.get("itemname", "") or f.get("manufactureritemdescription", "")).strip()
        if name:
            rec["names"][name] += 1
        maker = f.get("manufacturername", "").strip()
        if maker and re.search(r"[A-Za-z\u0590-\u05FF]{2,}", maker) and not re.search(r"לא ידוע|כללי|unknown", maker):
            rec["makers"][maker] += 1
        s = size_text(f.get("quantity"), f.get("unitqty") or f.get("unitofmeasure"))
        if s:
            rec["sizes"][s] += 1
        try:
            p = float(f.get("itemprice", ""))
            if 0 < p < 10000:
                rec["prices"].append(p)
        except ValueError:
            pass
        rec["chains"].add(CHAIN_INFO.get(chain, ("?", chain))[0])
        n += 1
    return n


def main():
    if "--no-download" not in sys.argv:
        try:
            download()
        except Exception as e:  # noqa: BLE001 - use whatever was downloaded
            print("download error:", e)
    acc = collections.defaultdict(lambda: {"names": collections.Counter(), "makers": collections.Counter(),
                                           "sizes": collections.Counter(), "prices": [], "chains": set()})
    chains_seen = set()
    for root, _, files in os.walk(DUMPS):
        if "status" in root.split(os.sep):
            continue
        chain = os.path.relpath(root, DUMPS).split(os.sep)[0]
        for fn in files:
            if "price" not in fn.lower():
                continue
            n = parse(chain, os.path.join(root, fn), acc)
            print(f"{chain}/{fn}: {n} items")
            if n:
                chains_seen.add(chain)
    meds = {}
    if "--no-download" not in sys.argv:
        try:
            meds = fetch_medicines()
        except Exception as e:  # noqa: BLE001
            print("medicines error:", e)
    if not acc and not meds:
        print("No products parsed")
        sys.exit(1)
    shards = collections.defaultdict(dict)
    for code, r in acc.items():
        if not r["names"]:
            continue
        pr = sorted(r["prices"])
        shards[code[-2:]][code] = [
            best_name(r["names"]),
            r["makers"].most_common(1)[0][0] if r["makers"] else "",
            r["sizes"].most_common(1)[0][0] if r["sizes"] else "",
            round(pr[0], 2) if pr else None,
            round(pr[-1], 2) if pr else None,
            round(statistics.median(pr), 2) if pr else None,
            len(r["chains"]),
            "".join(sorted(r["chains"])),
            "", "",
        ]
    for code, m in meds.items():
        code = code.lstrip("0").zfill(13) if len(code) > 13 else code
        row = shards[code[-2:]].get(code)
        if row:   # sold in the chains too: keep their name and prices, add the medicine facts
            row[8], row[9] = m["kind"], m["extra"]
            if not row[1]:
                row[1] = m["maker"]
        else:
            pr = m["price"]
            shards[code[-2:]][code] = [m["name"], m["maker"], m["size"], pr, pr, pr, 0, "", m["kind"], m["extra"]]
    os.makedirs(os.path.join(OUT, "p"), exist_ok=True)
    write_index({c: row for sh in shards.values() for c, row in sh.items()})
    total = 0
    for k, v in shards.items():
        total += len(v)
        with open(os.path.join(OUT, "p", f"{k}.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as fh:
        names = {}
        for folder in chains_seen:
            letter, title = CHAIN_INFO.get(folder, ("?", folder))
            names[letter] = title
        json.dump({"updated": datetime.date.today().isoformat(), "products": total, "medicines": len(meds),
                   "chains": names}, fh, ensure_ascii=False)
    print(f"Wrote {total} products from {len(chains_seen)} chains")


if __name__ == "__main__":
    main()
