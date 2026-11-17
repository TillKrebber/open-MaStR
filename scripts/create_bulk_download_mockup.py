"""One-time script to create a small bulk download mockup for testing.

Downloads wind, biomass and hydro data from the MaStR bulk download, trims each
XML table to at most MAX_RECORDS records, and writes the result as
tests/data/Gesamtdatenexport_mockup.zip.

Run this whenever the mockup needs to be regenerated from real data, e.g. after
a MaStR format change:

    python scripts/create_bulk_download_mockup.py
"""

import copy
import io
import math
import os
import random
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from multiprocessing import cpu_count
from pathlib import Path
from unittest.mock import patch

from lxml import etree

sys.path.insert(0, str(Path(__file__).parent.parent))
from open_mastr import Mastr
from open_mastr.utils.constants import BULK_DATA, BULK_INCLUDE_TABLES_MAP
from open_mastr.xml_download.utils_download_bulk import gen_version

MAX_RECORDS = 100  # should be dividable by four

OUTPUT_ZIP = (
    Path(__file__).parent.parent / "tests" / "data" / "Gesamtdatenexport_mockup.zip"
)

# Set to a directory path for debugging (directory is kept after the script finishes).
# Leave as None to use a temporary directory that is cleaned up by the OS once in a while.
DEBUG_DIR: str | None = "tmp/mastr-debug"

INCLUDED_TABLE_NAMES = {
    table for tech in BULK_DATA for table in BULK_INCLUDE_TABLES_MAP[tech]
} | {"katalogwerte"}


def get_table_base(filename: str) -> tuple[str, bool]:
    """Return (base_name, is_numbered) for an XML filename.

    'AnlagenEegSpeicher_3.xml' → ('AnlagenEegSpeicher', True)
    'EinheitenWind.xml'        → ('EinheitenWind', False)
    """
    stem = filename.rsplit(".", 1)[0]
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], True
    return stem, False


def build_xml_bytes(root_tag: str, records: list) -> bytes:
    """Serialize a list of lxml elements under root_tag as UTF-16 XML bytes."""
    new_root = etree.Element(root_tag)
    for rec in records:
        new_root.append(rec)
    buf = io.BytesIO()
    etree.ElementTree(new_root).write(buf, encoding="UTF-16", xml_declaration=True)
    return buf.getvalue()


