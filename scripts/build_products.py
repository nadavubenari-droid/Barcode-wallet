"""
Builds a barcode -> product table from the price lists Israeli supermarket chains
must publish by law (חוק שקיפות מחירים), for the Barcode Wallet app.

Output (in ./out):
  p/<last two digits of the barcode>.json  {"<barcode>": [name, maker, size, min, max, median, chains]}
  meta.json                                 {"updated": ..., "products": ..., "chains": [...]}
"""
import collections, datetime, gzip, io, json, os, re, statistics, sys, zipfile

from lxml import etree

CHAINS = ["SHUFERSAL", "RAMI_LEVY", "YOHANANOF", "OSHER_AD", "TIV_TAAM", "KING_STORE",
          "KESHET", "SUPER_YUDA", "STOP_MARKET", "POLIZER", "DOR_ALON", "SALACH_DABACH"]
FILES_PER_CHAIN = int(os.environ.get("FILES_PER_CHAIN", "6"))
DUMPS = "dumps"
OUT = "out"


def download():
    from il_supermarket_scarper import ScarpingTask
    task = ScarpingTask(
        enabled_scrapers=CHAINS,
        files_types=["PRICE_FULL_FILE"],
        multiprocessing=4,
        output_configuration={"output_mode": "disk", "base_storage_path": DUMPS},
        status_configuration={"database_type": "json", "base_path": os.path.join(DUMPS, "status")},
        timeout_in_seconds=60 * 25,
    )
    task.start(limit=FILES_PER_CHAIN)
    task.join()


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
        if maker and not re.search(r"לא ידוע|כללי|unknown", maker):
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
        rec["chains"].add(chain)
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
    if not acc:
        print("No products parsed")
        sys.exit(1)
    shards = collections.defaultdict(dict)
    for code, r in acc.items():
        if not r["names"]:
            continue
        pr = sorted(r["prices"])
        shards[code[-2:]][code] = [
            r["names"].most_common(1)[0][0],
            r["makers"].most_common(1)[0][0] if r["makers"] else "",
            r["sizes"].most_common(1)[0][0] if r["sizes"] else "",
            round(pr[0], 2) if pr else None,
            round(pr[-1], 2) if pr else None,
            round(statistics.median(pr), 2) if pr else None,
            len(r["chains"]),
        ]
    os.makedirs(os.path.join(OUT, "p"), exist_ok=True)
    total = 0
    for k, v in shards.items():
        total += len(v)
        with open(os.path.join(OUT, "p", f"{k}.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"updated": datetime.date.today().isoformat(), "products": total, "chains": sorted(chains_seen)}, fh, ensure_ascii=False)
    print(f"Wrote {total} products from {len(chains_seen)} chains")


if __name__ == "__main__":
    main()
