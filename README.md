# cbpi4-brausicher – Brausteuerung, die nicht still versagt

CraftBeerPi4-Plugin für den Brau-Pi mit HAT „Biersteuerung“. Phase 1 aus [`docs/zentrale.md`](https://github.com/timmkueck/Biersteuerung/blob/systemverbund/docs/zentrale.md) im Repo Biersteuerung:
Der Pi regelt autonom, und das so, dass ein Fehler auffällt und der Sud nicht leidet.

| In CraftBeerPi | Typ | Ersetzt |
|---|---|---|
| Sensor | **SicherFuehler** (DS18B20) | OneWire |
| Aktor | **SicherAktor** (Kessel, Heizelement, Rührwerk) | GPIOActor |
| Kessellogik | **SicherHysterese** | Hysteresis |
| Erweiterung | **Fortsetzen** nach Neustart (läuft von selbst) | – |

## Was an den eingebauten Teilen gefährlich ist

Gefunden im Quelltext von CraftBeerPi 4.7.7 und im Test nachgestellt:

| Eingebaut | Problem | Folge beim Brauen |
|---|---|---|
| OneWire-Sensor | Lesefehler werden verschluckt (`except: pass`), der **letzte Wert bleibt stehen** | Fühler fällt ab bei 60 °C, Soll 63 °C → die Heizung läuft ohne Ende |
| GPIOActor | Fehlt `RPi.GPIO`, wird **still eine Attrappe** eingesetzt | Oberfläche zeigt „Heizung an“, nichts heizt |
| Hysteresis | Eine einzige Ausnahme (z. B. Fühlerwert `None`) **beendet die Regelung** bis zum Neustart | Kessel regelt nicht mehr, kaum sichtbar |
| Schrittsteuerung | Ein beim Neustart aktiver Schritt wird **nicht fortgesetzt** | nach Absturz oder Stromspitze steht der Sud |
| MashStep-Timer | beginnt nach Neustart wieder bei der vollen Rastzeit | Rast zu lang |

## Was dieses Plugin macht

**SicherFuehler** liest den DS18B20 selbst und verwirft ungültige Werte: CRC falsch, außerhalb −55…125 °C,
85,0 °C (Einschaltwert nach Spannungseinbruch). Er kennt das Alter seines letzten gültigen Werts. Ab
„Max Alter s“ (10 s) ohne gültigen Wert kommt eine Meldung, und Heizungen mit diesem Fühler sind gesperrt.
Für die Anzeige liefert er immer eine Zahl (vor der ersten Messung 0), weil cbpi-Teile an `None` abbrechen.

**SicherAktor** schaltet über lgpio. Ein eigener Thread setzt den Ausgang: Bei Leistung unter 100 % taktet er
wie der GPIOActor (Takt 2 oder 5 s). Hängt CraftBeerPi länger als 3 s, schaltet der Thread aus. Nach jedem
Schalten liest er den Pegel zurück. Beim Beenden schaltet er aus und gibt den GPIO frei. Ein GPIO, den ein
anderer Aktor schon nutzt (auch das Wasserventil auf 19), wird abgelehnt.

Bei **Art = Heizung** gilt der Heizschutz:

| Regel | Vorgabe | Art |
|---|---|---|
| Fühler muss vom Typ SicherFuehler sein | – | ohne heizt nichts |
| Fühlerwert älter als 10 s | Max Alter s am Fühler | sperrt, geht von selbst weiter, sobald der Fühler wieder liefert |
| Übertemperatur | 105 °C | sperrt, **Aktion „Schutz zurücksetzen“** nötig |
| Kein Anstieg trotz vollem Heizen (≥ 50 %) | 1 K in 10 min, ab 95 °C nicht geprüft (Kochen) | sperrt, „Schutz zurücksetzen“ nötig. Fängt einen Fühler, der neben dem Kessel liegt, und eine ausgefallene Heizung |

Gesperrt heißt: Der Ausgang ist aus, CraftBeerPi zeigt weiter „an“ (gewünscht), eine Meldung sagt warum.

**SicherHysterese** regelt wie die eingebaute Hysteresis. Bei einem Fehler schaltet sie die Heizung aus, meldet
sich und regelt im nächsten Durchlauf weiter.

**Fortsetzen** merkt sich alle 5 s den aktiven Schritt und seine Restzeit (`config/brausicher_fortsetzen.json`).
Nach einem Neustart setzt es den Schritt fort, wenn die Pause höchstens 30 min war, mit der gemerkten Restzeit
(bis zu 5 s länger). War sie länger, meldet es sich und lässt den Schritt stehen: Am nächsten Morgen heizt
nichts von selbst. Wasserschritte von `cbpi4-ventil` öffnen nach einem Absturz absichtlich nicht.

## Belegung HAT „Biersteuerung“

| Aktor | GPIO | Art | Aktiv bei |
|---|---|---|---|
| Kessel (J4) | 27 | Heizung, Fühler im Kessel | HIGH |
| Heizelement (J8) | 20 | Heizung, Fühler im Kessel | HIGH |
| Rührwerk (J7) | 18 | Rührwerk | HIGH |
| Wasserventil (J13) | 19 | **nicht hier**, sondern `cbpi4-ventil` | HIGH |

## Einbau

Voraussetzungen wie bei `cbpi4-ventil` (lgpio im Python von CraftBeerPi, normal installieren, nicht mit `-e`):

```bash
cd ~ && git clone https://github.com/timmkueck/cbpi4-brausicher.git
~/cbpi4-venv/bin/pip install lgpio ~/cbpi4-brausicher
sudo mkdir -p /etc/systemd/system/craftbeerpi.service.d
sudo cp ~/cbpi4-brausicher/systemd/brausteuerung-sicher.conf /etc/systemd/system/craftbeerpi.service.d/
sudo rm -f /etc/systemd/system/craftbeerpi.service.d/ventil-zu.conf
sudo systemctl daemon-reload && sudo systemctl restart craftbeerpi
journalctl -u craftbeerpi | grep cbpi4_brausicher
```

`brausteuerung-sicher.conf` setzt die GPIOs 27, 20, 18, 19, 21 und 22 vor jedem Start und nach jedem Ende auf aus,
startet nach einem Absturz neu und ersetzt `ventil-zu.conf`. Der Dienstname `craftbeerpi` ist angenommen:
`systemctl list-units | grep -i craft` zeigt den richtigen.

Danach in CraftBeerPi umstellen: Fühler auf **SicherFuehler**, die Aktoren Kessel, Heizelement und Rührwerk auf
**SicherAktor**, die Kessellogik auf **SicherHysterese**. Alte GPIOActor auf denselben GPIOs vorher löschen.

## Probelauf mit Wasser (Nachweis Phase 1)

Bevor gebraut wird, mit Wasser im Kessel:

1. Rast 40 °C, 5 min starten. Heizung an, bei 40 °C aus, Timer läuft.
2. Kesselfühler abziehen. Nach etwa 10 s: Meldung, Heizung aus (Schütz hörbar). Wieder anstecken: weiter.
3. Während der Rast `sudo systemctl kill -s KILL craftbeerpi`. Ausgänge aus, Neustart nach 3 s, Meldung
   „fortgesetzt, Restzeit …“, Heizung regelt weiter.
4. Fühler aus dem Kessel nehmen und an die Luft legen, Soll 60 °C: nach 10 min „Kein Anstieg“, Heizung gesperrt.
5. Rührwerk an und aus.

## Tests

```bash
pip install cbpi4 pytest
python -m pytest tests/ -q
```

25 Tests: DS18B20-Auswertung, Takt, Wächter, Schaltfehler, Heizschutz (Alter, Übertemperatur, Anstieg, Kochen),
Aktoren und Kessellogik gegen das installierte cbpi4, Fortsetzen.

Am 19.10.2026 lief das Plugin außerdem in einem echten CraftBeerPi 4.7.7 (Simulation, nur an 127.0.0.1, nachgebildetes
1-Wire-Verzeichnis über `BRAUSICHER_W1_PFAD`):

- Rast 63 °C: Heizung an bei 55 °C, aus bei 64 °C, der Timer startet.
- `kill -9` mitten in der Rast: Der Schritt wird mit Restzeit fortgesetzt und heizt bei 61 °C wieder.
- Fühler mit CRC-Fehler: nach 10 s Meldung und Heizung aus. Fühler wieder gültig: Heizung an.

Dabei fiel auch der Absturz der eingebauten Hysteresis bei `None` auf.

25.10.2026: `SicherAktor.get_state()` liefert jetzt `self.state` (bool) statt `dict(state=…)`. Mit dem dict zeigte
jedes Dashboard (LED, SVG, Leitungen) den Aktor dauerhaft als eingeschaltet. Gefunden beim Gärdruck-Dashboard
([cbpi4-gaerdruck](https://github.com/timmkueck/cbpi4-gaerdruck)).

25.10.2026: `SicherHysterese` setzt beim Beenden `running = False` (wie die eingebaute Hysteresis). Vorher startete sie
ab der zweiten Rast nicht mehr: cbpi 4.7.7 stoppt die Kessellogik am Ende jedes Schritts nur über `state`, der nächste
Schritt bekam „KettleController already running“, und der Kessel heizte nicht. Gefunden im simulierten Brautag
([cbpi4-simulation](https://github.com/timmkueck/cbpi4-simulation)), Regressionstest dort.

**Nicht getestet mit echter Hardware:** lgpio-Ausgänge am Pi, echte DS18B20, `pinctrl` und `modprobe` im systemd-Zusatz.
Dafür ist der Probelauf mit Wasser da.

## Brau-Verbund

Gehört zur Brauerei von Timm: Brau-Pi mit HAT [Biersteuerung](https://github.com/timmkueck/Biersteuerung) und CraftBeerPi 4.7.7.
Gesamtbild: `docs/systeme.md` im Repo Biersteuerung.

| Repo | Inhalt |
|---|---|
| [cbpi4-brausicher](https://github.com/timmkueck/cbpi4-brausicher) | sichere Fühler, Aktoren (Heizschutz, Wächter), Kessellogik, Fortsetzen nach Neustart |
| [cbpi4-gaerdruck](https://github.com/timmkueck/cbpi4-gaerdruck) | Druckgärung im FermZilla: Drucksensor, Spunden/Nachdrücken, Kegs, Dashboard 2 |
| [cbpi4-ventil](https://github.com/timmkueck/cbpi4-ventil) | Brauwasser dosieren (Magnetventil + Durchflussmesser) |
| [cbpi4-simulation](https://github.com/timmkueck/cbpi4-simulation) | ganzer Brautag als Simulation, Störfälle, Prüfung der Schutzfunktionen |
| [cbpi4-bme280](https://github.com/timmkueck/cbpi4-bme280) | BME280-Sensor (meldet Fehler statt still leer) |
