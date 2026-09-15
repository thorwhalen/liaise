"""Regenerate ``liaise/data/confusables.json`` from Unicode's confusables data (UTS #39).

The file keeps every confusable whose source is one non-ASCII code point and whose
prototype is one ASCII letter or digit, grouped by prototype, with Unicode's copyright and
permission notice (Unicode License v3), which the licence requires to travel with copies.
:mod:`liaise.detect` folds those sources to their prototype before matching terms.

Usage, from the repository root::

    python misc/scripts/make_confusables.py                      # downloads the latest
    python misc/scripts/make_confusables.py confusables.txt license.txt
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen

CONFUSABLES_URL = "https://www.unicode.org/Public/security/latest/confusables.txt"
LICENSE_URL = "https://www.unicode.org/license.txt"
OUTPUT = Path(__file__).resolve().parents[2] / "liaise" / "data" / "confusables.json"


def _read(source: str) -> str:
    if source.startswith("https://"):
        with urlopen(source) as response:
            return response.read().decode("utf-8-sig")
    return Path(source).read_text(encoding="utf-8-sig")


def _header_value(text: str, field: str) -> str:
    prefix = f"# {field}:"
    line = next(line for line in text.splitlines() if line.startswith(prefix))
    return line[len(prefix) :].strip()


def ascii_prototypes(confusables: str) -> dict[str, str]:
    """``{prototype: sources}`` for every one-to-one, non-ASCII to ASCII-alphanumeric row."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for line in confusables.splitlines():
        fields = line.split("#", 1)[0].split(";")
        if len(fields) < 2:
            continue
        source = [chr(int(code, 16)) for code in fields[0].split()]
        target = [chr(int(code, 16)) for code in fields[1].split()]
        if len(source) == 1 and len(target) == 1:
            (char,), (prototype,) = source, target
            if not char.isascii() and prototype.isascii() and prototype.isalnum():
                grouped[prototype].append(char)
    return {
        prototype: "".join(sorted(grouped[prototype])) for prototype in sorted(grouped)
    }


def main(
    confusables_source: str = CONFUSABLES_URL, license_source: str = LICENSE_URL
) -> None:
    confusables = _read(confusables_source)
    record = {
        "source": CONFUSABLES_URL,
        "version": _header_value(confusables, "Version"),
        "date": _header_value(confusables, "Date"),
        "selection": "rows mapping one non-ASCII code point to one ASCII letter or digit",
        "notice": _read(license_source),
        "prototypes": ascii_prototypes(confusables),
    }
    OUTPUT.write_text(
        json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {sum(map(len, record['prototypes'].values()))} sources to {OUTPUT.name}"
    )


if __name__ == "__main__":
    main(*sys.argv[1:3])
