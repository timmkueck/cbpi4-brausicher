"""Tests ohne Hardware:  pip install cbpi4 pytest && python -m pytest tests/ -q"""
import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("cbpi")
from cbpi.api.dataclasses import Props  # noqa: E402
from cbpi.api.step import StepState  # noqa: E402

import cbpi4_brausicher as plugin  # noqa: E402
from cbpi4_brausicher import technik as T  # noqa: E402

GUT = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t={}\n"
SCHLECHT = "72 01 4b 46 7f ff 0e 10 57 : crc=00 NO\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"


class Uhr:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --- DS18B20 ----------------------------------------------------------------

def test_ds18b20_auswerten():
    assert T.ds18b20_auswerten(GUT.format(64250)) == 64.25
    assert T.ds18b20_auswerten(SCHLECHT) is None
    assert T.ds18b20_auswerten("") is None
    assert T.ds18b20_auswerten(GUT.format(-127000)) is None
    assert T.ds18b20_auswerten(GUT.format(85000), vorher=64.0) is None  # Einschaltwert
    assert T.ds18b20_auswerten(GUT.format(85000), vorher=84.5) == 85.0  # echte 85 Grad


def test_ds18b20_lesen(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "W1_PFAD", str(tmp_path))
    (tmp_path / "28-0001").mkdir()
    (tmp_path / "28-0001" / "w1_slave").write_text(GUT.format(21500))
    assert T.ds18b20_geraete() == ["28-0001"]
    assert T.ds18b20_lesen("28-0001") == 21.5
    assert T.ds18b20_lesen("28-fehlt") is None


# --- Steuerung -----------------------------------------------------------------

def test_takt_nach_leistung():
    uhr = Uhr()
    a = T.SimAusgang(27)
    s = T.Steuerung(a, takt_s=5, uhr=uhr, thread=False)
    s.wunsch, s.leistung = True, 40
    an = 0
    for i in range(500):  # 50 s in 0,1-s-Schritten
        uhr.t = 1000.0 + i * 0.1
        s.herz()
        s.schritt()
        an += a.an
    assert 190 <= an <= 210  # 40 %
    s.leistung = 100
    s.schritt()
    assert a.an
    s.freigabe = False
    s.schritt()
    assert not a.an


def test_waechter_schaltet_aus_wenn_cbpi_haengt():
    uhr = Uhr()
    a = T.SimAusgang(27)
    s = T.Steuerung(a, waechter_s=3, uhr=uhr, thread=False)
    s.wunsch = True
    s.schritt()
    assert a.an
    uhr.t += 3.5  # kein herz()
    s.schritt()
    assert not a.an and "reagiert nicht" in s.fehler
    s.herz()
    s.schritt()
    assert a.an and s.fehler == ""


def test_steuerung_mit_thread_und_beenden():
    a = T.SimAusgang(20)
    s = T.Steuerung(a, waechter_s=0.5)
    s.wunsch = True
    s.herz()
    time.sleep(0.3)
    assert a.an
    time.sleep(0.8)  # Herzschlag bleibt aus
    assert not a.an
    s.beenden()
    s.beenden()
    assert a.freigegeben


def test_schaltfehler_fuehrt_zu_aus():
    class Kaputt(T.SimAusgang):
        def setzen(self, an):
            if an:
                raise OSError("Leitung belegt")
            super().setzen(an)
    a = Kaputt(27)
    s = T.Steuerung(a, uhr=Uhr(), thread=False)
    s.wunsch = True
    s.schritt()
    assert not a.an and "Schalten fehlgeschlagen" in s.fehler


# --- Heizschutz ---------------------------------------------------------------

def test_heizschutz_fuehleralter():
    h = T.Heizschutz(max_alter_s=10)
    assert h.pruefen(60.0, 1.0, True) == (True, "")
    ok, grund = h.pruefen(60.0, 11.0, True)
    assert not ok and "älter" in grund
    assert h.pruefen(None, None, True)[0] is False
    assert h.pruefen(60.0, 2.0, True)[0] is True  # nicht verriegelnd


