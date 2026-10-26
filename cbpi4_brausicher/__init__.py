"""CraftBeerPi4-Plugin: sichere Fuehler, Aktoren und Fortsetzen nach Neustart.

SicherFuehler   DS18B20, kennt das Alter seines letzten gueltigen Werts
                (der eingebaute OneWire-Sensor meldet bei Lesefehlern still
                den alten Wert weiter).
SicherAktor     Ausgang ueber lgpio, Takt nach Leistung, Waechter-Thread,
                bei Art "Heizung" Heizschutz ueber einen SicherFuehler.
SicherHysterese Kessellogik, die bei Fehlern nicht stehen bleibt (die
                eingebaute Hysteresis endet nach einer einzigen Ausnahme).
Fortsetzen      nimmt den aktiven Schritt nach einem Neustart wieder auf,
                mit Restzeit (cbpi 4.7.7 tut das nicht).
"""
import asyncio
import json
import logging
import os
import time

from cbpi.api import (CBPiActor, CBPiExtension, CBPiKettleLogic, CBPiSensor,
                      Property, action, on_startup, parameters)
from cbpi.api.dataclasses import NotificationType
from cbpi.api.step import StepState
from cbpi.api.timer import Timer

from . import technik
from .technik import (Heizschutz, LgpioAusgang, SimAusgang, Steuerung,
                      TechnikFehler)

log = logging.getLogger("cbpi4_brausicher")

GPIOS = list(range(2, 28))
HEIZUNG, RUEHRWERK, SONSTIGES = "Heizung", "Rührwerk", "Sonstiges"


def _zahl(props, name, vorgabe):
    try:
        wert = props.get(name, vorgabe)
        return float(vorgabe if wert in (None, "") else wert)
    except (TypeError, ValueError):
        return float(vorgabe)


def fremde_gpio_aktoren(cbpi, gpio, eigene_id):
    namen = []
    for item in getattr(cbpi.actor, "data", []):
        if item.id == eigene_id:
            continue
        props = item.props.to_dict() if hasattr(item.props, "to_dict") else dict(item.props or {})
        for schluessel in ("GPIO", "GPIO Ventil", "GPIO Durchfluss"):
            if str(props.get(schluessel, "")) == str(gpio):
                namen.append(item.name)
    return sorted(set(namen))


# --- Fuehler --------------------------------------------------------------

@parameters([
    Property.Select("Sensor", options=technik.ds18b20_geraete(), description="DS18B20 (1-Wire-Adresse)"),
    Property.Number("Offset", configurable=True, default_value=0, description="Korrektur in K"),
    Property.Number("Max Alter s", configurable=True, default_value=10,
                    description="Ohne gültigen Wert so lange -> Meldung, Heizschutz sperrt"),
])
class SicherFuehler(CBPiSensor):

    def __init__(self, cbpi, id, props):
        super().__init__(cbpi, id, props)
        # Immer eine Zahl: Kessellogik und Maischschritte von cbpi vergleichen ohne
        # Pruefung und brechen bei None ab. Gueltigkeit steckt im Alter (alter()).
        self.value = 0.0
        self.zeit = None
        self.gemeldet = False
        self._roh = None

    async def start(self):
        await super().start()
        geraet = self.props.get("Sensor")
        wert = await asyncio.to_thread(technik.ds18b20_lesen, geraet) if geraet else None
        if wert is not None:
            self._uebernehmen(wert)

    def _uebernehmen(self, wert):
        self._roh = wert
        self.value = round(wert + _zahl(self.props, "Offset", 0), 2)
        self.zeit = time.monotonic()

    def alter(self):
        """Sekunden seit dem letzten gueltigen Wert, None ohne Wert."""
        return None if self.zeit is None else time.monotonic() - self.zeit

    async def run(self):
        geraet = self.props.get("Sensor")
        max_alter = _zahl(self.props, "Max Alter s", 10)
        start = time.monotonic()
        while self.running:
            wert = await asyncio.to_thread(technik.ds18b20_lesen, geraet, self._roh) if geraet else None
            if wert is not None:
                self._uebernehmen(wert)
                self.push_update(self.value)
                self.log_data(self.value)
                if self.gemeldet:
                    self.gemeldet = False
                    self.cbpi.notify("Fühler", f"{geraet}: liefert wieder ({self.value} °C)",
                                     NotificationType.SUCCESS)
            else:
                alter = self.alter() if self.zeit is not None else time.monotonic() - start
                if alter > max_alter and not self.gemeldet:
                    self.gemeldet = True
                    log.error("Fühler %s: seit %.0f s kein gültiger Wert", geraet, alter)
                    self.cbpi.notify("Fühler", f"{geraet or 'kein Fühler gewählt'}: seit {alter:.0f} s "
                                     "kein gültiger Wert. Heizungen mit diesem Fühler sind gesperrt.",
                                     NotificationType.ERROR)
            await asyncio.sleep(1)

    def get_state(self):
        return dict(value=self.value)

    def get_unit(self):
        return "°C"


