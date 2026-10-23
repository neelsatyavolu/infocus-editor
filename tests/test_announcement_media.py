"""Announcement media (scripts/announcement_media.py): links from submissions, Drive rewrites, and QR codes."""
import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import announcement_media as am  # noqa: E402

needs_swift = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="Vision/CoreImage need macOS")

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
SIGN_IN = b"<!doctype html><html><head><title>Google Drive: Sign-in</title></head></html>"


def test_links_take_the_media_link_first_then_urls_in_the_text():
    sub = {"text": "Sign up at bit.ly/paly-club or https://forms.gle/abc123.", "mediaLink": " https://drive.google.com/file/d/X/view ",
           "moreInfo": "Please put the QR on screen https://example.com/page"}
    assert am.links_of(sub) == ["https://drive.google.com/file/d/X/view", "https://example.com/page",
                                "https://bit.ly/paly-club", "https://forms.gle/abc123"]


def test_links_skip_blanks_and_duplicates():
    assert am.links_of({"text": "see https://a.com/x", "mediaLink": "https://a.com/x", "moreInfo": None}) == ["https://a.com/x"]
    assert am.links_of({"text": "No links here.", "mediaLink": "", "moreInfo": ""}) == []


@pytest.mark.parametrize("url, expected", [
    ("https://drive.google.com/file/d/1nkvCO64Tu-Q_y/view?usp=sharing", "https://drive.google.com/uc?export=download&id=1nkvCO64Tu-Q_y"),
    ("https://drive.google.com/open?id=ABC_def-1", "https://drive.google.com/uc?export=download&id=ABC_def-1"),
    ("https://docs.google.com/presentation/d/PRES1/edit#slide=id.p", "https://docs.google.com/presentation/d/PRES1/export/pdf"),
    ("https://docs.google.com/document/d/DOC1/edit", "https://docs.google.com/document/d/DOC1/export?format=pdf"),
    ("https://example.com/flyer.png", "https://example.com/flyer.png"),
])
def test_download_url_rewrites_google_links(url, expected):
    assert am.download_url(url) == expected


def test_folder_files_lists_the_files_of_a_shared_drive_folder():
    page = '<a href="https://drive.google.com/file/d/FILE_one-1234567890abc/view">a</a>' \
           '<a href="https://drive.google.com/file/d/FILE_two-1234567890abc/view">b</a>' \
           '<a href="https://drive.google.com/file/d/FILE_one-1234567890abc/view">a again</a>'
    assert am.folder_id("https://drive.google.com/drive/folders/FOLDER1?usp=share_link") == "FOLDER1"
    assert am.folder_id("https://drive.google.com/file/d/X/view") is None
    assert am.folder_files(page) == ["FILE_one-1234567890abc", "FILE_two-1234567890abc"]


def fake_fetch(responses):
    def fetch(url):
        if url not in responses:
            raise OSError("HTTP Error 404: Not Found")
        return responses[url]
    return fetch


def test_fetch_link_saves_images_and_reports_pages_and_private_files(tmp_path):
    fetch = fake_fetch({
        "https://drive.google.com/uc?export=download&id=IMG": ("image/png", "Club QR Code.png", PNG),
        "https://drive.google.com/uc?export=download&id=PRIV": ("text/html; charset=utf-8", None, SIGN_IN),
        "https://rsvp.example.com/x": ("text/html", None, b"<html><title>RSVP</title></html>"),
    })
    image = am.fetch_link("https://drive.google.com/file/d/IMG/view", tmp_path, "s0", fetch)
    assert image[0]["kind"] == "image" and Path(image[0]["file"]).read_bytes() == PNG
    assert image[0]["name"] == "Club QR Code.png"
    assert am.fetch_link("https://drive.google.com/file/d/PRIV/view", tmp_path, "s1", fetch) == [
        {"link": "https://drive.google.com/file/d/PRIV/view", "kind": "private",
         "note": "Not shared publicly (Google asks to sign in)."}]
    assert am.fetch_link("https://rsvp.example.com/x", tmp_path, "s2", fetch) == [
        {"link": "https://rsvp.example.com/x", "kind": "page"}]
    assert am.fetch_link("https://drive.google.com/file/d/GONE/view", tmp_path, "s3", fetch)[0]["kind"] == "private"
    gone = am.fetch_link("https://club.example.com/gone.png", tmp_path, "s4", fetch)
    assert gone[0]["kind"] == "unreachable" and "404" in gone[0]["note"]


