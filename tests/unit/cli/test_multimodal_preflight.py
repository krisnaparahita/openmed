"""Tests for the offline ``openmed multimodal preflight`` command."""

from __future__ import annotations

import json
import os
import struct
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from openmed.cli import main_module, multimodal_preflight
from openmed.cli._output import CliError

SECRET_NAME = "jane-doe-mrn-4471"


def _png(width: int = 8, height: int = 6) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00" * ((width + 1) * height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _jpeg(width: int = 16, height: int = 12) -> bytes:
    sof = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xc0"
        + struct.pack(">H", len(sof) + 2)
        + sof
        + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
        + b"\x00" * 8
        + b"\xff\xd9"
    )


def _tiff(width: int = 10, height: int = 7) -> bytes:
    entries = [
        (256, 3, 1, width),
        (257, 3, 1, height),
        (262, 3, 1, 1),
        (277, 3, 1, 1),
    ]
    ifd = struct.pack("<H", len(entries))
    for tag, kind, count, value in entries:
        ifd += struct.pack("<HHIHH", tag, kind, count, value, 0)
    ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", 8) + ifd + b"\x00" * 32


def _pdf() -> bytes:
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


def _wav(seconds: int = 1, rate: int = 8000) -> bytes:
    data = b"\x00\x00" * rate * seconds
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


DICOM_PREFIX = b"\x00" * 128 + b"DICM" + b"\x00" * 64
ACCEPTED = {
    "a.png": _png(),
    "b.jpg": _jpeg(),
    "c.tif": _tiff(),
    "e.wav": _wav(),
}


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    args = main_module.build_parser().parse_args(["multimodal", "preflight", *argv])
    try:
        code = multimodal_preflight.run_from_args(args)
    except CliError as exc:
        return exc.exit_code, exc.code
    return code, capsys.readouterr().out


def _payload(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict:
    code, out = _run([*argv, "--json"], capsys)
    return json.loads(out)["data"] | {"exit": code}


def test_command_is_registered() -> None:
    args = main_module.build_parser().parse_args(
        ["multimodal", "preflight", "x", "--batch", "--max-files", "5"]
    )
    assert args.command_path == "multimodal preflight"
    assert args.batch is True
    assert args.max_files == 5
    assert args.limit_profile == "desktop"


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_synthetic_files_are_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    path = tmp_path / name
    path.write_bytes(ACCEPTED[name])

    data = _payload([str(path)], capsys)

    assert data["exit"] == 0
    assert data["summary"]["accept"] == 1
    asset = data["assets"][0]
    assert asset["status"] == "accept"
    assert asset["asset_id"] == "asset-0001"
    assert asset["findings"] == []
    assert asset["digest"]["byte_count"] == len(ACCEPTED[name])


def test_dicom_prefix_abstains_for_missing_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "scan.dcm"
    path.write_bytes(DICOM_PREFIX)

    data = _payload([str(path)], capsys)

    asset = data["assets"][0]
    assert data["exit"] == 1
    assert asset["status"] == "abstain"
    assert asset["media_type"]["detected"] == "application/dicom"
    checks = {f["check"] for f in asset["findings"]}
    assert "metadata" in checks
    assert checks <= {"metadata", "limits"}


def test_pdf_reports_page_count_but_abstains_on_unevaluable_pixel_limits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The shared preflight contract cannot evaluate pixel ceilings for PDFs, so
    # it fails closed; the CLI must surface that rather than force acceptance.
    path = tmp_path / "d.pdf"
    path.write_bytes(_pdf())

    data = _payload([str(path)], capsys)

    asset = data["assets"][0]
    assert asset["status"] == "abstain"
    assert asset["manifest"]["pages"] == 1
    assert {(f["check"], f["reason_code"]) for f in asset["findings"]} == {
        ("limits", "insufficient_metadata")
    }


def test_mismatched_and_unknown_content_abstain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "text.txt").write_bytes(b"not a media file at all")
    (tmp_path / "trunc.png").write_bytes(_png()[:12])

    data = _payload([str(tmp_path), "--batch"], capsys)

    assert data["summary"]["abstain"] == 2
    reasons = {
        (f["check"], f["reason_code"])
        for asset in data["assets"]
        for f in asset["findings"]
    }
    assert ("media_type", "unknown") in reasons