# --- Aktor ----------------------------------------------------------------

@parameters([
    Property.Select("GPIO", options=GPIOS, description="Ausgang (BCM). HAT: Kessel 27, Heizelement 20, Rührwerk 18, "
                    "Kühlschrank 19, Ventile 16/17/26/13, Kühlspirale 10, Dampfkondensator 11"),
    Property.Select("Aktiv bei", options=["HIGH", "LOW"], description="HAT Biersteuerung: HIGH"),
    Property.Select("Art", options=[HEIZUNG, RUEHRWERK, SONSTIGES],
                    description="Heizung braucht einen SicherFuehler"),
    Property.Sensor("Fühler", description="SicherFuehler im Kessel (nur für Heizung)"),
    Property.Number("Max Temperatur", configurable=True, default_value=105, description="darüber aus und gesperrt"),
    Property.Number("Anstieg Minuten", configurable=True, default_value=10,
                    description="So lange voll heizen ohne Anstieg -> gesperrt (0 = aus)"),
    Property.Number("Mindestanstieg K", configurable=True, default_value=1, description="erwarteter Anstieg"),
    Property.Number("Anstieg ab °C ignorieren", configurable=True, default_value=95,
                    description="Beim Kochen steigt die Temperatur nicht mehr"),
    Property.Select("Takt s", options=[2, 5], description="Grundtakt für Leistung unter 100 %"),
    Property.Select("Simulation", options=["Nein", "Ja"], description="Ja = ohne Hardware"),
])
class SicherAktor(CBPiActor):

    async def start(self):
        self.steuerung = None
        self.schutz = None
        self.state = False
        self.power = 100
        self._gemeldet = ""
        p = self.props
        try:
            self.gpio = int(p.get("GPIO") if p.get("GPIO") not in (None, "") else -1)
            if self.gpio < 0:
                raise TechnikFehler("Kein GPIO gewählt")
            andere = fremde_gpio_aktoren(self.cbpi, self.gpio, self.id)
            if andere:
                raise TechnikFehler(f"GPIO {self.gpio} wird auch von {', '.join(andere)} benutzt")
            self.art = p.get("Art") or SONSTIGES
            aktiv_high = p.get("Aktiv bei", "HIGH") != "LOW"
            ausgang = (SimAusgang if p.get("Simulation", "Nein") == "Ja" else LgpioAusgang)(self.gpio, aktiv_high)
            self.steuerung = Steuerung(ausgang, takt_s=_zahl(p, "Takt s", 5))
            if self.art == HEIZUNG:
                self.schutz = Heizschutz(
                    max_temp=_zahl(p, "Max Temperatur", 105),
                    anstieg_min=_zahl(p, "Anstieg Minuten", 10), anstieg_k=_zahl(p, "Mindestanstieg K", 1),
                    anstieg_ab=_zahl(p, "Anstieg ab °C ignorieren", 95))
                self.steuerung.freigabe = False  # erst mit gueltigem Fuehlerwert
        except Exception as e:
            log.error("Aktor %s nicht bereit: %s", self.id, e)
            self.cbpi.notify("Aktor nicht bereit", str(e), NotificationType.ERROR)
            self.steuerung = None

    def _fuehler(self):
        item = self.cbpi.sensor.find_by_id(self.props.get("Fühler")) if self.props.get("Fühler") else None
        inst = getattr(item, "instance", None)
        return inst if isinstance(inst, SicherFuehler) else None

    async def on(self, power=None, output=None):
        if self.steuerung is None:
            self.cbpi.notify("Aktor", "Nicht bereit, siehe Log", NotificationType.ERROR)
            return
        if self.art == HEIZUNG and self._fuehler() is None:
            self.cbpi.notify("Heizung", "Ohne Fühler vom Typ SicherFuehler wird nicht geheizt.",
                             NotificationType.ERROR)
            return
        if power is not None:
            self.power = int(power)
        self.steuerung.leistung = self.power
        self.steuerung.wunsch = True
        self.state = True

    async def off(self):
        if self.steuerung is not None:
            self.steuerung.wunsch = False
        self.state = False

    async def set_power(self, power):
        self.power = max(0, min(100, int(power)))
        if self.steuerung is not None:
            self.steuerung.leistung = self.power
        await self.cbpi.actor.actor_update(self.id, self.power)

    @action("Schutz zurücksetzen", parameters=[])
    async def schutz_zuruecksetzen(self, **kwargs):
        if self.schutz is not None:
            self.schutz.zuruecksetzen()
            self._gemeldet = ""
            self.cbpi.notify("Heizung", "Heizschutz zurückgesetzt", NotificationType.INFO)

    def get_state(self):
        # Ein bool wie GPIOActor: Die Oberflaeche wertet den Wert direkt aus,
        # ein dict waere dort immer "an" (LED, SVG, Leitungen).
        return self.state

    def _melden(self, text, art):
        if text and text != self._gemeldet:
            log.warning("Aktor %s: %s", self.id, text)
            self.cbpi.notify("Heizung" if self.art == HEIZUNG else "Aktor", text, art)
        self._gemeldet = text

    async def run(self):
        while self.running:
            s = self.steuerung
            if s is not None:
                s.herz()
                if self.schutz is not None:
                    f = self._fuehler()
                    wert, alter = (f.value, f.alter()) if f is not None else (None, None)
                    erlaubt, grund = self.schutz.pruefen(wert, alter, s.wunsch and s.leistung >= 50)
                    s.freigabe = erlaubt
                    if self.state:
                        self._melden(grund, NotificationType.ERROR if self.schutz.verriegelt
                                     else NotificationType.WARNING)
                    elif not self.schutz.verriegelt:
                        self._gemeldet = ""
                if s.fehler:
                    self._melden(s.fehler, NotificationType.ERROR)
            await asyncio.sleep(0.5)

    async def stop(self):
        self._beenden()

    async def on_stop(self):
        self._beenden()

    def _beenden(self):
        s = getattr(self, "steuerung", None)
        if s is not None:
            s.beenden()
        self.state = False


