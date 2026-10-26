# Multimodal preflight CLI

`openmed multimodal preflight` runs the shared
[preflight report](preflight.md) over a local file, or an opt-in bounded
directory batch, **before** any optional decoder or model is loaded. It works
fully offline and never decodes, OCRs, transcribes, scans, or modifies a file.

```bash
openmed multimodal preflight scan.png
openmed multimodal preflight inbox/ --batch --max-files 200 --json
openmed multimodal preflight inbox/ --batch --limit-profile mobile
```

## How the manifest is built

The command derives each manifest from the bytes alone:

- the media type from `detect_media_type` over at most 132 leading bytes;
- `sha256` and `byte_size` from a streaming digest, bounded by the limit
  profile's byte ceiling (an over-ceiling file is never hashed and abstains on
  the `limits` check);
- width/height, PDF page count, or WAV duration from the bounded header readers
  (PNG/JPEG, TIFF, GIF, BMP, WebP, PDF geometry, WAV).

A header that cannot be read leaves its field out, so the profile reports it as
a finding and the asset abstains rather than being trusted. Media types with no
bounded header reader (a DICOM prefix) therefore abstain until their metadata is
supplied by a caller using the library API. The shared limit contract cannot
evaluate pixel ceilings for PDFs, so PDFs also abstain with
`limits:insufficient_metadata`; the CLI reports this and does not override it.
Content with no detectable supported type abstains with `media_type:unknown`.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `path` | | A file, or a directory when `--batch` is set. |
| `--batch` | off | Opt in to directory traversal. A directory without it is a usage error. |
| `--max-files N` | 1000 | Most files examined (1 to 10000). Extra files set `truncated`. |
| `--max-depth N` | 8 | Deepest directory level visited (1 to 32). |
| `--limit-profile` | `desktop` | `desktop` or `mobile` resource ceilings. |
| `--json` | off | Emit the standard `{ok, command, data}` envelope. |

## Traversal rules

- Symlinks (files or directories) are never followed; they are counted in
  `skipped`. A symlink given as the top-level path is refused. Files are opened
  with `O_NOFOLLOW` and must be regular files, so FIFOs and devices cannot
  block or leak.
- Entries are sorted bytewise at each level and visited depth-first, so order
  and `asset-NNNN` numbering do not depend on the filesystem.
- A failure on one file (unreadable, empty, malformed) becomes an `error` entry
  with a stable category and the batch continues.

## Output and privacy

Assets get opaque `asset-NNNN` identifiers. The payload contains only reason
codes, numeric facts, media-type names, and SHA-256 digests. It never contains
filenames, paths, file bytes, OCR or transcript text, metadata strings, or
credentials. Text mode prints one summary line and one line per asset:

```text
preflight[desktop]: 2 asset(s), 1 accept, 1 abstain, 0 error, 0 skipped
asset-0001 accept image/png
asset-0002 abstain application/pdf limits:insufficient_metadata
```

The exit status is `0` only when every asset is accepted and nothing was
skipped, truncated, or errored; otherwise `1`. Usage errors exit `2`.

`accept` means only that the shared contracts passed. It is not a claim that a
file is clinically valid, benign, or free of malware.
