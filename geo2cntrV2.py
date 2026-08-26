#!/usr/bin/env python3
"""
geo2cntrV2.py - Split a GPX waypoint file into one file per country.
made by AI
Two modes:

  OFFLINE (default, recommended)
    Uses a local country-boundary dataset (Natural Earth style GeoJSON,
    ISO 3166-1 alpha-2 codes) and shapely's point-in-polygon test.
    No internet needed after the dataset is downloaded once, no rate
    limits, and it's fast (thousands of points/second).
    A point that falls outside every polygon (e.g. a GPS fix just
    offshore, in a harbour, or on a small island missing from the
    dataset) is assigned to the NEAREST country instead of being
    silently dropped.

  ONLINE (--online)
    Falls back to the original approach: reverse geocoding via
    OpenStreetMap Nominatim (geopy). Nominatim's usage policy allows at
    most 1 request/second, so this mode is rate-limited and retries
    automatically on HTTP 429 / timeouts. Nearby points are cached by
    a rounded lat/lon so you don't pay for the same country lookup
    over and over - this is the main reason the original script kept
    hitting 429s.

Usage:
    python3 geo2cntr.py file.gpx                 # offline, auto-downloads dataset
    python3 geo2cntr.py file.gpx --online         # use Nominatim instead
    python3 geo2cntr.py file.gpx --outdir split/  # choose output folder
    python3 geo2cntr.py file.gpx --dataset countries.geojson  # reuse a local copy
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.request
from collections import defaultdict

# ----------------------------------------------------------------------
# console helpers
# ----------------------------------------------------------------------


def Rprint(text):
    print("\033[37;41m" + text + "\033[0m")


# ----------------------------------------------------------------------
# GPX parsing (same approach as the original script: a regex that keeps
# each <wpt> block's inner content byte-for-byte, only lat/lon get
# rewritten). Assumes lat="" comes before lon="" in the tag, which is
# how OsmAnd / Garmin BaseCamp / Locus all write it.
# ----------------------------------------------------------------------

WPT_PATTERN = re.compile(
    r'<wpt[^>]*\blat="([^"]+)"\s+lon="([^"]+)"[^>]*>(.*?)</wpt>', re.DOTALL
)

# The original hardcoded header only declared xmlns:osmand/xsi. If the
# source file's <wpt> extensions use other prefixes (Garmin BaseCamp
# exports commonly add gpxx:, wptx1:, ctx:, trp: ...) the split files
# come out as invalid XML ("unbound prefix"). Instead, copy whatever
# xmlns declarations the *source* file's root <gpx> tag actually has.
GPX_ROOT_TAG_PATTERN = re.compile(r"<gpx\b[^>]*>", re.DOTALL)
XMLNS_ATTR_PATTERN = re.compile(r'xmlns(:[A-Za-z0-9_]+)?="[^"]*"')


DEFAULT_NAMESPACE_DECLS = (
    "xmlns='http://www.topografix.com/GPX/1/1' "
    "xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'"
)

# Well-known extension prefixes used by Garmin BaseCamp / devices. These
# are only used as a fallback when a source file uses a prefix in its
# <wpt> extensions but never actually declares it on the root <gpx> tag
# (which happens - e.g. the sample MZ.gpx from this conversation has
# this exact problem). Any URI makes the XML well-formed again; these
# happen to be the real ones for the common Garmin extensions.
KNOWN_NAMESPACE_URIS = {
    "gpxx": "http://www.garmin.com/xmlschemas/GpxExtensions/v3",
    "wptx1": "http://www.garmin.com/xmlschemas/WaypointExtension/v1",
    "gpxtpx": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
    "trp": "http://www.garmin.com/xmlschemas/TripExtensions/v1",
    "ctx": "http://www.garmin.com/xmlschemas/CreationTimeExtension/v1",
}
TAG_PREFIX_PATTERN = re.compile(r"</?([A-Za-z][A-Za-z0-9_]*):[A-Za-z]")


def extract_namespace_decls(gpx_text):
    root_match = GPX_ROOT_TAG_PATTERN.search(gpx_text)
    root_tag = root_match.group(0) if root_match else ""

    decls = [m.group(0) for m in XMLNS_ATTR_PATTERN.finditer(root_tag)]
    if not decls:
        decls = [DEFAULT_NAMESPACE_DECLS]
    declared_prefixes = {m.group(1) for m in XMLNS_ATTR_PATTERN.finditer(root_tag) if m.group(1)}

    # Patch up any prefix that's used in the body but never declared,
    # so the output is guaranteed to be valid XML even if the source
    # wasn't.
    used_prefixes = {m.group(1) for m in TAG_PREFIX_PATTERN.finditer(gpx_text)}
    for prefix in sorted(used_prefixes - declared_prefixes):
        uri = KNOWN_NAMESPACE_URIS.get(prefix, f"urn:geo2cntr:undeclared:{prefix}")
        decls.append(f'xmlns:{prefix}="{uri}"')

    return " ".join(decls)


def build_gpx_header(code, namespace_decls):
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
        f"<gpx version='1.1' creator='AI+M444' {namespace_decls}>\n"
        f"<metadata>\n<name>{code}</name>\n<author>\n<name>AI+M444</name>\n"
        "<link href='https://mariush444.github.io/Osmand-tools/'/>\n"
        "</author>\n</metadata>\n"
    )


def parse_waypoints(gpx_text):
    """Yield (lat, lon, wpt_block_string) for every <wpt> in the file."""
    for match in WPT_PATTERN.finditer(gpx_text):
        lat = float(match.group(1))
        lon = float(match.group(2))
        wpt_block = f'<wpt lat="{lat:.6f}" lon="{lon:.6f}">{match.group(3)}</wpt>'
        yield lat, lon, wpt_block


# ----------------------------------------------------------------------
# writing output files (unchanged behaviour: append to existing file
# in the output dir if one is already there from a previous run)
# ----------------------------------------------------------------------


def write_country_files(countries_data, outdir, namespace_decls):
    os.makedirs(outdir, exist_ok=True)
    for country_code, wpts in countries_data.items():
        gpx_path = os.path.join(outdir, f"{country_code}.gpx")

        if os.path.exists(gpx_path):
            with open(gpx_path, "r", encoding="utf-8") as f:
                file_text = f.read()
            file_text = file_text.rstrip("\n")
            if file_text.endswith("</gpx>"):
                file_text = file_text[: -len("</gpx>")]
        else:
            file_text = build_gpx_header(country_code, namespace_decls)

        file_text += "\n".join(wpts) + "\n"
        file_text += "</gpx>"

        with open(gpx_path, "w", encoding="utf-8") as f:
            f.write(file_text)
        print(f"{gpx_path} was created/updated ({len(wpts)} points).")


# ----------------------------------------------------------------------
# OFFLINE geocoder: shapely point-in-polygon against a local dataset
# ----------------------------------------------------------------------

DATASET_URL = (
    "https://raw.githubusercontent.com/datasets/geo-countries/main/"
    "data/countries.geojson"
)


def ensure_dataset(path):
    if os.path.exists(path):
        return path
    print(f"Country boundary dataset not found at {path}.")
    print(f"Downloading it once from:\n  {DATASET_URL}")
    print("(~14 MB - after this it works fully offline.)")
    tmp_path = path + ".part"
    try:
        urllib.request.urlretrieve(DATASET_URL, tmp_path)
        os.replace(tmp_path, path)
    except Exception as e:
        Rprint(f"Could not download the dataset: {e}")
        Rprint(
            "Download it manually and pass it with --dataset, e.g.:\n"
            f"  curl -L -o countries.geojson {DATASET_URL}"
        )
        sys.exit(1)
    return path


class OfflineCountryLookup:
    def __init__(self, dataset_path):
        from shapely.geometry import shape
        from shapely.strtree import STRtree

        with open(dataset_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self._geoms = []
        self._codes = []
        for feat in data["features"]:
            code = feat["properties"].get("ISO3166-1-Alpha-2")
            if not code:
                continue
            self._geoms.append(shape(feat["geometry"]))
            self._codes.append(code.upper())

        self._tree = STRtree(self._geoms)
        self._shape_cls = shape

    def lookup(self, lat, lon):
        from shapely.geometry import Point

        pt = Point(lon, lat)
        for idx in self._tree.query(pt):
            if self._geoms[idx].contains(pt):
                return self._codes[idx]

        # Not inside any polygon (offshore point, small islet, harbour,
        # etc.) - fall back to the nearest country instead of dropping it.
        idx = self._tree.nearest(pt)
        return self._codes[idx]


def run_offline(gpx_text, dataset_path):
    try:
        import shapely  # noqa: F401
    except ImportError:
        Rprint("The offline mode needs the 'shapely' package.")
        Rprint("Install it with:  pip install shapely --break-system-packages")
        Rprint("(or run with --online to use internet-based geocoding instead)")
        sys.exit(1)

    dataset_path = ensure_dataset(dataset_path)
    print("Loading country boundaries...")
    lookup = OfflineCountryLookup(dataset_path)

    countries_data = defaultdict(list)
    count = 0
    for lat, lon, wpt_block in parse_waypoints(gpx_text):
        code = lookup.lookup(lat, lon)
        countries_data[code].append(wpt_block)
        count += 1
        print(f"\r{count} points processed - {lat:.5f} {lon:.5f} -> {code}", end="", flush=True)
    print("")
    return countries_data


# ----------------------------------------------------------------------
# ONLINE geocoder: Nominatim via geopy, rate-limited, retried, cached
# ----------------------------------------------------------------------


def run_online(gpx_text, delay_seconds=1.1, max_retries=5):
    try:
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter
        from geopy.exc import GeocoderServiceError, GeocoderTimedOut
    except ImportError:
        Rprint("The online mode needs the 'geopy' package.")
        Rprint("Install it with:  pip install geopy --break-system-packages")
        sys.exit(1)

    geolocator = Nominatim(user_agent="gpx_splitter")
    # geopy's RateLimiter enforces the minimum delay Nominatim's usage
    # policy requires (1 req/sec) and can automatically swallow/retry
    # transient errors (which is what was crashing on the 429s).
    reverse = RateLimiter(
        geolocator.reverse,
        min_delay_seconds=delay_seconds,
        max_retries=max_retries,
        error_wait_seconds=5.0,
        swallow_exceptions=False,
    )

    # cache by coordinates rounded to ~1 km - waypoints clustered
    # together (a whole trip inside one country) then cost one real
    # network request instead of one per point.
    cache = {}

    def get_country_code(lat, lon):
        key = (round(lat, 2), round(lon, 2))
        if key in cache:
            return cache[key]
        try:
            location = reverse((lat, lon), language="en", timeout=10)
            code = None
            if location and "country_code" in location.raw.get("address", {}):
                code = location.raw["address"]["country_code"].upper()
        except (GeocoderServiceError, GeocoderTimedOut) as e:
            print(f"\nGeocoding failed for {lat},{lon}: {e}")
            code = None
        cache[key] = code
        return code

    countries_data = defaultdict(list)
    points = list(parse_waypoints(gpx_text))
    total = len(points)

    interrupted = {"flag": False}

    def handle_sigint(signum, frame):
        interrupted["flag"] = True

    old_handler = signal.signal(signal.SIGINT, handle_sigint)

    processed = 0
    for lat, lon, wpt_block in points:
        if interrupted["flag"]:
            print(f"\nInterrupted - writing the {processed}/{total} points looked up so far.")
            break
        code = get_country_code(lat, lon)
        if code:
            countries_data[code].append(wpt_block)
        processed += 1
        print(f"\r{processed}/{total} {lat:.5f} {lon:.5f} -> {code}", end="", flush=True)
    print("")

    signal.signal(signal.SIGINT, old_handler)
    return countries_data


# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Split a GPX waypoint file into per-country GPX files.")
    parser.add_argument("source", help="path to the source .gpx file")
    parser.add_argument("--outdir", default=".", help="directory for the per-country output files (default: current dir)")
    parser.add_argument("--online", action="store_true", help="use Nominatim reverse geocoding instead of the offline dataset")
    parser.add_argument("--dataset", default=os.path.join(os.path.expanduser("~"), ".cache", "geo2cntr", "countries.geojson"), help="path to the local country-boundary GeoJSON (offline mode); downloaded automatically if missing")
    parser.add_argument("--delay", type=float, default=1.1, help="online mode only: seconds between Nominatim requests (default 1.1, don't go below 1.0)")

    args = parser.parse_args()

    if not os.path.isfile(args.source):
        Rprint(f"Error: File {args.source} not found.")
        sys.exit(1)

    print(f"Processing: {args.source}")
    with open(args.source, "r", encoding="utf-8") as f:
        gpx_text = f.read()

    if args.online:
        countries_data = run_online(gpx_text, delay_seconds=args.delay)
    else:
        countries_data = run_offline(gpx_text, args.dataset)

    if not countries_data:
        Rprint("No waypoints were classified - nothing to write.")
        sys.exit(1)

    namespace_decls = extract_namespace_decls(gpx_text)
    write_country_files(countries_data, args.outdir, namespace_decls)
    print("End of process")


if __name__ == "__main__":
    main()
