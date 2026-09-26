#!/usr/bin/env python3

import argparse
import csv
import math
import re
import sys
from pathlib import Path

# Framing bytes used by the Topcon serial transfer
STX = b"\x02"
ETX = b"\x03"

# Point/station name: any run of characters that are not field
# separators ('_'), record markers ('+', "'") or whitespace.
# Covers plain numbers (100, 101) as well as names like A1, PS-3, S.2.
# Names are kept as strings, so leading zeros are preserved.
NAME = r"[^_+'\s]+"

# Decimal number such as 1.450 (instrument/prism heights)
NUMBER = r"\d+(?:\.\d+)?"

# ============================================================
# CONFIGURATION
# ============================================================

# Default station coordinates (override with --station-xyz / --station)
STATION_X = 0.0
STATION_Y = 0.0
STATION_Z = 0.0

# Horizontal angle convention (override with --from-x-axis):
# True  -> 0 gon = North, angle increasing clockwise
# False -> 0 gon = X axis, angle increasing counter-clockwise
HORIZONTAL_FROM_NORTH = True

# Maximum accepted difference (m) between the horizontal distance sent
# by the instrument ('t' field) and the one recomputed from slope
# distance and zenith angle. A larger difference means the record was
# not decoded as expected.
HD_TOLERANCE = 0.005


# ============================================================
# ANGLE CONVERSIONS
# ============================================================

def gon_to_rad(gon):
    """Convert gon to radians."""
    return gon * math.pi / 200.0


def parse_gon(value):
    """
    Decode a Topcon angle.

    Examples:
        1001100 -> 100.1100 gon
        0009350 -> 0.9350 gon
        0977090 -> 97.7090 gon
    """

    value = value.strip()

    if not value:
        return None

    # Topcon values normally carry 4 implicit decimal places.
    return int(value) / 10000.0


# ============================================================
# RAW DATA CLEANUP (STX/ETX serial framing)
# ============================================================

def clean_raw_data(raw):
    """
    The raw Topcon file is a serial dump split into 132-character
    blocks, delimited by STX (0x02) at the beginning and by
    ETX CR LF (0x03 0x0D 0x0A) at the end:

        \\x02 <128 data bytes> <4 counter/checksum digits> \\x03\\r\\n

    The 4 trailing digits of each block are NOT part of the
    instrument data: they are a block counter belonging to the
    transfer protocol. If the file is read as continuous text
    without stripping them, they end up inserted in the middle of
    the numeric fields (distance, angles) exactly at the block
    boundaries, breaking the expected pattern and silently losing
    the observations that straddle a boundary.

    This function splits the file on the block separators, removes
    the 4 spurious digits from each block (except the last one,
    which terminates the transmission) and reassembles the original
    continuous stream.
    """

    # Blocks separated by ETX CR LF STX
    parts = re.split(rb"\x03\r\n\x02", raw)

    if len(parts) < 2:
        # No framing recognized: the file is not segmented into
        # blocks, so we return the data as-is.
        return raw.decode("latin-1")

    # The first block may have a leading NUL + STX to strip
    parts[0] = parts[0].lstrip(b"\x00\x02")

    # The last block contains the transmission trailer
    # (ETX CR LF EOT CR LF): keep only what precedes the first ETX
    parts[-1] = parts[-1].split(b"\x03", 1)[0]

    cleaned = bytearray()
    last_index = len(parts) - 1

    for i, part in enumerate(parts):
        if i < last_index and len(part) > 4:
            # Strip the 4 counter/checksum digits at the end of the block
            cleaned += part[:-4]
        else:
            cleaned += part

    return cleaned.decode("latin-1")


# ============================================================
# RECORD PARSING
# ============================================================

# The cleaned stream is a sequence of records, read in order:
#
#   station set-up:        100_(ST_)1.450         or  100_(P_)1.475
#   full observation:      +101_ ?+00093978m1001100+0009350g+00093978t
#   angle-only observation: +101_ <1004720+1373210
#   point code + prism:    _*V_,1.420_            or  _*_,1.400_
#
# NOTE ON THE ANGLE FIELDS:
# Comparing the extraction against a reference export of the same
# survey (columns "Horizontal Circle" and "Zenith Angle"), the two
# angle fields in the raw record are in the opposite order from
# what one would intuitively expect:
#
#     m<FIELD_1>+<FIELD_2>g
#
#     FIELD_1 (right after 'm', before the '+')  -> Zenith Angle
#     FIELD_2 (between '+' and 'g')              -> Horizontal Circle
#
# For point 101 the record gives FIELD_1=100.1100, FIELD_2=0.9350,
# and the reference file confirms Zenith=100.1100, Horizontal
# Circle=0.9350: therefore FIELD_1 is the zenith angle and FIELD_2
# is the horizontal angle (azimuth/bearing), not the other way
# around. The same order is assumed for angle-only records.
#
# The 't' field is the horizontal distance computed by the
# instrument: it is used to verify this interpretation (see
# check_horizontal_distance).

