"""Hardwarenahe Teile ohne cbpi-Abhaengigkeit: Ausgaenge, DS18B20 lesen, Ausgangssteuerung.

Grundsatz: Im Zweifel ist der Ausgang aus. Keine stille Attrappe, wenn
Hardware oder Bibliothek fehlt.
"""
import logging
import os
import threading
import time

log = logging.getLogger("cbpi4_brausicher")

W1_PFAD = os.environ.get("BRAUSICHER_W1_PFAD", "/sys/bus/w1/devices")


class TechnikFehler(Exception):
    pass


# --- Ausgaenge ----------------------------------------------------------

class SimAusgang:
    name = "Simulation"

    def __init__(self, gpio, aktiv_high=True):
        self.gpio = gpio
        self.an = False
        self.freigegeben = False
        self.wechsel = 0

    def setzen(self, an):
        if bool(an) != self.an:
            self.wechsel += 1
            log.info("Simulation GPIO %s -> %s", self.gpio, "AN" if an else "AUS")
        self.an = bool(an)

    def freigeben(self):
        self.an = False
        self.freigegeben = True


class LgpioAusgang:
    """Ein GPIO-Ausgang ueber lgpio, sofort im Aus-Zustand belegt, mit Rueckleseprobe."""

    name = "lgpio"

    def __init__(self, gpio, aktiv_high=True, chip=0):
        try:
            import lgpio
        except ImportError as e:
            raise TechnikFehler("Python-Modul lgpio fehlt im Python von CraftBeerPi "
                                "(pip install lgpio oder apt install python3-lgpio)") from e
        self.lg = lgpio
        self.gpio = int(gpio)
        self.pegel_an = 1 if aktiv_high else 0
        self.pegel_aus = 1 - self.pegel_an
        self.an = False
        self.h = lgpio.gpiochip_open(chip)
        try:
            lgpio.gpio_claim_output(self.h, self.gpio, self.pegel_aus)
        except Exception:
            lgpio.gpiochip_close(self.h)
            raise

    def setzen(self, an):
        soll = self.pegel_an if an else self.pegel_aus
        self.lg.gpio_write(self.h, self.gpio, soll)
        ist = self.lg.gpio_read(self.h, self.gpio)
        if ist != soll:
            raise TechnikFehler(f"GPIO {self.gpio}: geschrieben {soll}, gelesen {ist}")
        self.an = bool(an)

    def freigeben(self):
        for schritt in (lambda: self.lg.gpio_write(self.h, self.gpio, self.pegel_aus),
                        lambda: self.lg.gpio_free(self.h, self.gpio),
                        lambda: self.lg.gpiochip_close(self.h)):
            try:
                schritt()
            except Exception as e:
                log.warning("Freigeben GPIO %s: %s", self.gpio, e)
        self.an = False


# --- DS18B20 ------------------------------------------------------------

def ds18b20_geraete():
    try:
        return sorted(d for d in os.listdir(W1_PFAD) if d.startswith(("28-", "10-")))
    except OSError:
        return []


def ds18b20_auswerten(inhalt, vorher=None):
    """Wert in Grad C aus dem Inhalt von w1_slave, oder None bei ungueltigem Wert.

    Ungueltig: CRC falsch, ausserhalb -55..125, oder 85,0 (Einschaltwert des
    DS18B20 nach Spannungseinbruch), solange der vorige Wert nicht nahe 85 lag."""
    zeilen = inhalt.strip().split("\n")
    if len(zeilen) < 2 or not zeilen[0].rstrip().endswith("YES") or "t=" not in zeilen[1]:
        return None
    try:
        t = int(zeilen[1].split("t=")[-1]) / 1000.0
    except ValueError:
        return None
    if not -55.0 <= t <= 125.0:
        return None
    if t == 85.0 and (vorher is None or abs(vorher - 85.0) > 2.0):
        return None
    return t


def ds18b20_lesen(geraet, vorher=None):
    try:
        with open(os.path.join(W1_PFAD, geraet, "w1_slave")) as f:
            return ds18b20_auswerten(f.read(), vorher)
    except OSError:
        return None


# --- Ausgangssteuerung mit Waechter ---------------------------------------

