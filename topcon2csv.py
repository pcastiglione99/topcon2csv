#!/usr/bin/env python3

import argparse
import csv
import math
import re
from pathlib import Path

# Byte di framing usati dal trasferimento seriale Topcon
STX = b"\x02"
ETX = b"\x03"

# ============================================================
# CONFIGURAZIONE
# ============================================================

# Coordinate della stazione
STATION_X = 0.0
STATION_Y = 0.0
STATION_Z = 0.0

# Convenzione angolo orizzontale:
# True  -> 0 gon = Nord, angolo crescente in senso orario
# False -> 0 gon = asse X, angolo crescente antiorario
HORIZONTAL_FROM_NORTH = True


# ============================================================
# CONVERSIONI ANGOLARI
# ============================================================

def gon_to_rad(gon):
    """Converte gon in radianti."""
    return gon * math.pi / 200.0


def parse_gon(value):
    """
    Decodifica un angolo Topcon.

    Esempi:
        1001100 -> 100.1100 gon
        0009350 -> 0.9350 gon
        0977090 -> 97.7090 gon
    """

    value = value.strip()

    if not value:
        return None

    # I valori Topcon hanno normalmente 4 decimali impliciti.
    return int(value) / 10000.0


# ============================================================
# PULIZIA DATI GREZZI (framing seriale STX/ETX)
# ============================================================

def clean_raw_data(raw):
    """
    Il file grezzo Topcon e' un dump seriale suddiviso in blocchi da
    132 caratteri, delimitati da STX (0x02) all'inizio e da
    ETX CR LF (0x03 0x0D 0x0A) alla fine:

        \\x02 <128 byte di dati> <4 cifre di contatore/checksum> \\x03\\r\\n

    Le 4 cifre finali di ogni blocco NON fanno parte dei dati dello
    strumento: sono un contatore di blocco del protocollo di
    trasferimento. Se il file viene letto come testo continuo senza
    rimuoverle, finiscono per inserirsi nel mezzo dei campi numerici
    (distanza, angoli) proprio in corrispondenza dei confini di
    blocco, rompendo il pattern atteso e facendo perdere silenziosamente
    le osservazioni che cadono a cavallo di un confine.

    Questa funzione spezza il file sui separatori di blocco, toglie le
    4 cifre spurie da ogni blocco (tranne l'ultimo, che termina la
    trasmissione) e ricompone lo stream continuo originale.
    """

    # Blocchi separati da ETX CR LF STX
    parts = re.split(rb"\x03\r\n\x02", raw)

    if len(parts) < 2:
        # Nessun framing riconosciuto: il file non e' segmentato in
        # blocchi, restituiamo il dato cosi' com'e'.
        return raw.decode("latin-1")

    # Il primo blocco puo' avere un NUL + STX iniziali da rimuovere
    parts[0] = parts[0].lstrip(b"\x00\x02")

    # L'ultimo blocco contiene la coda di trasmissione
    # (ETX CR LF EOT CR LF): teniamo solo cio' che precede il primo ETX
    parts[-1] = parts[-1].split(b"\x03", 1)[0]

    cleaned = bytearray()
    last_index = len(parts) - 1

    for i, part in enumerate(parts):
        if i < last_index and len(part) > 4:
            # Rimuove le 4 cifre di contatore/checksum a fine blocco
            cleaned += part[:-4]
        else:
            cleaned += part

    return cleaned.decode("latin-1")


# ============================================================
# STAZIONE
# ============================================================

def find_station(data):
    """
    Cerca:

        100_(P_)1.475

    e restituisce:

        station = 100
        instrument_height = 1.475
    """

    match = re.search(
        r"(?P<station>\d+)_\(P_\)(?P<ih>\d+(?:\.\d+)?)",
        data
    )

    if not match:
        raise ValueError(
            "Impossibile trovare la stazione nel file."
        )

    station = int(match.group("station"))
    instrument_height = float(match.group("ih"))

    return station, instrument_height


# ============================================================
# ALTEZZA PRISMA
# ============================================================

def find_prism_height(data):
    """
    Cerca:

        _*V_,1.420_

    """

    match = re.search(
        r"_\*?V_,(?P<th>\d+(?:\.\d+)?)_",
        data
    )

    if not match:
        raise ValueError(
            "Impossibile trovare l'altezza prisma."
        )

    return float(match.group("th"))


# ============================================================
# PARSING DELLE OSSERVAZIONI
# ============================================================