RECORD_PATTERN = re.compile(
    rf"""
    (?P<station>{NAME})
    _\((?:P|ST)_\)
    (?P<ih>{NUMBER})

    |

    \+
    (?P<point>{NAME})
    _
    [^+]*

    \+
    (?P<distance_m>\d+)
    m

    (?P<zenith>\d+)

    \+
    (?P<horizontal>\d+)
    g

    \+
    (?P<distance_t>\d+)
    t

    |

    \+
    (?P<angle_point>{NAME})
    _\ <
    (?P<angle_zenith>\d+)
    \+
    (?P<angle_horizontal>\d+)

    |

    _\*?
    (?P<code>[^_]*)
    _,
    (?P<th>{NUMBER})
    _
    """,
    re.VERBOSE
)


def parse_survey(data):
    """
    Read the cleaned stream in order and return:

        setups:        [{"station": "100", "instrument_height": 1.45}, ...]
        observations:  [{"point": "101", "setup": 0, ...}, ...]

    Each observation refers to the station set-up that precedes it
    ("setup" is the index in setups). Repeated identical set-up
    records (the instrument often sends the station twice) are
    merged.

    PRISM HEIGHT:
    The code/prism record that follows an observation belongs to it,
    so every point gets its own prism height. An observation without
    its own prism record uses the last prism height seen before it,
    or the first one in the file if none was seen yet.
    """

    setups = []
    observations = []

    first_th = None
    current_th = None

    for match in RECORD_PATTERN.finditer(data):

        if match.group("station") is not None:

            setup = {
                "station": match.group("station"),
                "instrument_height": float(match.group("ih")),
            }

            if not setups or setups[-1] != setup:
                setups.append(setup)

        elif match.group("point") is not None:

            observations.append({
                "point": match.group("point"),
                "setup": len(setups) - 1,
                "angle_only": False,
                # Distances are in mm
                "distance_m": int(match.group("distance_m")) / 1000.0,
                "distance_t": int(match.group("distance_t")) / 1000.0,
                "horizontal_gon": parse_gon(match.group("horizontal")),
                "zenith_gon": parse_gon(match.group("zenith")),
                "code": "",
                "prism_height": None,
                "previous_th": current_th,
            })

        elif match.group("angle_point") is not None:

            observations.append({
                "point": match.group("angle_point"),
                "setup": len(setups) - 1,
                "angle_only": True,
                "distance_m": None,
                "distance_t": None,
                "horizontal_gon": parse_gon(
                    match.group("angle_horizontal")
                ),
                "zenith_gon": parse_gon(match.group("angle_zenith")),
                "code": "",
                "prism_height": None,
                "previous_th": current_th,
            })

        else:

            th = float(match.group("th"))

            if first_th is None:
                first_th = th

            current_th = th

            if observations and observations[-1]["prism_height"] is None:
                observations[-1]["prism_height"] = th
                observations[-1]["code"] = match.group("code")

    if not setups:
        raise ValueError(
            "Could not find the station in the file."
        )

    if first_th is None:
        raise ValueError(
            "Could not find the prism height."
        )

    for obs in observations:

        # Observations sent before any station record belong to the
        # first station
        if obs["setup"] < 0:
            obs["setup"] = 0

        if obs["prism_height"] is None:
            if obs["previous_th"] is not None:
                obs["prism_height"] = obs["previous_th"]
            else:
                obs["prism_height"] = first_th

        del obs["previous_th"]

    return setups, observations


# ============================================================
# CONSISTENCY CHECK
# ============================================================

def check_horizontal_distance(obs):
    """
    Compare the horizontal distance sent by the instrument ('t'
    field) with D * sin(Z) recomputed from slope distance and zenith
    angle. Returns the difference in metres.

    If the zenith and horizontal fields were swapped, or the record
    was otherwise misread, the difference is large.
    """

    Z = gon_to_rad(obs["zenith_gon"])
    expected = obs["distance_m"] * math.sin(Z)

    return abs(expected - obs["distance_t"])


# ============================================================
# COORDINATE COMPUTATION
# ============================================================

