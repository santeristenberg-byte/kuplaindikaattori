#!/usr/bin/env python3
"""
Kuplamittari – viikoittainen pörssiuutiskirje.

Ajetaan kupla.py:n jälkeen. Lukee mittarin faktapaketin (outputs/latest.json), hakee viikon
markkinaluvut, antaa Claude-tekoälyn kirjoittaa tekstin viikon uutisten pohjalta ja lähettää
HTML-sähköpostin. Jos tekoäly ei ole käytettävissä, teksti kootaan datasta valmiilla pohjalla.

Tärkeä periaate: KAIKKI luvut (taulukko, mittarit, kuplalukema) tulevat Pythonista. Tekoäly
kirjoittaa vain tekstit, ja sille annetaan valmiiksi lasketut faktat (esim. "onko ennätys"),
jotta se ei voi keksiä tai pyöristää lukuja.

Ajo:
  python newsletter.py                  normaali ajo (AI jos ANTHROPIC_API_KEY on asetettu)
  python newsletter.py --no-email       rakenna kirje, älä lähetä
  python newsletter.py --no-ai          pakota ilmainen pohja
  python newsletter.py --mock-ai F      käytä tiedoston F JSON-vastausta AI:n sijaan (testaus)
  python newsletter.py --mock-markets F käytä tiedoston F markkinadataa (testaus ilman verkkoa)
  python newsletter.py --mock-feargreed F käytä tiedoston F pelko/ahneus-lukemaa (testaus ilman verkkoa)
  python newsletter.py --date 2026-09-19   aja kuin olisi annettu päivä
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
import traceback
from email.message import EmailMessage
from email.utils import formataddr

import numpy as np
import pandas as pd
import requests

import tutka

# ----------------------------------------------------------------------------
# Asetukset
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "outputs")
NL_DIR = os.path.join(HERE, "newsletter")
ARCHIVE_DIR = os.path.join(NL_DIR, "arkisto")
NL_HISTORY = os.path.join(NL_DIR, "history.json")
LATEST_JSON = os.path.join(OUT_DIR, "latest.json")
REPORT_TXT = os.path.join(OUT_DIR, "raportti.txt")
NEWSLETTER_HTML = os.path.join(OUT_DIR, "uutiskirje.html")

API_URL = "https://api.anthropic.com/v1/messages"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()
FALLBACK_MODEL = "claude-haiku-4-5-20251001"
MAX_SEARCHES = int(os.environ.get("MAX_SEARCHES", "8"))          # kutsu 1: uutiset ja liikkujat
MAX_SEARCHES_TUTKA = int(os.environ.get("MAX_SEARCHES_TUTKA", "6"))  # kutsu 2: tutka
PRICES = {  # USD / miljoona tokenia (syöte, tuotos) — kustannusseurantaa varten
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}
SEARCH_PRICE = 0.01
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "75"))
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")   # esim. https://kayttaja.github.io/repo (GitHub Pages)

NBSP = " "
MINUS = "−"
WEEKDAYS = ["ma", "ti", "ke", "to", "pe", "la", "su"]

# Värit (dataviz-ohjeiden tilapaletti; täyttö kantaa vakavuuden, tausta on saman sävyn vaalea aste)
LEVEL = {
    "good":     {"fill": "#0ca30c", "track": "#c7e8c6", "cls": "good"},
    "warning":  {"fill": "#fab219", "track": "#fcecc9", "cls": "warn"},
    "serious":  {"fill": "#ec835a", "track": "#f8e1d8", "cls": "ser"},
    "critical": {"fill": "#d03b3b", "track": "#f2d2d1", "cls": "crit"},
    "none":     {"fill": "#c3c2b7", "track": "#e1e0d9", "cls": "none"},
}
ZONES = [(0, 40, "Rauhallinen"), (40, 60, "Kallis"), (60, 75, "Esiaste"), (75, 90, "Kupla"), (90, 100, "Ääri")]

# Pelko/ahneus-mittari: eri asia kuin kuplalukema (lyhyen aikavälin tunnelma, ei pitkän aikavälin
# yliarvostus), joten oma, neutraali kaksisuuntainen väripaletti (sininen<->punainen), ei
# hyvä/paha-väritystä kuten kuplamittarin tila-väreissä.
FG_ZONES = [(0, 25, "Äärimmäinen pelko"), (25, 45, "Pelko"), (45, 55, "Neutraali"),
            (55, 75, "Ahneus"), (75, 100, "Äärimmäinen ahneus")]
FG_COLORS = ["#184f95", "#6da7ec", "#c3c2b7", "#f0b3b2", "#e34948"]
FG_COLORS_DARK = ["#0d366b", "#3987e5", "#585650", "#8a4a49", "#e66767"]

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print("VAROITUS:", msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# Muotoilu (suomalaiset merkintätavat)
# ----------------------------------------------------------------------------
def fi_num(x: float, dec: int = 0) -> str:
    s = f"{abs(x):,.{dec}f}".replace(",", NBSP).replace(".", ",")
    return (MINUS if x < 0 else "") + s


def fi_signed(x: float, dec: int = 1) -> str:
    if round(x, dec) == 0:
        return "±0" + ("," + "0" * dec if dec else "")
    return ("+" if x > 0 else MINUS) + fi_num(abs(x), dec)


def fi_date(d: dt.date) -> str:
    return f"{d.day}.{d.month}.{d.year}"


def fi_range(a: dt.date, b: dt.date) -> str:
    if a.month == b.month and a.year == b.year:
        return f"{a.day}.–{b.day}.{b.month}.{b.year}"
    return f"{a.day}.{a.month}.–{b.day}.{b.month}.{b.year}"


def week_info(today: dt.date) -> dict:
    """Viimeisin päättynyt pörssiviikko (ma–pe) ja seuraava viikko."""
    d = (today.weekday() - 4) % 7
    if d == 0:
        d = 7                       # perjantaina viikko ei ole vielä päättynyt -> edellinen
    end = today - dt.timedelta(days=d)
    start = end - dt.timedelta(days=4)
    nstart = end + dt.timedelta(days=3)
    nend = nstart + dt.timedelta(days=4)
    iso = end.isocalendar()
    return {"tanaan": today, "alku": start, "loppu": end, "ensi_alku": nstart, "ensi_loppu": nend,
            "viikko": iso[1], "vuosi": iso[0]}


# ----------------------------------------------------------------------------
# Markkinadata
# ----------------------------------------------------------------------------
# (nimi, lähde, tunnus, tyyppi, varalähde)
MARKETS = [
    ("S&P 500",              "yahoo", "^GSPC",    "index", None),
    ("Nasdaq Composite",     "yahoo", "^IXIC",    "index", None),
    ("Dow Jones",            "yahoo", "^DJI",     "index", None),
    ("Stoxx 600 (Eurooppa)", "yahoo", "^STOXX",   "index", None),
    ("OMX Helsinki 25",      "yahoo", "^OMXH25",  "index", ("fred", "NASDAQOMXH25")),
    ("USA 10 v korko",       "fred",  "DGS10",    "yield", ("yahoo", "^TNX")),
    ("VIX-pelkoindeksi",     "yahoo", "^VIX",     "vix",   None),
    ("EUR/USD",              "yahoo", "EURUSD=X", "fx",    None),
    ("Öljy (WTI)",           "yahoo", "CL=F",     "usd1",  None),
    ("Kulta",                "yahoo", "GC=F",     "usd0",  None),
    ("Bitcoin",              "yahoo", "BTC-USD",  "usd0",  None),
]


def yahoo_daily(ticker: str) -> pd.Series:
    import yfinance as yf
    df = yf.download(ticker, period="1y", interval="1d", auto_adjust=False, progress=False)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    s = pd.to_numeric(close, errors="coerce").dropna()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    return s[~s.index.duplicated(keep="last")].sort_index()


def fred_daily(sid: str) -> pd.Series:
    start = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    if FRED_API_KEY:
        url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
               f"&api_key={FRED_API_KEY}&file_type=json&observation_start={start}")
        obs = requests.get(url, timeout=60).json()["observations"]
        s = pd.Series([o["value"] for o in obs], index=pd.to_datetime([o["date"] for o in obs]))
    else:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
        from io import StringIO
        df = pd.read_csv(StringIO(requests.get(url, timeout=60).text))
        s = pd.Series(df.iloc[:, 1].values, index=pd.to_datetime(df.iloc[:, 0]))
    return pd.to_numeric(s, errors="coerce").dropna().sort_index()


def _series(source: str, code: str) -> pd.Series:
    s = yahoo_daily(code) if source == "yahoo" else fred_daily(code)
    if code == "^TNX" and not s.empty and s.iloc[-1] > 20:   # vanha esitystapa: tuotto × 10
        s = s / 10
    return s


def market_row(name: str, kind: str, s: pd.Series, wk: dict) -> dict | None:
    end = pd.Timestamp(wk["loppu"])
    s = s[s.index <= end]
    if s.empty:
        return None
    cur_d, cur = s.index[-1], float(s.iloc[-1])
    if (end - cur_d).days > 6:
        return None                                    # data liian vanha
    prev = s[s.index <= end - pd.Timedelta(days=7)]
    row = {"nimi": name, "tyyppi": kind, "taso": cur, "pvm": str(cur_d.date())}
    if not prev.empty:
        p = float(prev.iloc[-1])
        if kind == "yield":
            row["muutos"] = (cur - p) * 100            # korkopisteet
        elif kind == "vix":
            row["muutos"] = cur - p                    # indeksipisteet
        else:
            row["muutos"] = (cur / p - 1) * 100        # prosentit
    year = s[s.index > end - pd.Timedelta(days=365)]
    if kind == "index" and not year.empty:
        row["huipusta_52vk_pct"] = (cur / float(year.max()) - 1) * 100
    return row


def fetch_markets(wk: dict) -> list[dict]:
    rows = []
    for name, src, code, kind, fb in MARKETS:
        row = None
        for s_src, s_code in [(src, code)] + ([fb] if fb else []):
            try:
                row = market_row(name, kind, _series(s_src, s_code), wk)
                if row:
                    break
            except Exception as e:  # noqa: BLE001
                warn(f"markkinadata {name} ({s_code}) epäonnistui: {str(e)[:120]}")
        if row:
            rows.append(row)
        else:
            warn(f"markkinadata {name}: ei tuoretta arvoa, rivi jätetään pois")
    return rows


# ----------------------------------------------------------------------------
# Pelko/ahneus-mittari
# ----------------------------------------------------------------------------
def _pctrank(s: pd.Series, value: float | None) -> float | None:
    """Nykyarvon persentiili sarjan omassa historiassa, 0-100. Sama periaate kuin
    kupla.py:n pisteytyksessä: verrataan vain sarjan omaan menneisyyteen, ei mielivaltaiseen
    rajaan, jotta hyvin erilaiset mittayksiköt (pisteet, prosentit, korkopisteet) tulevat
    vertailukelpoisiksi."""
    s = s.dropna()
    if value is None or not np.isfinite(value) or len(s) < 40:
        return None
    return float((s < value).sum()) / len(s) * 100.0


def _fg_zone(score: float) -> int:
    for i, (lo, hi, _) in enumerate(FG_ZONES):
        if score < hi or hi == 100:
            return i
    return len(FG_ZONES) - 1


def fear_greed(wk: dict) -> dict | None:
    """Pelko/ahneus-mittari 0-100 (0 = äärimmäinen pelko, 100 = äärimmäinen ahneus). Eri asia
    kuin kuplalukema: tämä mittaa viikon markkinatunnelmaa lyhyellä aikavälillä, kuplalukema
    pitkän aikavälin yliarvostusta.

    Neljä osaa, kukin oman noin 1 vuoden historiansa persentiilinä (kuten kupla.py:n
    pisteytys, mutta päivätasolla): VIX käänteisesti (matala VIX = ahneus), S&P 500:n poikkeama
    125 pv liukuvasta keskiarvosta (momentum), S&P 500:n ja kullan suhteellinen 20 pv tuotto
    (turvasatamakysyntä: kulta voittaa = pelko) sekä luottomarginaali BAA10Y käänteisesti (leveä
    marginaali = pelko). Puuttuvat osat jätetään pois: jos alle kaksi osaa saadaan laskettua,
    lukemaa ei julkaista sen sijaan että näytettäisiin harhaanjohtavan ohut luku."""
    end = pd.Timestamp(wk["loppu"])
    osat: list[dict] = []

    def add(nimi: str, arvo: float | None) -> None:
        if arvo is not None:
            osat.append({"nimi": nimi, "pisteet": round(arvo, 1)})

    try:
        vix = yahoo_daily("^VIX")
        vix = vix[vix.index <= end]
        v = None if vix.empty else _pctrank(vix, float(vix.iloc[-1]))
        add("VIX-pelkoindeksi", None if v is None else 100 - v)
    except Exception as e:  # noqa: BLE001
        warn(f"pelko/ahneus: VIX epäonnistui: {str(e)[:120]}")

    spx = pd.Series(dtype=float)
    try:
        spx = yahoo_daily("^GSPC")
        spx = spx[spx.index <= end]
        if len(spx) > 130:
            dev = (spx / spx.rolling(125).mean() - 1) * 100
            dev = dev.dropna()
            if not dev.empty:
                add("S&P 500:n momentum (125 pv ka.)", _pctrank(dev, float(dev.iloc[-1])))
    except Exception as e:  # noqa: BLE001
        warn(f"pelko/ahneus: momentum epäonnistui: {str(e)[:120]}")

    try:
        gold = yahoo_daily("GC=F")
        gold = gold[gold.index <= end]
        if len(spx) > 25 and len(gold) > 25:
            spread = ((spx.pct_change(20) - gold.pct_change(20).reindex(spx.index)) * 100).dropna()
            if not spread.empty:
                add("Osakkeet vs. kulta (turvasatamakysyntä)", _pctrank(spread, float(spread.iloc[-1])))
    except Exception as e:  # noqa: BLE001
        warn(f"pelko/ahneus: turvasatamakysyntä epäonnistui: {str(e)[:120]}")

    try:
        baa = fred_daily("BAA10Y")
        baa = baa[baa.index <= end]
        b = None if baa.empty else _pctrank(baa, float(baa.iloc[-1]))
        add("Luottomarginaali (Baa − 10 v)", None if b is None else 100 - b)
    except Exception as e:  # noqa: BLE001
        warn(f"pelko/ahneus: luottomarginaali epäonnistui: {str(e)[:120]}")

    if len(osat) < 2:
        warn(f"pelko/ahneus: vain {len(osat)} osaa laskettavissa, jätetään koko mittari pois tällä viikolla")
        return None
    pisteet = round(sum(o["pisteet"] for o in osat) / len(osat), 1)
    zi = _fg_zone(pisteet)
    return {"pisteet": pisteet, "vyohyke": FG_ZONES[zi][2], "vyohyke_i": zi, "osat": osat}


def fmt_level(r: dict) -> str:
    k, v = r["tyyppi"], r["taso"]
    if k == "yield":
        return fi_num(v, 2) + NBSP + "%"
    if k == "vix":
        return fi_num(v, 1)
    if k == "fx":
        return fi_num(v, 4)
    if k == "usd1":
        return fi_num(v, 1) + NBSP + "$"
    if k == "usd0":
        return fi_num(v, 0) + NBSP + "$"
    return fi_num(v, 0) if v >= 1000 else fi_num(v, 2)


def fmt_change(r: dict) -> tuple[str, str]:
    """(teksti, suunta) — suunta: up/down/neutral. Korkojen ja VIX:n muutos ei ole hyvä/huono,
    joten ne näytetään neutraalina. Merkki ▲▼ kertoo suunnan, väri ei yksin."""
    if "muutos" not in r:
        return ("–", "neutral")
    m, k = r["muutos"], r["tyyppi"]
    arrow = "&#9650;" if m > 0 else ("&#9660;" if m < 0 else "")
    if k == "yield":
        return (f"{arrow}{NBSP}{fi_signed(m, 0)}{NBSP}bp".strip(), "neutral")
    if k == "vix":
        return (f"{arrow}{NBSP}{fi_signed(m, 1)}".strip(), "neutral")
    return (f"{arrow}{NBSP}{fi_signed(m, 1)}{NBSP}%".strip(), "up" if m > 0 else ("down" if m < 0 else "neutral"))


def fmt_change_plain(r: dict) -> str:
    if "muutos" not in r:
        return ""
    m, k = r["muutos"], r["tyyppi"]
    if k == "yield":
        return f"{fi_signed(m, 0)} bp"
    if k == "vix":
        return fi_signed(m, 1)
    return f"{fi_signed(m, 1)} %"


# ----------------------------------------------------------------------------
# Faktapaketti ja historia
# ----------------------------------------------------------------------------
def load_history() -> list[dict]:
    if os.path.exists(NL_HISTORY):
        with open(NL_HISTORY, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(hist: list[dict]) -> None:
    os.makedirs(NL_DIR, exist_ok=True)
    with open(NL_HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)


def issue_info(hist: list[dict], wk: dict) -> tuple[int, dict | None]:
    """Numero pysyy samana, jos samalla viikolla ajetaan uudelleen. Vertailu edelliseen viikkoon."""
    key = f"{wk['vuosi']}-{wk['viikko']:02d}"
    earlier = [h for h in hist if h.get("viikko_id") != key]
    same = [h for h in hist if h.get("viikko_id") == key]
    number = same[0]["numero"] if same else len(earlier) + 1
    prev = earlier[-1] if earlier else None
    return number, prev


def build_factpack(latest: dict, markets: list[dict], wk: dict, number: int, prev: dict | None) -> dict:
    comp = latest["kuplalukema"]
    change = None
    cov_changed = False
    if prev is not None:
        change = round(comp - prev["kuplalukema"], 1)
        cov_changed = abs(latest.get("kattavuus_pct", 0) - prev.get("kattavuus_pct", 0)) >= 3
    vars_compact = {}
    for k, v in latest["muuttujat"].items():
        vars_compact[k] = {kk: v.get(kk) for kk in (
            "nimi", "persentiili", "arvo", "yksikko", "arvon_kk", "on_ennatys", "kuplamaisempi_kuin_nyt_vuosina",
            "historian_kuplamaisin_arvo", "historian_kuplamaisin_kk", "historia_alkaa", "kuplamaisempi_kun_pienempi")
            if v.get(kk) is not None}
    return {
        "numero": number,
        "viikko": wk["viikko"],
        "kulunut_porssiviikko": fi_range(wk["alku"], wk["loppu"]),
        "ensi_viikko": fi_range(wk["ensi_alku"], wk["ensi_loppu"]),
        "kuplalukema": comp,
        "vyohyke": latest["vyohyke"],
        "taso": latest["taso"],
        "edellisen_viikon_lukema": None if prev is None else prev["kuplalukema"],
        "muutos_viime_viikosta": change,
        "muutos_johtuu_osin_mittarin_kattavuudesta": cov_changed,
        "mittarin_kattavuus_pct": latest.get("kattavuus_pct"),
        "teemat": latest["teemat"],
        "muuttujat": vars_compact,
        "analogiat": latest.get("analogiat", []),
        "raakatasot": latest.get("raakatasot", {}),
        "markkinat": [{"nimi": r["nimi"], "taso": round(r["taso"], 4), "pvm": r["pvm"],
                       **({"viikkomuutos": round(r["muutos"], 2),
                           "muutoksen_yksikko": {"yield": "korkopistettä", "vix": "pistettä"}.get(r["tyyppi"], "%")}
                          if "muutos" in r else {}),
                       **({"52vk_huipusta_pct": round(r["huipusta_52vk_pct"], 1)} if "huipusta_52vk_pct" in r else {})}
                      for r in markets],
        "mittari_kattaa_nyt": "arvostus, velkavipu, rahaolot ja velkaantuminen",
        "mittariin_tulossa": "sijoittajien mielialat (sentimentti) ja markkinan keskittyminen teknologiajätteihin",
    }


# ----------------------------------------------------------------------------
# AI-kirjoittaja (Claude API + verkkohaku)
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """Olet Kuplamittarin päätoimittaja. Kuplamittari on suomenkielinen viikoittainen sähköpostiuutiskirje tavallisille sijoittajille. Se kertoo viikon tärkeimmät pörssitapahtumat ja analysoi selkokielellä, mitä kuplamittari (osakemarkkinan kuplariskin mittari 0–100) tällä viikolla näyttää. Lukijan pitää ymmärtää kirje ilman taloustieteen koulutusta, mutta kokenutkin sijoittaja haluaa lukea sen.