# --- Kessellogik ----------------------------------------------------------------

@parameters([
    Property.Number("OffsetOn", configurable=True, default_value=0.5, description="so viel unter Soll: Heizung an"),
    Property.Number("OffsetOff", configurable=True, default_value=0, description="so viel unter Soll: Heizung aus"),
])
class SicherHysterese(CBPiKettleLogic):
    """Wie die eingebaute Hysteresis, aber ein Fehler beendet die Regelung nicht:
    Heizung aus, Meldung, im naechsten Durchlauf weiter."""

    async def run(self):
        an_ab = _zahl(self.props, "OffsetOn", 0.5)
        aus_ab = _zahl(self.props, "OffsetOff", 0)
        gemeldet = ""
        heizung = None
        try:
            while self.running:
                try:
                    kessel = self.get_kessel()
                    heizung = kessel.heater
                    wert = self.get_sensor_value(kessel.sensor).get("value")
                    soll = float(self.get_kettle_target_temp(self.id))
                    if not isinstance(wert, (int, float)):
                        raise ValueError(f"kein Fühlerwert ({wert!r})")
                    item = self.cbpi.actor.find_by_id(heizung)
                    heizt = bool(getattr(getattr(item, "instance", None), "state", False))
                    if wert < soll - an_ab and not heizt:
                        await self.actor_on(heizung)
                    elif wert >= soll - aus_ab and heizt:
                        await self.actor_off(heizung)
                    if gemeldet:
                        gemeldet = ""
                        self.cbpi.notify("Kessel", "Regelung läuft wieder", NotificationType.SUCCESS)
                except Exception as e:
                    text = f"Regelung gestört, Heizung aus: {e}"
                    if text != gemeldet:
                        gemeldet = text
                        log.error("SicherHysterese %s: %s", self.id, text)
                        self.cbpi.notify("Kessel", text, NotificationType.ERROR)
                    if heizung:
                        try:
                            await self.actor_off(heizung)
                        except Exception:
                            pass
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Wie die eingebaute Hysteresis: kettle.stop() setzt nur state zurueck,
            # ohne running=False verweigert cbpi den naechsten Start ("already running")
            self.running = False
            if heizung:
                await self.actor_off(heizung)

    def get_kessel(self):
        k = self.get_kettle(self.id)
        if k is None:
            raise ValueError("Kessel nicht gefunden")
        return k


