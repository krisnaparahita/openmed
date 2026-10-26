"""Offline ``openmed multimodal preflight`` command.

Runs :func:`openmed.multimodal.preflight.preflight_asset` over one file or an
opt-in, bounded directory batch before any optional decoder or model is loaded.
The manifest is derived from the bytes themselves (bounded media-type
detection, a streaming SHA-256, and bounded header readers), so nothing is
decoded and nothing is mutated.

Output is privacy-safe by default: assets get opaque ``asset-NNNN`` identifiers
in stable traversal order, and the payload carries only reason codes, numeric
facts, and digests -- never filenames, paths, file bytes, OCR or transcript
text, metadata strings, or credentials.
"""

from __future__ import annotations

import argparse
import errno
import os
import stat
import sys
from pathlib import Path
from typing import Any, BinaryIO, Sequence, TextIO

from ._output import EXIT_ERROR, EXIT_USAGE, CliError, emit, emit_error

SCHEMA_VERSION = 1
DEFAULT_MAX_FILES = 1000
HARD_MAX_FILES = 10_000
DEFAULT_MAX_DEPTH = 8
HARD_MAX_DEPTH = 32

_LIMIT_PROFILES = ("desktop", "mobile")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def add_multimodal_command(subparsers: "argparse._SubParsersAction") -> None:
    """Register the ``openmed multimodal`` command group."""
    parser = subparsers.add_parser(
        "multimodal",
        help="Offline multimodal asset tooling.",
    )
    commands = parser.add_subparsers(dest="multimodal_command")
    preflight = commands.add_parser(
        "preflight",
        help="Check a file or bounded directory batch before decoding anything.",
    )
    _add_preflight_arguments(preflight)
    preflight.set_defaults(handler=run_from_args)


