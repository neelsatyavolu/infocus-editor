"""Media people attached to their announcement submissions (brief.submissions), and QR codes for cards. macOS only.

    python announcement_media.py fetch <brief.json> <showDir>/analysis/media
    python announcement_media.py read  <image>              # the QR codes in any image (a flyer, a filmed card still)
    python announcement_media.py make  <text or URL> <out.png>

- fetch: for each submission, follows its media link and the links in its text and notes. Images are saved as they
  are, a PDF's first page and a shared Google Slides/Docs file become a PNG, a shared Drive folder's files are each
  fetched, and every image is searched for QR codes. Writes media.json (`[{submission, items}]`, `submission` being the
  index in brief.submissions) and prints a summary. An item's `kind` is `image`, `page` (a web page, not a file),
  `private` (Drive asks to sign in) or `unreachable`; an image has `file`, `name`, `size`, `qr` ([{payload, box}]) and
  `qrOnly` (the image is just one QR code).
- make: a clean black-on-white QR code PNG (about 1200 px) for the card's `image`.
Stdlib only; QR reading and making use Apple Vision and Core Image (qr_codes.swift, compiled on first use).
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
QR_TOOL = os.path.join(SCRIPTS, "bin", "qr_codes")
MAX_BYTES = 40_000_000
MAX_FOLDER_FILES = 6
URL = re.compile(r"(?:https?://|\b(?:bit\.ly|forms\.gle|tinyurl\.com)/)[^\s<>\"')\]]+", re.I)
DRIVE_FILE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^#]*&)?id=)([\w-]+)")
DRIVE_FOLDER = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)")
GOOGLE_DOC = re.compile(r"docs\.google\.com/(presentation|document)/d/([\w-]+)")
FOLDER_FILE = re.compile(r"drive\.google\.com/file/d/([\w-]{20,})")
NOT_FETCHABLE = {"canva.com": "Canva", "www.canva.com": "Canva", "adobe.com": "Adobe Express", "new.express.adobe.com": "Adobe Express"}


def links_of(sub):
    """The submission's media link, then the links in its notes and text, without repeats."""
    found = []
    for field in ("mediaLink", "moreInfo", "text"):
        for raw in URL.findall(sub.get(field) or ""):
            link = raw.rstrip(".,;:!?").strip()
            link = link if re.match(r"https?://", link, re.I) else f"https://{link}"
            if link not in found:
                found.append(link)
    return found


def download_url(link):
    """Where a shared Google file downloads from (Slides and Docs as a PDF); any other link as it is."""
    if m := DRIVE_FILE.search(link):
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if m := GOOGLE_DOC.search(link):
        kind, doc = m.groups()
        return f"https://docs.google.com/presentation/d/{doc}/export/pdf" if kind == "presentation" \
            else f"https://docs.google.com/document/d/{doc}/export?format=pdf"
    return link


def folder_id(link):
    m = DRIVE_FOLDER.search(link)
    return m.group(1) if m else None


def folder_files(page):
    """File ids in a shared Drive folder's embedded view, in order."""
    ids = []
    for fid in FOLDER_FILE.findall(page):
        if fid not in ids:
            ids.append(fid)
    return ids[:MAX_FOLDER_FILES]


def http_fetch(url):
    """(content type, file name or None, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) InFocusEditor"})
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise OSError(f"larger than {MAX_BYTES // 1_000_000} MB")
        name = None
        if m := re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", res.headers.get("Content-Disposition") or "", re.I):
            name = urllib.request.unquote(m.group(1))
        return res.headers.get("Content-Type") or "", name, body


def sips_png(src, dest, max_size=None):
    """A PNG of an image or a PDF's first page, via macOS sips."""
    cmd = ["sips", "-s", "format", "png", *(["-Z", str(max_size)] if max_size else []), str(src), "--out", str(dest)]
    subprocess.run(cmd, check=True, capture_output=True)
    return dest