def sample_records_streaming(
    zip_file: zipfile.ZipFile, fname: str, n: int
) -> tuple[str | None, list]:
    """Reservoir-sample n records from a zipped XML entry.

    This sampling is memory efficient, which is important since the XML files are
    large. See https://en.wikipedia.org/wiki/Reservoir_sampling.

    Uses iterparse + elem.clear() so only the sampled records and one element
    at a time are held in memory — the full tree is never built.
    """
    reservoir: list = []
    count = 0
    root_tag: str | None = None
    depth = 0

    with zip_file.open(fname) as raw_f:
        for event, elem in etree.iterparse(raw_f, events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    root_tag = elem.tag
            else:
                if depth == 2:
                    count += 1
                    rec = copy.deepcopy(elem)
                    if len(reservoir) < n:
                        reservoir.append(rec)
                    else:
                        j = random.randint(0, count - 1)
                        if j < n:
                            reservoir[j] = rec
                    elem.clear()
                depth -= 1

    return root_tag, reservoir


def gen_xml_download_url_mock(
    when: time.struct_time = time.localtime(), use_version="current", use_stichtag=False
) -> str:
    """Generate the download URL for the specified date.

    Note that not all dates are archived on the website.
    Normally only today is available, the export is usually made
    between 02:00 and 04:00, which means before 04:00 the current data may not
    yet be available and the download could fail.

    Note also that this function will not be able to generate URLs for dates
    before 2024 because a different URL scheme was used then which had some random
    data embedded in the name to make it harder to automate downloads.


    Args:
        when (time.struct_time, optional): Time object used to generate url.
        Defaults to time.localtime().
        use_version (str, optional): One of "current", "before", "after". "current"
        will generate the url for the expected MaStR version. "before" will generate
        the url for the previous MaStR version.
        "after" will generate the url for the subsequent MaStR version.

        "current": Gesamtdatenexport_20250403_25.1.zip
        "before": Gesamtdatenexport_20250403_24.2.zip
        "after": Gesamtdatenexport_20250403_25.2.zip

        Defaults to "current".
    """
    version = gen_version(when, use_version)
    date = time.strftime("%Y%m%d", when)

    if use_stichtag:
        url_str = f"https://mastr-download.regiocom.net/test/Stichtag/Gesamtdatenexport_{date}_{version}.zip"
    else:
        url_str = f"https://mastr-download.regiocom.net/test/Gesamtdatenexport_{date}_{version}.zip"

    return url_str


def make_mastr_nummer_unique(record: etree._Element, counter: int) -> None:
    # Make identifier fields unique
    for elem in record.iter():
        tag = etree.QName(elem).localname if isinstance(elem.tag, str) else ""
        if "MastrNummer" in tag and elem.text:
            # suffix, but keep the total length within field limits
            suffix = f"X{counter:04d}"
            elem.text = elem.text[: len(elem.text) - len(suffix)] + suffix


def main() -> None:
    recommended = min(cpu_count() - 1, 4)
    num_procs = input(
        f"Number of processes for parallelized XML parsing? "
        f"(Recommended max: {recommended}; Leave empty for no parallelization"
        " or enter number of processes: "
    )
    if num_procs.strip():
        os.environ["NUMBER_OF_PROCESSES"] = num_procs

    if DEBUG_DIR:
        Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
        tmpdir = DEBUG_DIR
    else:
        tmpdir = tempfile.mkdtemp()

    print(f"Downloading to {tmpdir} …")
    print("-------IMPORTANT NOTE-------")
    print(
        "After the zipped xml files were downloaded, you can abort the writing to the database."
    )
    print(
        "You can then comment out the mastr.download() line in this script and rerun this script."
    )
    print(
        "This works as the script only depends on the xml files being downloaded"
        " - it does not use the sqlite database."
    )
    print("-------IMPORTANT NOTE-------")
    mastr = Mastr(output_dir=tmpdir)
    with patch(
        "open_mastr.xml_download.utils_download_bulk.gen_xml_download_url",
        gen_xml_download_url_mock,
    ):
        mastr.download(data=BULK_DATA, date="20261001", bulk_cleansing=False)

    xml_dir = Path(tmpdir) / "data" / "xml_download"
    zips = sorted(xml_dir.glob("Gesamtdatenexport_*.zip"))
    if not zips:
        sys.exit(f"ERROR: no zip found in {xml_dir}")
    src_zip = zips[-1]
    print(f"Source zip: {src_zip}  ({src_zip.stat().st_size / 1024 / 1024:.1f} MB)")

    OUTPUT_ZIP.parent.mkdir(parents=True, exist_ok=True)

    with (
        zipfile.ZipFile(src_zip, "r") as src,
        zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        # categorise files by table base name
        table_groups: dict[str, list[str]] = defaultdict(list)
        katalogwerte_name: str | None = None
        for name in src.namelist():
            base, _ = get_table_base(name)
            if base.lower() == "katalogwerte":
                katalogwerte_name = name
            elif base.lower() in INCLUDED_TABLE_NAMES:
                table_groups[base].append(name)

        # add katalogwerte to output zip
        if katalogwerte_name:
            content = src.read(katalogwerte_name)
            dst.writestr(katalogwerte_name, content)
            print(f"  added {katalogwerte_name} ({len(content) / 1024:.1f} KB)")

        # sample records per file and write ≤4 files per table
        for base, files in table_groups.items():
            n_per_file = math.ceil(MAX_RECORDS / len(files))
            root_tag = None
            sampled = []
            for fname in files:
                file_root_tag, records = sample_records_streaming(
                    src, fname, n_per_file
                )
                if root_tag is None:
                    root_tag = file_root_tag
                sampled.extend(records)
            if root_tag is None:
                raise ValueError(
                    f"Found no XML records for {base}, cannot build a mockup file"
                )

            # checking the sample length and duplicating with changed mastr ID if not enough
            if len(sampled) < MAX_RECORDS:
                print(f"ALERT: Only {len(sampled)} records available for {base}")
                n_original = len(sampled)
                dup_counter = 0
                while len(sampled) < MAX_RECORDS:
                    original = sampled[dup_counter % n_original]
                    duplicate = copy.deepcopy(original)
                    make_mastr_nummer_unique(duplicate, dup_counter)
                    sampled.append(duplicate)
                    dup_counter += 1

            # make sure that there are really only MAX_RECORDS there
            sampled = sampled[:MAX_RECORDS]

            # Create one file in mock if only one file is available in zipped download
            if len(files) == 1:
                output_chunks = [(files[0], sampled)]
            # Else create exactly four files in mock
            else:
                a = int(MAX_RECORDS / 4)
                output_chunks = [
                    (f"{base}_1.xml", sampled[:a]),
                    (f"{base}_2.xml", sampled[a : 2 * a]),
                    (f"{base}_3.xml", sampled[2 * a : 3 * a]),
                    (f"{base}_4.xml", sampled[3 * a :]),
                ]

            for out_name, records in output_chunks:
                content = build_xml_bytes(root_tag, records)
                dst.writestr(out_name, content)
                print(f"  added {out_name} ({len(content) / 1024:.1f} KB)")

    size_kb = OUTPUT_ZIP.stat().st_size / 1024
    print(f"\nMockup written to {OUTPUT_ZIP}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
