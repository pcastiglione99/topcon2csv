import csv
from pathlib import Path

import pytest

import topcon2csv as t

SAMPLE = Path(__file__).parent / "data" / "gts229_sample.txt"

EXPECTED_SAMPLE_CSV = """\
pid,station,x,y,z,distance,horizontal_gon,zenith_gon,instrument_height,prism_height
100,100,0.000000,0.000000,0.000000,,,,1.450,
108,100,0.846142,13.898395,-1.004488,13.964,3.8710,104.8120,1.450,1.400
109,100,19.756982,7.772127,-0.922474,21.253,76.1400,102.9140,1.450,1.400
110,100,13.430411,10.366766,-0.889310,16.992,58.1510,103.5210,1.450,1.400
111,100,3.589005,14.413573,-0.960201,14.888,15.5360,104.3230,1.450,1.400
"""


def sample_stream():
    """The sample file with the serial framing already removed."""
    return t.clean_raw_data(SAMPLE.read_bytes())


def run(tmp_path, data, *options):
    """Run the script on `data` (str or bytes) and return the CSV rows."""
    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.csv"

    if isinstance(data, str):
        data = data.encode("latin-1")

    input_path.write_bytes(data)
    t.main([str(input_path), "-o", str(output_path), *options])

    with output_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def by_pid(rows):
    return {row["pid"]: row for row in rows}


# ============================================================
# SAMPLE FILE
# ============================================================

def test_sample_csv(tmp_path, capsys):
    output_path = tmp_path / "out.csv"

    t.main([str(SAMPLE), "-o", str(output_path)])

    csv_text = output_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert csv_text == EXPECTED_SAMPLE_CSV

    out = capsys.readouterr().out
    assert "angle-only points (no distance) skipped: 101" in out
    # The horizontal distance check passes on real data
    assert "may be misread" not in out


# ============================================================
# SERIAL FRAMING
# ============================================================

def frame(stream, block_size=128):
    """
    Split a continuous stream into STX/ETX blocks with counters.
    The last block carries no counter, since clean_raw_data keeps the
    last block as-is (in real dumps its trailing digits are harmless).
    """
    blocks = [
        stream[i:i + block_size]
        for i in range(0, len(stream), block_size)
    ]

    raw = b"\x00"

    for n, block in enumerate(blocks):
        counter = f"{n:04d}" if n < len(blocks) - 1 else ""
        raw += b"\x02" + block.encode("latin-1")
        raw += counter.encode() + b"\x03\r\n"

    return raw + b"\x04\r\n"


def test_clean_raw_data_removes_block_counters():
    stream = sample_stream()

    assert t.clean_raw_data(frame(stream)) == stream


def test_clean_raw_data_leaves_unframed_data_alone():
    assert t.clean_raw_data(b"100_(P_)1.475") == "100_(P_)1.475"


def test_counters_inside_numbers_do_not_lose_points():
    # Small blocks, so that almost every record straddles a boundary
    stream = sample_stream()
    setups, observations = t.parse_survey(
        t.clean_raw_data(frame(stream, block_size=17))
    )

    assert [obs["point"] for obs in observations] == [
        str(n) for n in range(101, 112)
    ]


# ============================================================
# RECORD FORMATS
# ============================================================

def test_old_style_tags():
    # (P_) station tag and _*V_ code, as in the README example
    data = (
        "100_(P_)1.475_"
        "+101_ ?+00093978m1001100+0009350g+00093978t_*V_,1.420_"
    )

    setups, observations = t.parse_survey(data)

    assert setups == [{"station": "100", "instrument_height": 1.475}]
    obs, = observations
    assert obs["point"] == "101"
    assert obs["zenith_gon"] == 100.11
    assert obs["horizontal_gon"] == 0.935
    assert obs["distance_m"] == 93.978
    assert obs["prism_height"] == 1.42
    assert obs["code"] == "V"


def test_alphanumeric_names(tmp_path):
    data = (
        sample_stream()
        .replace("'100_(ST_)", "'S1_(ST_)")
        .replace("+108_ ?", "+A1_ ?")
        .replace("+109_ ?", "+PS-3_ ?")
        .replace("+110_ ?", "+0110_ ?")
        .replace("+111_ ?", "+B.2_ ?")
    )

    rows = run(tmp_path, data)

    assert [row["pid"] for row in rows] == ["S1", "A1", "PS-3", "0110", "B.2"]
    assert {row["station"] for row in rows} == {"S1"}


def test_missing_station():
    with pytest.raises(ValueError, match="station"):
        t.parse_survey("+101_ ?+00093978m1001100+0009350g+00093978t_*_,1.4_")


def test_missing_prism_height():
    with pytest.raises(ValueError, match="prism"):
        t.parse_survey(
            "100_(P_)1.475_+101_ ?+00093978m1001100+0009350g+00093978t"
        )


def test_no_observations_exits_with_message(tmp_path):
    input_path = tmp_path / "empty.txt"
    input_path.write_bytes(b"100_(P_)1.475_*_,1.400_")

    with pytest.raises(ValueError, match="No observations"):
        t.main([str(input_path), "-o", str(tmp_path / "out.csv")])