TYYLI
- Sujuvaa, elävää yleiskieltä kuin parhaalla talousjournalistilla. Lyhyitä virkkeitä. Selitä termi arkikielellä, kun käytät sitä ensimmäisen kerran (esim. CAPE = hinta suhteessa kymmenen vuoden keskimääräiseen tulokseen).
- Tiivis: sähköpostiin menevät tekstikentät (kaikki paitsi syvasukellus) yhteensä noin 650–900 sanaa. Jokainen virke ansaitsee paikkansa.
- Etsi viikon tarina ja kytke uutiset kuplariskiin. Historialliset rinnastukset tekevät kirjeestä kiinnostavan, mutta vain tosiasioihin perustuvina.
- Suomalaiset merkintätavat: desimaalipilkku (5,0 %), välilyönti ennen %-merkkiä, tuhaterotin välilyönnillä (80 000), päivämäärät muodossa 23.9., ajatusviiva välimerkkinä.
- Saat käyttää **lihavointia** säästeliäästi. Ei muita muotoiluja, ei linkkejä tekstissä, ei emojeja.

TARKKUUS (ehdoton)
- Mittarin luvut, persentiilit, historiatiedot ja markkinaluvut: käytä vain faktapaketin lukuja. Sano "ennätys" tai "korkein koskaan" vain, jos faktapaketissa on_ennatys on true. Muuten kerro kenttä kuplamaisempi_kuin_nyt_vuosina ("korkeammalla on käyty vain vuosina 1999–2000").
- Kenttä kuplamaisempi_kun_pienempi = true tarkoittaa, että pienempi arvo on kuplamaisempi (esim. osakeriskipreemio, luottomarginaali, reaalikorko).
- Viikon tapahtumat ja ensi viikon kalenteri: hae verkkohaulla. Kerro vain asioita, jotka löydät luotettavista lähteistä ja jotka osuvat annetuille päiville. Jos et ole varma luvusta tai päivästä, jätä se pois.
- Älä keksi lainauksia. Lainaa vain, jos lähteessä on lainaus, ja suomenna se uskollisesti.
- Ei sijoitusneuvontaa: älä kehota ostamaan, myymään tai ajoittamaan. Mittari kertoo riskin tasosta, ei ajankohdasta.
- Kuplalukema paljastetaan vasta kirjeen lopussa: älä mainitse lukemaa numerona kentissä otsikko, ingressi, esikatselu tai viikko. Teemojen pisteitä ei tarvitse toistaa tekstissä, ne näkyvät lukijalle mittareina.
- Jos muutos_johtuu_osin_mittarin_kattavuudesta on true, älä tulkitse lukeman muutosta markkinan muutokseksi.

MITTARIN TULKINTA
- Lukema 0–100 kertoo, kuinka kuplamainen tila on omaan historiaansa verrattuna. Vyöhykkeet: 0–40 rauhallinen, 40–60 kallis, 60–75 esiaste, 75–90 kupla-alue, 90+ historiallinen ääri.
- Teemat: Arvostus (kuinka kalliita osakkeet ovat), Velkavipu (kuinka paljon osakkeita ostetaan velaksi), Raha ja velka (keskuspankit, korot ja talouden velkaantuminen; toimii usein jarruna).
- Persentiili 99 tarkoittaa, että lukema on kuplamaisempi kuin 99 %:ssa mitatusta historiasta.
- Analogiat kertovat samankaltaisuuden aiempien kuplien huippuhetkiin (0–100 %). Kerro sekä yhtäläisyydet että erot.

VASTAUS
Tee ensin verkkohaut. Palauta lopuksi AINOASTAAN yksi JSON-objekti ilman muuta tekstiä sen jälkeen:
{
  "otsikko": "…",
  "ingressi": "…",
  "esikatselu": "…",
  "viikko": [{"otsikko": "…", "teksti": "…"}],
  "teemat": {"arvostus": "…", "velkavipu": "…", "raha": "…"},
  "historia": ["…", "…"],
  "ensi_viikko": [{"paiva": "ke 23.9.", "teksti": "…"}],
  "loppusanat": "…",
  "viikon_luku": {"luku": "…", "otsikko": "…", "teksti": "…"},
  "liikkujat": [{"tunnus": "…", "syy": "…"}],
  "minuutti": {"aihe": "…", "teksti": "…"},
  "syvasukellus": {"otsikko": "…", "teksti": "…"},
  "lahteet": [{"otsikko": "…", "url": "https://…"}]
}
Kenttien sisältö:
- otsikko: viikon tarina, enintään 60 merkkiä.
- ingressi: 2–3 virkettä, miksi viikko oli kiinnostava kuplariskin kannalta.
- esikatselu: postilaatikon esikatseluteksti, enintään 90 merkkiä.
- viikko: 4–6 tärkeintä tapahtumaa, tärkein ensin. Otsikko on lyhyt lause pisteeseen päättyen, teksti 1–2 virkettä lukuineen.
- teemat: kullekin 2–4 virkettä, konkreettiset luvut faktapaketista.
- historia: 2–3 lyhyttä kappaletta: lähimmät analogiat, osuva historiallinen rinnastus tämän viikon tapahtumiin ja muistutus mittasuhteista.
- ensi_viikko: 3–6 tärkeintä tapahtumaa, mukaan merkittävimmät yhdysvaltalaiset tulosjulkistukset. paiva muodossa "ke 23.9.", teksti kertoo miksi sillä on väliä.
- viikon_luku: yksi pysäyttävä luku viikolta (faktapaketista tai luotettavasta uutisesta). luku lyhyenä ("5,00 %", "1,45 biljoonaa $", "96 vuotta"), otsikko enintään 50 merkkiä, teksti 1–2 virkettä siitä, miksi luku on merkittävä. Ei kuplalukemaa.
- liikkujat: faktapaketin kentän liikkujat kolmelle suurimmalle nousijalle ja kolmelle suurimmalle laskijalle yksi virke kurssiliikkeen syystä. Käytä tunnusta täsmälleen faktapaketin muodossa. Jos syy ei löydy luotettavasta lähteestä, jätä syy tyhjäksi – älä arvaa.
- minuutti: yksi sijoittamisen käsite selitettynä arkikielellä (60–90 sanaa), mieluiten kytköksissä viikon aiheisiin. Älä toista faktapaketin listassa aiemmat_minuutit olevia aiheita.
- loppusanat: 1–2 virkettä, jotka tiivistävät mistä lukema syntyy (ilman lukemaa numerona).
- syvasukellus: kirjeen VERKKOVERSION oma laajempi osio, jota ei mahdu sähköpostiin. Sähköpostissa näkyy vain otsikko ja houkutin, koko teksti näkyy vain verkkosivulla – siksi tämä saa olla paljon pidempi kuin muut kentät, noin 300–500 sanaa. Pura tässä laajemmin auki jokin viikon aihe, kuplamittarin teema tai historiallinen vertailu, johon muissa kentissä ei ollut tilaa. Ei kuulu 650–900 sanan kokonaisrajaan.
- lahteet: 3–8 tärkeintä käyttämääsi lähdettä."""


def user_prompt(facts: dict) -> str:
    return (
        f"Kirjoita Kuplamittarin numero {facts['numero']} (viikko {facts['viikko']}).\n"
        f"Kulunut pörssiviikko: {facts['kulunut_porssiviikko']}. Ensi viikko: {facts['ensi_viikko']}.\n\n"
        f"Hae verkosta (enintään {MAX_SEARCHES} hakua):\n"
        f"1) viikon {facts['kulunut_porssiviikko']} tärkeimmät pörssi- ja talousuutiset (Yhdysvallat ja Eurooppa)\n"
        f"2) mitä Helsingin pörssissä tapahtui samalla viikolla\n"
        f"3) ensi viikon {facts['ensi_viikko']} tärkeimmät talousjulkistukset, keskuspankkitapahtumat ja tulosjulkistukset\n"
        f"4) syyt viikon suurimpiin kurssiliikkeisiin (faktapaketin liikkujat)\n"
        f"5) tarvittaessa viikon keskustelu osakkeiden arvostuksista, tekoälyosakkeista tai kuplasta\n\n"
        "Kirjoita sitten kirje tämän faktapaketin pohjalta:\n```json\n"
        + json.dumps(facts, ensure_ascii=False, indent=1, default=str)
        + "\n```"
    )


def _post(body: dict) -> dict:
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = None
    for attempt in range(3):
        r = requests.post(API_URL, headers=headers, json=body, timeout=600)
        if r.status_code == 200:
            return r.json()
        last = f"HTTP {r.status_code}: {r.text[:400]}"
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(15 * (attempt + 1))
            continue
        break
    raise RuntimeError(last)


def call_claude(system: str, user: str, model: str, max_searches: int = 0) -> tuple[str, dict, list[dict]]:
    """Palauttaa (kaikki tekstit yhdistettynä, käyttötiedot, viitatut lähteet)."""
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches or MAX_SEARCHES,
              "user_location": {"type": "approximate", "country": "FI", "timezone": "Europe/Helsinki"}}]
    messages = [{"role": "user", "content": user}]
    usage = {"malli": model, "syote_tokenit": 0, "tuotos_tokenit": 0, "haut": 0}
    texts, cited = [], []
    for _ in range(6):                          # pause_turn-jatkot
        data = _post({"model": model, "max_tokens": 8000, "system": system,
                      "messages": messages, "tools": tools})
        u = data.get("usage") or {}
        usage["syote_tokenit"] += int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
            + int(u.get("cache_creation_input_tokens") or 0)
        usage["tuotos_tokenit"] += int(u.get("output_tokens") or 0)
        usage["haut"] += int((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        content = data.get("content") or []
        for b in content:
            if b.get("type") == "text":
                texts.append(b.get("text") or "")
                for c in b.get("citations") or []:
                    if c.get("url"):
                        cited.append({"otsikko": c.get("title") or c["url"], "url": c["url"]})
        if data.get("stop_reason") == "pause_turn":
            messages = messages + [{"role": "assistant", "content": content}]
            continue
        if data.get("stop_reason") == "max_tokens":
            warn("AI-vastaus katkesi max_tokens-rajaan")
        break
    pin, pout = PRICES.get(model, (3.0, 15.0))
    usage["kustannus_usd"] = round(usage["syote_tokenit"] / 1e6 * pin + usage["tuotos_tokenit"] / 1e6 * pout
                                   + usage["haut"] * SEARCH_PRICE, 3)
    return "".join(texts), usage, cited


EXPECTED_KEYS = ("otsikko", "ingressi", "esikatselu", "viikko", "teemat", "historia", "ensi_viikko", "loppusanat",
                 "lahteet", "syvasukellus")


def extract_json(text: str, keys: tuple = EXPECTED_KEYS) -> dict:
    """Vastauksen pääobjekti: se JSON-objekti, jossa on eniten odotettuja pääkenttiä (tasapelissä
    myöhäisin). Pelkkä 'otsikko'-kenttä ei riitä, koska myös sisäkkäisissä alkioissa (viikko,
    lahteet) on otsikko. Kestää saatetekstit, koodiaidat ja viittausten pilkkomat tekstilohkot."""
    dec = json.JSONDecoder()
    best, best_score = None, 1 if len(keys) > 2 else 0   # vähintään kaksi pääkenttää
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        score = sum(k in obj for k in keys)
        if score >= best_score:
            best, best_score = obj, score
    if best is None:
        raise ValueError("vastauksesta ei löytynyt kirjeen JSON-objektia")
    return best


def _ai(system: str, user: str, keys: tuple, max_searches: int, label: str) -> tuple[dict, dict]:
    errors = []
    for model in [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL != MODEL else []):
        try:
            text, usage, cited = call_claude(system, user, model, max_searches)
            obj = extract_json(text, keys)
            if "lahteet" in keys and not obj.get("lahteet") and cited:
                seen, uniq = set(), []
                for c in cited:
                    if c["url"] not in seen:
                        seen.add(c["url"])
                        uniq.append(c)
                obj["lahteet"] = uniq[:8]
            print(f"AI ({label}): {usage['malli']}, {usage['haut']} hakua, "
                  f"{usage['syote_tokenit']} + {usage['tuotos_tokenit']} tokenia, n. {usage['kustannus_usd']} $", flush=True)
            return obj, usage
        except Exception as e:  # noqa: BLE001
            errors.append(f"{model}: {str(e)[:300]}")
            warn(f"AI ({label}, {model}) epäonnistui: {str(e)[:300]}")
    raise RuntimeError(" | ".join(errors))


def ai_write(facts: dict) -> tuple[dict, dict]:
    return _ai(SYSTEM_PROMPT, user_prompt(facts), EXPECTED_KEYS, MAX_SEARCHES, "kirje")


# ----------------------------------------------------------------------------
# Tutka: toinen AI-kutsu (itsearvio, oppi, nousuehdokkaat, sektorinäkymä ja kuplavaroitus)
# ----------------------------------------------------------------------------
TUTKA_KEYS = ("itsearvio", "uusi_oppi", "ehdokkaat", "varoitus", "sektorinakyma")

TUTKA_SYSTEM = """Olet Kuplamittarin tutka-analyytikko. Tutka on uutiskirjeen osio, jossa julkaistaan joka viikko kolme nousuehdokasta, yksi sektorinäkymä ja yksi kuplavaroitus seuraavalle pörssiviikolle. Ennusteet jäädytetään ennen viikon alkua ja pisteytetään mekaanisesti: nousuehdokas ja sektorinäkymä osuvat, jos ne voittavat vertailunsa (osakkeilla ja ETF:illä S&P 500, kryptoilla bitcoin), ja kuplavaroitus osuu, jos se häviää vertailulleen. Lukijat näkevät jokaisen onnistumisen ja virheen.