def _add_preflight_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path",
        type=Path,
        help="A file, or a directory when --batch is given.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Opt in to directory traversal (symlinks are never followed).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=f"Most files examined in a batch (default {DEFAULT_MAX_FILES}, "
        f"at most {HARD_MAX_FILES}).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"Deepest subdirectory level visited (default {DEFAULT_MAX_DEPTH}, "
        f"at most {HARD_MAX_DEPTH}).",
    )
    parser.add_argument(
        "--limit-profile",
        choices=_LIMIT_PROFILES,
        default="desktop",
        help="Resource ceiling profile (default desktop).",
    )


def run_from_args(
    args: argparse.Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run preflight; exit 1 when any asset abstains or errors."""
    stdout = stdout or sys.stdout
    _validate_bounds(args)
    path: Path = args.path
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        raise CliError(
            "Input path not found or unreadable.",
            code="input_not_found",
            exit_code=EXIT_ERROR,
        ) from None
    if stat.S_ISLNK(mode):
        raise CliError(
            "Input path is a symlink; symlinks are never followed.",
            code="symlink_refused",
            exit_code=EXIT_USAGE,
        )
    if stat.S_ISDIR(mode):
        if not args.batch:
            raise CliError(
                "Input is a directory; pass --batch to opt in to traversal.",
                code="batch_required",
                exit_code=EXIT_USAGE,
            )
        files, skipped, truncated = _walk(path, args.max_files, args.max_depth)
    elif stat.S_ISREG(mode):
        files, skipped, truncated = [path], 0, False
    else:
        raise CliError(
            "Input is not a regular file or directory.",
            code="unsupported_input",
            exit_code=EXIT_USAGE,
        )

    payload = build_payload(
        files, args.limit_profile, skipped=skipped, truncated=truncated
    )
    summary = payload["summary"]
    emit(args, payload, human=format_text(payload), stream=stdout)
    clean = (
        summary["abstain"] == 0
        and summary["error"] == 0
        and summary["skipped"] == 0
        and not summary["truncated"]
    )
    return 0 if clean else EXIT_ERROR


def _validate_bounds(args: argparse.Namespace) -> None:
    for name, hard in (("max_files", HARD_MAX_FILES), ("max_depth", HARD_MAX_DEPTH)):
        value = getattr(args, name)
        if not 1 <= value <= hard:
            flag = "--" + name.replace("_", "-")
            raise CliError(
                f"{flag} must be between 1 and {hard}.",
                code="invalid_bound",
                exit_code=EXIT_USAGE,
            )


def _walk(root: Path, max_files: int, max_depth: int) -> tuple[list[Path], int, bool]:
    """Collect regular files in stable order without following symlinks.

    Entries are sorted bytewise at every level and visited depth-first, so the
    order is independent of the filesystem. ``skipped`` counts symlinks,
    special files, unreadable directories, and directories beyond
    ``max_depth``; ``truncated`` is set when more than ``max_files`` regular
    files exist.
    """
    files: list[Path] = []
    skipped = 0
    truncated = False

    def visit(directory: Path, depth: int) -> None:
        nonlocal skipped, truncated
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: os.fsencode(e.name))
        except OSError:
            skipped += 1
            return
        for entry in entries:
            if truncated:
                return
            try:
                if entry.is_symlink():
                    skipped += 1
                elif entry.is_dir(follow_symlinks=False):
                    if depth >= max_depth:
                        skipped += 1
                    else:
                        visit(Path(entry.path), depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    if len(files) >= max_files:
                        truncated = True
                        return
                    files.append(Path(entry.path))
                else:
                    skipped += 1
            except OSError:
                skipped += 1

    visit(root, 1)
    return files, skipped, truncated


def build_payload(
    files: Sequence[Path],
    limit_profile: str,
    *,
    skipped: int = 0,
    truncated: bool = False,
) -> dict[str, Any]:
    """Preflight ``files`` and return the deterministic, path-free payload."""
    assets = [
        _preflight_file(path, f"asset-{index:04d}", limit_profile)
        for index, path in enumerate(files, start=1)
    ]
    counts = {"accept": 0, "abstain": 0, "error": 0}
    for asset in assets:
        counts[asset["status"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "limit_profile": limit_profile,
        "summary": {
            "total": len(assets),
            **counts,
            "skipped": skipped,
            "truncated": truncated,
        },
        "assets": assets,
    }


def _error_asset(asset_id: str, category: str) -> dict[str, Any]:
    return {"asset_id": asset_id, "status": "error", "error": category}


def _preflight_file(path: Path, asset_id: str, limit_profile: str) -> dict[str, Any]:
    # Every per-file failure is contained so one bad file never stops a batch.
    try:
        return _preflight_open(path, asset_id, limit_profile)
    except _FileError as exc:
        return _error_asset(asset_id, exc.category)
    except Exception:  # noqa: BLE001 - value-free category, never the message
        return _error_asset(asset_id, "preflight_failed")


class _FileError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _preflight_open(path: Path, asset_id: str, limit_profile: str) -> dict[str, Any]:
    from openmed.multimodal.asset_limits import DESKTOP_V1, MOBILE_V1
    from openmed.multimodal.digest import DigestLimitExceededError, digest_asset
    from openmed.multimodal.media_type import (
        MAX_MEDIA_TYPE_PREFIX_BYTES,
        detect_media_type,
    )
    from openmed.multimodal.preflight import PreflightError, preflight_asset

    profile = DESKTOP_V1 if limit_profile == "desktop" else MOBILE_V1
    stream = _open_regular(path)
    with stream:
        size = os.fstat(stream.fileno()).st_size
        prefix = stream.read(MAX_MEDIA_TYPE_PREFIX_BYTES)
        stream.seek(0)
        if size == 0:
            raise _FileError("empty_file")
        media_type = detect_media_type(prefix)
        if media_type is None:
            # No supported type to declare, so no manifest can be built: abstain
            # with the same content-free finding the preflight report uses.
            return _unknown_media_type(asset_id)
        try:
            sha256 = digest_asset(stream, max_bytes=profile.max_byte_size).sha256
        except DigestLimitExceededError:
            # Over the byte ceiling: never hash it; the limits check abstains.
            sha256 = "0" * 64
        stream.seek(0)
        manifest: dict[str, Any] = {
            "asset_id": asset_id,
            "media_type": media_type,
            "sha256": sha256,
            "byte_size": size,
        }
        manifest.update(_header_fields(stream, media_type, size))
        stream.seek(0)
        try:
            report = preflight_asset(manifest, stream, limit_profile=profile)
        except PreflightError:
            raise _FileError("preflight_failed") from None
    return {"asset_id": asset_id, **report.to_dict()}


def _unknown_media_type(asset_id: str) -> dict[str, Any]:
    from openmed.multimodal.preflight import PreflightFinding

    return {
        "asset_id": asset_id,
        "status": "abstain",
        "media_type": {"detected": None, "status": "unknown"},
        "findings": [PreflightFinding("media_type", "unknown").to_dict()],
    }


def _open_regular(path: Path) -> BinaryIO:
    """Open without following symlinks; refuse anything but a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise _FileError("symlink_refused") from None
        raise _FileError("unreadable") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _FileError("not_regular_file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _header_fields(stream: BinaryIO, media_type: str, size: int) -> dict[str, Any]:
    """Read bounded header facts for the manifest; failures yield no fields.

    A missing field is not an error here: the manifest profile reports it as a
    finding, so an unreadable header abstains instead of being trusted.
    """
    from openmed.multimodal.bmp_dimensions import read_bmp_dimensions
    from openmed.multimodal.gif_dimensions import read_gif_dimensions
    from openmed.multimodal.image_header import read_image_header
    from openmed.multimodal.pdf_geometry import read_pdf_geometry
    from openmed.multimodal.tiff_metadata import read_tiff_metadata
    from openmed.multimodal.wav_metadata import read_wav_metadata
    from openmed.multimodal.webp_dimensions import read_webp_dimensions

    dimension_readers = {
        "image/png": read_image_header,
        "image/jpeg": read_image_header,
        "image/tiff": read_tiff_metadata,
        "image/gif": read_gif_dimensions,
        "image/bmp": read_bmp_dimensions,
        "image/webp": read_webp_dimensions,
    }
    try:
        if media_type in dimension_readers:
            header = dimension_readers[media_type](stream)
            return {"width": header.width, "height": header.height}
        if media_type == "audio/wav":
            return {"duration_seconds": read_wav_metadata(stream).duration_seconds}
        if media_type == "application/pdf":
            report = read_pdf_geometry(stream)
            if report.page_count:
                return {"pages": report.page_count}
    except Exception:  # noqa: BLE001 - header failure is reported via findings
        pass
    return {}


def format_text(payload: dict[str, Any]) -> str:
    """Concise, deterministic text; identifiers and reason codes only."""
    summary = payload["summary"]
    lines = [
        f"preflight[{payload['limit_profile']}]: {summary['total']} asset(s), "
        f"{summary['accept']} accept, {summary['abstain']} abstain, "
        f"{summary['error']} error, {summary['skipped']} skipped"
        + (", truncated" if summary["truncated"] else "")
    ]
    for asset in payload["assets"]:
        if asset["status"] == "error":
            lines.append(f"{asset['asset_id']} error {asset['error']}")
            continue
        reasons = ",".join(
            dict.fromkeys(f"{f['check']}:{f['reason_code']}" for f in asset["findings"])
        )
        detected = (asset["media_type"] or {}).get("detected") or "unknown"
        lines.append(
            f"{asset['asset_id']} {asset['status']} {detected}"
            + (f" {reasons}" if reasons else "")
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point for ``python -m openmed.cli.multimodal_preflight``."""
    parser = argparse.ArgumentParser(
        prog="openmed multimodal preflight",
        description="Offline multimodal preflight for a file or bounded batch.",
    )
    _add_preflight_arguments(parser)
    parser.add_argument("--json", dest="json_output", action="store_true")
    parser.set_defaults(command_path="multimodal preflight")
    args = parser.parse_args(argv)
    try:
        return run_from_args(args)
    except CliError as exc:
        return emit_error(args, exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