def calculate_xyz(
    distance,
    horizontal_gon,
    zenith_gon,
    instrument_height,
    prism_height,
    station_xyz=(STATION_X, STATION_Y, STATION_Z),
    from_north=HORIZONTAL_FROM_NORTH,
):
    """
    Compute X, Y, Z from:

        slope distance
        horizontal angle (Horizontal Circle / bearing)
        zenith angle (Zenith Angle)

    Zenith Angle convention (total station standard):

        0 gon   = zenith (vertical, upwards)
        100 gon = horizon
        200 gon = nadir (vertical, downwards)

    Therefore:

        horizontal_component   = D * sin(Z)
        height difference (dz) = D * cos(Z)

    and:

        Z_elevation = station_Z + dz + IH - TH
    """

    station_x, station_y, station_z = station_xyz

    H = gon_to_rad(horizontal_gon)
    Z = gon_to_rad(zenith_gon)

    # Horizontal component (Zenith Angle: sin(Z) gives the horizontal)
    horizontal_distance = distance * math.sin(Z)

    # --------------------------------------------------------
    # X / Y
    # --------------------------------------------------------

    if from_north:

        # 0 gon = North
        # 100 gon = East
        # 200 gon = South
        # 300 gon = West

        x = (
            station_x
            + horizontal_distance * math.sin(H)
        )

        y = (
            station_y
            + horizontal_distance * math.cos(H)
        )

    else:

        # 0 gon = positive X
        # 100 gon = positive Y

        x = (
            station_x
            + horizontal_distance * math.cos(H)
        )

        y = (
            station_y
            + horizontal_distance * math.sin(H)
        )

    # --------------------------------------------------------
    # Z
    # --------------------------------------------------------

    z = (
        station_z
        + distance * math.cos(Z)
        + instrument_height
        - prism_height
    )

    return x, y, z


# ============================================================
# COMMAND LINE
# ============================================================

def parse_args(argv=None):

    parser = argparse.ArgumentParser(
        description=(
            "Convert raw Topcon GTS-229 data "
            "into a CSV with XYZ coordinates."
        )
    )

    parser.add_argument(
        "input",
        help="Raw Topcon file"
    )

    parser.add_argument(
        "-o",
        "--output",
        default="topcon_xyz.csv",
        help="Output CSV file"
    )

    parser.add_argument(
        "--station-xyz",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        default=(STATION_X, STATION_Y, STATION_Z),
        help=(
            "Coordinates of the station "
            f"(default: {STATION_X:g} {STATION_Y:g} {STATION_Z:g})"
        )
    )

    parser.add_argument(
        "--station",
        nargs=4,
        action="append",
        default=[],
        metavar=("NAME", "X", "Y", "Z"),
        help=(
            "Coordinates of a specific station, for files with "
            "several stations (repeatable)"
        )
    )

    parser.add_argument(
        "--from-x-axis",
        action="store_true",
        help=(
            "0 gon = X axis, angles counter-clockwise "
            "(default: 0 gon = North, clockwise)"
        )
    )

    parser.add_argument(
        "--include-angle-only",
        action="store_true",
        help=(
            "Write angle-only points (no distance) to the CSV "
            "with empty coordinates"
        )
    )

    args = parser.parse_args(argv)

    station_coordinates = {}

    for name, *xyz in args.station:
        try:
            station_coordinates[name] = tuple(float(v) for v in xyz)
        except ValueError:
            parser.error(
                f"--station {name}: coordinates must be numbers"
            )

    args.station_coordinates = station_coordinates

    return args


# ============================================================
# MAIN
# ============================================================