def test_a_sign_in_page_far_into_the_body_is_still_private(tmp_path):
    page = b"<!doctype html>" + b" " * 800_000 + b"<title>Google Drive: Sign-in</title>"
    fetch = fake_fetch({"https://drive.google.com/uc?export=download&id=P": ("text/html", None, page)})
    assert am.fetch_link("https://drive.google.com/file/d/P/view", tmp_path, "s0", fetch)[0]["kind"] == "private"


def test_fetch_link_opens_a_shared_folder(tmp_path):
    folder = '<a href="https://drive.google.com/file/d/IMGINFOLDER_123456789/view">qr.png</a>'
    fetch = fake_fetch({
        "https://drive.google.com/embeddedfolderview?id=FOLD": ("text/html", None, folder.encode()),
        "https://drive.google.com/uc?export=download&id=IMGINFOLDER_123456789": ("image/png", "qr.png", PNG),
    })
    items = am.fetch_link("https://drive.google.com/drive/folders/FOLD?usp=share_link", tmp_path, "s0", fetch)
    assert [(i["kind"], i["name"]) for i in items] == [("image", "qr.png")]


def test_canva_and_other_editor_links_are_not_fetchable_media(tmp_path):
    items = am.fetch_link("https://www.canva.com/design/DAH/xyz/edit", tmp_path, "s0", fake_fetch({}))
    assert items[0]["kind"] == "unreachable" and "Canva" in items[0]["note"]


def test_qr_only_means_one_code_filling_the_image():
    assert am.qr_only([{"payload": "x", "box": [300, 300, 2700, 2700]}], (3000, 3000))
    assert not am.qr_only([{"payload": "x", "box": [818, 1291, 1031, 1503]}], (2550, 3300))
    assert not am.qr_only([{"payload": "x", "box": [0, 0, 900, 900]}, {"payload": "y", "box": [0, 0, 9, 9]}], (1000, 1000))


@needs_swift
def test_make_then_read_round_trips(tmp_path):
    out = tmp_path / "qr.png"
    am.make_qr("https://forms.gle/MyaGEmdMPEzj7ADm6", out)
    codes = am.read_qr(out)
    assert [c["payload"] for c in codes] == ["https://forms.gle/MyaGEmdMPEzj7ADm6"]


@needs_swift
def test_run_writes_media_json_per_submission(tmp_path):
    qr = tmp_path / "src.png"
    am.make_qr("https://forms.gle/abc", qr)
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"submissions": [
        {"text": "Chess club today in room 100.", "mediaLink": None, "moreInfo": None},
        {"text": "Scan the QR to join!", "mediaLink": "https://drive.google.com/file/d/QR/view", "moreInfo": None},
    ]}))
    fetch = fake_fetch({"https://drive.google.com/uc?export=download&id=QR": ("image/png", "join.png", qr.read_bytes())})
    out = tmp_path / "media"
    summary = am.run(brief, out, fetch)
    media = json.loads((out / "media.json").read_text())
    assert media[0] == {"submission": 0, "items": []}
    item = media[1]["items"][0]
    assert item["kind"] == "image" and item["qrOnly"] is True
    assert [c["payload"] for c in item["qr"]] == ["https://forms.gle/abc"]
    assert "https://forms.gle/abc" in summary


def test_run_says_so_when_the_brief_has_no_submissions(tmp_path):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"announcements": ["x"]}))
    assert "no submissions" in am.run(brief, tmp_path / "media", fake_fetch({})).lower()
    assert json.loads((tmp_path / "media" / "media.json").read_text()) == []
