#!/usr/bin/env python3
"""Check and publish the community preset catalog (GoldenNugget-Presets).

    python tools/preset_catalog.py check CATALOG_DIR [--changed-since SHA] [--summary FILE]
    python tools/preset_catalog.py build CATALOG_DIR OUTPUT_FILE

``check`` validates every ``presets/*.gnmpreset`` and ``templates/*.batter`` with
the same rules the app uses. With ``--changed-since`` it also fails when the
change touches anything other than preset or template files; the catalog's pull
request workflow passes it unless the author is on the team. ``--summary``
writes a Markdown table with each preset's risk level and every file each
template's tweaks write, for the pull request page.

``build`` writes the index.json that GitHub Pages serves to the app and copies
the templates into ``templates/`` next to it. Preset entries in the index keep
the existing share-code format for app compatibility, generated from the
``.gnmpreset`` source files.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Allow running from a GoldenNugget checkout: tools/preset_catalog.py CATALOG ...
# or with GN source next to/above as in CI (catalog checked out separately).
GN_CANDIDATES = [ROOT / "gn", ROOT.parent / "gn", ROOT]
for base in GN_CANDIDATES:
    for p in (base, base / "src" / "qt"):
        if p.exists() and str(p) not in sys.path:
            sys.path.insert(0, str(p))

MAX_FILE_BYTES = 256 * 1024
PRESET_PATH = re.compile(r"^presets/[^/]+\.gnmpreset$")
TEMPLATE_PATH = re.compile(r"^templates/[^/]+\.batter$")
BATTER_BUILDER_URL = "https://github.com/GoldensHQ/GoldenNugget-TemplateBuilder/releases/latest"

MERGED_V3_KEY = "Preset Version 3 (YAML)"
MERGED_V2_KEY = "Preset Version 2 (JSON)"


def _literal_block(text: str, key: str) -> str:
    """Return the dedented contents of a top-level YAML literal block."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(key + ":"):
            continue
        block = []
        for following in lines[index + 1:]:
            if following and not following[0].isspace():
                break
            block.append(following)
        indent = min((len(item) - len(item.lstrip(" ")) for item in block if item.strip()),
                     default=0)
        return "\n".join(item[indent:] if item.strip() else "" for item in block).strip("\n")
    raise ValueError(f"Missing {key} block.")