def main(argv=None):

    args = parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

    from_north = not args.from_x_axis

    # --------------------------------------------------------
    # FILE READING
    # --------------------------------------------------------

    raw = input_path.read_bytes()

    # The file contains control characters and a block-based serial
    # framing (STX/ETX) with spurious counters to be removed.
    data = clean_raw_data(raw)

    # --------------------------------------------------------
    # PARSING
    # --------------------------------------------------------

    setups, observations = parse_survey(data)

    measured = [obs for obs in observations if not obs["angle_only"]]
    angle_only = [obs for obs in observations if obs["angle_only"]]

    if not measured and not (args.include_angle_only and angle_only):
        raise ValueError(
            "No observations found in the file."
        )

    def station_xyz(station):
        return args.station_coordinates.get(station, args.station_xyz)

    station_names = []

    for setup in setups:
        if setup["station"] not in station_names:
            station_names.append(setup["station"])

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print()
    print("========================================")
    print("        TOPCON GTS-229 IMPORT")
    print("========================================")
    print()

    for setup in setups:
        x, y, z = station_xyz(setup["station"])
        print(f"Station:              {setup['station']}")
        print(f"Station X:            {x:.3f}")
        print(f"Station Y:            {y:.3f}")
        print(f"Station Z:            {z:.3f}")
        print(f"Instrument height:    {setup['instrument_height']:.3f} m")
        print()

    prism_heights = sorted({obs["prism_height"] for obs in observations})

    print(
        "Prism height:         "
        + ", ".join(f"{th:.3f}" for th in prism_heights)
        + " m"
    )
    print(f"Observations:         {len(measured)}")
    print()

    # --------------------------------------------------------
    # WARNINGS
    # --------------------------------------------------------

    warnings = []

    unknown = sorted(set(args.station_coordinates) - set(station_names))

    if unknown:
        warnings.append(
            "--station given for stations not in the file: "
            + ", ".join(unknown)
        )

    if len(station_names) > 1:
        default_stations = [
            name for name in station_names
            if name not in args.station_coordinates
        ]

        if len(default_stations) > 1:
            warnings.append(
                "several stations use the same default coordinates "
                f"({', '.join(default_stations)}): give each one its "
                "own coordinates with --station NAME X Y Z"
            )

        warnings.append(
            "several stations in the file: the horizontal circle of "
            "each set-up is used as-is, check that they share the "
            "same orientation"
        )

    if angle_only:
        action = (
            "written without coordinates"
            if args.include_angle_only
            else "skipped"
        )
        warnings.append(
            f"angle-only points (no distance) {action}: "
            + ", ".join(obs["point"] for obs in angle_only)
        )

    for obs in measured:
        difference = check_horizontal_distance(obs)

        if difference > HD_TOLERANCE:
            warnings.append(
                f"point {obs['point']}: horizontal distance from the "
                f"instrument ({obs['distance_t']:.3f} m) differs by "
                f"{difference:.3f} m from the computed one, the record "
                "may be misread"
            )

    for warning in warnings:
        print(f"WARNING: {warning}")

    if warnings:
        print()

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    fieldnames = [
        "pid",
        "station",
        "x",
        "y",
        "z",
        "distance",
        "horizontal_gon",
        "zenith_gon",
        "instrument_height",
        "prism_height",
    ]

    with output_path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()

        for index, setup in enumerate(setups):

            station = setup["station"]
            instrument_height = setup["instrument_height"]
            sx, sy, sz = station_xyz(station)

            # ------------------------------------------------
            # STATION
            # ------------------------------------------------

            writer.writerow({
                "pid": station,
                "station": station,
                "x": f"{sx:.6f}",
                "y": f"{sy:.6f}",
                "z": f"{sz:.6f}",
                "distance": "",
                "horizontal_gon": "",
                "zenith_gon": "",
                "instrument_height": f"{instrument_height:.3f}",
                "prism_height": "",
            })

            # ------------------------------------------------
            # POINTS
            # ------------------------------------------------

            for obs in observations:

                if obs["setup"] != index:
                    continue

                if obs["angle_only"]:

                    if not args.include_angle_only:
                        continue

                    writer.writerow({
                        "pid": obs["point"],
                        "station": station,
                        "x": "",
                        "y": "",
                        "z": "",
                        "distance": "",
                        "horizontal_gon": f"{obs['horizontal_gon']:.4f}",
                        "zenith_gon": f"{obs['zenith_gon']:.4f}",
                        "instrument_height": f"{instrument_height:.3f}",
                        "prism_height": f"{obs['prism_height']:.3f}",
                    })

                    continue

                x, y, z = calculate_xyz(
                    distance=obs["distance_m"],
                    horizontal_gon=obs["horizontal_gon"],
                    zenith_gon=obs["zenith_gon"],
                    instrument_height=instrument_height,
                    prism_height=obs["prism_height"],
                    station_xyz=(sx, sy, sz),
                    from_north=from_north,
                )

                writer.writerow({
                    "pid": obs["point"],
                    "station": station,
                    "x": f"{x:.6f}",
                    "y": f"{y:.6f}",
                    "z": f"{z:.6f}",
                    "distance": f"{obs['distance_m']:.3f}",
                    "horizontal_gon": f"{obs['horizontal_gon']:.4f}",
                    "zenith_gon": f"{obs['zenith_gon']:.4f}",
                    "instrument_height": f"{instrument_height:.3f}",
                    "prism_height": f"{obs['prism_height']:.3f}",
                })

                print(
                    f"Point {obs['point']:>3}: "
                    f"X={x:>10.3f} "
                    f"Y={y:>10.3f} "
                    f"Z={z:>10.3f} "
                    f"D={obs['distance_m']:.3f} m "
                    f"H={obs['horizontal_gon']:.4f} gon "
                    f"Z={obs['zenith_gon']:.4f} gon"
                )

    print()
    print(f"CSV created: {output_path}")
    print()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f"Error: {error}")