def test_heizschutz_uebertemperatur_verriegelt():
    h = T.Heizschutz(max_temp=105)
    ok, grund = h.pruefen(106.0, 1.0, True)
    assert not ok and "Übertemperatur" in grund
    assert h.pruefen(60.0, 1.0, True)[0] is False  # bleibt gesperrt
    h.zuruecksetzen()
    assert h.pruefen(60.0, 1.0, True)[0] is True


def test_heizschutz_kein_anstieg():
    uhr = Uhr()
    h = T.Heizschutz(anstieg_min=10, anstieg_k=1, anstieg_ab=95, uhr=uhr)
    assert h.pruefen(20.0, 1.0, True)[0]  # Beginn der Ueberwachung
    for _ in range(9):
        uhr.t += 60
        assert h.pruefen(20.3, 1.0, True)[0]
    uhr.t += 61  # 10 min seit Beginn, nur 0,5 K
    ok, grund = h.pruefen(20.5, 1.0, True)
    assert not ok and "Kein Anstieg" in grund


def test_heizschutz_anstieg_setzt_zurueck_und_kochen_ausgenommen():
    uhr = Uhr()
    h = T.Heizschutz(anstieg_min=10, anstieg_k=1, anstieg_ab=95, uhr=uhr)
    t = 50.0
    for _ in range(30):  # 30 min, je 1,2 K in 10 min
        uhr.t += 60
        t += 0.12
        assert h.pruefen(t, 1.0, True)[0]
    for _ in range(90):  # 90 min Kochen bei 99 Grad
        uhr.t += 60
        assert h.pruefen(99.0, 1.0, True)[0]
    for _ in range(30):  # Taktbetrieb (unter 50 %) zaehlt nicht
        uhr.t += 60
        assert h.pruefen(66.0, 1.0, False)[0]


def test_lgpio_fehlt_ist_ein_fehler(monkeypatch):
    monkeypatch.setitem(sys.modules, "lgpio", None)
    with pytest.raises(T.TechnikFehler, match="lgpio fehlt"):
        T.LgpioAusgang(27)


# --- Plugin gegen cbpi ------------------------------------------------------------

class FakeCbpi:
    def __init__(self, tmp_path=None):
        self.notes, self.registered, self.startup = [], {}, []
        self.log = types.SimpleNamespace(log_data=lambda id, v: None)
        self.ws = types.SimpleNamespace(send=lambda d, s=False: None)
        self.plugin = types.SimpleNamespace(register=lambda n, c: self.registered.__setitem__(n, c))
        self.actor = types.SimpleNamespace(data=[], actor_update=self._noop)
        self.sensor = types.SimpleNamespace(data=[], find_by_id=lambda i: next(
            (s for s in self.sensor.data if s.id == i), None))
        self.config_folder = types.SimpleNamespace(get_file_path=lambda f: str(tmp_path / f))

    async def _noop(self, *a):
        return True

    def push_update(self, *a, **k):
        pass

    def notify(self, title, message, type=None, action=[]):
        self.notes.append((title, message, type))

    def register_on_startup(self, obj):
        self.startup.append(obj)


def fuehler_anlegen(c, wert=60.0, alter=1.0):
    f = plugin.SicherFuehler(c, "f1", Props({"Sensor": "28-0001"}))
    f.value = wert
    f.zeit = None if alter is None else time.monotonic() - alter
    c.sensor.data.append(types.SimpleNamespace(id="f1", name="Kessel", instance=f))
    return f


def aktor(c, **props):
    p = {"GPIO": 27, "Art": "Heizung", "Fühler": "f1", "Simulation": "Ja", **props}
    a = plugin.SicherAktor(c, "a1", Props(p))
    c.actor.data.append(types.SimpleNamespace(id="a1", name="Kessel", props=Props(p), instance=a))
    return a


async def laufen_lassen(a, sekunden):
    a.running = True
    t = asyncio.create_task(a.run())
    await asyncio.sleep(sekunden)
    a.running = False
    await t


def test_setup_registriert():
    c = FakeCbpi()
    plugin.setup(c)
    assert set(c.registered) == {"SicherFuehler", "SicherAktor", "SicherHysterese", "BrausicherFortsetzen"}


