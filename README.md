# topcon2csv

Convert raw **Topcon GTS-229** total station serial dumps into a CSV with XYZ
coordinates.

## 1. Download the data from the instrument

Connect the total station to the PC with the serial cable (or with a USB-serial
adapter, typically `/dev/ttyUSB0`).

Configure the port and start listening:

```sh
stty -F /dev/ttyUSB0 9600 cs8 -parenb cstopb -echo raw; cat /dev/ttyUSB0 > src.txt
```

What the parameters mean: 9600 baud, 8 data bits, no parity, 2 stop bits, no
echo, no processing of control characters (`raw`) — essential, because the dump
contains STX/ETX bytes that must not be altered.

Then, on the instrument, start the data transfer (communication menu / *send
data*). The bytes land in `src.txt` while `cat` keeps running: when the transfer
is over, stop it with `Ctrl+C`.

Quick check that the file actually contains something:

```sh
ls -l src.txt
```

If the file stays empty: check that the port is the right one (`dmesg | tail`
after plugging in the adapter), that the baud rates set on the instrument and on
the port match, and that your user has access to the serial port (group `uucp`
or `dialout` depending on the distribution).

## 2. Convert to CSV

Only Python 3 is required (no external dependencies):

```sh
python3 topcon2csv.py src.txt -o topcon_xyz.csv
```

Options:

- `input` — raw file downloaded from the instrument (positional, required)
- `-o`, `--output` — output CSV file (default: `topcon_xyz.csv`)
- `--station-xyz X Y Z` — coordinates of the station (default: `0 0 0`, so the
  resulting coordinates are relative to the station)
- `--station NAME X Y Z` — coordinates of a specific station, for files with
  several stations; can be repeated
- `--from-x-axis` — 0 gon = X axis, angles increasing counter-clockwise
  (default: 0 gon = North, angles increasing clockwise)
- `--include-angle-only` — also write the points measured with angles only (no
  distance) to the CSV, with empty coordinates

Example with georeferenced coordinates:

```sh
python3 topcon2csv.py src.txt -o topcon_xyz.csv --station-xyz 1000 2000 100
```

The script prints a summary (stations, instrument heights, prism heights,
number of observations), any warnings and the list of computed points. If the
file can't be read or doesn't contain a station, a prism height or any
observation, it prints an error and exits with status 1.

## 3. Output

The CSV contains, for each station, one row for the station followed by one row
for each point surveyed from it, with the following columns:

| Column | Description |
| --- | --- |
| `pid` | point name (numeric or alphanumeric, e.g. `101`, `A1`) |
| `station` | name of the station the point was surveyed from |
| `x`, `y`, `z` | computed coordinates (m) |
| `distance` | slope distance (m) |
| `horizontal_gon` | horizontal angle / horizontal circle (gon) |
| `zenith_gon` | zenith angle (gon, 0 = zenith, 100 = horizon) |
| `instrument_height` | instrument height of the station (m) |
| `prism_height` | prism height used for the point (m) |

Point and station names are kept exactly as sent by the instrument, including
leading zeros (`0110`). When opening the CSV in a spreadsheet, import `pid` and
`station` as text, otherwise the leading zeros may be dropped.

## 4. What the script reads from the file

- **Stations**: each `NAME_(ST_)IH` or `NAME_(P_)IH` record starts a set-up;
  the points that follow belong to it. Repeated identical station records are
  merged. With several stations, give each one its coordinates with `--station`:
  otherwise the script warns that they share the default coordinates. The
  horizontal circle of each set-up is used as-is, so the set-ups must share the
  same orientation.
- **Prism height**: read for each point from the `_*CODE_,TH_` record that
  follows it, so changing the prism height during the survey is handled. A point
  without its own prism record uses the last prism height seen before it.
- **Angle-only points** (`+NAME_ <...` records, measured without distance): no
  coordinates can be computed; they are listed in a warning and skipped, unless
  `--include-angle-only` is given.
- **Consistency check**: the horizontal distance sent by the instrument (`t`
  field) is compared with the one computed from slope distance and zenith angle.
  If they differ by more than 5 mm, the script warns that the record may have
  been misread (for example if the order of the angle fields were different).

## 5. Tests

```sh
python3 -m pytest
```

The tests use `tests/data/gts229_sample.txt`, a real dump, as sample input.

## Notes on the raw format

The serial dump is split into 132-character blocks delimited by `STX` (`0x02`)
and `ETX CR LF` (`0x03 0x0D 0x0A`); the last 4 digits of each block are a
transfer protocol counter and are **not** part of the data. The script strips
them before parsing: reading the file as continuous text without this cleanup
breaks the numeric fields that straddle block boundaries and silently loses
observations.