TEHTÄVÄ
1) Itsearvio. Jos faktapaketissa on viime viikon tulokset, arvioi ne rehellisesti 2–4 virkkeellä: mikä perustelu kesti, mikä ei ja kuinka paljon oli sattumaa. Yhden viikon tulos on suurelta osin kohinaa, joten älä ylitulkitse onnistumista tai epäonnistumista. Vertaa kertymää pelkkään momentum-sääntöön. Tiivistä yksi konkreettinen oppi (1 virke), jota käytät jatkossa. Jos tuloksia ei vielä ole, jätä itsearvio ja uusi_oppi tyhjiksi.
2) Valitse kolme nousuehdokasta ja yksi kuplavaroitus ensi viikolle.
   - Pääpaino USA:n osakkeissa (S&P 500). Kolmen ehdokkaan joukossa enintään yksi sektori- tai teema-ETF ja enintään yksi krypto. Ei suomalaisia osakkeita.
   - Hajauta: ehdokkaat eri sektoreilta.
   - Käytä faktapaketin seulottuja ehdokkaita lähtökohtana, mutta saat valita minkä tahansa S&P 500 -yhtiön tai faktapaketissa mainitun ETF:n tai kryptovaluutan. Käytä tunnusta Yahoo-muodossa täsmälleen (esim. NVDA, BRK-B, SMH, SOL-USD).
   - Etsi verkkohaulla ensi viikon laukaisijoita: tulosjulkistukset, tuotelanseeraukset, sijoittajapäivät, makrodata, analyytikoiden muutokset. Hyvä ehdokas yhdistää vahvan trendin ja konkreettisen laukaisijan.
   - Ota huomioon kuplamittarin tila: korkealla kuplalukemalla ylikuumentuneet momentum-kohteet ovat alttiita äkillisille laskuille.
   - Kuplavaroitus on ylikuumentunut kohde tai kohde, jonka arvostus on irronnut perusteista ja joka todennäköisemmin häviää vertailulleen ensi viikolla. Se ei ole lyhyeksimyyntikehotus.
   - Hyödynnä aiemmat opit.
3) Valitse lisäksi yksi sektorinäkymä: se GICS-sektori (faktapaketin ehdokkaat.gics_sektorit-listalta, esim. XLK, XLE, XLV, XLF, XLI, XLY, XLP, XLC, XLB, XLU, XLRE), jonka uskot pärjäävän parhaiten suhteessa S&P 500 -indeksiin ensi viikolla. Tämä on kirjeen "mikä sektori boomaa seuraavaksi" -osio, joten kirjoita se kuten terävä markkinakommentaattori some-ketjussa pohtisi: nimeä konkreettinen narratiivi tai sektorirotaatioteema (esim. tekoälyn pääomamenot, korkosyklin käänne, energian hinnat, kulutuksen palautuminen, sääntelymuutos, vaalit, kausivaihtelu), ja perustele miksi juuri tämä sektori hyötyy siitä seuraavien viikkojen aikana muita enemmän. Tämäkin pisteytetään mekaanisesti, joten pysy silti faktoissa – rohkea näkemys, ei ylilupausta.
4) Kirjoita kullekin kohteelle perustelu (1–2 virkettä konkreettisin luvuin, sektorinäkymälle saa käyttää 2–4 virkettä laajemman narratiivin avaamiseen), laukaisija (mikä voi liikuttaa kurssia ensi viikolla, päivämäärän kanssa jos se on tiedossa) ja riski (1 virke).

SÄÄNNÖT
- Älä lupaa tuottoja äläkä käytä sanoja "varma" tai "taattu". Yhden viikon ennuste on aina epävarma.
- Käytä vain tietoja, jotka löydät luotettavista lähteistä tai faktapaketista. Älä keksi päivämääriä tai lukuja.
- Ei sijoitusneuvontaa eikä kehotuksia ostaa tai myydä.
- Suomalaiset merkintätavat: desimaalipilkku ja välilyönti ennen %-merkkiä.