def test_heizung_ohne_sicherfuehler_heizt_nicht():
    async def go():
        c = FakeCbpi()
        a = aktor(c, **{"Fühler": None})
        await a.start()
        await a.on(100)
        await a.stop()
        return c, a
    c, a = asyncio.run(go())
    assert not a.state and any("SicherFuehler" in n[1] for n in c.notes)


def test_heizung_mit_frischem_fuehler_heizt():
    async def go():
        c = FakeCbpi()
        fuehler_anlegen(c, 60.0, 1.0)
        a = aktor(c)
        await a.start()
        await a.on(100)
        await laufen_lassen(a, 0.8)
        an = a.steuerung.a.an
        await a.stop()
        return an
    assert asyncio.run(go()) is True


def test_heizung_mit_altem_fuehler_aus_und_meldung():
    async def go():
        c = FakeCbpi()
        fuehler_anlegen(c, 60.0, 30.0)
        a = aktor(c)
        await a.start()
        await a.on(100)
        await laufen_lassen(a, 0.8)
        an = a.steuerung.a.an
        await a.stop()
        return c, an
    c, an = asyncio.run(go())
    assert an is False and any("älter" in n[1] for n in c.notes)


def test_uebertemperatur_sperrt_bis_zuruecksetzen():
    async def go():
        c = FakeCbpi()
        f = fuehler_anlegen(c, 107.0, 1.0)
        a = aktor(c)
        await a.start()
        await a.on(100)
        await laufen_lassen(a, 0.8)
        gesperrt = not a.steuerung.a.an
        f.value = 60.0
        await laufen_lassen(a, 0.8)
        noch = not a.steuerung.a.an
        await a.schutz_zuruecksetzen()
        await laufen_lassen(a, 0.8)
        wieder = a.steuerung.a.an
        await a.stop()
        return c, gesperrt, noch, wieder
    c, gesperrt, noch, wieder = asyncio.run(go())
    assert gesperrt and noch and wieder
    assert any("Übertemperatur" in n[1] for n in c.notes)


def test_ruehrwerk_ohne_fuehler():
    async def go():
        c = FakeCbpi()
        a = aktor(c, Art="Rührwerk", GPIO=18, **{"Fühler": None})
        await a.start()
        await a.on(100)
        await laufen_lassen(a, 0.5)
        an = a.steuerung.a.an
        await a.off()
        await laufen_lassen(a, 0.5)
        aus = not a.steuerung.a.an
        await a.stop()
        return an, aus, a.steuerung.a.freigegeben
    assert asyncio.run(go()) == (True, True, True)


def test_gpio_doppelt_abgelehnt():
    async def go():
        c = FakeCbpi()
        c.actor.data.append(types.SimpleNamespace(id="v", name="Brauwasser",
                                                  props=Props({"GPIO Ventil": 27}), instance=None))
        a = aktor(c)
        await a.start()
        return c, a
    c, a = asyncio.run(go())
    assert a.steuerung is None and any("Brauwasser" in n[1] for n in c.notes)


def test_sicherfuehler_liest_und_meldet_ausfall(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "W1_PFAD", str(tmp_path))
    (tmp_path / "28-0001").mkdir()
    datei = tmp_path / "28-0001" / "w1_slave"
    datei.write_text(GUT.format(63500))

    async def go():
        c = FakeCbpi()
        f = plugin.SicherFuehler(c, "f1", Props({"Sensor": "28-0001", "Max Alter s": 2, "Offset": 0.5}))
        f.push_update = lambda v: None
        f.running = True
        t = asyncio.create_task(f.run())
        await asyncio.sleep(1.5)
        wert, alter = f.value, f.alter()
        datei.write_text(SCHLECHT)  # CRC-Fehler: alter Wert darf nicht frisch bleiben
        await asyncio.sleep(3.5)
        alt = f.alter()
        f.running = False
        await t
        return c, wert, alter, alt
    c, wert, alter, alt = asyncio.run(go())
    assert wert == 64.0 and alter < 1.5 and alt > 2
    assert any("kein gültiger Wert" in n[1] for n in c.notes)


# --- Fortsetzen ---------------------------------------------------------------