# --- Fortsetzen nach Neustart ------------------------------------------------

class Fortsetzen(CBPiExtension):
    """Merkt sich alle 5 s den aktiven Schritt und seine Restzeit und nimmt ihn
    nach einem Neustart wieder auf -- nur, wenn der letzte Eintrag hoechstens
    MAX_PAUSE_S alt ist (kein Heizen am naechsten Morgen)."""

    MAX_PAUSE_S = 30 * 60
    DATEI = "brausicher_fortsetzen.json"

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.pfad = cbpi.config_folder.get_file_path(self.DATEI)
        self._task = None
        cbpi.register_on_startup(self)

    def _aktiv(self):
        return next((s for s in self.cbpi.step.profile if s.status == StepState.ACTIVE), None)

    @staticmethod
    def restzeit(instanz):
        t = getattr(instanz, "timer", None)
        task = getattr(t, "_task", None)
        if t is None or task is None or task.done() or not getattr(t, "end_time", None):
            return None
        return max(0, int(t.end_time - time.time()))

    def _lesen(self):
        try:
            with open(self.pfad) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _schreiben(self, daten):
        tmp = self.pfad + ".neu"
        with open(tmp, "w") as f:
            json.dump(daten, f)
        os.replace(tmp, self.pfad)

    @on_startup("brausicher_fortsetzen", order=99)
    async def beim_start(self):
        try:
            await self.fortsetzen()
        except Exception as e:
            log.exception("Fortsetzen")
            self.cbpi.notify("Fortsetzen", f"Fehler: {e}", NotificationType.ERROR)
        self._task = asyncio.create_task(self.merken())

    async def fortsetzen(self):
        step = self._aktiv()
        if step is None or step.instance is None or step.instance.running:
            return
        stand = self._lesen()
        pause = time.time() - stand.get("zeit", 0)
        if stand.get("schritt") != step.id or pause > self.MAX_PAUSE_S or pause < -60:
            self.cbpi.notify("Fortsetzen", f"Schritt „{step.name}“ war beim Beenden aktiv, wird aber nicht "
                             "fortgesetzt (zu lange aus oder kein Stand). Bitte prüfen und von Hand starten.",
                             NotificationType.WARNING)
            log.warning("Nicht fortgesetzt: %s (Pause %.0f s, Stand %s)", step.name, pause, stand)
            return
        rest = stand.get("rest")
        inst = step.instance
        if rest is not None and hasattr(inst, "on_timer_done") and hasattr(inst, "on_timer_update"):
            inst.timer = Timer(int(rest), on_update=inst.on_timer_update, on_done=inst.on_timer_done)
        await self.cbpi.step.start_step(step)
        text = f"Schritt „{step.name}“ nach {pause / 60:.0f} min Pause fortgesetzt"
        if rest is not None:
            text += f", Restzeit {Timer.format_time(int(rest))}"
        log.info(text)
        self.cbpi.notify("Fortsetzen", text, NotificationType.INFO)

    async def merken(self):
        while True:
            try:
                step = self._aktiv()
                if step is not None and step.instance is not None and step.instance.running:
                    self._schreiben({"schritt": step.id, "name": step.name, "zeit": time.time(),
                                     "rest": self.restzeit(step.instance)})
                elif os.path.exists(self.pfad):
                    os.remove(self.pfad)
            except Exception as e:
                log.error("Fortsetzen merken: %s", e)
            await asyncio.sleep(5)


def setup(cbpi):
    cbpi.plugin.register("SicherFuehler", SicherFuehler)
    cbpi.plugin.register("SicherAktor", SicherAktor)
    cbpi.plugin.register("SicherHysterese", SicherHysterese)
    cbpi.plugin.register("BrausicherFortsetzen", Fortsetzen)