def test_oversized_file_is_rejected_by_limits_without_hashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from openmed.multimodal import asset_limits

    small = replace(asset_limits.DESKTOP_V1, name="tiny", max_byte_size=40)
    monkeypatch.setattr(asset_limits, "DESKTOP_V1", small)
    path = tmp_path / "big.png"
    path.write_bytes(_png(64, 64))

    data = _payload([str(path)], capsys)

    asset = data["assets"][0]
    assert asset["status"] == "abstain"
    assert ("limits", "limit_exceeded") in {
        (f["check"], f["reason_code"]) for f in asset["findings"]
    }


def test_default_output_has_no_names_paths_or_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / SECRET_NAME
    folder.mkdir()
    (folder / f"{SECRET_NAME}.png").write_bytes(_png())
    (folder / "x.txt").write_bytes(b"patient " + SECRET_NAME.encode())

    code, text_out = _run([str(folder), "--batch"], capsys)
    _, json_out = _run([str(folder), "--batch", "--json"], capsys)

    for out in (text_out, json_out):
        assert SECRET_NAME not in out
        assert str(tmp_path) not in out
        assert "patient" not in out
    assert code == 1
    assert text_out.splitlines()[0].startswith("preflight[desktop]: 2 asset(s)")


def test_directory_requires_opt_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.png").write_bytes(_png())

    assert _run([str(tmp_path)], capsys) == (2, "batch_required")


def test_directory_order_is_stable_and_recursive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "z.png").write_bytes(_png(9, 9))
    (tmp_path / "b.png").write_bytes(_png(3, 3))
    (tmp_path / "a.png").write_bytes(_png(2, 2))

    first = _payload([str(tmp_path), "--batch"], capsys)
    second = _payload([str(tmp_path), "--batch"], capsys)

    assert first == second
    sizes = [a["manifest"]["width"] for a in first["assets"]]
    assert sizes == [2, 3, 9]
    assert [a["asset_id"] for a in first["assets"]] == [
        "asset-0001",
        "asset-0002",
        "asset-0003",
    ]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinks_are_never_followed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(_png())
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.png").write_bytes(_png())
    (root / "link.png").symlink_to(outside / "secret.png")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    data = _payload([str(root), "--batch"], capsys)

    assert data["summary"]["total"] == 1
    assert data["summary"]["skipped"] == 2
    assert data["exit"] == 1
    assert _run([str(root / "link.png")], capsys) == (2, "symlink_refused")


def test_batch_continues_across_file_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.png").write_bytes(_png())
    (tmp_path / "b.png").write_bytes(b"")
    unreadable = tmp_path / "c.png"
    unreadable.write_bytes(_png())
    unreadable.chmod(0)
    (tmp_path / "d.png").write_bytes(_png())

    data = _payload([str(tmp_path), "--batch"], capsys)
    unreadable.chmod(0o600)

    statuses = [a["status"] for a in data["assets"]]
    assert statuses[0] == "accept"
    assert statuses[-1] == "accept"
    assert data["assets"][1] == {
        "asset_id": "asset-0002",
        "status": "error",
        "error": "empty_file",
    }
    assert data["summary"]["error"] >= 1
    assert data["exit"] == 1


def test_batch_is_bounded_by_file_count_and_depth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.png").write_bytes(_png())
    deep = tmp_path / "d1" / "d2"
    deep.mkdir(parents=True)
    (deep / "deep.png").write_bytes(_png())

    capped = _payload([str(tmp_path), "--batch", "--max-files", "3"], capsys)
    shallow = _payload([str(tmp_path), "--batch", "--max-depth", "1"], capsys)

    assert capped["summary"]["total"] == 3
    assert capped["summary"]["truncated"] is True
    assert shallow["summary"]["total"] == 5
    assert shallow["summary"]["skipped"] == 1


@pytest.mark.parametrize(
    "flag", [["--max-files", "0"], ["--max-files", "10001"], ["--max-depth", "33"]]
)
def test_invalid_bounds_are_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: list[str]
) -> None:
    (tmp_path / "a.png").write_bytes(_png())

    assert _run([str(tmp_path), "--batch", *flag], capsys) == (2, "invalid_bound")


def test_missing_path_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run([str(tmp_path / "nope")], capsys) == (1, "input_not_found")


def test_files_are_not_mutated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(_png())
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    _run([str(path)], capsys)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png"]


def test_fifo_is_skipped_without_blocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs mkfifo")
    os.mkfifo(tmp_path / "pipe.png")
    (tmp_path / "a.png").write_bytes(_png())

    data = _payload([str(tmp_path), "--batch"], capsys)

    assert data["summary"]["total"] == 1
    assert data["summary"]["skipped"] == 1


def test_standalone_main_matches_group_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(_png())

    assert multimodal_preflight.main([str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
