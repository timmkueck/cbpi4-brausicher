# CLAUDE.md – cbpi4-brausicher

Gilt zusätzlich zur übergeordneten `~/Claude/CLAUDE.md`. CraftBeerPi4-Plugin für die Brausteuerung auf dem Brau-Pi
(HAT „Biersteuerung“): Phase 1 der Brauzentrale (`Biersteuerung/docs/zentrale.md`). Teil des **Brau-Verbunds** von Timm. Gesamtbild in `docs/systeme.md` der Repos `Biersteuerung` und
`BrewPiLess-PCB` (gleiche Datei). Nachbar-Repos liegen lokal nebeneinander unter `~/Claude/`:
`cbpi4-brausicher`, `cbpi4-gaerdruck`, `cbpi4-ventil`, `cbpi4-simulation`, `cbpi4-bme280`, `Biersteuerung` (HAT,
Braubrücke, Plugin `cbpi4-kellersensor`).

## Inhalt

- `SicherFuehler` (DS18B20 mit Alter), `SicherAktor` (lgpio, Takt nach Leistung, Wächter-Thread, Heizschutz bei
  Art „Heizung“), `SicherHysterese` (Kessellogik, die bei Fehlern weiterläuft), `BrausicherFortsetzen` (aktiven
  Schritt nach Neustart mit Restzeit fortsetzen). Typnamen nicht umbenennen, sonst sind angelegte Teile ungültig.
- Grund für alles: Die eingebauten Teile versagen still (OneWire behält den letzten Wert, GPIOActor nimmt eine
  Attrappe, Hysteresis endet nach einer Ausnahme). Nichts davon zurückbauen.
- `technik.py` ohne cbpi (Ausgänge, DS18B20, Steuerung, Heizschutz), dort testbar; `SimAusgang` für Tests und die
  Simulation (`cbpi4-simulation` liest dessen Pegel `steuerung.a.an`).
- `systemd/brausteuerung-sicher.conf`: Ausgänge beim Start/Stopp von cbpi aus.

## Regeln

- **Echte Heizung, echte Ventile.** Nie gegen den Brau-Pi testen. Lokal nur mit `Simulation = Ja`.
- `SicherAktor` lehnt doppelte GPIOs ab (auch über `cbpi4-ventil`); im cbpi darf kein zweiter Aktor denselben GPIO
  schalten.
- Zweig `main`. Tests: `pip install cbpi4 pytest && python -m pytest tests/ -q`.
- UI-Texte und Meldungen mit Umlauten, Code-Kommentare ohne.

## cbpi-4.7.7-Fallen (gemessen, gelten für alle Plugins des Verbunds)

- Ein Sensor darf **nie `None`** liefern: Kessellogik, MashStep und Gärschritte vergleichen ungeprüft. Gültigkeit
  steckt im Alter des Werts (`alter()`).
- Aktor-`get_state()` liefert einen **bool**, kein dict, sonst zeigt die Oberfläche LED/SVG/Leitungen immer „an“.
- Schritte schalten nur mit `asyncio.create_task(self.next())` aus `run()` weiter.
- Kessel- und Gärtanklogik setzen im `finally` von `run()` `self.running = False`. cbpi stoppt sie am Schrittende nur
  über `state` und verweigert sonst den nächsten Start („already running“).
- Der eingebaute „Fermenter Autostart“ startet nichts (ruft `start()` statt `start_logic()`).
- `POST /kettle/{id}/off` liefert HTTP 500 (Methode fehlt), Kessel nur per `toggle` schalten.
- cbpi findet Plugins über `pkgutil.iter_modules`: `pip install -e` wird nicht geladen. Normal installieren oder
  `pip install -e . --config-settings editable_mode=compat`.
- Lokal testen: `web.run_app` bindet 0.0.0.0, also über einen Wrapper mit `host="127.0.0.1"` starten, eigener
  Konfigordner und Port (`cbpi --config-folder-path=… start`, Port in `config.yaml`).