def load_gnmpreset(path: Path) -> dict:
    """Read the version 2 preset carried by a merged .gnmpreset export.

    GoldenNugget writes both a version 3 YAML block and the version 2 JSON
    dictionary the app still uses. It has no version 3 reader, so the catalog
    reads the JSON block directly: no YAML dependency and no GUI import.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "HEADER:":
        raise ValueError("Not a merged .gnmpreset file.")
    header = {}
    for line in lines[1:]:
        if line and not line[0].isspace():
            break
        key, separator, value = line.strip().partition(":")
        if separator:
            header[key] = value.strip()
    includes = [part.strip() for part in header.get("Includes", "").split(",")]
    if header.get("PresetVersion") != "3" or "2" not in includes:
        raise ValueError("Not a merged version 3 and version 2 preset file.")
    if not _literal_block(text, MERGED_V3_KEY):
        raise ValueError("The version 3 preset block is empty.")
    try:
        data = json.loads(_literal_block(text, MERGED_V2_KEY))
    except ValueError as error:
        raise ValueError("The version 2 preset block is not valid JSON.") from error
    if not isinstance(data, dict):
        raise ValueError("The version 2 preset block is not a preset dictionary.")
    return data


def _entry_from_gnmpreset(path: Path, preset_catalog, share_code, valid_structure):
    data = load_gnmpreset(path)
    if not valid_structure(data):
        raise preset_catalog.CatalogError("Invalid preset structure.")
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    raw = {
        "format": 1,
        "name": meta.get("name") or path.stem,
        "author": meta.get("author", ""),
        "description": meta.get("description", ""),
        "ios_min": meta.get("ios_min", ""),
        "ios_max": meta.get("ios_max", ""),
        "tags": meta.get("tags", []),
        "untested": bool(meta.get("untested", False)),
        "code": share_code.encode(data.get("tweaks", {})),
    }
    return preset_catalog.validate_entry(raw, path.stem, valid_structure)


def load_entries(catalog: Path, changed=frozenset()):
    """Return (entries, errors) where errors are (path, message) pairs."""
    from src.controllers import preset_catalog, share_code
    from src.controllers.preset_codec import valid_import_structure
    entries, errors, codes = [], [], {}
    paths = sorted((catalog / "presets").glob("*.gnmpreset"))
    for path in sorted(paths, key=lambda item: item.relative_to(catalog).as_posix() in changed):
        relative = path.relative_to(catalog).as_posix()
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise preset_catalog.CatalogError("The file is larger than 256 KB.")
            entry = _entry_from_gnmpreset(path, preset_catalog, share_code, valid_import_structure)
        except Exception as error:
            message = str(error) if isinstance(error, (preset_catalog.CatalogError, share_code.ShareCodeError)) else f"Not a valid .gnmpreset: {error}"
            errors.append((relative, message))
            continue
        if entry.code in codes:
            errors.append((relative, f"The same share code is already in {codes[entry.code]}."))
            continue
        codes[entry.code] = relative
        entries.append((relative, entry))
    return entries, errors


def load_templates(catalog: Path, changed=frozenset()):
    """Return (entries, errors) for ``templates/*.batter``, like load_entries."""
    from src.controllers import preset_catalog
    from src.controllers.wallpaper_validation import validate_wallpaper
    entries, errors, digests = [], [], {}
    paths = sorted((catalog / "templates").glob("*.batter"))
    for path in sorted(paths, key=lambda item: item.relative_to(catalog).as_posix() in changed):
        relative = path.relative_to(catalog).as_posix()
        try:
            if path.is_symlink() or path.stat().st_size > preset_catalog.MAX_TEMPLATE_BYTES:
                raise preset_catalog.CatalogError("The template is a link or larger than 25 MB.")
            problems = [item for item in validate_wallpaper(path)["issues"] if item["level"] == "error"]
            if problems:
                raise preset_catalog.CatalogError("; ".join(
                    f"{item['path'] or path.name}: {item['message']}" for item in problems[:5]))
            entry = preset_catalog.template_entry(path, path.stem)
        except Exception as error:
            errors.append((relative, str(error) or type(error).__name__))
            continue
        if entry.sha256 in digests:
            errors.append((relative, f"The same template is already in {digests[entry.sha256]}."))
            continue
        digests[entry.sha256] = relative
        entries.append((relative, entry))
    return entries, errors


def changed_paths(catalog: Path, base: str):
    output = subprocess.run(
        ["git", "-C", str(catalog), "diff", "--name-status", "-z", "--no-renames", f"{base}...HEAD"],
        check=True, capture_output=True).stdout.decode("utf-8", "replace")
    fields = [field for field in output.split("\0") if field]
    return list(zip(fields[0::2], fields[1::2]))


def change_errors(changes):
    errors = []
    for status, path in changes:
        if not PRESET_PATH.match(path) and not TEMPLATE_PATH.match(path):
            errors.append((path, "Community pull requests may only add or edit files in presets/ "
                                 "and templates/*.batter."))
        elif status == "D":
            errors.append((path, "Only maintainers remove presets and templates."))
    return errors


def risk_rows(entries):
    from src.controllers import share_code
    from src.controllers.risk_assessment import assess
    rows = []
    for relative, entry in entries:
        report = assess(share_code.decode(entry.code)["tweaks"], entry.ios_min)
        reasons = "; ".join(factor.text for factor in report.sorted_factors()[:3])
        rows.append((relative, entry, report.level.label(), reasons))
    return rows


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def _size(size):
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{max(1, round(size / 1024))} KB"


def summary_markdown(rows, errors, only=None, templates=()):
    lines = ["## Preset check", ""]
    if errors:
        lines += ["**Problems:**", ""] + [f"- `{path}`: {_cell(message)}" for path, message in errors] + [""]
        if any(TEMPLATE_PATH.match(path) for path, _message in errors):
            lines += [f"> To fix a template, open it in [Batter Builder]({BATTER_BUILDER_URL}): its Check "
                      "page lists every problem with the tweak it belongs to, and Save writes the fixed file.",
                      ""]
    shown = [row for row in rows if only is None or row[0] in only]
    if shown:
        lines += ["| File | Name | Author | iOS | Tweaks | Risk | Why |", "|---|---|---|---|---|---|---|"]
        for relative, entry, level, reasons in shown:
            untested = " ⚠ not tested on a device" if entry.untested else ""
            lines.append(f"| `{relative}` | {_cell(entry.name)}{untested} | {_cell(entry.author)} | "
                         f"{_cell(entry.ios_range() or 'any')} | {entry.tweak_count} | {level} | {_cell(reasons)} |")
        lines.append("")
    shown_templates = [item for item in templates if only is None or item[0] in only]
    for relative, entry in shown_templates:
        lines += [f"### Template `{relative}`", "",
                  f"**{_cell(entry.name)}** by {_cell(entry.author)} · iOS {_cell(entry.ios_range() or 'any')} · "
                  + _size(entry.size) + (f" · restores to `{_cell(entry.domain)}`" if entry.domain else "")
                  + (" · ⚠ not tested on a device" if entry.untested else ""),
                  ""]
        if entry.writes:
            lines += ["| Tweak | Writes |", "|---|---|"]
            lines += [f"| {_cell(label)} | " + "<br>".join(f"`{_cell(path)}`" for path in paths) + " |"
                      for label, paths in entry.writes]
        else:
            lines.append("No tweaks; only the template's own files.")
        lines.append("")
    if not shown and not shown_templates and not errors:
        lines.append("No preset or template files changed.")
    return "\n".join(lines) + "\n"


def check(args):
    catalog = Path(args.catalog)
    only, change_problems = None, []
    if args.changed_since:
        changes = changed_paths(catalog, args.changed_since)
        change_problems = change_errors(changes)
        only = {path for status, path in changes if status != "D"}
    entries, errors = load_entries(catalog, only or frozenset())
    templates, template_errors = load_templates(catalog, only or frozenset())
    errors += template_errors + change_problems
    rows = risk_rows(entries)
    for path, message in errors:
        print(f"::error file={path}::{message}")
    summary = summary_markdown(rows, errors, only, templates)
    print(summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as file:
            file.write(summary)
    return 1 if errors else 0


def build(args):
    from src.controllers.preset_catalog import build_index
    catalog = Path(args.catalog)
    entries, errors = load_entries(catalog)
    templates, template_errors = load_templates(catalog)
    errors += template_errors
    for path, message in errors:
        print(f"::error file={path}::{message}")
    if errors:
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if templates:
        folder = output.parent / "templates"
        folder.mkdir(exist_ok=True)
        for relative, entry in templates:
            shutil.copyfile(catalog / relative, folder / entry.filename)
    index = build_index([entry for _, entry in entries], templates=[entry for _, entry in templates])
    output.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} preset(s) and {len(templates)} template(s) to {output.parent}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="Validate the catalog")
    check_parser.add_argument("catalog")
    check_parser.add_argument("--changed-since", help="Base commit; restrict the change to preset files")
    check_parser.add_argument("--summary", help="Append a Markdown report to this file")
    build_parser = commands.add_parser("build", help="Write index.json")
    build_parser.add_argument("catalog")
    build_parser.add_argument("output")
    args = parser.parse_args(argv)
    return check(args) if args.command == "check" else build(args)


if __name__ == "__main__":
    sys.exit(main())
