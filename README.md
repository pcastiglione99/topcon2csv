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

The script prints a summary (station, instrument height, prism height, number of
observations) and the list of computed points.

## 3. Output

The CSV contains one row for the station and one row for each surveyed point,
with the following columns:

| Column | Description |
| --- | --- |
| `pid` | point number |
| `station` | number of the reference station |
| `x`, `y`, `z` | computed coordinates (m) |
| `distance` | slope distance (m) |
| `horizontal_gon` | horizontal angle / horizontal circle (gon) |
| `zenith_gon` | zenith angle (gon, 0 = zenith, 100 = horizon) |
| `instrument_height` | instrument height (m) |
| `prism_height` | prism height (m) |

## 4. Configuration

The computation parameters are constants at the top of `topcon2csv.py`, to be
edited if the survey does not start from zero coordinates:

- `STATION_X`, `STATION_Y`, `STATION_Z` — station coordinates
  (default `0, 0, 0`: the resulting coordinates are relative to the station)
- `HORIZONTAL_FROM_NORTH` — `True` (default): 0 gon = North, angles increasing
  clockwise; `False`: 0 gon = X axis, angles increasing counter-clockwise

The instrument height and the prism height are read automatically from the raw
file.

## Notes on the raw format

The serial dump is split into 132-character blocks delimited by `STX` (`0x02`)
and `ETX CR LF` (`0x03 0x0D 0x0A`); the last 4 digits of each block are a
transfer protocol counter and are **not** part of the data. The script strips
them before parsing: reading the file as continuous text without this cleanup
breaks the numeric fields that straddle block boundaries and silently loses
observations.