def _schritt_mit_timer():
    class Inst:
        def __init__(self):
            self.running = False
            self.timer = None
            self.gestartet = False

        async def on_timer_done(self, timer):
            pass

        async def on_timer_update(self, timer, s):
            pass
    return types.SimpleNamespace(id="s1", name="Rast 63", status=StepState.ACTIVE, instance=Inst())


def _fortsetzen(tmp_path, stand):
    c = FakeCbpi(tmp_path)
    step = _schritt_mit_timer()
    gestartet = []

    async def start_step(s):
        gestartet.append(s)
    c.step = types.SimpleNamespace(profile=[step], start_step=start_step)
    if stand is not None:
        (tmp_path / plugin.Fortsetzen.DATEI).write_text(json.dumps(stand))
    ext = plugin.Fortsetzen(c)
    asyncio.run(ext.fortsetzen())
    return c, step, gestartet


def test_fortsetzen_mit_restzeit(tmp_path):
    c, step, gestartet = _fortsetzen(tmp_path, {"schritt": "s1", "zeit": time.time() - 120, "rest": 1500})
    assert gestartet == [step] and step.instance.timer._timemout == 1500
    assert any("Restzeit 00:25:00" in n[1] for n in c.notes)


def test_nicht_fortsetzen_nach_langer_pause(tmp_path):
    c, step, gestartet = _fortsetzen(tmp_path, {"schritt": "s1", "zeit": time.time() - 3 * 3600, "rest": 1500})
    assert gestartet == [] and any("nicht fortgesetzt" in n[1] for n in c.notes)


def test_nicht_fortsetzen_ohne_stand(tmp_path):
    c, step, gestartet = _fortsetzen(tmp_path, None)
    assert gestartet == []


def test_fortsetzen_meldet_sich_beim_start_an(tmp_path):
    c = FakeCbpi(tmp_path)
    ext = plugin.Fortsetzen(c)
    assert c.startup == [ext]


# --- Kessellogik und Startwert ----------------------------------------------------

def test_fuehler_hat_vor_dem_ersten_lauf_eine_zahl(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "W1_PFAD", str(tmp_path))
    (tmp_path / "28-0001").mkdir()
    (tmp_path / "28-0001" / "w1_slave").write_text(GUT.format(48000))
    c = FakeCbpi()
    f = plugin.SicherFuehler(c, "f1", Props({"Sensor": "28-0001"}))
    assert f.get_state() == {"value": 0.0}  # nie None
    asyncio.run(f.start())
    assert f.get_state() == {"value": 48.0} and f.alter() < 1


def test_sicherhysterese_ueberlebt_fehler():
    async def go():
        c = FakeCbpi()
        werte = {"v": None}
        heizung = types.SimpleNamespace(id="h", instance=types.SimpleNamespace(state=False))
        schaltungen = []

        async def on(id, power=100, output=None):
            heizung.instance.state = True
            schaltungen.append("an")

        async def off(id):
            heizung.instance.state = False
            schaltungen.append("aus")
        c.actor = types.SimpleNamespace(data=[heizung], on=on, off=off,
                                        find_by_id=lambda i: heizung if i == "h" else None)
        kessel = types.SimpleNamespace(heater="h", sensor="f", target_temp=63)
        c.kettle = types.SimpleNamespace(find_by_id=lambda i: kessel)
        c.sensor = types.SimpleNamespace(get_sensor_value=lambda i: {"value": werte["v"]})
        logik = plugin.SicherHysterese(c, "k", Props({"OffsetOn": 0.5, "OffsetOff": 0}))
        logik.running = True
        t = asyncio.create_task(logik.run())
        await asyncio.sleep(1.5)          # None: Fehler, Regelung laeuft weiter
        werte["v"] = 55.0
        await asyncio.sleep(1.2)          # jetzt heizen
        an = heizung.instance.state
        werte["v"] = "kaputt"
        await asyncio.sleep(1.2)          # Fehler: aus
        aus = not heizung.instance.state
        werte["v"] = 64.0
        await asyncio.sleep(1.2)
        logik.running = False
        await t
        return c, an, aus, t.done()
    c, an, aus, fertig = asyncio.run(go())
    assert an and aus and fertig
    assert any("Regelung gestört" in n[1] for n in c.notes)
    assert any("läuft wieder" in n[1] for n in c.notes)