VASTAUS
Tee ensin verkkohaut. Palauta lopuksi AINOASTAAN yksi JSON-objekti ilman muuta tekstiä sen jälkeen:
{
  "itsearvio": "…",
  "uusi_oppi": "…",
  "ehdokkaat": [{"tunnus": "…", "perustelu": "…", "laukaisija": "…", "riski": "…"}],
  "varoitus": {"tunnus": "…", "perustelu": "…", "laukaisija": "…", "riski": "…"},
  "sektorinakyma": {"tunnus": "…", "perustelu": "…", "laukaisija": "…", "riski": "…"}
}"""


def tutka_user_prompt(tf: dict) -> str:
    return (
        f"Laadi tutka Kuplamittarin numeroon {tf['numero']}. Ennusteet koskevat pörssiviikkoa {tf['ennustettava_viikko']}; "
        f"tulos lasketaan maanantain avauskurssista perjantain päätöskurssiin.\n"
        f"Hae verkosta (enintään {MAX_SEARCHES_TUTKA} hakua) ensi viikon tulosjulkistukset ja muut laukaisijat ehdokkaillesi.\n\n"
        "Faktapaketti:\n```json\n" + json.dumps(tf, ensure_ascii=False, indent=1, default=str) + "\n```"
    )


def ai_tutka(tf: dict) -> tuple[dict, dict]:
    return _ai(TUTKA_SYSTEM, tutka_user_prompt(tf), TUTKA_KEYS, MAX_SEARCHES_TUTKA, "tutka")


def _norm_ticker(t) -> str:
    return str(t or "").strip().upper().replace(".", "-")


def validate_picks(obj: dict, uni: pd.DataFrame) -> list[dict]:
    """AI:n valinnat -> enintään 3 nousuehdokasta (max 1 ETF ja 1 krypto) + 1 varoitus + 1 sektorinäkymä
    (GICS-sektori-ETF), vain universumista."""
    meta = uni.set_index("tunnus")
    picks, seen, n_etf, n_cry = [], set(), 0, 0
    for c in (obj.get("ehdokkaat") or [])[:6]:
        if not isinstance(c, dict):
            continue
        t = _norm_ticker(c.get("tunnus"))
        if t not in meta.index or t in seen:
            warn(f"tutkan ehdokas {c.get('tunnus')!r} hylätty: ei universumissa tai toistuu")
            continue
        lk = meta.loc[t, "luokka"]
        if (lk in ("sektori", "teema") and n_etf >= 1) or (lk == "krypto" and n_cry >= 1):
            warn(f"tutkan ehdokas {t} hylätty: liikaa ETF:iä tai kryptoja")
            continue
        n_etf += lk in ("sektori", "teema")
        n_cry += lk == "krypto"
        seen.add(t)
        picks.append({"tunnus": t, "tyyppi": "nousu", "perustelu": _clean(c.get("perustelu"), 400),
                      "laukaisija": _clean(c.get("laukaisija"), 250), "riski": _clean(c.get("riski"), 250)})
        if len(picks) == 3:
            break
    v = obj.get("varoitus")
    v_tunnus = None
    if isinstance(v, dict):
        t = _norm_ticker(v.get("tunnus"))
        if t in meta.index and t not in seen:
            picks.append({"tunnus": t, "tyyppi": "varoitus", "perustelu": _clean(v.get("perustelu"), 400),
                          "laukaisija": _clean(v.get("laukaisija"), 250), "riski": _clean(v.get("riski"), 250)})
            v_tunnus = t
        else:
            warn(f"kuplavaroitus {v.get('tunnus')!r} hylätty")
    sk = obj.get("sektorinakyma")
    if isinstance(sk, dict):
        t = _norm_ticker(sk.get("tunnus"))
        if t in meta.index and meta.loc[t, "luokka"] == "sektori" and t not in seen and t != v_tunnus:
            picks.append({"tunnus": t, "tyyppi": "sektori", "perustelu": _clean(sk.get("perustelu"), 600),
                          "laukaisija": _clean(sk.get("laukaisija"), 250), "riski": _clean(sk.get("riski"), 250)})
        else:
            warn(f"sektorinäkymä {sk.get('tunnus')!r} hylätty: tuntematon, ei GICS-sektori-ETF tai toistuu")
    return picks


# ----------------------------------------------------------------------------
# Tutkan koonti (data, tuloskortti, jäädytys)
# ----------------------------------------------------------------------------
def build_tutka(wk: dict, run_date: dt.date, mock_prices: str | None = None) -> dict:
    uni = tutka.load_universe(offline=bool(mock_prices))
    if mock_prices:
        prices = pd.read_csv(mock_prices, index_col=0, parse_dates=True)
        prices = prices[prices.index <= pd.Timestamp(run_date)]      # simuloi ajohetkeä
        opens = None                                                  # testidatassa vain päätöskurssit
        uni = uni[uni["tunnus"].isin(prices.columns)]
    else:
        prices, opens = tutka.download_prices(uni["tunnus"].tolist() + [tutka.BENCH_EQ])
    feat = tutka.features(prices, wk["loppu"])
    led = tutka.load_ledger()
    closed = tutka.score_open(led, prices, opens)
    return {"uni": uni, "prices": prices, "feat": feat, "led": led, "closed": closed,
            "movers": tutka.movers(feat, uni), "sectors": tutka.sectors(feat),
            "candidates": tutka.candidates(feat, uni), "rules": tutka.rule_picks(feat, uni)}


def scorecard(tk: dict) -> dict | None:
    """Tällä ajolla suljetut ennusteet: julkaistut (näytetään) ja varjovalinnat (vertailu)."""
    led, closed = tk["led"], tk["closed"]
    if not closed:
        return None
    pub = {w: led["viikot"].get(w, {}).get("julkaistu", "ai") for w in {e["viikko_id"] for e in closed}}
    shown = [e for e in closed if e.get("lahde") == pub[e["viikko_id"]]]
    other = [e for e in closed if e.get("lahde") != pub[e["viikko_id"]]]
    if not shown:
        return None
    return {"rivit": shown, "vertailu": other, "julkaistu": pub[shown[0]["viikko_id"]],
            "kertyma_ai": tutka.stats(led, "ai"), "kertyma_saanto": tutka.stats(led, "saanto")}


def tutka_factpack(facts: dict, tk: dict, wk: dict, number: int, sc: dict | None) -> dict:
    led = tk["led"]
    cols = ("tunnus", "nimi", "tyyppi", "perustelu", "laukaisija", "tulos_pct", "vertailu", "vertailu_pct",
            "ylituotto_pct", "osuma")
    return {
        "numero": number,
        "ennustettava_viikko": fi_range(wk["ensi_alku"], wk["ensi_loppu"]),
        "lahtopaiva": fi_date(wk["loppu"]),
        "kuplamittari": {"kuplalukema": facts["kuplalukema"], "vyohyke": facts["vyohyke"],
                         "teemat": {k: {"nimi": v["nimi"], "pisteet": v["pisteet"], "tila": v["tila"]}
                                    for k, v in facts["teemat"].items()}},
        "viime_viikon_tutka": [{k: e.get(k) for k in cols} for e in (sc["rivit"] if sc else [])],
        "momentum_saannon_tulos_viime_viikolla": [{k: e.get(k) for k in ("tunnus", "tyyppi", "ylituotto_pct", "osuma")}
                                                  for e in (sc["vertailu"] if sc else [])],
        "kertyma_tekoaly": tutka.stats(led, "ai"),
        "kertyma_momentum_saanto": tutka.stats(led, "saanto"),
        "aiemmat_opit": [o["oppi"] for o in led["opit"]],
        "ehdokkaat": tk["candidates"],
        "viikon_sektorit": tk["sectors"],
    }


def template_itsearvio(sc: dict) -> str:
    rows = sc["rivit"]
    hits = sum(e["osuma"] for e in rows)
    best = max(rows, key=lambda e: e["ylituotto_pct"] if e["tyyppi"] != "varoitus" else -e["ylituotto_pct"])
    return (f"Viime viikon tutka osui {hits}/{len(rows)}. Parhaiten onnistui {best['nimi']} "
            f"({fi_signed(best['ylituotto_pct'], 1)} %-yks. vertailuunsa). Yhden viikon tulos on pitkälti sattumaa, "
            "siksi menetelmää arvioidaan kertymän perusteella.")


def run_tutka(args, facts: dict, tk: dict, wk: dict, number: int, week_key: str, run_date: dt.date) -> tuple[dict, dict]:
    """Palauttaa (näkymä renderöintiin, AI-käyttö). Jäädyttää ennusteet kirjanpitoon (tallennus myöhemmin)."""
    led = tk["led"]
    sc = scorecard(tk)
    meta = led["viikot"].get(week_key, {})
    itsearvio, oppi, usage, note = meta.get("itsearvio", ""), meta.get("oppi", ""), {}, ""
    already = [e for e in led["ennusteet"] if e["viikko_id"] == week_key]
    if already:
        published = meta.get("julkaistu", "ai" if any(e["lahde"] == "ai" for e in already) else "saanto")
    else:
        tobj = None
        if args.mock_ai2:
            with open(args.mock_ai2, encoding="utf-8") as f:
                tobj = extract_json(f.read(), TUTKA_KEYS)
        elif not args.no_ai and API_KEY and run_date.weekday() >= 5:
            try:
                tobj, usage = ai_tutka(tutka_factpack(facts, tk, wk, number, sc))
            except Exception as e:  # noqa: BLE001
                warn(f"tutkan AI-kutsu epäonnistui, käytetään momentum-sääntöä: {str(e)[:200]}")
        ai_picks = validate_picks(tobj, tk["uni"]) if tobj else []
        if tobj and sc:
            itsearvio = _clean(tobj.get("itsearvio"), 700)
            oppi = _clean(tobj.get("uusi_oppi"), 250)
        published = "ai" if sum(p["tyyppi"] == "nousu" for p in ai_picks) >= 2 else "saanto"
        if tobj and published == "saanto":
            note = "Tekoälyn valinnat eivät läpäisseet tarkistusta, joten tämän viikon ehdokkaat valitsi momentum-sääntö."
        frozen = []
        if published == "ai":
            frozen += tutka.freeze(led, ai_picks, tk["prices"], tk["uni"], wk, week_key, number, run_date, "ai")
        frozen += tutka.freeze(led, tk["rules"], tk["prices"], tk["uni"], wk, week_key, number, run_date, "saanto")
        if frozen:
            led["viikot"][week_key] = {"julkaistu": published, "itsearvio": itsearvio, "oppi": oppi}
            tutka.add_lesson(led, oppi, number, run_date)
    if sc and not itsearvio:
        itsearvio = template_itsearvio(sc)
    picks = [e for e in led["ennusteet"] if e["viikko_id"] == week_key and e["lahde"] == published]
    return ({"scorecard": sc, "itsearvio": itsearvio, "oppi": oppi, "picks": picks, "published": published,
             "note": note, "feat": tk["feat"], "movers": tk["movers"], "sectors": tk["sectors"]}, usage)


# Sijoittajan minuutti -varapohja (käytetään, jos tekoäly ei ole käytettävissä)
MINUUTIT = [
    ("CAPE eli Shillerin P/E-luku",
     "Tavallinen P/E-luku vertaa osakkeen hintaa yhden vuoden tulokseen, jolloin yksittäinen hyvä tai huono vuosi "
     "vääristää kuvaa. Robert Shillerin kehittämä CAPE vertaa hintaa kymmenen vuoden inflaatiokorjattuun keskitulokseen. "
     "Se tasoittaa suhdanteet ja kertoo, kuinka kallis markkina on pitkässä juoksussa. Korkea CAPE on historiallisesti "
     "ennakoinut heikompia kymmenen vuoden tuottoja, mutta se ei kerro, milloin käänne tulee."),
    ("Buffett-indikaattori",
     "Buffett-indikaattori vertaa koko osakemarkkinan arvoa maan bruttokansantuotteeseen. Warren Buffett kutsui sitä "
     "vuonna 2001 ehkä parhaaksi yksittäiseksi arvostusmittariksi. Kun pörssin arvo kasvaa paljon nopeammin kuin talous, "
     "jonka tuloksista yhtiöt viime kädessä elävät, lukema nousee. Nykyään yhtiöiden ulkomaiset tulot vääristävät "
     "mittaria, joten sitä kannattaa verrata omaan historiaansa."),
    ("Osakeriskipreemio",
     "Osakeriskipreemio on lisätuotto, jota sijoittajat odottavat osakkeilta riskittömään valtionlainaan verrattuna. "
     "Sitä voi arvioida vertaamalla osakkeiden tulostuottoa eli tulosta jaettuna hinnalla valtionlainan korkoon. Kun ero "
     "kapenee, osakkeiden riskistä maksetaan vähemmän. Se on usein merkki siitä, että osakkeet ovat kalliita suhteessa "
     "korkoihin."),
    ("Velalla ostaminen (margin debt)",
     "Arvopaperiluotolla sijoittaja ostaa osakkeita velaksi niin, että vakuutena ovat osakkeet itse. Velkavipu kasvattaa "
     "sekä voittoja että tappioita. Kun kurssit laskevat, välittäjä voi vaatia lisävakuuksia, jolloin sijoittajat "
     "joutuvat myymään. Myynti voi kiihdyttää laskua. Siksi velalla ostamisen nopea kasvu on perinteinen kuplan merkki."),
    ("Persentiili",
     "Persentiili kertoo, mihin kohtaan historiaa nykyinen lukema sijoittuu. Jos arvostuksen persentiili on 95, lukema "
     "on korkeampi kuin 95 prosentissa kaikista mitatuista kuukausista. Kuplamittari käyttää persentiilejä, jotta hyvin "
     "erilaisia mittareita voi verrata samalla asteikolla."),
    ("Vertailuindeksi",
     "Vertailuindeksi on mittatikku, johon sijoituksen tuottoa verrataan. Jos osake nousee 3 prosenttia mutta S&P 500 "
     "nousee 4 prosenttia, osake jäi jälkeen, vaikka se tuotti voittoa. Siksi Kuplamittarin tutka pisteytetään aina "
     "suhteessa vertailuindeksiin eikä pelkän nousun perusteella."),
    ("Momentum",
     "Momentum tarkoittaa ilmiötä, jossa viime kuukausina hyvin menestyneet osakkeet ovat keskimäärin menestyneet "
     "vielä jonkin aikaa. Se on yksi rahoitustutkimuksen tunnetuimmista havainnoista. Kääntöpuolena ovat äkilliset "
     "romahdukset: kun trendi katkeaa, eniten nousseet kohteet voivat pudota nopeimmin."),
    ("200 päivän liukuva keskiarvo",
     "200 päivän liukuva keskiarvo on osakkeen päätöskurssien keskiarvo noin kymmenen viime kuukauden ajalta. Monet "
     "sijoittajat pitävät sen yläpuolella kulkevaa kurssia nousutrendin merkkinä. Kun kurssi karkaa kauas keskiarvonsa "
     "yläpuolelle, osake on usein ylikuumentunut."),
]


def template_minuutti(number: int, previous: list[str]) -> dict:
    for i in range(len(MINUUTIT)):
        aihe, teksti = MINUUTIT[(number - 1 + i) % len(MINUUTIT)]
        if aihe not in previous:
            return {"aihe": aihe, "teksti": teksti}
    aihe, teksti = MINUUTIT[(number - 1) % len(MINUUTIT)]
    return {"aihe": aihe, "teksti": teksti}


def template_luku(facts: dict) -> dict:
    md = (facts.get("raakatasot") or {}).get("margin_debt") or {}
    if md.get("taso_mrd_usd"):
        return {"luku": f"{fi_num(md['taso_mrd_usd'] / 1000, 2)} biljoonaa $",
                "otsikko": "Velalla ostettuja osakkeita",
                "teksti": "Sen verran yhdysvaltalaiset sijoittajat ovat lainanneet välittäjiltään osakeostoihin. "
                          "Velkavipu kiihdyttää nousuja, mutta myös laskuja."}
    y = _mk(facts, "USA 10 v korko")
    if y:
        return {"luku": f"{fi_num(y['taso'], 2)} %", "otsikko": "USA:n 10 vuoden korko",
                "teksti": "Pitkä korko on osakkeiden tärkein vertailukohta: mitä korkeampi korko, sitä vähemmän "
                          "kalliille arvostuksille jää tilaa."}
    return {}


def template_syvasukellus(facts: dict) -> dict:
    """Ilman tekoälyä koottu, hieman pidempi verkko-osio: pureudutaan kuplamaisimpaan teemaan
    ja lähimpään historialliseen analogiaan tarkemmin kuin lyhyt kirje ehtii."""
    th = facts["teemat"]
    top = max((t for t in th.values() if t.get("pisteet") is not None), key=lambda t: t["pisteet"], default=None)
    an = (facts.get("analogiat") or [None])[0]
    if not top and not an:
        return {}
    parts = []
    if top:
        parts.append(f"Kuplamittarin kolmesta teemasta kuplamaisin on tällä hetkellä {top['nimi'].lower()}, "
                      f"jonka tila on {top['tila'].lower()} ({disp(top['pisteet'])}/100). Se tarkoittaa, että nykytaso on "
                      "kuplamaisempi kuin valtaosassa mitattua historiaa – mittari vertaa aina lukemaa sen omaan "
                      "menneisyyteen, ei mielivaltaiseen rajaan.")
    if an:
        parts.append(f"Lähin historiallinen vertailukohta on huippu {an['huippu']} ({an['samankaltaisuus_pct']} % "
                      f"samankaltaisuus nykytilanteeseen). Samankaltaisinta: {', '.join(an['samankaltaisinta'][:2]).lower()}. "
                      f"Erilaisinta: {', '.join(an['erilaisinta'][:2]).lower()}. Historia ei toista itseään "
                      "täsmälleen, mutta samat rakenteelliset piirteet – liiallinen optimismi, halpa velka tai "
                      "molemmat – ovat toistuneet joka kerta ennen suurta korjausliikettä.")
    parts.append("Muista: korkea lukema kertoo riskin tasosta, ei ajankohdasta. Kuplat voivat paisua vuosia ennen "
                  "puhkeamistaan, ja mittari on tarkoitettu pitkän aikavälin näkymän hahmottamiseen, ei viikon "
                  "kaupankäyntipäätöksiin.")
    return {"otsikko": f"Syväsukellus: {top['nimi'].lower()}" if top else "Syväsukellus: historian kaiut",
            "teksti": " ".join(parts)}


# ----------------------------------------------------------------------------
# Ilmainen pohja (varakirjoittaja): sama rakenne datasta
# ----------------------------------------------------------------------------
def _mk(facts: dict, name: str) -> dict | None:
    return next((m for m in facts["markkinat"] if m["nimi"] == name), None)


def _move_phrase(m: dict | None, name: str) -> str | None:
    if not m or "viikkomuutos" not in m:
        return None
    x = m["viikkomuutos"]
    verb = "nousi" if x > 0 else ("laski" if x < 0 else "pysyi ennallaan")
    return f"{name} {verb} {fi_num(abs(x), 1)} %" if x != 0 else f"{name} pysyi ennallaan"


def template_write(facts: dict) -> dict:
    th, v, raw = facts["teemat"], facts["muuttujat"], facts.get("raakatasot", {})
    sp, nq, hx = _mk(facts, "S&P 500"), _mk(facts, "Nasdaq Composite"), _mk(facts, "OMX Helsinki 25")
    top = max((t for t in th.values() if t.get("pisteet") is not None), key=lambda t: t["pisteet"], default=None)

    moves = [p for p in (_move_phrase(sp, "S&P 500"), _move_phrase(nq, "Nasdaq"), _move_phrase(hx, "Helsingin OMXH25")) if p]
    otsikko = (moves[0] if moves else "Pörssiviikko") + (f" – {top['nimi'].lower()} {top['tila'].lower()}" if top else "")
    ingressi = (f"Kuluneella viikolla {', '.join(moves)}. " if moves else "") + \
        (f"Kuplamittarin teemoista kuplamaisin on {top['nimi'].lower()}, jonka tila on {top['tila'].lower()}." if top else "")

    viikko = []
    for name in ("S&P 500", "Nasdaq Composite", "OMX Helsinki 25", "Stoxx 600 (Eurooppa)"):
        p = _move_phrase(_mk(facts, name), name)
        if p:
            viikko.append({"otsikko": p + ".", "teksti": ""})
    y = _mk(facts, "USA 10 v korko")
    if y:
        viikko.append({"otsikko": f"USA:n 10 vuoden korko on {fi_num(y['taso'], 2)} %.", "teksti": ""})
    vx = _mk(facts, "VIX-pelkoindeksi")
    if vx:
        viikko.append({"otsikko": f"VIX-pelkoindeksi on {fi_num(vx['taso'], 1)}.", "teksti": ""})

    def var_sentence(k, text_fn):
        return text_fn(v[k]) if k in v and v[k].get("arvo") is not None else ""

    arvostus = " ".join(s for s in [
        var_sentence("A1", lambda a: f"Shillerin P/E-luku (hinta suhteessa kymmenen vuoden keskitulokseen) on {fi_num(a['arvo'], 1)}. "
                     + ("Se on korkein koskaan mitattu." if a.get("on_ennatys") else
                        (f"Korkeammalla on käyty vain vuosina {a['kuplamaisempi_kuin_nyt_vuosina']}." if a.get("kuplamaisempi_kuin_nyt_vuosina") else ""))),
        var_sentence("A6", lambda a: f"Osakkeiden riskipreemio on kuplamaisempi kuin {fi_num(a['persentiili'], 0)} %:ssa historiasta."
                     if a.get("persentiili") is not None else ""),
    ] if s)
    md = raw.get("margin_debt") or {}
    velkavipu = ""
    if md:
        velkavipu = (f"Sijoittajien arvopaperiluottoja (margin debt) on {fi_num(md['taso_mrd_usd'] / 1000, 2)} biljoonaa dollaria"
                     + (f", {fi_num(md['kasvu_12kk_pct'], 0)} % enemmän kuin vuosi sitten." if md.get("kasvu_12kk_pct") is not None else "."))
    raha_bits = []
    if "B4" in v and v["B4"].get("persentiili") is not None:
        raha_bits.append("Keskuspankit eivät paisuta taseitaan." if v["B4"]["persentiili"] < 40 else "Keskuspankkien taseet kasvavat.")
    if "B7" in v and v["B7"].get("persentiili") is not None:
        raha_bits.append("Talouden velka ei kasva suhteessa talouden kokoon." if v["B7"]["persentiili"] < 40
                         else "Talouden velkaantuminen kiihtyy.")
    teemat = {"arvostus": arvostus, "velkavipu": velkavipu, "raha": " ".join(raha_bits)}

    historia = []
    an = facts.get("analogiat") or []
    if an:
        a0 = an[0]
        historia.append(f"Nykytila muistuttaa eniten huippua {a0['huippu']} ({a0['samankaltaisuus_pct']} % samankaltaisuus). "
                        f"Samankaltaisinta: {', '.join(a0['samankaltaisinta'][:2]).lower()}. "
                        f"Erilaisinta: {', '.join(a0['erilaisinta'][:2]).lower()}.")
    historia.append("Muistutus mittasuhteista: Alan Greenspan varoitti ”irrationaalisesta innostuksesta” joulukuussa 1996, "
                    "ja pörssi nousi sen jälkeen vielä yli kolme vuotta. Korkea lukema kertoo riskin tasosta, ei ajankohdasta.")

    hot = [t["nimi"].lower() for t in th.values() if t.get("pisteet") is not None and t["pisteet"] >= 75]
    calm = [t["nimi"].lower() for t in th.values() if t.get("pisteet") is not None and t["pisteet"] < 40]
    loppu = (f"Lukemaa nostavat {' ja '.join(hot)}" if hot else "Mikään teema ei ole ääripäässä") + \
        (f", jarruna toimii {' ja '.join(calm)}." if calm else ".")
    return {"otsikko": otsikko[:90], "ingressi": ingressi, "esikatselu": "Viikon pörssi ja kuplariski: mitä mittari näyttää?",
            "viikko": viikko[:6], "teemat": teemat, "historia": historia, "ensi_viikko": [], "loppusanat": loppu,
            "lahteet": []}


# ----------------------------------------------------------------------------
# Tarkistus ja yhdistäminen
# ----------------------------------------------------------------------------
def _clean(s, maxlen: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if len(s) > maxlen:
        s = s[:maxlen].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s


def sanitize(ai: dict | None, tmpl: dict, score: float) -> tuple[dict, list[str]]:
    """Ottaa AI:n kentät, jos ne ovat kelvollisia; muuten pohjan. Palauttaa myös listan kentistä,
    jotka korvattiin pohjalla (näytetään lokissa)."""
    replaced = []
    out = {}
    ai = ai or {}

    def pick(key, ok):
        if key in ai and ok(ai[key]):
            return ai[key]
        if ai:
            replaced.append(key)
        return tmpl[key]

    is_str = lambda x: isinstance(x, str) and x.strip() != ""
    nums = "|".join(sorted({str(int(score)), str(int(round(score)))}))
    reveals = re.compile(rf"(\b({nums})\s*/\s*100\b|\bkuplalukema\w*\s+(on\s+)?\d)", re.I)
    no_reveal = lambda x: is_str(x) and not reveals.search(x)

    out["otsikko"] = _clean(pick("otsikko", no_reveal), 90)
    out["ingressi"] = _clean(pick("ingressi", no_reveal), 700)
    out["esikatselu"] = _clean(pick("esikatselu", no_reveal), 110)
    wk = pick("viikko", lambda x: isinstance(x, list) and len(x) >= 2)
    out["viikko"] = [{"otsikko": _clean(i.get("otsikko"), 140), "teksti": _clean(i.get("teksti"), 420)}
                     for i in wk if isinstance(i, dict) and (i.get("otsikko") or i.get("teksti"))][:6]
    te = ai.get("teemat") if isinstance(ai.get("teemat"), dict) else {}
    out["teemat"] = {}
    for k in ("arvostus", "velkavipu", "raha"):
        if is_str(te.get(k)):
            out["teemat"][k] = _clean(te[k], 900)
        else:
            if ai:
                replaced.append(f"teemat.{k}")
            out["teemat"][k] = tmpl["teemat"].get(k, "")
    hi = pick("historia", lambda x: isinstance(x, list) and any(is_str(p) for p in x))
    out["historia"] = [_clean(p, 900) for p in hi if is_str(p)][:3]
    nw = ai.get("ensi_viikko") if isinstance(ai.get("ensi_viikko"), list) else tmpl["ensi_viikko"]
    out["ensi_viikko"] = [{"paiva": _clean(i.get("paiva"), 20), "teksti": _clean(i.get("teksti"), 300)}
                          for i in nw if isinstance(i, dict) and i.get("teksti")][:5]
    out["loppusanat"] = _clean(pick("loppusanat", no_reveal), 400)
    src = ai.get("lahteet") if isinstance(ai.get("lahteet"), list) else []
    out["lahteet"] = [{"otsikko": _clean(s.get("otsikko") or s.get("url"), 90), "url": s["url"].strip()}
                      for s in src if isinstance(s, dict) and isinstance(s.get("url"), str)
                      and re.match(r"^https?://", s["url"].strip())][:8]

    vl = ai.get("viikon_luku")
    if isinstance(vl, dict) and is_str(vl.get("luku")) and is_str(vl.get("teksti")) \
            and not reveals.search(" ".join(str(v) for v in vl.values())):
        out["viikon_luku"] = {"luku": _clean(vl["luku"], 30), "otsikko": _clean(vl.get("otsikko"), 70),
                              "teksti": _clean(vl["teksti"], 320)}
    else:
        if ai:
            replaced.append("viikon_luku")
        out["viikon_luku"] = tmpl.get("viikon_luku") or {}
    lk = ai.get("liikkujat") if isinstance(ai.get("liikkujat"), list) else []
    out["liikkujat"] = {_norm_ticker(i.get("tunnus")): _clean(i.get("syy"), 220)
                        for i in lk if isinstance(i, dict) and i.get("tunnus") and is_str(i.get("syy"))}
    mi = ai.get("minuutti")
    if isinstance(mi, dict) and is_str(mi.get("aihe")) and is_str(mi.get("teksti")):
        out["minuutti"] = {"aihe": _clean(mi["aihe"], 70), "teksti": _clean(mi["teksti"], 800)}
    else:
        if ai:
            replaced.append("minuutti")
        out["minuutti"] = tmpl.get("minuutti") or {}
    sd = ai.get("syvasukellus")
    if isinstance(sd, dict) and is_str(sd.get("otsikko")) and is_str(sd.get("teksti")) \
            and not reveals.search(f"{sd['otsikko']} {sd['teksti']}"):
        out["syvasukellus"] = {"otsikko": _clean(sd["otsikko"], 90), "teksti": _clean(sd["teksti"], 3500)}
    else:
        if ai:
            replaced.append("syvasukellus")
        out["syvasukellus"] = tmpl.get("syvasukellus") or {}
    return out, replaced


# ----------------------------------------------------------------------------
# HTML-sähköposti
# ----------------------------------------------------------------------------
STYLE = """<style>
  body { margin:0; padding:0; }
  .num { font-variant-numeric: tabular-nums; }
  @media (prefers-color-scheme: dark) {
    .bg-page  { background:#0d0d0d !important; }
    .bg-card  { background:#1a1a19 !important; }
    .ink-1    { color:#ffffff !important; }
    .ink-2    { color:#c3c2b7 !important; }
    .ink-3    { color:#898781 !important; }
    .hair     { border-color:#2c2c2a !important; }
    .up       { color:#0ca30c !important; }
    .down     { color:#e66767 !important; }
    .sector   { color:#b79aef !important; }
    .trk-good { background:#164315 !important; }
    .trk-warn { background:#5d4819 !important; }
    .trk-ser  { background:#593a2d !important; }
    .trk-crit { background:#512423 !important; }
    .trk-none { background:#2c2c2a !important; }
    .pill-good, .pill-warn, .pill-ser, .pill-crit { color:#ffffff !important; }
    .pill-good { background:#164315 !important; }
    .pill-warn { background:#5d4819 !important; }
    .pill-ser  { background:#593a2d !important; }
    .pill-crit { background:#512423 !important; }
    a { color:#86b6ef !important; }
    .bg-soft { background:#232322 !important; }
    .hm-u3 { background:#2b6a2a !important; } .hm-u2 { background:#1f4f1e !important; } .hm-u1 { background:#183a17 !important; }
    .hm-d1 { background:#3d2020 !important; } .hm-d2 { background:#522625 !important; } .hm-d3 { background:#6e2e2d !important; }
    .hm-0 { background:#2c2c2a !important; }
    .gap { border-color:#1a1a19 !important; }
    .edge-up { border-left-color:#0ca30c !important; } .edge-warn { border-left-color:#e66767 !important; }
    .edge-sector { border-left-color:#b79aef !important; }
    .fg-0 { background:#0d366b !important; } .fg-1 { background:#3987e5 !important; } .fg-2 { background:#585650 !important; }
    .fg-3 { background:#8a4a49 !important; } .fg-4 { background:#e66767 !important; }
  }
  @media (max-width: 480px) {
    .px { padding-left:20px !important; padding-right:20px !important; }
    .hero { font-size:56px !important; }
  }
</style>"""

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def rich(s: str) -> str:
    """Escapaa tekstin ja sallii vain **lihavoinnin**. Sitoo luvun ja %-merkin yhteen."""
    t = html.escape(s or "", quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(\d) (%|\$|bp\b)", rf"\1{NBSP}\2", t)
    return t


def strip_md(s: str) -> str:
    """Poistaa **lihavointi**-merkinnät raakatekstiin (historia, arkisto)."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", s or "")


def label(text: str) -> str:
    return (f'<div class="ink-3" style="font-size:12px;font-weight:700;letter-spacing:0.1em;'
            f'text-transform:uppercase;color:#898781;">{text}</div>')


def meter(score: float | None, level: str, height: int = 6) -> str:
    lv = LEVEL.get(level, LEVEL["none"])
    w = 0 if score is None else max(1, min(99, int(round(score))))
    r = height // 2
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td width="{w}%" height="{height}" style="background:{lv["fill"]};height:{height}px;'
            f'border-radius:{r}px 0 0 {r}px;font-size:0;line-height:0;">&nbsp;</td>'
            f'<td class="trk-{lv["cls"]}" height="{height}" style="background:{lv["track"]};height:{height}px;'
            f'border-radius:0 {r}px {r}px 0;font-size:0;line-height:0;">&nbsp;</td></tr></table>')


def disp(score: float) -> int:
    """Lukijalle näytettävä kokonaisluku. Pyöristys alaspäin: 99,5 näytetään 99:nä, koska '100'
    tarkoittaisi lukijalle kaikkien aikojen ennätystä."""
    return 100 if score >= 100 else int(score)


def zone_of(score: float) -> int:
    for i, (lo, hi, _) in enumerate(ZONES):
        if score < hi:
            return i
    return len(ZONES) - 1


TUTKA_ILMOITUS = os.environ.get("TUTKA_ILMOITUS", "Kirje on toistaiseksi laatijan omaan käyttöön.")
HEAT = {"u3": "#9fd79e", "u2": "#c7e8c6", "u1": "#e3f3e2", "d1": "#f8e4e3", "d2": "#f2d2d1", "d3": "#e8a9a8", "0": "#efeee9"}


def _row(inner: str, top: int = 30) -> str:
    return f'<tr><td class="px" style="padding:{top}px 32px 0;">{inner}</td></tr>'


def _pct_cell(x: float, dec: int = 1) -> tuple[str, str, str]:
    """(teksti, luokka, väri) prosenttimuutokselle; suunta näkyy nuolena ja etumerkkinä, ei vain värinä."""
    arrow = "&#9650;" if x > 0 else ("&#9660;" if x < 0 else "")
    txt = f"{arrow}{NBSP}{fi_signed(x, dec)}{NBSP}%".strip()
    return (txt, "up", "#006300") if x > 0 else ((txt, "down", "#d03b3b") if x < 0 else (txt, "ink-2", "#52514e"))


def render_feargreed(fg: dict | None) -> str:
    """Pelko/ahneus-mittari värillisenä palkkina (5 vyöhykettä, pelko vasemmalla, ahneus oikealla)
    ja osoittimena palkin päällä. Rakennettu pelkillä taulukkosoluilla ja väripohjilla ilman
    SVG:tä tai kuvia, koska moni sähköpostiohjelma (mm. Gmailin sovellukset) ei piirrä inline-SVG:tä
    luotettavasti – aiempi SVG-versio näkyi osalle lukijoista pelkkänä tekstinä "PELKOAHNEUS"."""
    if not fg:
        return ""
    score = max(0.0, min(100.0, fg["pisteet"]))
    seg = "".join(
        f'<td class="fg-{i}" width="{hi - lo}%" height="14" style="background:{FG_COLORS[i]};height:14px;'
        f'font-size:0;line-height:0;{"border-radius:7px 0 0 7px;" if i == 0 else ""}'
        f'{"border-radius:0 7px 7px 0;" if i == len(FG_ZONES) - 1 else ""}">&nbsp;</td>'
        for i, (lo, hi, _) in enumerate(FG_ZONES))
    pointer = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
               f'<td width="{score:.0f}%" style="font-size:0;line-height:0;">&nbsp;</td>'
               f'<td class="ink-1" style="font-size:18px;line-height:1;color:#0b0b0b;font-weight:700;">&#9660;</td>'
               f'<td style="font-size:0;line-height:0;">&nbsp;</td></tr></table>')
    bar = (pointer
           + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{seg}</tr></table>'
           + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
           + '<td align="left" class="ink-3" style="font-size:11px;font-weight:700;letter-spacing:0.06em;color:#898781;padding-top:4px;">PELKO</td>'
           + '<td align="right" class="ink-3" style="font-size:11px;font-weight:700;letter-spacing:0.06em;color:#898781;padding-top:4px;">AHNEUS</td>'
           + '</tr></table>')
    parts_txt = ", ".join(o["nimi"] for o in fg["osat"])
    return _row(
        label("Pelko vai ahneus?")
        + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;">'
        f'<tr><td style="padding:6px 4px 0;">{bar}</td></tr>'
        '<tr><td align="center" class="ink-1" style="padding-top:14px;font-size:22px;font-weight:700;color:#0b0b0b;">'
        f'{disp(score)}<span class="ink-2" style="font-size:15px;font-weight:600;color:#52514e;">/100</span>'
        f'&nbsp;&middot;&nbsp;{html.escape(fg["vyohyke"])}</td></tr>'
        '<tr><td align="center" class="ink-2" style="padding-top:8px;font-size:13px;line-height:1.5;color:#52514e;'
        'max-width:420px;">Viikon markkinatunnelma lyhyellä aikavälillä – eri asia kuin kuplalukema, joka mittaa '
        'yliarvostusta pitkällä aikavälillä.</td></tr>'
        f'<tr><td align="center" class="ink-3" style="padding-top:6px;font-size:11px;color:#898781;">'
        f'Perustuu: {html.escape(parts_txt)}.</td></tr>'
        "</table>", 30)


def render_luku(vl: dict) -> str:
    if not vl or not vl.get("luku"):
        return ""
    head = (f'<div class="ink-1" style="font-size:15px;font-weight:700;color:#0b0b0b;padding-top:2px;">{rich(vl["otsikko"])}</div>'
            if vl.get("otsikko") else "")
    return _row('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="border-left:4px solid #ec835a;"><tr><td style="padding:2px 0 2px 16px;">'
                + label("Viikon luku")
                + f'<div class="ink-1" style="font-size:34px;font-weight:700;line-height:1.15;color:#0b0b0b;padding-top:4px;">{rich(vl["luku"])}</div>'
                + head
                + f'<div class="ink-2" style="font-size:15px;line-height:1.5;color:#52514e;padding-top:4px;">{rich(vl.get("teksti", ""))}</div>'
                + '</td></tr></table>', 26)


def render_sectors(secs: list[dict]) -> str:
    if not secs:
        return ""
    cells = []
    for sct in secs:
        x = sct["viikko_pct"]
        k = "0" if abs(x) < 0.1 else (("u" if x > 0 else "d") + ("1" if abs(x) < 1 else ("2" if abs(x) < 3 else "3")))
        arrow = "&#9650;" if x > 0 else ("&#9660;" if x < 0 else "&#9679;")
        cells.append(f'<td class="hm-{k} gap" width="33%" valign="top" style="background:{HEAT[k]};padding:8px 10px;'
                     f'border:2px solid #fcfcfb;border-radius:6px;"><div class="ink-1" style="font-size:12px;color:#0b0b0b;">{html.escape(sct["nimi"])}</div>'
                     f'<div class="ink-1" style="font-size:15px;font-weight:700;color:#0b0b0b;white-space:nowrap;">{arrow}{NBSP}{fi_signed(x, 1)}{NBSP}%</div></td>')
    rows = "".join("<tr>" + "".join(cells[i:i + 3]) + ("<td></td>" * (3 - len(cells[i:i + 3]))) + "</tr>"
                   for i in range(0, len(cells), 3))
    return _row(label("Sektorit viikolla") +
                '<p class="ink-2" style="margin:6px 0 8px;font-size:13px;color:#52514e;">S&amp;P 500:n sektorit viikon muutoksen mukaan, paras ensin.</p>'
                '<table role="presentation" class="num" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="font-variant-numeric:tabular-nums;">{rows}</table>', 28)


def render_movers(mv: dict, reasons: dict) -> str:
    if not mv or not (mv.get("nousijat") or mv.get("laskijat")):
        return ""

    def block(title, items):
        out = [f'<tr><td colspan="2" class="ink-3" style="padding:10px 0 2px;font-size:12px;font-weight:700;color:#898781;">{title}</td></tr>']
        for m in items[:3]:
            txt, cls, col = _pct_cell(m["viikko_pct"])
            why = reasons.get(m["tunnus"], "")
            why_html = f'<div class="ink-2" style="font-size:13px;line-height:1.45;color:#52514e;padding-top:2px;">{rich(why)}</div>' if why else ""
            out.append('<tr><td class="ink-1 hair" style="padding:8px 8px 8px 0;border-top:1px solid #e1e0d9;color:#0b0b0b;font-size:15px;">'
                       f'<strong>{html.escape(m["nimi"])}</strong> <span class="ink-3" style="font-size:12px;color:#898781;">'
                       f'{html.escape(m["tunnus"])} &middot; {html.escape(str(m["sektori"]))}</span>{why_html}</td>'
                       f'<td class="{cls} hair" align="right" valign="top" style="padding:8px 0;border-top:1px solid #e1e0d9;'
                       f'color:{col};white-space:nowrap;font-size:15px;font-weight:700;">{txt}</td></tr>')
        return "".join(out)
    return _row(label("Viikon nousijat ja laskijat") +
                '<table role="presentation" class="num" width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="margin-top:4px;font-variant-numeric:tabular-nums;">'
                + block("Suurimmat nousijat (S&amp;P 500)", mv.get("nousijat", []))
                + block("Suurimmat laskijat", mv.get("laskijat", [])) + '</table>', 28)


def _stat_line(st: dict) -> str:
    return "–" if not st or not st.get("n") else f'{st["osumat"]}/{st["n"]} ({fi_num(st["osumaprosentti"], 0)}{NBSP}%)'


def render_scorecard(tv: dict) -> str:
    sc = tv.get("scorecard")
    if not sc:
        if tv.get("picks"):
            return _row(label("Tuloskortti") + '<p class="ink-2" style="margin:8px 0 0;font-size:15px;line-height:1.55;color:#52514e;">'
                        'Tuloskortti alkaa ensi numerossa: tämän numeron tutka on ensimmäinen, joka pisteytetään.</p>', 28)
        return ""
    rows = sc["rivit"]
    hits = sum(e["osuma"] for e in rows)
    trs = []
    for e in rows:
        kind = ("&#9650;&nbsp;Nousu" if e["tyyppi"] == "nousu" else
                "&#8635;&nbsp;Sektori" if e["tyyppi"] == "sektori" else "&#9888;&#65038;&nbsp;Varoitus")
        ok = e["osuma"]
        verdict = (f'<span class="up" style="color:#006300;font-weight:700;">&#10003;&nbsp;Osui</span>' if ok else
                   f'<span class="down" style="color:#d03b3b;font-weight:700;">&#10007;&nbsp;Ohi</span>')
        cell = "padding:8px 0;border-top:1px solid #e1e0d9;"
        trs.append(f'<tr><td class="ink-1 hair" style="{cell}color:#0b0b0b;font-size:14px;">'
                   f'<strong>{html.escape(e["nimi"])}</strong><br><span class="ink-3" style="font-size:12px;color:#898781;">{kind} &middot; {html.escape(e["tunnus"])}</span></td>'
                   f'<td class="ink-1 hair" align="right" style="{cell}color:#0b0b0b;font-size:14px;white-space:nowrap;">{fi_signed(e["tulos_pct"], 1)}{NBSP}%</td>'
                   f'<td class="ink-2 hair" align="right" style="{cell}color:#52514e;font-size:14px;white-space:nowrap;">{fi_signed(e["ylituotto_pct"], 1)}{NBSP}%-yks.</td>'
                   f'<td class="hair" align="right" style="{cell}font-size:14px;white-space:nowrap;">{verdict}</td></tr>')
    head = ('<tr class="ink-3" style="color:#898781;font-size:12px;"><td style="padding:0 0 6px;">Viime viikon ennuste</td>'
            '<td align="right" style="padding:0 0 6px;">Tuotto</td><td align="right" style="padding:0 0 6px;">Vs. vertailu</td>'
            '<td align="right" style="padding:0 0 6px;width:70px;">Tulos</td></tr>')
    ka, ks = sc.get("kertyma_ai") or {}, sc.get("kertyma_saanto") or {}
    summary = (f'<div class="ink-1" style="font-size:16px;font-weight:700;color:#0b0b0b;padding-top:12px;">Tällä viikolla {hits}/{len(rows)} osui.</div>'
               f'<div class="ink-2" style="font-size:13px;line-height:1.5;color:#52514e;padding-top:4px;">Kertymä numerosta 1: '
               f'tekoälyn valinnat {_stat_line(ka)} &middot; pelkkä momentum-sääntö {_stat_line(ks)}. '
               'Vertailuna S&amp;P 500, kryptoilla bitcoin.</div>')
    ia = (f'<p class="ink-1" style="margin:12px 0 0;font-size:15px;line-height:1.6;color:#0b0b0b;">{rich(tv["itsearvio"])}</p>'
          if tv.get("itsearvio") else "")
    op = (f'<div class="bg-soft ink-1" style="margin-top:12px;background:#f4f3ee;border-radius:8px;padding:10px 14px;font-size:14px;'
          f'line-height:1.5;color:#0b0b0b;"><strong>Opittu:</strong> {rich(tv["oppi"])}</div>' if tv.get("oppi") else "")
    return _row(label("Tuloskortti") + '<table role="presentation" class="num" width="100%" cellpadding="0" cellspacing="0" '
                'border="0" style="margin-top:10px;font-variant-numeric:tabular-nums;">' + head + "".join(trs) + '</table>'
                + summary + ia + op, 28)


def render_tutka(tv: dict, wk: dict) -> str:
    picks = tv.get("picks") or []
    if not picks:
        return _row(label("Tutka") + '<p class="ink-2" style="margin:8px 0 0;font-size:15px;color:#52514e;">'
                    'Tutkan ennusteet julkaistaan lauantain numerossa.</p>', 28)
    feat = tv.get("feat")
    kinds = {p["tyyppi"] for p in picks}
    # Kerro johdannossa vain se, mitä tällä viikolla oikeasti on jäädytetty – ei luvata sektorinäkymää,
    # jos esim. sektori-ETF:ien dataa ei tällä kertaa saatu.
    nimet = [n for k, n in (("nousu", "nousuehdokkaat"), ("sektori", "sektorinäkymä"), ("varoitus", "kuplavaroitus"))
             if k in kinds]
    lista = " ja ".join(nimet) if len(nimet) < 3 else f"{', '.join(nimet[:-1])} ja {nimet[-1]}"
    intro = (f'Ensi viikon ({fi_range(wk["ensi_alku"], wk["ensi_loppu"])}) {lista}. '
             'Tulos lasketaan maanantain avauskurssista perjantain päätöskurssiin, eli juuri niin kuin lukija olisi '
             'voinut toimia. Tulokset ensi numerossa.')
    if tv.get("published") == "saanto":
        intro += " " + (tv.get("note") or "Tällä viikolla ehdokkaat valitsi pelkkä momentum-sääntö.")
    cards = []
    order = ([p for p in picks if p["tyyppi"] == "nousu"] + [p for p in picks if p["tyyppi"] == "sektori"]
             + [p for p in picks if p["tyyppi"] == "varoitus"])
    for p in order:
        typ = p["tyyppi"]
        if typ == "nousu":
            edge, col, cls, txtcls, tag = "#0ca30c", "#006300", "edge-up", "up", "&#9650;&nbsp;Nousuehdokas"
        elif typ == "sektori":
            edge, col, cls, txtcls, tag = "#6f42c1", "#6f42c1", "edge-sector", "sector", "&#8635;&nbsp;Sektorinäkymä"
        else:
            edge, col, cls, txtcls, tag = "#d03b3b", "#d03b3b", "edge-warn", "down", "&#9888;&#65038;&nbsp;Kuplavaroitus"
        data = []
        if feat is not None and p["tunnus"] in feat.index:
            f = feat.loc[p["tunnus"]]
            if typ == "sektori" and pd.notna(f.get("r4w")):
                data.append(f"4 vk {fi_signed(float(f['r4w']), 1)}{NBSP}%")
            if pd.notna(f.get("r12w")):
                data.append(f"12 vk {fi_signed(float(f['r12w']), 1)}{NBSP}%")
            if pd.notna(f.get("yli_200pv")):
                data.append(f"{fi_signed(float(f['yli_200pv']), 0)}{NBSP}% vs. 200 pv keskiarvo")
        if p.get("kurssi_jaadytettaessa"):
            data.append(f"viim. kurssi {fi_num(p['kurssi_jaadytettaessa'], 2)}{NBSP}$")
        lines = "".join(
            f'<div class="ink-1" style="font-size:14px;line-height:1.5;color:#0b0b0b;padding-top:4px;"><strong>{k}:</strong> {rich(v)}</div>'
            for k, v in (("Miksi", p.get("perustelu")), ("Laukaisija", p.get("laukaisija")), ("Riski", p.get("riski"))) if v)
        # Sektori-ETF:llä nimi ON jo sektorin nimi (esim. "Energia"), joten alaotsikkoon riittää tunnus
        # eikä toisteta samaa sanaa kahdesti.
        sub = (html.escape(p["tunnus"]) if typ == "sektori" else
               f'{html.escape(p["tunnus"])} &middot; {html.escape(str(p.get("sektori", "")))}')
        cards.append(f'<table role="presentation" class="hair {cls}" width="100%" cellpadding="0" cellspacing="0" border="0" '
                     f'style="border:1px solid #e1e0d9;border-left:4px solid {edge};border-radius:8px;margin-top:10px;">'
                     f'<tr><td style="padding:12px 16px;"><div class="{txtcls}" style="font-size:12px;font-weight:700;letter-spacing:0.06em;'
                     f'text-transform:uppercase;color:{col};">{tag}</div>'
                     f'<div class="ink-1" style="font-size:16px;font-weight:700;color:#0b0b0b;padding-top:2px;">{html.escape(p["nimi"])} '
                     f'<span class="ink-3" style="font-size:13px;font-weight:400;color:#898781;">{sub}</span></div>'
                     f'<div class="ink-3 num" style="font-size:12px;color:#898781;padding-top:2px;">{" &middot; ".join(data)}</div>'
                     f'{lines}</td></tr></table>')
    return _row(label("Tutka") + f'<p class="ink-2" style="margin:8px 0 0;font-size:14px;line-height:1.5;color:#52514e;">{intro}</p>'
                + "".join(cards), 28)


def render_minuutti(mi: dict) -> str:
    if not mi or not mi.get("teksti"):
        return ""
    return _row('<table role="presentation" class="bg-soft" width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="background:#f4f3ee;border-radius:10px;"><tr><td style="padding:16px 18px;">'
                + label("Sijoittajan minuutti")
                + f'<div class="ink-1" style="font-size:16px;font-weight:700;color:#0b0b0b;padding-top:6px;">{rich(mi.get("aihe", ""))}</div>'
                + f'<div class="ink-1" style="font-size:15px;line-height:1.6;color:#0b0b0b;padding-top:4px;">{rich(mi["teksti"])}</div>'
                + '</td></tr></table>', 30)


def render_syvasukellus(sd: dict | None, run_date: dt.date, web: bool) -> str:
    """Laajempi verkko-osio. Sähköpostissa (web=False) näytetään vain otsikko ja houkutin, jossa
    linkki verkkoversioon – ellei SITE_URL ole asetettu, jolloin osio jätetään kokonaan pois
    sähköpostista (turha lupaus linkistä joka ei toimi). Verkkoversiolla (web=True) koko teksti
    näkyy sellaisenaan."""
    if not sd or not sd.get("teksti"):
        return ""
    if web:
        return _row('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                    'style="border:1px solid #e1e0d9;border-radius:10px;"><tr><td style="padding:18px 20px;">'
                    + label("Syväsukellus")
                    + f'<div class="ink-1" style="font-size:18px;font-weight:700;color:#0b0b0b;padding-top:6px;">{rich(sd.get("otsikko", ""))}</div>'
                    + f'<div class="ink-1" style="font-size:15px;line-height:1.65;color:#0b0b0b;padding-top:8px;">{rich(sd["teksti"])}</div>'
                    + '</td></tr></table>', 30)
    if not SITE_URL:
        return ""
    url = f'{SITE_URL}/newsletter/arkisto/{run_date.isoformat()}.html'
    return _row('<table role="presentation" class="bg-soft" width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="background:#f4f3ee;border-radius:10px;"><tr><td style="padding:16px 18px;">'
                + label("Syväsukellus (verkossa)")
                + f'<div class="ink-1" style="font-size:16px;font-weight:700;color:#0b0b0b;padding-top:6px;">{rich(sd.get("otsikko", ""))}</div>'
                + '<div class="ink-2" style="font-size:14px;line-height:1.5;color:#52514e;padding-top:6px;">'
                'Tämä ei mahtunut kirjeeseen – koko juttu on luettavissa verkkoversiossa.</div>'
                f'<div style="padding-top:10px;"><a href="{html.escape(url, quote=True)}" '
                'style="color:#256abf;font-size:14px;font-weight:700;text-decoration:none;">Lue kokonaan&nbsp;&#8599;</a></div>'
                + '</td></tr></table>', 30)


def teaser(tv: dict | None) -> str:
    parts = []
    if tv and tv.get("scorecard"):
        rows = tv["scorecard"]["rivit"]
        parts.append(f"tuloskortti ({sum(e['osuma'] for e in rows)}/{len(rows)} osui)")
    if tv and tv.get("picks"):
        kinds = {p["tyyppi"] for p in tv["picks"]}
        nimet = [n for k, n in (("nousu", "nousuehdokkaat"), ("sektori", "sektorinäkymä"), ("varoitus", "kuplavaroitus"))
                 if k in kinds]
        lista = " ja ".join(nimet) if len(nimet) < 3 else f"{', '.join(nimet[:-1])} ja {nimet[-1]}"
        parts.append(f"tutkan uudet {lista}")
    parts += ["viikon nousijat", "kuplalukema lopussa"]
    return ("Tässä numerossa: " + " &middot; ".join(parts) + ".") if parts else ""


def render_html(facts: dict, texts: dict, wk: dict, run_date: dt.date, ai_used: bool,
                tv: dict | None = None, web: bool = False) -> str:
    score = facts["kuplalukema"]
    level = facts["taso"]
    lv = LEVEL.get(level, LEVEL["none"])
    P = []
    a = P.append
    a('<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">'
      '<meta name="viewport" content="width=device-width, initial-scale=1">'
      '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
      f'<title>Kuplamittari – viikko {wk["viikko"]}/{wk["vuosi"]}</title>{STYLE}</head>')
    a('<body class="bg-page" style="margin:0;padding:0;background:#f9f9f7;">')
    a('<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;">'
      f'{html.escape(texts["esikatselu"])}' + "&#8199;" * 40 + '</div>')
    a('<table role="presentation" class="bg-page" width="100%" cellpadding="0" cellspacing="0" border="0" '
      'style="background:#f9f9f7;"><tr><td align="center" style="padding:24px 12px;">')
    a('<table role="presentation" class="bg-card hair" width="100%" cellpadding="0" cellspacing="0" border="0" '
      f'style="max-width:600px;background:#fcfcfb;border:1px solid #e1e0d9;border-radius:14px;font-family:{FONT};">')

    # Otsake
    a('<tr><td class="px" style="padding:28px 32px 0;"><table role="presentation" width="100%" cellpadding="0" '
      'cellspacing="0" border="0"><tr><td class="ink-1" style="font-size:15px;font-weight:800;letter-spacing:0.14em;'
      'color:#0b0b0b;"><span style="color:#ec835a;">&#9679;</span>&nbsp;KUPLAMITTARI</td>'
      f'<td class="ink-3" align="right" style="font-size:13px;color:#898781;">Viikko {wk["viikko"]} &middot; '
      f'{WEEKDAYS[run_date.weekday()]} {fi_date(run_date)}</td></tr></table>'
      f'<div class="ink-3" style="font-size:13px;color:#898781;padding-top:4px;">Viikkokatsaus pörssin kuplariskiin '
      f'&middot; Numero {facts["numero"]}</div></td></tr>')
    if SITE_URL:
        if web:
            nav = f'<a href="{html.escape(SITE_URL, quote=True)}/newsletter/arkisto/index.html" style="color:#256abf;text-decoration:none;">&larr;&nbsp;Kaikki numerot</a>'
        else:
            issue_url = f'{SITE_URL}/newsletter/arkisto/{run_date.isoformat()}.html'
            nav = (f'<a href="{html.escape(issue_url, quote=True)}" style="color:#256abf;text-decoration:none;">Avaa selaimessa&nbsp;&#8599;</a>'
                   f'&nbsp;&middot;&nbsp;<a href="{html.escape(SITE_URL, quote=True)}/newsletter/arkisto/index.html" '
                   'style="color:#256abf;text-decoration:none;">Kaikki numerot&nbsp;&#8599;</a>')
        a(f'<tr><td class="px ink-3" style="padding:8px 32px 0;font-size:12px;color:#898781;">{nav}</td></tr>')
    a('<tr><td class="px" style="padding:22px 32px 0;">'
      f'<h1 class="ink-1" style="margin:0;font-size:28px;line-height:1.2;font-weight:700;color:#0b0b0b;">{rich(texts["otsikko"])}</h1>'
      f'<p class="ink-2" style="margin:12px 0 0;font-size:17px;line-height:1.55;color:#52514e;">{rich(texts["ingressi"])}</p>'
      '</td></tr>')

    tz = teaser(tv)
    if tz:
        a(f'<tr><td class="px ink-3" style="padding:10px 32px 0;font-size:13px;line-height:1.5;color:#898781;">{tz}</td></tr>')
    a(render_luku(texts.get("viikon_luku") or {}))

    # Viikko lyhyesti
    if texts["viikko"]:
        a(f'<tr><td class="px" style="padding:30px 32px 0;">{label("Viikko lyhyesti")}'
          '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;">')
        for i, it in enumerate(texts["viikko"]):
            border = "" if i == 0 else "border-top:1px solid #e1e0d9;"
            head = f"<strong>{rich(it['otsikko'])}</strong> " if it["otsikko"] else ""
            a(f'<tr><td class="ink-1 hair" style="padding:8px 0;font-size:15px;line-height:1.55;color:#0b0b0b;{border}">'
              f'{head}{rich(it["teksti"])}</td></tr>')
        a('</table></td></tr>')

    # Markkinat numeroina
    if facts["_markets"]:
        a(f'<tr><td class="px" style="padding:28px 32px 0;">{label("Markkinat numeroina")}'
          '<table role="presentation" class="num" width="100%" cellpadding="0" cellspacing="0" border="0" '
          'style="margin-top:10px;font-size:14px;font-variant-numeric:tabular-nums;">'
          '<tr class="ink-3" style="color:#898781;font-size:12px;"><td style="padding:0 0 6px;"></td>'
          f'<td align="right" style="padding:0 0 6px;">Taso pe {wk["loppu"].day}.{wk["loppu"].month}.</td>'
          '<td align="right" style="padding:0 0 6px;width:96px;">Viikko</td></tr>')
        rows = facts["_markets"]
        for i, r in enumerate(rows):
            bb = "border-bottom:1px solid #e1e0d9;" if i == len(rows) - 1 else ""
            cell = f"padding:8px 0;border-top:1px solid #e1e0d9;{bb}"
            ch, direction = fmt_change(r)
            color = {"up": ("up", "#006300"), "down": ("down", "#d03b3b")}.get(direction, ("ink-2", "#52514e"))
            a(f'<tr><td class="ink-1 hair" style="{cell}color:#0b0b0b;">{html.escape(r["nimi"])}</td>'
              f'<td class="ink-1 hair" align="right" style="{cell}color:#0b0b0b;">{fmt_level(r)}</td>'
              f'<td class="{color[0]} hair" align="right" style="{cell}color:{color[1]};white-space:nowrap;">{ch}</td></tr>')
        a('</table><div class="ink-3" style="font-size:12px;color:#898781;padding-top:6px;">'
          'Viikkomuutos edellisen perjantain päätöskurssista. Korot korkopisteinä (bp).</div></td></tr>')

    if tv:
        a(render_sectors(tv.get("sectors") or []))
        a(render_movers(tv.get("movers") or {}, texts.get("liikkujat") or {}))
        a(render_scorecard(tv))
        a(render_tutka(tv, wk))

    # Mitä Kuplamittari näkee
    a(f'<tr><td class="px" style="padding:30px 32px 0;">{label("Mitä Kuplamittari näkee")}'
      '<p class="ink-2" style="margin:8px 0 14px;font-size:14px;line-height:1.5;color:#52514e;">'
      'Jokainen lukema on suhteutettu omaan historiaansa: 0 = rauhallisin, 100 = kuplamaisin koskaan mitattu.</p>')
    theme_keys = [k for k in ("arvostus", "velkavipu", "raha") if k in facts["teemat"]]
    for i, k in enumerate(theme_keys):
        t = facts["teemat"][k]
        tl = LEVEL.get(t.get("taso", "none"), LEVEL["none"])
        sc = t.get("pisteet")
        sc_txt = "–" if sc is None else f"{disp(sc)}/100"
        mb = "margin-bottom:12px;" if i < len(theme_keys) - 1 else ""
        a('<table role="presentation" class="hair" width="100%" cellpadding="0" cellspacing="0" border="0" '
          f'style="border:1px solid #e1e0d9;border-radius:10px;{mb}">'
          '<tr><td style="padding:16px 18px 8px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
          f'border="0"><tr><td class="ink-1" style="font-size:16px;font-weight:700;color:#0b0b0b;">{html.escape(t["nimi"])}</td>'
          '<td class="ink-2" align="right" style="font-size:13px;color:#52514e;white-space:nowrap;">'
          f'<span style="color:{tl["fill"]};">&#9679;</span>&nbsp;<strong class="ink-1" style="color:#0b0b0b;">'
          f'{html.escape(t.get("tila", ""))}</strong>&nbsp;&middot;&nbsp;{sc_txt}</td></tr></table></td></tr>'
          f'<tr><td style="padding:0 18px 12px;">{meter(sc, t.get("taso", "none"))}</td></tr>'
          '<tr><td class="ink-1" style="padding:0 18px 16px;font-size:15px;line-height:1.55;color:#0b0b0b;">'
          f'{rich(texts["teemat"].get(k, ""))}</td></tr></table>')
    a('</td></tr>')

    # Historia
    if texts["historia"]:
        a(f'<tr><td class="px" style="padding:30px 32px 0;">{label("Toistaako historia itseään?")}')
        for i, ptxt in enumerate(texts["historia"]):
            a(f'<p class="ink-1" style="margin:{10 if i == 0 else 12}px 0 0;font-size:15px;line-height:1.6;color:#0b0b0b;">'
              f'{rich(ptxt)}</p>')
        a('</td></tr>')

    # Ensi viikolla
    if texts["ensi_viikko"]:
        a(f'<tr><td class="px" style="padding:30px 32px 0;">{label("Ensi viikolla seurataan")}'
          '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
          'style="margin-top:10px;font-size:15px;line-height:1.5;">')
        for it in texts["ensi_viikko"]:
            a('<tr><td class="ink-1" valign="top" style="padding:6px 12px 6px 0;width:70px;font-weight:700;color:#0b0b0b;'
              f'white-space:nowrap;">{html.escape(it["paiva"])}</td>'
              f'<td class="ink-1" style="padding:6px 0;color:#0b0b0b;">{rich(it["teksti"])}</td></tr>')
        a('</table></td></tr>')

    a(render_minuutti(texts.get("minuutti") or {}))
    a(render_syvasukellus(texts.get("syvasukellus"), run_date, web))
    a(render_feargreed(facts.get("_feargreed")))

    # Viikon kuplalukema (lopussa, kuten toivottu)
    zi = zone_of(score)
    change_html = ""
    if facts.get("muutos_viime_viikosta") is not None:
        c = facts["muutos_viime_viikosta"]
        arrow = "&#9650;" if c > 0 else ("&#9660;" if c < 0 else "&#9679;")
        note = " (osin mittarin kattavuuden muutoksesta)" if facts.get("muutos_johtuu_osin_mittarin_kattavuudesta") else ""
        change_html = (f'<div class="ink-2" style="padding-top:10px;font-size:14px;color:#52514e;">{arrow}&nbsp;'
                       f'{fi_signed(c, 1)} viime viikosta{note}</div>')
    a('<tr><td class="px" style="padding:34px 32px 0;"><table role="presentation" class="hair" width="100%" '
      'cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #e1e0d9;">'
      f'<tr><td align="center" style="padding:28px 0 0;">{label("Viikon kuplalukema")}'
      '<div class="ink-1" style="padding-top:6px;color:#0b0b0b;line-height:1;">'
      f'<span class="hero" style="font-size:72px;font-weight:700;letter-spacing:-0.02em;">{disp(score)}</span>'
      '<span class="ink-2" style="font-size:24px;font-weight:600;color:#52514e;">&nbsp;/&nbsp;100</span></div>'
      f'<div style="padding-top:14px;"><span class="pill-{lv["cls"]}" style="display:inline-block;background:{lv["track"]};'
      'color:#0b0b0b;font-size:14px;font-weight:700;padding:6px 14px;border-radius:999px;">'
      f'&#9888;&#65038;&nbsp;{html.escape(facts["vyohyke"])}</span></div>{change_html}</td></tr>'
      f'<tr><td style="padding:22px 0 0;">{meter(score, level, 12)}'
      '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
      'style="margin-top:6px;font-size:11px;line-height:1.3;"><tr>')
    for i, (lo, hi, name) in enumerate(ZONES):
        rng = f"{lo}–{hi}" if hi < 100 else f"{lo}+"
        cur = i == zi
        a(f'<td class="{"ink-1" if cur else "ink-3"} hair" width="{hi - lo}%" style="color:{"#0b0b0b" if cur else "#898781"};'
          f'{"font-weight:700;" if cur else ""}border-left:1px solid #c3c2b7;padding:3px 0 0 5px;">{rng}<br>{name}</td>')
    a('</tr></table></td></tr>')
    a('<tr><td class="ink-1" align="center" style="padding:22px 8px 0;font-size:16px;line-height:1.55;color:#0b0b0b;">'
      f'{rich(texts["loppusanat"])}</td></tr>')
    a('<tr><td class="ink-3" align="center" style="padding:12px 8px 0;font-size:13px;line-height:1.5;color:#898781;">'
      f'Mittari kattaa nyt: {facts["mittari_kattaa_nyt"]}. Tulevissa numeroissa mukaan: {facts["mittariin_tulossa"]}.'
      '</td></tr></table></td></tr>')

    # Lähteet ja alatunniste
    foot = []
    if texts["lahteet"]:
        links = " &middot; ".join(f'<a href="{html.escape(s["url"], quote=True)}" style="color:#256abf;text-decoration:none;">'
                                  f'{html.escape(s["otsikko"])}</a>' for s in texts["lahteet"])
        foot.append(f'<div style="padding-bottom:10px;">Viikon lähteet: {links}</div>')
    if tv and tv.get("picks"):
        foot.append('<div style="padding-bottom:10px;">Tutka on Kuplamittarin algoritmin ja tekoälyn laatima seurantalista, '
                    'ei sijoitusneuvo eikä osto- tai myyntikehotus. Ennusteet tallennetaan muuttumattomina ennen viikon alkua, '
                    f'ja kaikkien tulokset raportoidaan. {html.escape(TUTKA_ILMOITUS)}</div>')
    author = ("Tekstin kirjoitti tekoäly (Claude) mittarin datan ja viikon uutisten pohjalta."
              if ai_used else "Tämä numero on koottu automaattipohjalla ilman tekoälykirjoittajaa.")
    foot.append(f'Kuplamittari on tietopaketti, ei sijoitusneuvontaa. Historia ei takaa tulevaa. {author} '
                'Mittarin data: Robert Shiller / Yale, FRED, FINRA, Yahoo Finance.')
    a('<tr><td class="px" style="padding:30px 32px 28px;"><div class="ink-3 hair" style="border-top:1px solid #e1e0d9;'
      f'padding-top:14px;font-size:12px;line-height:1.5;color:#898781;">{"".join(foot)}</div></td></tr>')
    a('</table></td></tr></table></body></html>')
    return "\n".join(P)


def render_text(facts: dict, texts: dict, wk: dict, run_date: dt.date, tv: dict | None = None) -> str:
    strip = lambda s: re.sub(r"\*\*(.+?)\*\*", r"\1", s or "")
    pct = lambda x: f"{fi_signed(x, 1)} %"
    L = [f"KUPLAMITTARI – viikko {wk['viikko']}, {fi_date(run_date)} (numero {facts['numero']})", "",
         strip(texts["otsikko"]).upper(), strip(texts["ingressi"]), ""]
    vl = texts.get("viikon_luku") or {}
    if vl.get("luku"):
        L += [f"VIIKON LUKU: {strip(vl['luku'])} – {strip(vl.get('otsikko', ''))}", strip(vl.get("teksti", "")), ""]
    if texts["viikko"]:
        L.append("VIIKKO LYHYESTI")
        L += [f"- {strip(i['otsikko'])} {strip(i['teksti'])}".rstrip() for i in texts["viikko"]]
        L.append("")
    if facts["_markets"]:
        L.append("MARKKINAT NUMEROINA")
        L += [f"- {r['nimi']}: {fmt_level(r).replace(NBSP, ' ')} {fmt_change_plain(r)}".rstrip() for r in facts["_markets"]]
        L.append("")
    if tv:
        if tv.get("sectors"):
            L.append("SEKTORIT VIIKOLLA: " + ", ".join(f"{x['nimi']} {pct(x['viikko_pct'])}" for x in tv["sectors"]))
            L.append("")
        mv = tv.get("movers") or {}
        if mv.get("nousijat"):
            L.append("VIIKON NOUSIJAT JA LASKIJAT")
            for m in mv["nousijat"][:3] + mv.get("laskijat", [])[:3]:
                why = (texts.get("liikkujat") or {}).get(m["tunnus"], "")
                L.append(f"- {m['nimi']} ({m['tunnus']}) {pct(m['viikko_pct'])}" + (f": {strip(why)}" if why else ""))
            L.append("")
        sc = tv.get("scorecard")
        if sc:
            L.append("TULOSKORTTI")
            for e in sc["rivit"]:
                kind_txt = "Nousu" if e["tyyppi"] == "nousu" else "Sektori" if e["tyyppi"] == "sektori" else "Varoitus"
                L.append(f"- {kind_txt} {e['nimi']} ({e['tunnus']}): "
                         f"{pct(e['tulos_pct'])}, vs. vertailu {fi_signed(e['ylituotto_pct'], 1)} %-yks. "
                         f"{'OSUI' if e['osuma'] else 'OHI'}")
            L.append(f"Kertymä: tekoäly {_stat_line(sc.get('kertyma_ai'))}, momentum-sääntö {_stat_line(sc.get('kertyma_saanto'))}"
                     .replace(NBSP, " "))
            if tv.get("itsearvio"):
                L.append(strip(tv["itsearvio"]))
            if tv.get("oppi"):
                L.append("Opittu: " + strip(tv["oppi"]))
            L.append("")
        if tv.get("picks"):
            L.append(f"TUTKA – ensi viikko {fi_range(wk['ensi_alku'], wk['ensi_loppu'])}")
            pick_order = ([x for x in tv["picks"] if x["tyyppi"] == "nousu"]
                          + [x for x in tv["picks"] if x["tyyppi"] == "sektori"]
                          + [x for x in tv["picks"] if x["tyyppi"] == "varoitus"])
            for p in pick_order:
                tag_txt = ("NOUSUEHDOKAS" if p["tyyppi"] == "nousu" else
                           "SEKTORINÄKYMÄ" if p["tyyppi"] == "sektori" else "KUPLAVAROITUS")
                L.append(f"- {tag_txt}: {p['nimi']} ({p['tunnus']})")
                for k, v in (("Miksi", p.get("perustelu")), ("Laukaisija", p.get("laukaisija")), ("Riski", p.get("riski"))):
                    if v:
                        L.append(f"  {k}: {strip(v)}")
            L.append("")
    L.append("MITÄ KUPLAMITTARI NÄKEE")
    for k in ("arvostus", "velkavipu", "raha"):
        t = facts["teemat"].get(k)
        if t:
            sc_ = "–" if t.get("pisteet") is None else f"{disp(t['pisteet'])}/100"
            L.append(f"{t['nimi']} – {t.get('tila', '')} ({sc_}): {strip(texts['teemat'].get(k, ''))}")
    L.append("")
    if texts["historia"]:
        L.append("TOISTAAKO HISTORIA ITSEÄÄN?")
        L += [strip(p) for p in texts["historia"]]
        L.append("")
    if texts["ensi_viikko"]:
        L.append("ENSI VIIKOLLA SEURATAAN")
        L += [f"- {i['paiva']}: {strip(i['teksti'])}" for i in texts["ensi_viikko"]]
        L.append("")
    mi = texts.get("minuutti") or {}
    if mi.get("teksti"):
        L += [f"SIJOITTAJAN MINUUTTI: {strip(mi.get('aihe', ''))}", strip(mi["teksti"]), ""]
    sd = texts.get("syvasukellus") or {}
    if sd.get("teksti") and SITE_URL:
        L += [f"SYVÄSUKELLUS (verkossa): {strip(sd.get('otsikko', ''))}",
              f"Koko juttu: {SITE_URL}/newsletter/arkisto/{run_date.isoformat()}.html", ""]
    fg = facts.get("_feargreed")
    if fg:
        L += [f"PELKO VAI AHNEUS: {disp(fg['pisteet'])}/100 – {fg['vyohyke']}",
              "(Lyhyen aikavälin markkinatunnelma, eri asia kuin kuplalukema.)", ""]
    L += [f"VIIKON KUPLALUKEMA: {disp(facts['kuplalukema'])}/100 – {facts['vyohyke']}", strip(texts["loppusanat"]), ""]
    if texts["lahteet"]:
        L.append("Lähteet: " + " | ".join(f"{s['otsikko']} <{s['url']}>" for s in texts["lahteet"]))
    L.append("Kuplamittari on tietopaketti, ei sijoitusneuvontaa. Tutka on seurantalista, ei osto- tai myyntikehotus.")
    return "\n".join(L).replace(NBSP, " ")


# ----------------------------------------------------------------------------
# Arkiston hakemistosivu (GitHub Pages -verkkoversio)
# ----------------------------------------------------------------------------
def render_archive_index(hist: list[dict]) -> str:
    """Kaikkien aiempien numeroiden listaus verkkosivuksi. Julkaistaan newsletter/arkisto/index.html,
    joka näkyy selaimessa kun GitHub Pages on päällä (ks. UUTISKIRJE.md). Uusin numero ensin."""
    rows = sorted(hist, key=lambda h: h.get("pvm", ""), reverse=True)
    items = []
    for h in rows:
        try:
            pvm = dt.date.fromisoformat(h["pvm"])
            pvm_txt = fi_date(pvm)
        except Exception:  # noqa: BLE001
            pvm_txt = h.get("pvm", "")
        score = h.get("kuplalukema")
        pill = LEVEL.get(h.get("taso") or "none", LEVEL["none"])
        score_txt = f"{disp(score)}/100" if score is not None else "–"
        items.append(
            '<tr><td style="padding:14px 0;border-top:1px solid #e1e0d9;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td style="font-size:13px;color:#898781;width:90px;">{pvm_txt}<br>Nro {h.get("numero", "?")}</td>'
            '<td style="font-size:15px;color:#0b0b0b;padding:0 12px;">'
            f'<a href="{html.escape(h.get("pvm", ""), quote=True)}.html" style="color:#0b0b0b;text-decoration:none;font-weight:600;">'
            f'{html.escape(h.get("otsikko") or "(otsikko puuttuu)")}</a></td>'
            f'<td align="right" style="white-space:nowrap;"><span style="display:inline-block;background:{pill["track"]};'
            f'color:#0b0b0b;font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;">{score_txt}'
            f'{" &middot; " + html.escape(h["vyohyke"]) if h.get("vyohyke") else ""}</span></td>'
            '</tr></table></td></tr>')
    body = "".join(items) if items else '<tr><td style="padding:20px 0;color:#898781;">Ei vielä numeroita.</td></tr>'
    return (
        '<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="color-scheme" content="light dark"><title>Kuplamittari – arkisto</title>{STYLE}</head>'
        '<body class="bg-page" style="margin:0;padding:0;background:#f9f9f7;">'
        '<table role="presentation" class="bg-page" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f9f9f7;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" class="bg-card hair" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:640px;background:#fcfcfb;border:1px solid #e1e0d9;border-radius:14px;font-family:{FONT};">'
        '<tr><td class="px" style="padding:28px 32px 8px;">'
        '<div class="ink-1" style="font-size:15px;font-weight:800;letter-spacing:0.14em;color:#0b0b0b;">'
        '<span style="color:#ec835a;">&#9679;</span>&nbsp;KUPLAMITTARI</div>'
        '<h1 class="ink-1" style="margin:10px 0 0;font-size:24px;color:#0b0b0b;">Kaikki numerot</h1>'
        '<p class="ink-2" style="margin:8px 0 0;font-size:14px;color:#52514e;">'
        f'{len(rows)} numero{"a" if len(rows) != 1 else ""}. Uusin ensin.</p></td></tr>'
        f'<tr><td class="px" style="padding:10px 32px 24px;"><table role="presentation" width="100%" '
        f'cellpadding="0" cellspacing="0" border="0">{body}</table></td></tr>'
        '</table></td></tr></table></body></html>')


# ----------------------------------------------------------------------------
# Sähköposti
# ----------------------------------------------------------------------------
def recipients() -> list[str]:
    raw = os.environ.get("EMAIL_TO", "") or os.environ.get("GMAIL_USER", "")
    return [r.strip() for r in re.split(r"[,;\s]+", raw) if r.strip()]


def build_message(subject: str, text: str, html_body: str | None) -> EmailMessage:
    user = os.environ.get("GMAIL_USER", "kuplamittari@example.com")
    to = recipients() or [user]
    msg = EmailMessage()
    msg["From"] = formataddr(("Kuplamittari", user))
    msg["Subject"] = subject
    if len(to) == 1:
        msg["To"] = to[0]
    else:                                   # jakelulista: vastaanottajat eivät näe toisiaan
        msg["To"] = formataddr(("Kuplamittarin lukijat", user))
        msg["Bcc"] = ", ".join(to)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
        if os.environ.get("LIITA_RAPORTTI", "0") == "1" and os.path.exists(REPORT_TXT):
            with open(REPORT_TXT, "rb") as f:
                msg.add_attachment(f.read(), maintype="text", subtype="plain",
                                   filename="kuplamittari_tekninen_raportti.txt")
    return msg


def send(msg: EmailMessage) -> None:
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        print("Sähköpostin ympäristömuuttujat puuttuvat — kirjettä ei lähetetty.", flush=True)
        return
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    print("Uutiskirje lähetetty:", ", ".join(recipients()), flush=True)


# ----------------------------------------------------------------------------
def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--mock-ai")
    ap.add_argument("--mock-ai2", help="tutkan AI-vastaus tiedostosta (testaus)")
    ap.add_argument("--mock-markets")
    ap.add_argument("--mock-feargreed", help="pelko/ahneus-mittarin JSON tiedostosta (testaus ilman verkkoa)")
    ap.add_argument("--mock-prices", help="osakeuniversumin hinnat CSV:stä (testaus ilman verkkoa)")
    ap.add_argument("--save-state", action="store_true", help="tallenna ennusteet ja historia myös ilman lähetystä")
    ap.add_argument("--date")
    args = ap.parse_args()

    run_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    wk = week_info(run_date)
    week_key = f"{wk['vuosi']}-{wk['viikko']:02d}"
    print("=" * 64)
    print(f"KUPLAMITTARI-UUTISKIRJE — viikko {wk['viikko']} ({fi_range(wk['alku'], wk['loppu'])}), "
          f"AI: {'mock' if args.mock_ai else ('pois' if args.no_ai else (MODEL if API_KEY else 'EI AVAINTA'))}")
    print("=" * 64)

    try:
        with open(LATEST_JSON, encoding="utf-8") as f:
            latest = json.load(f)
        created = dt.datetime.fromisoformat(latest["luotu_utc"]).date()
        if abs((run_date - created).days) > 2:
            raise RuntimeError(f"faktapaketti on vanhentunut ({created}); mittarin laskenta (kupla.py) "
                               "epäonnistui tällä viikolla, joten vanhoja lukuja ei lähetetä uutena")
        if args.mock_markets:
            with open(args.mock_markets, encoding="utf-8") as f:
                markets = json.load(f)
        else:
            markets = fetch_markets(wk)
        if args.mock_feargreed:
            with open(args.mock_feargreed, encoding="utf-8") as f:
                fg = json.load(f)
        else:
            try:
                fg = fear_greed(wk)
            except Exception as e:  # noqa: BLE001
                warn(f"pelko/ahneus-mittari epäonnistui, osio jätetään pois: {str(e)[:200]}")
                fg = None
        hist = load_history()
        number, prev = issue_info(hist, wk)
        facts = build_factpack(latest, markets, wk, number, prev)

        # Tutkan data: liikkujat, sektorit, ehdokkaat ja viime viikon ennusteiden pisteytys
        tk = None
        try:
            tk = build_tutka(wk, run_date, args.mock_prices)
            facts["liikkujat"] = {k: [{x: m[x] for x in ("tunnus", "nimi", "sektori", "viikko_pct")} for m in v]
                                  for k, v in tk["movers"].items()}
            facts["sektorit"] = tk["sectors"]
        except Exception as e:  # noqa: BLE001
            warn(f"tutkan data epäonnistui, tutka-osiot jätetään pois: {str(e)[:200]}")
        prev_topics = [h["minuutti"] for h in hist if h.get("minuutti")]
        facts["aiemmat_minuutit"] = prev_topics[-12:]

        # Kutsu 1: kirjeen tekstit
        ai_obj, usage = None, {}
        if args.mock_ai:
            with open(args.mock_ai, encoding="utf-8") as f:
                ai_obj = extract_json(f.read())
            usage = {"malli": "mock"}
        elif not args.no_ai and API_KEY:
            try:
                ai_obj, usage = ai_write(facts)
            except Exception as e:  # noqa: BLE001
                warn(f"AI-kirjoittaja ei onnistunut, käytetään ilmaista pohjaa: {str(e)[:200]}")
        elif not args.no_ai:
            warn("ANTHROPIC_API_KEY puuttuu — käytetään ilmaista pohjaa")

        tmpl = template_write(facts)
        tmpl["viikon_luku"] = template_luku(facts)
        tmpl["minuutti"] = template_minuutti(number, prev_topics)
        tmpl["syvasukellus"] = template_syvasukellus(facts)
        texts, replaced = sanitize(ai_obj, tmpl, facts["kuplalukema"])
        if replaced:
            warn(f"AI-vastauksesta korvattiin pohjalla kentät: {', '.join(replaced)}")

        # Kutsu 2: tutka (itsearvio, oppi, uudet ennusteet) + jäädytys kirjanpitoon
        tv, usage2 = None, {}
        if tk:
            try:
                tv, usage2 = run_tutka(args, facts, tk, wk, number, week_key, run_date)
            except Exception as e:  # noqa: BLE001
                warn(f"tutka epäonnistui, osio jätetään pois: {str(e)[:200]}")
                traceback.print_exc()

        facts["_markets"] = markets
        facts["_feargreed"] = fg
        ai_used = ai_obj is not None
        page = render_html(facts, texts, wk, run_date, ai_used, tv, web=False)
        page_web = render_html(facts, texts, wk, run_date, ai_used, tv, web=True)
        text = render_text(facts, texts, wk, run_date, tv)
        size_kb = len(page.encode("utf-8")) / 1024
        print(f"Kirjeen koko {size_kb:.0f} kt (Gmailin katkaisuraja 102 kt)")
        if size_kb > 95:
            warn(f"kirje on {size_kb:.0f} kt – Gmail voi katkaista sen ja piilottaa lopun kuplalukeman")

        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        with open(NEWSLETTER_HTML, "w", encoding="utf-8") as f:
            f.write(page)
        with open(os.path.join(ARCHIVE_DIR, f"{run_date.isoformat()}.html"), "w", encoding="utf-8") as f:
            f.write(page_web)
        print(f"Uutiskirje tallennettu: {NEWSLETTER_HTML}")
        if not SITE_URL:
            print("HUOM: SITE_URL ei ole asetettu, joten kirjeeseen ei tullut linkkiä verkkoversioon "
                  "(ks. UUTISKIRJE.md, kohta 'Verkkosivu').")

        subject = f"Kuplamittari vko {wk['viikko']}: {re.sub(r'[*]', '', texts['otsikko'])}"
        if prev and facts["kuplalukema"] >= ALERT_THRESHOLD > prev["kuplalukema"]:
            subject = "⚠ " + subject
        sent = False
        if not args.no_email:
            send(build_message(subject, text, page))
            sent = True
        if sent or args.save_state:
            if tk:
                tutka.save_ledger(tk["led"])
            cost = round(sum(u.get("kustannus_usd") or 0 for u in (usage, usage2)), 3)
            hist = [h for h in hist if h.get("viikko_id") != week_key] + [{
                "viikko_id": week_key, "numero": number, "pvm": run_date.isoformat(),
                "kuplalukema": facts["kuplalukema"], "vyohyke": facts["vyohyke"], "taso": facts["taso"],
                "otsikko": strip_md(texts["otsikko"]), "kattavuus_pct": latest.get("kattavuus_pct"),
                "ai": usage.get("malli") if ai_used else None, "kustannus_usd": cost or None,
                "minuutti": (texts.get("minuutti") or {}).get("aihe"),
            }]
            save_history(hist)
            with open(os.path.join(ARCHIVE_DIR, "index.html"), "w", encoding="utf-8") as f:
                f.write(render_archive_index(hist))
        if WARNINGS:
            print("\nVAROITUKSET:\n  - " + "\n  - ".join(WARNINGS))
        return 0

    except Exception:  # noqa: BLE001
        # Viimeinen turvaverkko: lähetetään mittarin tekstiraportti, jotta viikko ei jää väliin.
        print("UUTISKIRJEEN KOOSTAMINEN EPÄONNISTUI:", flush=True)
        traceback.print_exc()
        if not args.no_email:
            fresh = False
            try:
                with open(LATEST_JSON, encoding="utf-8") as f:
                    created = dt.datetime.fromisoformat(json.load(f)["luotu_utc"]).date()
                fresh = abs((run_date - created).days) <= 2 and os.path.exists(REPORT_TXT)
            except Exception:  # noqa: BLE001
                pass
            if fresh:
                with open(REPORT_TXT, encoding="utf-8") as f:
                    body = ("Uutiskirjeen koostaminen epäonnistui tällä viikolla, joten tässä mittarin tekstiraportti.\n"
                            "Virheen tiedot löytyvät GitHub Actionsin lokista.\n\n" + f.read())
            else:
                body = ("Kuplamittarin laskenta epäonnistui tällä viikolla, eikä uutiskirjettä voitu koota.\n"
                        "Katso virheen tiedot GitHub Actionsin lokista (Actions → viimeisin ajo).")
            try:
                send(build_message(f"Kuplamittari vko {wk['viikko']} (varaversio)", body, None))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