class Steuerung:
    """Setzt den Ausgang in einem eigenen Thread: Takt (Leistung in %) und Freigabe.

    `wunsch`, `leistung` und `freigabe` setzt der cbpi-Aktor aus der asyncio-
    Schleife; dazu `herz()` mindestens alle `waechter_s`. Bleibt der Herzschlag
    aus (cbpi haengt), schaltet der Thread den Ausgang aus."""

    def __init__(self, ausgang, takt_s=5.0, waechter_s=3.0, uhr=time.monotonic, thread=True):
        self.a = ausgang
        self.takt_s = float(takt_s)
        self.waechter_s = float(waechter_s)
        self.uhr = uhr
        self.wunsch = False
        self.leistung = 100
        self.freigabe = True
        self.grund = ""
        self.fehler = ""
        self._herz = uhr()
        self._aus = threading.Event()
        self._lock = threading.Lock()
        self.a.setzen(False)
        self._thread = None
        if thread:
            self._thread = threading.Thread(target=self._schleife, name=f"ausgang-{ausgang.gpio}", daemon=True)
            self._thread.start()

    def herz(self):
        self._herz = self.uhr()

    def soll(self):
        """Gewuenschter Pegel jetzt (ohne Waechter)."""
        if not (self.wunsch and self.freigabe):
            return False
        p = max(0, min(100, int(self.leistung or 0)))
        if p >= 100:
            return True
        if p <= 0:
            return False
        return (self.uhr() % self.takt_s) < self.takt_s * p / 100.0

    def schritt(self):
        with self._lock:
            if self._aus.is_set():
                return
            an = self.soll()
            if an and self.uhr() - self._herz > self.waechter_s:
                an = False
                if not self.fehler:
                    self.fehler = "CraftBeerPi reagiert nicht, Ausgang aus"
                    log.error("GPIO %s: %s", self.a.gpio, self.fehler)
            elif self.uhr() - self._herz <= self.waechter_s:
                self.fehler = ""
            try:
                if an != self.a.an:
                    self.a.setzen(an)
            except Exception as e:
                self.fehler = f"Schalten fehlgeschlagen: {e}"
                log.error("GPIO %s: %s", self.a.gpio, self.fehler)
                try:
                    self.a.setzen(False)
                except Exception:
                    pass

    def _schleife(self):
        while not self._aus.wait(0.1):
            try:
                self.schritt()
            except Exception:
                log.exception("Ausgangssteuerung")

    def beenden(self):
        if self._aus.is_set():
            return
        with self._lock:
            self._aus.set()
            try:
                self.a.setzen(False)
            except Exception as e:
                log.error("Ausschalten beim Beenden: %s", e)
            self.a.freigeben()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)


class Heizschutz:
    """Entscheidet aus Fuehlerwert und -alter, ob geheizt werden darf.

    Nicht verriegelnd: Fuehler zu alt (sobald er wieder liefert, geht es weiter).
    Verriegelnd: Uebertemperatur, kein Temperaturanstieg trotz Heizen. Loesen nur
    von Hand (Aktion "Schutz zuruecksetzen")."""

    def __init__(self, max_temp=105.0, max_alter_s=10.0, anstieg_min=10.0, anstieg_k=1.0,
                 anstieg_ab=95.0, uhr=time.monotonic):
        self.max_temp = max_temp
        self.max_alter_s = max_alter_s
        self.anstieg_s = anstieg_min * 60.0
        self.anstieg_k = anstieg_k
        self.anstieg_ab = anstieg_ab
        self.uhr = uhr
        self.verriegelt = ""
        self._heiz_seit = None
        self._temp_start = None

    def zuruecksetzen(self):
        self.verriegelt = ""
        self._heiz_seit = None

    def pruefen(self, wert, alter_s, heizt_voll):
        """(erlaubt, grund). heizt_voll: Ausgang soll mit >= 50 % laufen."""
        if self.verriegelt:
            return False, self.verriegelt
        if wert is None or alter_s is None or alter_s > self.max_alter_s:
            self._heiz_seit = None
            return False, f"Fühlerwert fehlt oder älter als {self.max_alter_s:g} s"
        if wert >= self.max_temp:
            self.verriegelt = f"Übertemperatur {wert:.1f} °C (Grenze {self.max_temp:g} °C)"
            return False, self.verriegelt
        jetzt = self.uhr()
        if self.anstieg_s > 0 and heizt_voll and wert < self.anstieg_ab:
            if self._heiz_seit is None:
                self._heiz_seit, self._temp_start = jetzt, wert
            elif wert - self._temp_start >= self.anstieg_k:
                self._heiz_seit, self._temp_start = jetzt, wert
            elif jetzt - self._heiz_seit >= self.anstieg_s:
                self.verriegelt = (f"Kein Anstieg um {self.anstieg_k:g} K in {self.anstieg_s / 60:g} min "
                                   "(Fühler nicht im Kessel? Heizung defekt?)")
                return False, self.verriegelt
        else:
            self._heiz_seit = None
        return True, ""