def parse_observations(data):
    """
    Cerca record del tipo:

        +101_ ?+00093978m1001100+0009350g+00093978t

    NOTA SUI CAMPI ANGOLARI:
    Confrontando l'estrazione con un export di riferimento dello stesso
    rilievo (colonne "Horizontal Circle" e "Zenith Angle"), i due campi
    angolari nel record grezzo sono nell'ordine opposto a quanto ci si
    aspetterebbe intuitivamente:

        m<CAMPO_1>+<CAMPO_2>g

        CAMPO_1 (subito dopo 'm', prima del '+')  -> Zenith Angle
        CAMPO_2 (tra '+' e 'g')                   -> Horizontal Circle

    Per il punto 101 il record da' CAMPO_1=100.1100, CAMPO_2=0.9350,
    e il file di riferimento conferma Zenith=100.1100, Horizontal
    Circle=0.9350: quindi CAMPO_1 e' lo zenith, CAMPO_2 e' l'angolo
    orizzontale (azimut/bearing), non il contrario.
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
        # DISTANZA
        # ----------------------------------------------------

        distance_m = int(
            match.group("distance_m")
        ) / 1000.0

        distance_t = int(
            match.group("distance_t")
        ) / 1000.0

        # ----------------------------------------------------
        # ANGOLI
        # ----------------------------------------------------

        # Angolo orizzontale (Horizontal Circle / azimut-bearing)
        horizontal_gon = parse_gon(
            match.group("horizontal")
        )

        # Angolo zenitale (Zenith Angle: 0 gon = zenit, 100 gon =
        # orizzonte, 200 gon = nadir)
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
# CALCOLO COORDINATE
# ============================================================

def calculate_xyz(
    distance,
    horizontal_gon,
    zenith_gon,
    instrument_height,
    prism_height,
):
    """
    Calcola X, Y, Z a partire da:

        distanza inclinata (slope distance)
        angolo orizzontale (Horizontal Circle / bearing)
        angolo zenitale (Zenith Angle)

    Convenzione Zenith Angle (standard stazione totale):

        0 gon   = zenit (verticale, verso l'alto)
        100 gon = orizzonte
        200 gon = nadir (verticale, verso il basso)

    Quindi:

        componente_orizzontale = D * sin(Z)
        dislivello (dz)        = D * cos(Z)

    e:

        Z_quota = station_Z + dz + IH - TH
    """

    H = gon_to_rad(horizontal_gon)
    Z = gon_to_rad(zenith_gon)

    # Componente orizzontale (Zenith Angle: sin(Z) da' l'orizzontale)
    horizontal_distance = distance * math.sin(Z)

    # --------------------------------------------------------
    # X / Y
    # --------------------------------------------------------

    if HORIZONTAL_FROM_NORTH:

        # 0 gon = Nord
        # 100 gon = Est
        # 200 gon = Sud
        # 300 gon = Ovest

        x = (
            STATION_X
            + horizontal_distance * math.sin(H)
        )

        y = (
            STATION_Y
            + horizontal_distance * math.cos(H)
        )

    else:

        # 0 gon = X positivo
        # 100 gon = Y positivo

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
            "Converte dati grezzi Topcon GTS-229 "
            "in CSV con coordinate XYZ."
        )
    )

    parser.add_argument(
        "input",
        help="File grezzo Topcon"
    )

    parser.add_argument(
        "-o",
        "--output",
        default="topcon_xyz.csv",
        help="File CSV di output"
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    # --------------------------------------------------------
    # LETTURA FILE
    # --------------------------------------------------------

    raw = input_path.read_bytes()

    # Il file contiene caratteri di controllo e un framing seriale
    # a blocchi (STX/ETX) con contatori spuri da rimuovere.
    data = clean_raw_data(raw)

    # --------------------------------------------------------
    # STAZIONE
    # --------------------------------------------------------

    station, instrument_height = find_station(data)

    # --------------------------------------------------------
    # PRISMA
    # --------------------------------------------------------

    prism_height = find_prism_height(data)

    # --------------------------------------------------------
    # OSSERVAZIONI
    # --------------------------------------------------------

    observations = parse_observations(data)

    if not observations:
        raise RuntimeError(
            "Nessuna osservazione trovata nel file."
        )

    print()
    print("========================================")
    print("        TOPCON GTS-229 IMPORT")
    print("========================================")
    print()
    print(f"Stazione:             {station}")
    print(f"X stazione:           {STATION_X:.3f}")
    print(f"Y stazione:           {STATION_Y:.3f}")
    print(f"Z stazione:           {STATION_Z:.3f}")
    print(f"Altezza strumento:    {instrument_height:.3f} m")
    print(f"Altezza prisma:       {prism_height:.3f} m")
    print(f"Osservazioni:         {len(observations)}")
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
        # STAZIONE
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
        # PUNTI
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
                f"Punto {obs['point']:>3}: "
                f"X={x:>10.3f} "
                f"Y={y:>10.3f} "
                f"Z={z:>10.3f} "
                f"D={obs['distance_m']:.3f} m "
                f"H={obs['horizontal_gon']:.4f} gon "
                f"Z={obs['zenith_gon']:.4f} gon"
            )

    print()
    print(f"CSV creato: {output_path}")
    print()


if __name__ == "__main__":
    main()