# ============================================================
# PRISM HEIGHT PER POINT
# ============================================================

def test_prism_height_per_point(tmp_path):
    base = by_pid(run(tmp_path, sample_stream()))

    # Change only the prism height recorded after point 110
    stream = sample_stream()
    start = stream.index("+110_")
    end = stream.index("+111_")
    changed = (
        stream[:start]
        + stream[start:end].replace(",1.400_", ",1.800_")
        + stream[end:]
    )

    rows = by_pid(run(tmp_path, changed))

    assert rows["110"]["prism_height"] == "1.800"
    assert float(rows["110"]["z"]) == pytest.approx(
        float(base["110"]["z"]) - 0.4
    )

    for pid in ("108", "109", "111"):
        assert rows[pid] == base[pid]


def test_prism_height_inherited_when_missing():
    data = (
        "100_(P_)1.475_"
        "+101_ ?+00010000m1000000+0000000g+00010000t_*_,1.300_"
        # No prism record for 102: uses the previous one
        "+102_ ?+00010000m1000000+0000000g+00010000t"
        "+103_ ?+00010000m1000000+0000000g+00010000t_*_,1.500_"
    )

    setups, observations = t.parse_survey(data)

    assert [obs["prism_height"] for obs in observations] == [1.3, 1.3, 1.5]


# ============================================================
# MULTIPLE STATIONS
# ============================================================

def multi_station_stream():
    stream = sample_stream()
    return stream.replace("+110_", "'200_(ST_)1.500_+110_", 1)


def test_repeated_station_record_is_merged():
    setups, observations = t.parse_survey(sample_stream())

    assert setups == [{"station": "100", "instrument_height": 1.45}]


def test_points_follow_their_station(tmp_path, capsys):
    rows = run(
        tmp_path,
        multi_station_stream(),
        "--station", "200", "10", "20", "5",
    )

    assert [(row["pid"], row["station"]) for row in rows] == [
        ("100", "100"),
        ("108", "100"),
        ("109", "100"),
        ("200", "200"),
        ("110", "200"),
        ("111", "200"),
    ]

    rows = by_pid(rows)
    assert rows["200"]["x"] == "10.000000"
    assert rows["110"]["instrument_height"] == "1.500"
    # Same observation, moved by the station offset
    assert float(rows["110"]["x"]) == pytest.approx(13.430411 + 10)
    assert float(rows["110"]["z"]) == pytest.approx(-0.889310 + 5 + 0.05)

    out = capsys.readouterr().out
    assert "same orientation" in out
    assert "same default coordinates" not in out


def test_warns_when_stations_share_default_coordinates(tmp_path, capsys):
    run(tmp_path, multi_station_stream())

    assert "same default coordinates (100, 200)" in capsys.readouterr().out


def test_warns_on_unknown_station_option(tmp_path, capsys):
    run(tmp_path, sample_stream(), "--station", "999", "1", "2", "3")

    assert "not in the file: 999" in capsys.readouterr().out


# ============================================================
# OPTIONS
# ============================================================

def test_station_xyz(tmp_path):
    rows = by_pid(run(
        tmp_path, sample_stream(), "--station-xyz", "1000", "2000", "100"
    ))

    assert rows["100"]["x"] == "1000.000000"
    assert float(rows["108"]["x"]) == pytest.approx(1000.846142)
    assert float(rows["108"]["y"]) == pytest.approx(2013.898395)
    assert float(rows["108"]["z"]) == pytest.approx(100 - 1.004488)


def test_from_x_axis_swaps_x_and_y(tmp_path):
    north = by_pid(run(tmp_path, sample_stream()))
    x_axis = by_pid(run(tmp_path, sample_stream(), "--from-x-axis"))

    assert x_axis["108"]["x"] == north["108"]["y"]
    assert x_axis["108"]["y"] == north["108"]["x"]


def test_include_angle_only(tmp_path, capsys):
    rows = by_pid(run(tmp_path, sample_stream(), "--include-angle-only"))

    row = rows["101"]
    assert (row["x"], row["y"], row["z"], row["distance"]) == ("", "", "", "")
    assert row["zenith_gon"] == "100.4720"
    assert row["horizontal_gon"] == "137.3210"
    assert row["prism_height"] == "1.400"

    assert "written without coordinates" in capsys.readouterr().out


# ============================================================
# HORIZONTAL DISTANCE CHECK
# ============================================================

def test_horizontal_distance_matches_on_sample():
    setups, observations = t.parse_survey(sample_stream())

    for obs in observations:
        if not obs["angle_only"]:
            assert t.check_horizontal_distance(obs) < t.HD_TOLERANCE


def test_horizontal_distance_detects_swapped_angles(tmp_path, capsys):
    # Swap zenith and horizontal fields of point 108
    stream = sample_stream().replace(
        "m1048120+0038710g", "m0038710+1048120g"
    )

    run(tmp_path, stream)

    assert "point 108: horizontal distance" in capsys.readouterr().out