def fetch_link(link, out_dir, stem, fetch=http_fetch):
    """The media items behind one link (a shared folder can hold several)."""
    host = re.sub(r"^https?://", "", link, flags=re.I).split("/")[0].lower()
    if host in NOT_FETCHABLE:
        return [{"link": link, "kind": "unreachable", "note": f"A {NOT_FETCHABLE[host]} editor link; it needs a sign-in."}]
    if fid := folder_id(link):
        try:
            _, _, page = fetch(f"https://drive.google.com/embeddedfolderview?id={fid}")
        except OSError as e:
            return [{"link": link, "kind": "unreachable", "note": str(e)}]
        files = folder_files(page.decode("utf-8", "replace"))
        if not files:
            return [{"link": link, "kind": "private", "note": "A Drive folder with no files shared publicly."}]
        return [item for i, f in enumerate(files)
                for item in fetch_link(f"https://drive.google.com/file/d/{f}/view", out_dir, f"{stem}_{i}", fetch)]
    try:
        ctype, name, body = fetch(download_url(link))
    except OSError as e:
        if "google.com" in host and "404" in str(e):
            return [{"link": link, "kind": "private", "note": "Not shared publicly, or deleted (Google says not found)."}]
        return [{"link": link, "kind": "unreachable", "note": str(e)}]
    ctype = ctype.split(";")[0].strip().lower()
    if ctype == "text/html" or body[:15].lower().startswith((b"<!doctype html", b"<html")):
        if "google.com" in host and (b"Sign-in</title>" in body or b"accounts.google.com/ServiceLogin" in body):
            return [{"link": link, "kind": "private", "note": "Not shared publicly (Google asks to sign in)."}]
        return [{"link": link, "kind": "page"}]
    if not (ctype.startswith("image/") or ctype == "application/pdf"):
        return [{"link": link, "kind": "unreachable", "note": f"Not an image or PDF ({ctype or 'unknown type'})."}]
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}.get(ctype, "." + ctype.split("/")[-1])
    saved = out_dir / f"{stem}{ext}"
    saved.write_bytes(body)
    return [{"link": link, "kind": "image", "file": str(saved), "name": name or saved.name}]


def ensure_qr_tool():
    if not os.path.exists(QR_TOOL):
        os.makedirs(os.path.dirname(QR_TOOL), exist_ok=True)
        subprocess.run(["swiftc", "-O", os.path.join(SCRIPTS, "qr_codes.swift"), "-o", QR_TOOL], check=True, capture_output=True)
    return QR_TOOL


def read_qr(image):
    out = subprocess.run([ensure_qr_tool(), "read", str(image)], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        return []
    # Vision prints this diagnostic to stdout on macOS VMs before its valid JSON.
    diagnostic = "E5RT encountered an STL exception. msg = On-device compilation within a VM only supports CPU currently.."
    return json.loads(out.stdout.strip().removeprefix(diagnostic))


def make_qr(text, out):
    subprocess.run([ensure_qr_tool(), "make", text, str(out)], check=True, capture_output=True)
    return out


def image_size(image):
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(image)], capture_output=True, text=True).stdout
    return tuple(int(v) for v in re.findall(r"pixel(?:Width|Height): (\d+)", out))


def qr_only(codes, size):
    """One QR code covering most of the image: the file can go on the card as it is."""
    if len(codes) != 1 or len(size) != 2:
        return False
    x0, y0, x1, y1 = codes[0]["box"]
    return (x1 - x0) * (y1 - y0) >= 0.3 * size[0] * size[1]


def finish_image(item):
    """PDFs (first page), HEIC and the like to a PNG; then its QR codes and size."""
    src = Path(item["file"])
    if src.suffix not in (".png", ".jpg"):
        item["file"] = str(sips_png(src, src.with_suffix(".png")))
    item["qr"] = read_qr(item["file"])
    item["size"] = list(image_size(item["file"]))
    item["qrOnly"] = qr_only(item["qr"], item["size"])
    return item


def run(brief_path, out_dir, fetch=http_fetch):
    subs = json.loads(Path(brief_path).read_text()).get("submissions") or []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    media, lines = [], []
    for i, sub in enumerate(subs):
        items = [finish_image(item) if item["kind"] == "image" else item
                 for n, link in enumerate(links_of(sub)) for item in fetch_link(link, out_dir, f"s{i}_{n}", fetch)]
        media.append({"submission": i, "items": items})
        for item in items:
            what = item["kind"] if item["kind"] != "image" else f"image {item['file']} ({item['name']})"
            codes = ", ".join(c["payload"] for c in item.get("qr", [])) or "none"
            qr = f"; QR: {codes}{' (just the QR)' if item.get('qrOnly') else ''}" if item["kind"] == "image" else ""
            lines.append(f"submission {i}: {item['link']} -> {what}{qr}{'; ' + item['note'] if item.get('note') else ''}")
    (out_dir / "media.json").write_text(json.dumps(media, indent=2))
    if not subs:
        return "No submissions in the brief."
    return "\n".join(lines) or "No media or links in any submission."


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == "fetch":
        print(run(args[1], args[2]))
    elif len(args) == 2 and args[0] == "read":
        print(json.dumps(read_qr(args[1])))
    elif len(args) == 3 and args[0] == "make":
        print(make_qr(args[1], args[2]))
    else:
        sys.exit(__doc__)
