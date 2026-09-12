# topcon2csv

Conversione dei dati grezzi di una stazione totale **Topcon GTS-229** in un CSV
con coordinate XYZ, partendo dal dump seriale dello strumento.

## 1. Scaricare i dati dallo strumento

Collegare la stazione totale al PC con il cavo seriale (o con un adattatore
USB-seriale, tipicamente `/dev/ttyUSB0`).

Configurare la porta e mettersi in ascolto:

```sh
stty -F /dev/ttyUSB0 9600 cs8 -parenb cstopb -echo raw; cat /dev/ttyUSB0 > src.txt
```

Significato dei parametri: 9600 baud, 8 bit di dati, nessuna parità, 2 bit di
stop, nessun echo, nessuna elaborazione dei caratteri di controllo (`raw`) —
indispensabile perché il dump contiene byte STX/ETX che non devono essere
alterati.

Poi, sullo strumento, avviare il trasferimento dei dati (menu di
comunicazione / *send data*). I byte arrivano su `src.txt` mentre `cat` resta in
esecuzione: quando il trasferimento è finito, fermarlo con `Ctrl+C`.

Verifica rapida che il file contenga qualcosa:

```sh
ls -l src.txt
```

Se il file resta vuoto: controllare che la porta sia quella giusta
(`dmesg | tail` dopo aver collegato l'adattatore), che i baud rate impostati
sullo strumento e sulla porta coincidano, e che l'utente abbia accesso alla
seriale (gruppo `uucp` o `dialout` a seconda della distribuzione).

## 2. Convertire in CSV

Serve solo Python 3 (nessuna dipendenza esterna):

```sh
python3 topcon2csv.py src.txt -o topcon_xyz.csv
```

Opzioni:

- `input` — file grezzo scaricato dallo strumento (posizionale, obbligatorio)
- `-o`, `--output` — file CSV di output (default: `topcon_xyz.csv`)

Lo script stampa a schermo il riepilogo (stazione, altezza strumento, altezza
prisma, numero di osservazioni) e la lista dei punti calcolati.

## 3. Output

Il CSV contiene una riga per la stazione e una riga per ogni punto rilevato,
con le colonne:

| Colonna | Descrizione |
| --- | --- |
| `pid` | numero del punto |
| `station` | numero della stazione di riferimento |
| `x`, `y`, `z` | coordinate calcolate (m) |
| `distance` | distanza inclinata (m) |
| `horizontal_gon` | angolo orizzontale / cerchio orizzontale (gon) |
| `zenith_gon` | angolo zenitale (gon, 0 = zenit, 100 = orizzonte) |
| `instrument_height` | altezza strumento (m) |
| `prism_height` | altezza prisma (m) |

## 4. Configurazione

I parametri di calcolo sono costanti in testa a `topcon2csv.py`, da modificare
se il rilievo non parte da coordinate nulle:

- `STATION_X`, `STATION_Y`, `STATION_Z` — coordinate della stazione
  (default `0, 0, 0`: le coordinate risultano relative alla stazione)
- `HORIZONTAL_FROM_NORTH` — `True` (default): 0 gon = Nord, angoli crescenti in
  senso orario; `False`: 0 gon = asse X, angoli crescenti in senso antiorario

L'altezza strumento e l'altezza prisma vengono lette automaticamente dal file
grezzo.

## Note sul formato grezzo

Il dump seriale è suddiviso in blocchi da 132 caratteri delimitati da `STX`
(`0x02`) e `ETX CR LF` (`0x03 0x0D 0x0A`); le ultime 4 cifre di ogni blocco sono
un contatore del protocollo di trasferimento e **non** fanno parte dei dati.
Lo script le rimuove prima del parsing: leggere il file come testo continuo
senza questa pulizia spezza i campi numerici a cavallo dei confini di blocco e
fa perdere silenziosamente delle osservazioni.
