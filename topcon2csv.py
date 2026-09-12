#!/usr/bin/env python3

import argparse
import csv
import math
import re
from pathlib import Path

# Framing bytes used by the Topcon serial transfer
STX = b"\x02"
ETX = b"\x03"

# ============================================================
# CONFIGURATION
# ============================================================

# Station coordinates
STATION_X = 0.0
STATION_Y = 0.0
STATION_Z = 0.0

# Horizontal angle convention:
# True  -> 0 gon = North, angle increasing clockwise
# False -> 0 gon = X axis, angle increasing counter-clockwise
HORIZONTAL_FROM_NORTH = True


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
# STATION
# ============================================================

def find_station(data):
    """
    Looks for:

        100_(P_)1.475

    and returns:

        station = 100
        instrument_height = 1.475
    """

    match = re.search(
        r"(?P<station>\d+)_\(P_\)(?P<ih>\d+(?:\.\d+)?)",
        data
    )

    if not match:
        raise ValueError(
            "Could not find the station in the file."
        )

    station = int(match.group("station"))
    instrument_height = float(match.group("ih"))

    return station, instrument_height


# ============================================================
# PRISM HEIGHT
# ============================================================

def find_prism_height(data):
    """
    Looks for:

        _*V_,1.420_

    """

    match = re.search(
        r"_\*?V_,(?P<th>\d+(?:\.\d+)?)_",
        data
    )

    if not match:
        raise ValueError(
            "Could not find the prism height."
        )

    return float(match.group("th"))


# ============================================================
# OBSERVATION PARSING
# ============================================================

def parse_observations(data):
    """
    Looks for records such as:

        +101_ ?+00093978m1001100+0009350g+00093978t

    NOTE ON THE ANGLE FIELDS:
    Comparing the extraction against a reference export of the same
    survey (columns "Horizontal Circle" and "Zenith Angle"), the two
    angle fields in the raw record are in the opposite order from
    what one would intuitively expect:

        m<FIELD_1>+<FIELD_2>g

        FIELD_1 (right after 'm', before the '+')  -> Zenith Angle
        FIELD_2 (between '+' and 'g')              -> Horizontal Circle

    For point 101 the record gives FIELD_1=100.1100, FIELD_2=0.9350,
    and the reference file confirms Zenith=100.1100, Horizontal
    Circle=0.9350: therefore FIELD_1 is the zenith angle and FIELD_2
    is the horizontal angle (azimuth/bearing), not the other way
    around.
    """

    pattern = re.compile(
        r"""
        \+
        (?P<point>\d+)
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
        """,
        re.VERBOSE
    )

    observations = []

    for match in pattern.finditer(data):

        point = int(match.group("point"))

        # ----------------------------------------------------
        # DISTANCE
        # ----------------------------------------------------

        distance_m = int(
            match.group("distance_m")
        ) / 1000.0

        distance_t = int(
            match.group("distance_t")
        ) / 1000.0

        # ----------------------------------------------------
        # ANGLES
        # ----------------------------------------------------

        # Horizontal angle (Horizontal Circle / azimuth-bearing)
        horizontal_gon = parse_gon(
            match.group("horizontal")
        )

        # Zenith angle (Zenith Angle: 0 gon = zenith, 100 gon =
        # horizon, 200 gon = nadir)
        zenith_gon = parse_gon(
            match.group("zenith")
        )

        observations.append({
            "point": point,
            "distance_m": distance_m,
            "distance_t": distance_t,
            "horizontal_gon": horizontal_gon,
            "zenith_gon": zenith_gon,
        })

    return observations


# ============================================================
# COORDINATE COMPUTATION
# ============================================================

def calculate_xyz(
    distance,
    horizontal_gon,
    zenith_gon,
    instrument_height,
    prism_height,
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

    H = gon_to_rad(horizontal_gon)
    Z = gon_to_rad(zenith_gon)

    # Horizontal component (Zenith Angle: sin(Z) gives the horizontal)
    horizontal_distance = distance * math.sin(Z)

    # --------------------------------------------------------
    # X / Y
    # --------------------------------------------------------

    if HORIZONTAL_FROM_NORTH:

        # 0 gon = North
        # 100 gon = East
        # 200 gon = South
        # 300 gon = West

        x = (
            STATION_X
            + horizontal_distance * math.sin(H)
        )

        y = (
            STATION_Y
            + horizontal_distance * math.cos(H)
        )

    else:

        # 0 gon = positive X
        # 100 gon = positive Y

        x = (
            STATION_X
            + horizontal_distance * math.cos(H)
        )

        y = (
            STATION_Y
            + horizontal_distance * math.sin(H)
        )

    # --------------------------------------------------------
    # Z
    # --------------------------------------------------------

    z = (
        STATION_Z
        + distance * math.cos(Z)
        + instrument_height
        - prism_height
    )

    return x, y, z


# ============================================================
# MAIN
# ============================================================

def main():

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

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    # --------------------------------------------------------
    # FILE READING
    # --------------------------------------------------------

    raw = input_path.read_bytes()

    # The file contains control characters and a block-based serial
    # framing (STX/ETX) with spurious counters to be removed.
    data = clean_raw_data(raw)

    # --------------------------------------------------------
    # STATION
    # --------------------------------------------------------

    station, instrument_height = find_station(data)

    # --------------------------------------------------------
    # PRISM
    # --------------------------------------------------------

    prism_height = find_prism_height(data)

    # --------------------------------------------------------
    # OBSERVATIONS
    # --------------------------------------------------------

    observations = parse_observations(data)

    if not observations:
        raise RuntimeError(
            "No observations found in the file."
        )

    print()
    print("========================================")
    print("        TOPCON GTS-229 IMPORT")
    print("========================================")
    print()
    print(f"Station:              {station}")
    print(f"Station X:            {STATION_X:.3f}")
    print(f"Station Y:            {STATION_Y:.3f}")
    print(f"Station Z:            {STATION_Z:.3f}")
    print(f"Instrument height:    {instrument_height:.3f} m")
    print(f"Prism height:         {prism_height:.3f} m")
    print(f"Observations:         {len(observations)}")
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

        # ----------------------------------------------------
        # STATION
        # ----------------------------------------------------

        writer.writerow({
            "pid": station,
            "station": station,
            "x": STATION_X,
            "y": STATION_Y,
            "z": STATION_Z,
            "distance": "",
            "horizontal_gon": "",
            "zenith_gon": "",
            "instrument_height": instrument_height,
            "prism_height": "",
        })

        # ----------------------------------------------------
        # POINTS
        # ----------------------------------------------------

        for obs in observations:

            x, y, z = calculate_xyz(
                distance=obs["distance_m"],
                horizontal_gon=obs["horizontal_gon"],
                zenith_gon=obs["zenith_gon"],
                instrument_height=instrument_height,
                prism_height=prism_height,
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
                "prism_height": f"{prism_height:.3f}",
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
    main()
