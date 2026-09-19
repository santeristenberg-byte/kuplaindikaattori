#!/usr/bin/env python3
"""
Kuplaindikaattori v0.1 — vaihe 1
Pilarit A (arvostus) ja B (velkavipu & likviditeetti), kuplariski 0–100,
ääriarvolaskuri, analogiapiste historiallisiin huippuihin, sähköpostiraportti.

Kaikki datalähteet ovat ilmaisia ja julkisia:
  - Shiller (Yale)   : CAPE, S&P 500 hinta, tulokset, CPI
  - FRED (St. Louis) : BKT, korot, M2, keskuspankkitaseet, HY-spread, Z.1-tase
  - FINRA            : margin debt
  - Yahoo Finance    : S&P 500, Wilshire 5000 (viikkotuoreus)

Ajo:  python kupla.py            (oikea data + sähköposti, jos ympäristömuuttujat asetettu)
      python kupla.py --demo     (synteettinen data, testaa vain putken toimivuuden)
      python kupla.py --no-email (oikea data, ei sähköpostia)
"""
import argparse
import datetime as dt
import io
import json
import os
import smtplib
import sys
import time
import traceback
from email.message import EmailMessage

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Asetukset
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "outputs")
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")
MARGIN_CACHE = os.path.join(DATA_DIR, "margin_debt_cache.csv")
CHART_PNG = os.path.join(OUT_DIR, "kuplariski.png")
REPORT_TXT = os.path.join(OUT_DIR, "raportti.txt")

MIN_HISTORY_MONTHS = 120      # muuttuja mukaan vasta 10 v historian jälkeen
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "75"))

# Vaiheen 1 painot (suunnitelman 30/20 uudelleennormalisoituna, kun C, D, E puuttuvat)
PILLAR_WEIGHTS = {"A": 0.60, "B": 0.40}
PILLAR_NAMES = {"A": "Arvostus", "B": "Vipu & likviditeetti"}

# Historialliset huiput, joihin verrataan
PEAKS = {
    "1972-12 Nifty Fifty": "1972-12-31",
    "2000-03 Dotcom": "2000-03-31",
    "2007-10 Luottokupla": "2007-10-31",
    "2021-11 Pandemiakupla": "2021-11-30",
}

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
}
# FRED: useita reittejä samaan dataan; kokeillaan järjestyksessä
FRED_URLS = [
    # cosd pakottaa koko historian: ilman sitä päivittäiset sarjat (korot, spreadit, valuutat)
    # palautuvat vain kaavion oletusikkunalta (~3 v)
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}&cosd=1900-01-01&coed=2100-12-31",
    "https://fred.stlouisfed.org/series/{id}/downloaddata/{id}.csv",
    "https://fred.stlouisfed.org/data/{id}.txt",
]
FRED_API = "https://api.stlouisfed.org/fred/series/observations?series_id={id}&api_key={key}&file_type=json"
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()   # valinnainen, ilmainen: fred.stlouisfed.org/docs/api/api_key.html
FRED_CACHE_DIR = os.path.join(DATA_DIR, "fred_cache")

SHILLER_URLS = [
    "http://www.econ.yale.edu/~shiller/data/ie_data.xls",
    "https://shillerdata.com/",                         # sivu, jolta etsitään ie_data.xls-linkki
]
MULTPL_CAPE = "https://www.multpl.com/shiller-pe/table/by-month"
MULTPL_SPX = "https://www.multpl.com/s-p-500-historical-prices/table/by-month"
SHILLER_CACHE = os.path.join(DATA_DIR, "shiller_cache.csv")
SHILLER_DEBUG = os.path.join(OUT_DIR, "shiller_debug.txt")
FINRA_URL = "https://www.finra.org/investors/learn-to-invest/advanced-investing/margin-statistics"

HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "90"))
HTTP_RETRIES = 3

VERSION = "v0.9 (2026-09-19: faktapaketti uutiskirjeelle, B7 3 v)"

DEMO = False
WARNINGS: list[str] = []
FACTS: dict = {}   # uutiskirjeen faktapaketin raakatasot (täytetään build_variablesissa)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print("VAROITUS:", msg, file=sys.stderr)


# ----------------------------------------------------------------------------
# Apufunktiot
# ----------------------------------------------------------------------------
def to_monthly(s: pd.Series) -> pd.Series:
    s = pd.Series(pd.to_numeric(s, errors="coerce").values, index=pd.to_datetime(s.index)).dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.resample("ME").last()


def demo_series(start: str, level: float, vol: float, seed: int, drift: float = 0.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, dt.date.today(), freq="ME")
    steps = rng.normal(drift, vol, len(idx))
    return pd.Series(level * np.exp(np.cumsum(steps)), index=idx)


# ----------------------------------------------------------------------------
# Datalähteet
# ----------------------------------------------------------------------------
def http_get(url: str, timeout: int = HTTP_TIMEOUT, retries: int = HTTP_RETRIES) -> requests.Response:
    """GET uudelleenyrityksillä ja kasvavalla odotuksella. Viimeinen virhe nostetaan."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def _cache_path(name: str) -> str:
    os.makedirs(FRED_CACHE_DIR, exist_ok=True)
    return os.path.join(FRED_CACHE_DIR, f"{name}.csv")


def cache_save(name: str, s: pd.Series) -> None:
    try:
        s.rename("value").rename_axis("date").to_csv(_cache_path(name))
    except Exception:  # noqa: BLE001
        pass


def cache_load(name: str) -> pd.Series | None:
    p = _cache_path(name)
    if not os.path.exists(p):
        return None
    c = pd.read_csv(p, parse_dates=["date"], index_col="date")["value"]
    return to_monthly(c)


def _parse_fred_text(text: str) -> pd.Series:
    """Tulkitsee sekä CSV:n (DATE,VALUE / observation_date,ID) että .txt-muodon."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    rows = []
    for ln in lines:
        parts = ln.replace("\t", " ").split(",") if "," in ln else ln.split()
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if not (len(d) == 10 and d[4] == "-" and d[7] == "-"):
            continue
        rows.append((d, v))
    if not rows:
        raise ValueError("ei yhtään datariviä")
    s = pd.Series([r[1] for r in rows], index=[r[0] for r in rows])
    return to_monthly(s)


def fetch_fred(sid: str) -> pd.Series:
    if DEMO:
        return demo_series("1960-01-01", 100, 0.02, hash(sid) % 1000, 0.003)

    # Strategia: kokeillaan reittejä ja pidetään PISIN tulos. Jokin reitti voi palauttaa
    # päivittäisestä sarjasta vain lyhyen ikkunan; pisin voittaa, eikä lyhyt tulos
    # koskaan pääse lyhentämään välimuistia. Riittävän pitkä (>= LONG_ENOUGH) lopettaa haun.
    LONG_ENOUGH = 240  # kk
    errors: list[str] = []
    best: pd.Series | None = None
    best_route = ""

    def consider(s: pd.Series, route: str):
        nonlocal best, best_route
        n = int(s.notna().sum())
        if s.empty or n == 0:
            return
        if best is None or n > int(best.notna().sum()):
            best, best_route = s, route

    # 1) Virallinen API, jos avain on annettu (luotettavin; pyydetään koko historia eksplisiittisesti)
    if FRED_API_KEY:
        try:
            r = http_get(FRED_API.format(id=sid, key=FRED_API_KEY) + "&observation_start=1900-01-01&limit=100000")
            obs = r.json()["observations"]
            s = to_monthly(pd.Series([o["value"] for o in obs], index=[o["date"] for o in obs]))
            consider(s, "api")
        except Exception as e:  # noqa: BLE001
            errors.append(f"api: {str(e)[:100]}")
            warn(f"FRED {sid}: API-haku epäonnistui vaikka avain on asetettu — tarkista FRED_API_KEY ({str(e)[:80]})")

    # 2) Julkiset CSV/TXT-reitit, kunnes joku antaa riittävän pitkän
    if best is None or int(best.notna().sum()) < LONG_ENOUGH:
        for pattern in FRED_URLS:
            try:
                r = http_get(pattern.format(id=sid), retries=2)
                consider(_parse_fred_text(r.text), pattern.split("/")[3] if "/" in pattern else pattern)
                if best is not None and int(best.notna().sum()) >= LONG_ENOUGH:
                    break
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:120])

    # 3) Yhdistä välimuistiin: pidetään kaikki tunnettu historia, tuore voittaa päällekkäisillä
    cached = cache_load(sid)
    if best is None:
        if cached is not None and not cached.empty:
            warn(f"FRED {sid}: verkkohaku epäonnistui, käytetään välimuistia ({cached.index[-1].date()} asti)")
            return cached
        raise RuntimeError("; ".join(errors))

    merged = best
    if cached is not None and not cached.empty:
        merged = pd.concat([cached, best])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged = merged.resample("ME").last()
    cache_save(sid, merged)

    n = int(merged.notna().sum())
    if n < MIN_HISTORY_MONTHS:
        hint = "" if FRED_API_KEY else " — ilmainen FRED_API_KEY todennäköisesti korjaa tämän"
        warn(f"FRED {sid}: vain {n} kk historiaa (reitti: {best_route}){hint}")
    return merged


def fetch_yahoo(ticker: str) -> pd.Series:
    if DEMO:
        return demo_series("1970-01-01", 100, 0.045, hash(ticker) % 1000, 0.006)
    import yfinance as yf
    df = yf.download(ticker, period="max", interval="1d", auto_adjust=False, progress=False)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return to_monthly(close)


def fetch_shiller() -> pd.DataFrame:
    """Palauttaa kuukausitaulukon: P (nimellishinta), E (nimellistulos), CPI, CAPE."""
    if DEMO:
        p = demo_series("1871-01-01", 4, 0.04, 1, 0.004)
        cpi = demo_series("1871-01-01", 10, 0.005, 2, 0.002)
        e = p * 0.06
        real_e = e / cpi * cpi.iloc[-1]
        cape = (p / cpi * cpi.iloc[-1]) / real_e.rolling(120).mean()
        return pd.DataFrame({"P": p, "E": e, "CPI": cpi, "CAPE": cape})

    errors = []
    df = None
    for fn in (_shiller_from_xls, _shiller_from_multpl):
        try:
            df = fn()
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {str(e)[:160]}")
    if df is None:
        if os.path.exists(SHILLER_CACHE):
            warn("Shiller: kaikki verkkolähteet epäonnistuivat, käytetään välimuistia — " + " | ".join(errors))
            return pd.read_csv(SHILLER_CACHE, parse_dates=["date"], index_col="date")
        raise RuntimeError(" | ".join(errors))
    if errors:
        warn("Shiller: ensisijainen lähde epäonnistui, käytettiin varalähdettä — " + " | ".join(errors))
    os.makedirs(DATA_DIR, exist_ok=True)
    df.rename_axis("date").to_csv(SHILLER_CACHE)
    return df


def _shiller_dates(dates: pd.Series) -> pd.DatetimeIndex:
    """Shillerin päivämäärä on muotoa 2026.09 (=syyskuu) ja 2026.1 (=lokakuu)."""
    year = dates.astype(int)
    month = ((dates - year) * 100).round().astype(int).clip(1, 12)
    return pd.to_datetime(pd.DataFrame({"year": year, "month": month, "day": 1})) + pd.offsets.MonthEnd(0)


def _compute_cape(p: pd.Series, e: pd.Series, cpi: pd.Series) -> pd.Series:
    """CAPE = reaalihinta / 10 v reaalitulosten keskiarvo. Sama määritelmä kuin Shillerillä."""
    cpi_now = cpi.dropna().iloc[-1]
    real_p = p / cpi * cpi_now
    real_e = e / cpi * cpi_now
    return real_p / real_e.rolling(120, min_periods=120).mean()


def _shiller_from_xls() -> pd.DataFrame:
    content = None
    errs = []
    for url in SHILLER_URLS:
        try:
            r = http_get(url, retries=2)
            if url.endswith(".xls"):
                content = r.content
            else:
                # shillerdata.com: etsi sivulta xls-linkki
                import re
                m = re.search(r'href="([^"]*ie_data\.xls[^"]*)"', r.text)
                if not m:
                    raise RuntimeError("ie_data.xls-linkkiä ei löytynyt sivulta")
                href = m.group(1).replace("&amp;", "&")
                if href.startswith("/"):
                    href = "https://shillerdata.com" + href
                content = http_get(href, retries=2).content
            if content[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" and b"<html" in content[:2000].lower():
                raise RuntimeError("palvelin palautti HTML-sivun, ei xls-tiedostoa")
            break
        except Exception as e:  # noqa: BLE001
            errs.append(f"{url}: {str(e)[:100]}")
            content = None
    if content is None:
        raise RuntimeError(" ; ".join(errs))

    raw = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=None)

    # Etsi otsikkorivi: rivi, jolla on "Date" ja "P" ja "CPI"
    header_row = None
    for i in range(min(15, len(raw))):
        row = [str(v).strip() for v in raw.iloc[i].tolist()]
        if "Date" in row and "P" in row and "CPI" in row:
            header_row = i
            break
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(SHILLER_DEBUG, "w", encoding="utf-8") as f:
            f.write(f"otsikkorivi: {header_row}\n\n")
            f.write(raw.head(12).to_string())
            f.write("\n\n...viimeiset rivit:\n")
            f.write(raw.tail(6).to_string())
    except Exception:  # noqa: BLE001
        pass
    if header_row is None:
        raise RuntimeError("Shillerin otsikkoriviä (Date/P/CPI) ei löytynyt — ks. outputs/shiller_debug.txt")

    hdr = [str(v).strip() for v in raw.iloc[header_row].tolist()]
    col = {name: hdr.index(name) for name in ("Date", "P", "E", "CPI") if name in hdr}
    if "E" not in col:
        raise RuntimeError("E-saraketta ei löytynyt")

    body = raw.iloc[header_row + 1:]
    dates = pd.to_numeric(body.iloc[:, col["Date"]], errors="coerce")
    ok = dates.notna() & (dates > 1800) & (dates < 2200)
    body, dates = body[ok], dates[ok]

    df = pd.DataFrame(
        {
            "P": pd.to_numeric(body.iloc[:, col["P"]], errors="coerce").values,
            "E": pd.to_numeric(body.iloc[:, col["E"]], errors="coerce").values,
            "CPI": pd.to_numeric(body.iloc[:, col["CPI"]], errors="coerce").values,
        },
        index=_shiller_dates(dates),
    )
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Tulokset raportoidaan viiveellä: täytetään viimeiset kuukaudet eteenpäin (max 6 kk)
    df["E"] = df["E"].ffill(limit=6)
    df["CPI"] = df["CPI"].ffill(limit=3)
    df["CAPE"] = _compute_cape(df["P"], df["E"], df["CPI"])

    last = df["CAPE"].dropna()
    if last.empty or not (3 < last.iloc[-1] < 100):
        raise RuntimeError(f"laskettu CAPE epäuskottava ({last.iloc[-1] if not last.empty else 'tyhjä'}) — ks. outputs/shiller_debug.txt")
    return df


def _shiller_from_multpl() -> pd.DataFrame:
    """Varalähde: multpl.com julkaisee Shillerin CAPE:n ja S&P 500:n kuukausitaulukkoina."""
    def table(url: str) -> pd.Series:
        r = http_get(url, retries=2)
        tables = pd.read_html(io.StringIO(r.text))
        t = next(t for t in tables if t.shape[1] >= 2 and "Date" in str(t.columns[0]))
        d = pd.to_datetime(t.iloc[:, 0].astype(str), format="%b %d, %Y", errors="coerce")
        v = pd.to_numeric(t.iloc[:, 1].astype(str).str.replace(r"[^0-9.]", "", regex=True), errors="coerce")
        s = pd.Series(v.values, index=d.values).dropna()
        return to_monthly(s)

    cape = table(MULTPL_CAPE)
    p = table(MULTPL_SPX)
    if cape.empty or not (3 < cape.iloc[-1] < 100):
        raise RuntimeError("multpl CAPE epäuskottava")
    df = pd.DataFrame({"P": p, "CAPE": cape})
    df["E"] = np.nan
    df["CPI"] = np.nan
    return df.sort_index()


def _parse_seed_dates(raw: pd.Series) -> pd.Series:
    """Tulkitsee kuukausisarakkeen monessa muodossa: 2024-03, 03/2024, Mar-24, Mar 2024, jne."""
    d = pd.Series(pd.NaT, index=raw.index)
    for fmt in ("%b-%y", "%b-%Y", "%b %Y", "%B %Y", "%B-%Y", "%m/%Y", "%Y-%m", "%Y-%m-%d", "%m/%d/%Y"):
        miss = d.isna()
        if miss.any():
            d[miss] = pd.to_datetime(raw[miss], format=fmt, errors="coerce")
    miss = d.isna()
    if miss.any():
        d[miss] = pd.to_datetime(raw[miss], errors="coerce")
    return d


def _read_margin_seed() -> pd.Series:
    """Lukee FINRA-margin debtin repoon tallennetusta tiedostosta.

    Kaksi tuettua tapaa:
      1) FINRA:n virallinen Excel (data/finra_margin_seed.xlsx tai margin-statistics.xlsx),
         jonka voi ladata selaimella osoitteesta
         finra.org/sites/default/files/2021-03/margin-statistics.xlsx — koko historia 1997→.
         Lukija etsii otsikkorivin, "Debit"-sarakkeen ja kuukausisarakkeen automaattisesti.
      2) Yksinkertainen kahden sarakkeen CSV (data/finra_margin_seed.csv): kuukausi, margin debt.
    Tarpeen, koska FINRA:n sivu torjuu automaattiset haut (403)."""
    candidates = ["finra_margin_seed.xlsx", "margin-statistics.xlsx", "finra_margin_seed.csv"]
    for name in candidates:
        p = os.path.join(DATA_DIR, name)
        if not os.path.exists(p):
            continue
        try:
            raw_df = pd.read_excel(p, header=None) if name.endswith("xlsx") else pd.read_csv(p, header=None)
            s = _extract_margin(raw_df)
            if s is not None and not s.empty:
                return s
            warn(f"FINRA-siementiedostosta {name} ei löytynyt margin debt -saraketta")
        except Exception as e:  # noqa: BLE001
            warn(f"FINRA-siementiedostoa {name} ei voitu lukea: {e}")
    return pd.Series(dtype=float)


def _extract_margin(raw_df: pd.DataFrame) -> pd.Series | None:
    """Poimii kuukausi + debit balance mistä tahansa taulukkomuodosta (FINRA-xlsx tai oma CSV)."""
    # Etsi otsikkorivi, jolla on "debit" jossain solussa
    header_row = None
    for i in range(min(15, len(raw_df))):
        cells = [str(v).lower() for v in raw_df.iloc[i].tolist()]
        if any("debit" in c for c in cells):
            header_row = i
            break

    if header_row is not None:
        hdr = [str(v).strip() for v in raw_df.iloc[header_row].tolist()]
        low = [h.lower() for h in hdr]
        debit_col = next(j for j, h in enumerate(low) if "debit" in h)
        # Kuukausisarake: ensin erilliset Year + Month (jos molemmat bare-sarakkeina),
        # sitten yhdistetty "year-month"/"date"/"period", viimeisenä ensimmäinen sarake.
        ycol = next((j for j, h in enumerate(low) if h.strip() in ("year", "yr")), None)
        mcol = next((j for j, h in enumerate(low) if h.strip() in ("month", "mo", "mon")), None)
        combo_col = next((j for j, h in enumerate(low)
                          if any(k in h for k in ("year-month", "yearmonth", "date", "period"))
                          or ("month" in h and "-" in h)), None)
        body = raw_df.iloc[header_row + 1:]
        if ycol is not None and mcol is not None:
            combo = (body.iloc[:, ycol].astype(str).str.strip().str.replace(r"\.0$", "", regex=True) + "-"
                     + body.iloc[:, mcol].astype(str).str.strip().str.replace(r"\.0$", "", regex=True))
            d = _parse_seed_dates(combo)
        elif combo_col is not None:
            d = _parse_seed_dates(body.iloc[:, combo_col].astype(str).str.strip())
        elif mcol is not None:
            d = _parse_seed_dates(body.iloc[:, mcol].astype(str).str.strip())
        else:
            d = _parse_seed_dates(body.iloc[:, 0].astype(str).str.strip())
        v = pd.to_numeric(body.iloc[:, debit_col].astype(str).str.replace(r"[^0-9.]", "", regex=True), errors="coerce")
    else:
        # Ei "debit"-otsikkoa → oletetaan yksinkertainen kahden sarakkeen CSV (kuukausi, luku)
        df = raw_df.iloc[:, :2].dropna(how="all")
        d = _parse_seed_dates(df.iloc[:, 0].astype(str).str.strip())
        v = pd.to_numeric(df.iloc[:, 1].astype(str).str.replace(r"[^0-9.]", "", regex=True), errors="coerce")

    s = pd.Series(v.values, index=d.values).dropna()
    s = s[s.index.notna()]
    if s.empty:
        return None
    return to_monthly(s)


def fetch_finra_margin() -> pd.Series:
    """Margin debt (milj. USD). Yhdistää siemendatan, verkkohaun ja välimuistin."""
    cache = pd.Series(dtype=float)
    if os.path.exists(MARGIN_CACHE):
        c = pd.read_csv(MARGIN_CACHE, parse_dates=["date"], index_col="date")
        cache = c["margin_debt"].astype(float)
    seed = _read_margin_seed()
    if not seed.empty:
        cache = pd.concat([seed, cache])
        cache = cache[~cache.index.duplicated(keep="last")].sort_index()

    if DEMO:
        return demo_series("1997-01-01", 100000, 0.04, 7, 0.006)

    # Jos siemen/välimuisti jo kattaa viime kuukauden, ei yritetä verkkohakua lainkaan:
    # FINRA torjuu robotit (403), joten haku vain hidastaisi ja tuottaisi turhan varoituksen.
    if not cache.empty and cache.dropna().index.max() >= (pd.Timestamp(dt.date.today()) - pd.Timedelta(days=40)):
        return to_monthly(cache)

    fresh = pd.Series(dtype=float)
    try:
        r = http_get(FINRA_URL, retries=1)
        tables = pd.read_html(io.StringIO(r.text))
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            mcol = next((c for c in t.columns if "month" in str(c).lower()), None)
            dcol = next((c for c in t.columns if "debit" in str(c).lower()), None)
            if mcol is None or dcol is None:
                continue
            d = pd.to_datetime(t[mcol].astype(str).str.strip(), format="%b-%y", errors="coerce")
            v = pd.to_numeric(t[dcol].astype(str).str.replace(r"[^0-9.]", "", regex=True), errors="coerce")
            part = pd.Series(v.values, index=d.values).dropna()
            part = part[part.index.notna()]
            fresh = pd.concat([fresh, part])
        fresh = to_monthly(fresh)
    except Exception as e:  # noqa: BLE001
        warn(f"FINRA-haku epäonnistui ({e}); käytetään välimuistia")

    merged = pd.concat([cache, fresh])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if merged.empty:
        raise RuntimeError("Margin debt -dataa ei saatu mistään")
    os.makedirs(DATA_DIR, exist_ok=True)
    merged.rename("margin_debt").rename_axis("date").to_csv(MARGIN_CACHE)
    return to_monthly(merged)


def _peak(series: pd.Series, start: str, end: str) -> dict | None:
    """Sarjan huippuarvo ja sen kuukausi annetulla aikavälillä (uutiskirjeen vertailuja varten)."""
    w = series.loc[start:end].dropna()
    if w.empty:
        return None
    return {"arvo": round(float(w.max()), 1), "kk": str(w.idxmax().date())[:7]}


def _last(series, decimals=2) -> dict | None:
    if series is None:
        return None
    s = series.dropna()
    if s.empty:
        return None
    return {"arvo": round(float(s.iloc[-1]), decimals), "kk": str(s.index[-1].date())[:7]}


def safe(fn, *args, **kw):
    """Suorittaa datahaun; virheessä palauttaa None ja kirjaa varoituksen."""
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        warn(f"{fn.__name__}{args} epäonnistui: {e}")
        return None


# ----------------------------------------------------------------------------
# Muuttujien rakentaminen
# ----------------------------------------------------------------------------
def build_variables() -> tuple[pd.DataFrame, dict]:
    """Palauttaa raakamuuttujat (kuukausittain) ja metatiedot (pilari, käännetty)."""
    vars_: dict[str, pd.Series] = {}
    meta: dict[str, dict] = {}

    def add(key, name, pillar, series, invert=False, ffill=7):
        if series is None or series.dropna().empty:
            warn(f"Muuttuja {key} ({name}) puuttuu")
            return
        vars_[key] = series.astype(float)
        meta[key] = {"name": name, "pillar": pillar, "invert": invert, "ffill": ffill}

    sh = safe(fetch_shiller)
    spx = safe(fetch_yahoo, "^GSPC")
    gdp = safe(fetch_fred, "GDP")  # neljännesvuosittain, mrd USD

    # --- Pilari A: arvostus ---------------------------------------------------
    cape = None
    if sh is not None:
        cape = sh["CAPE"].copy()
        # Nowcast: skaalataan viimeisin Shillerin CAPE tämän päivän S&P 500 -hinnalla
        if spx is not None:
            last = cape.dropna().index[-1]
            p_last = sh["P"].dropna().loc[last]
            spx_now = spx.dropna().iloc[-1]
            if spx.dropna().index[-1] > last:
                cape.loc[spx.dropna().index[-1]] = cape.loc[last] * spx_now / p_last
                cape = cape.sort_index()
    add("A1", "Shiller CAPE (S&P 500)", "A", cape)

    w5000 = safe(fetch_yahoo, "^W5000")
    if w5000 is None or w5000.dropna().empty:
        w5000 = safe(fetch_fred, "WILL5000PRFC")
    if w5000 is not None and gdp is not None:
        g = gdp.reindex(w5000.index.union(gdp.index)).ffill().reindex(w5000.index)
        add("A4", "Buffett-indikaattori (Wilshire 5000 / BKT)", "A", w5000 / g)

    cpi_u = safe(fetch_fred, "CPIAUCSL")
    if cape is not None:
        dfii = safe(fetch_fred, "DFII10")
        dgs10 = safe(fetch_fred, "DGS10")
        infl10 = None
        cpi = sh["CPI"] if (sh is not None and sh["CPI"].notna().sum() > 240) else cpi_u
        if cpi is not None:
            infl10 = ((cpi / cpi.shift(120)) ** (1 / 10) - 1) * 100
        real_yield = None
        if dfii is not None:
            real_yield = dfii
        FACTS["usa_10v_korko_pct"] = _last(dgs10)
        FACTS["usa_10v_reaalikorko_pct"] = _last(dfii)
        if dgs10 is not None and infl10 is not None:
            proxy = (dgs10 - infl10.reindex(dgs10.index)).dropna()
            real_yield = proxy if real_yield is None else real_yield.combine_first(proxy)
        if real_yield is not None:
            erp = (100 / cape) - real_yield.reindex(cape.index).ffill()
            add("A6", "Osakeriskipreemio (1/CAPE − reaalikorko)", "A", erp, invert=True)

    eq = safe(fetch_fred, "NCBEILQ027S")      # yritysten osakkeiden markkina-arvo
    nw = safe(fetch_fred, "TNWMVBSNNCB")      # yritysten nettovarallisuus markkina-arvoon
    if eq is not None and nw is not None:
        add("A8", "Tobinin Q", "A", (eq / nw.reindex(eq.index)).dropna())

    # --- Pilari B: vipu & likviditeetti -------------------------------------
    md = safe(fetch_finra_margin)
    if md is not None and not md.dropna().empty:
        mdd = md.dropna()
        yoy = (mdd / mdd.shift(12) - 1) * 100
        FACTS["margin_debt"] = {
            "taso_mrd_usd": round(float(mdd.iloc[-1]) / 1000, 1),
            "kk": str(mdd.index[-1].date())[:7],
            "kasvu_12kk_pct": round(float(yoy.dropna().iloc[-1]), 1) if yoy.notna().any() else None,
            "kasvun_huippu_1999_2000": _peak(yoy, "1999-01", "2000-12"),
            "kasvun_huippu_2020_2021": _peak(yoy, "2020-06", "2021-12"),
            "tason_ennatys_mrd_usd": round(float(mdd.max()) / 1000, 1),
            "tason_ennatys_kk": str(mdd.idxmax().date())[:7],
        }
    if md is not None and gdp is not None:
        g = gdp.reindex(md.index.union(gdp.index)).ffill().reindex(md.index)
        add("B1", "Margin debt / BKT", "B", md / g)
        add("B2", "Margin debtin 12 kk kasvu", "B", np.log(md / md.shift(12)))

    m2 = safe(fetch_fred, "M2SL")
    if m2 is not None and cpi_u is not None:
        real_m2 = (m2.pct_change(12) - cpi_u.pct_change(12).reindex(m2.index)) * 100
        add("B3", "Reaalinen M2-kasvu (12 kk)", "B", real_m2)

    walcl = safe(fetch_fred, "WALCL")          # Fed, milj. USD
    ecb = safe(fetch_fred, "ECBASSETSW")       # EKP, milj. EUR
    boj = safe(fetch_fred, "JPNASSETS")        # BoJ, 100 milj. JPY
    eur = safe(fetch_fred, "DEXUSEU")          # USD per EUR
    jpy = safe(fetch_fred, "DEXJPUS")          # JPY per USD
    if walcl is not None:
        liq = walcl.copy()
        if ecb is not None and eur is not None:
            liq = liq.add((ecb * eur.reindex(ecb.index).ffill()).reindex(liq.index).ffill(), fill_value=0)
        if boj is not None and jpy is not None:
            liq = liq.add((boj * 100 / jpy.reindex(boj.index).ffill()).reindex(liq.index).ffill(), fill_value=0)
        add("B4", "Keskuspankkilikviditeetti, 12 kk muutos (Fed+EKP+BoJ)", "B", np.log(liq / liq.shift(12)))

    # Luottomarginaali. HUOM: ICE BofA HY OAS (BAMLH0A0HYM2) rajoitettiin FRED:ssä 4/2026
    # alkaen vain 3 vuoteen, joten se ei riitä persentiilihistoriaan. Käytetään Moody's Baa −
    # 10v -marginaalia (BAA10Y): päivittäin 1986→, rajoittamaton, kattaa 2000 ja 2007 huiput.
    hy = safe(fetch_fred, "BAA10Y")
    if hy is None or hy.dropna().empty:
        hy = safe(fetch_fred, "BAMLH0A0HYM2")  # varasarja, jos BAA10Y ei jostain syystä vastaa
    add("B5", "Luottomarginaali (Moody's Baa − 10v)", "B", hy, invert=True)

    ff = safe(fetch_fred, "FEDFUNDS")
    pce = safe(fetch_fred, "PCEPILFE")
    FACTS["fed_ohjauskorko_pct"] = _last(ff)
    if pce is not None:
        FACTS["pohjainflaatio_pce_12kk_pct"] = _last(pce.pct_change(12) * 100, 1)
    if ff is not None and pce is not None:
        add("B6", "Reaalinen ohjauskorko", "B", ff - pce.pct_change(12).reindex(ff.index) * 100, invert=True)

    debt = safe(fetch_fred, "QUSPAM770A")     # yksityinen velka / BKT (BIS)
    if debt is not None:
        # BIS julkaisee 2–3 neljänneksen viiveellä; hidas muuttuja, joten 12 kk kantaminen on ok
        add("B7", "Yksityisen velan/BKT 3 v muutos", "B", debt - debt.shift(36), ffill=12)

    frame = pd.DataFrame(vars_).sort_index()
    return frame, meta


# ----------------------------------------------------------------------------
# Laskenta
# ----------------------------------------------------------------------------
def compute(frame: pd.DataFrame, meta: dict):
    # Täytetään harvat sarjat eteenpäin enintään 7 kk. Neljännesvuosidata (Z.1, BIS)
    # raportoidaan 1–2 neljänneksen viiveellä, joten lyhyempi raja jättäisi tuoreimman
    # kuukauden tyhjäksi. Kaikki tässä täytettävät ovat hitaita taso-/suhdemuuttujia,
    # joten arvon kantaminen eteenpäin neljänneksen on vakiokäytäntö.
    filled = pd.DataFrame({k: frame[k].ffill(limit=meta[k].get("ffill", 7)) for k in frame.columns}, index=frame.index)

    pct = pd.DataFrame(index=filled.index)
    z = pd.DataFrame(index=filled.index)
    for k in filled.columns:
        s = filled[k]
        p = s.expanding(min_periods=MIN_HISTORY_MONTHS).rank(pct=True) * 100
        mu = s.expanding(min_periods=MIN_HISTORY_MONTHS).mean()
        sd = s.expanding(min_periods=MIN_HISTORY_MONTHS).std()
        zz = (s - mu) / sd
        if meta[k]["invert"]:
            p, zz = 100 - p, -zz
        pct[k] = p
        z[k] = zz

    pillars = pd.DataFrame(index=pct.index)
    coverage = pd.DataFrame(index=pct.index)
    for pil in PILLAR_WEIGHTS:
        cols = [k for k in pct.columns if meta[k]["pillar"] == pil]
        pillars[pil] = pct[cols].mean(axis=1)
        coverage[pil] = pct[cols].notna().sum(axis=1) / max(len(cols), 1)

    w = pd.Series(PILLAR_WEIGHTS)
    avail = pillars.notna() * w
    composite = (pillars.fillna(0) * w).sum(axis=1) / avail.sum(axis=1).replace(0, np.nan)

    extreme = (pct >= 90).sum(axis=1) / pct.notna().sum(axis=1).replace(0, np.nan)
    total_cov = pct.notna().sum(axis=1) / len(pct.columns)

    return pct, z, pillars, coverage, composite, extreme, total_cov


def analogs(z: pd.DataFrame, meta: dict, asof: pd.Timestamp):
    now = z.loc[asof].dropna()
    out = []
    for name, date in PEAKS.items():
        d = pd.Timestamp(date)
        if d not in z.index:
            continue
        peak = z.loc[d].dropna()
        common = now.index.intersection(peak.index)
        if len(common) < 2:
            continue
        diff = (now[common] - peak[common])
        rms = float(np.sqrt((diff ** 2).mean()))
        score = 100 * max(0.0, 1 - rms / 2.0)
        order = diff.abs().sort_values()
        out.append(
            {
                "name": name,
                "score": score,
                "n": len(common),
                "similar": [meta[k]["name"] for k in order.index[:3]],
                "different": [meta[k]["name"] for k in order.index[::-1][:3]],
            }
        )
    return sorted(out, key=lambda x: -x["score"])


# ----------------------------------------------------------------------------
# Raportointi
# ----------------------------------------------------------------------------
def load_history() -> pd.DataFrame:
    if os.path.exists(HISTORY_CSV):
        return pd.read_csv(HISTORY_CSV, parse_dates=["run_date", "asof"])
    return pd.DataFrame()


def interpret(x: float) -> str:
    if x < 40:
        return "Ei kuplamaisia piirteitä"
    if x < 60:
        return "Kallis, ei kupla"
    if x < 75:
        return "Kuplan esiaste"
    if x < 90:
        return "Kupla-alue"
    return "Historiallinen ääri"


def na_reasons(frame: pd.DataFrame, pct_asof: pd.Series, asof: pd.Timestamp) -> dict:
    """Selittää jokaiselle n/a-muuttujalle, miksi arvoa ei ole: vanhentunut data vai liian lyhyt historia."""
    out = {}
    for k in pct_asof.index:
        if pd.notna(pct_asof[k]) or k not in frame.columns:
            continue
        s = frame[k].dropna()
        if s.empty:
            out[k] = "ei dataa"
            continue
        lv = s.index[-1]
        lag = (asof.year - lv.year) * 12 + (asof.month - lv.month)
        n = len(s)
        if n < MIN_HISTORY_MONTHS:
            out[k] = f"historia {n} kk < {MIN_HISTORY_MONTHS} kk"
        elif lag > 7:
            out[k] = f"viimeisin data {lv.date()}, {lag} kk vanha"
        else:
            out[k] = f"viimeisin data {lv.date()}"
    return out


def make_report(asof, comp, pillars, coverage, extreme, cov, pct, meta, ana, hist, reasons=None):
    reasons = reasons or {}
    today = dt.date.today()
    week = today.isocalendar()[1]
    prev = None
    if not hist.empty:
        prev = hist.iloc[-1]
    delta = "" if prev is None else f"  ({comp - prev['kuplariski']:+.1f} vs. edellinen ajo {prev['run_date'].date()})"

    lines = [
        f"KUPLAINDIKAATTORI — viikko {week}/{today.year}   (data {asof.date()})",
        "=" * 64,
        f"KUPLARISKI       {comp:5.1f} / 100   {interpret(comp)}{delta}",
        f"                 datan kattavuus {cov*100:.0f} %",
    ]
    for pil in PILLAR_WEIGHTS:
        v = pillars[pil]
        vs = f"{v:5.1f}" if pd.notna(v) else "  n/a"
        lines.append(f"  {PILLAR_NAMES[pil]:<24}{vs}   (paino {PILLAR_WEIGHTS[pil]*100:.0f} %, kattavuus {coverage[pil]*100:.0f} %)")
    n_ext = int((pct >= 90).sum())
    n_av = int(pct.notna().sum())
    lines.append(f"  Ääriarvoja               {n_ext} / {n_av} muuttujasta yli 90. persentiilin ({extreme*100:.0f} %)")
    lines.append("")
    lines.append("MUUTTUJAT (persentiili omaa historiaa vasten, 100 = kuplamaisin)")
    for k in pct.index:
        v = pct[k]
        bar = "" if pd.isna(v) else "#" * int(v // 10)
        vs = "  n/a" if pd.isna(v) else f"{v:5.0f}"
        why = f"  ({reasons[k]})" if pd.isna(v) and k in reasons else ""
        lines.append(f"  {k} {meta[k]['name']:<48}{vs}  {bar}{why}")
    lines.append("")
    if ana:
        lines.append("ANALOGIA (mitä historiallista huippua nykytila muistuttaa)")
        for a in ana:
            lines.append(f"  {a['name']:<26}{a['score']:5.0f} %   ({a['n']} yhteistä muuttujaa)")
        best = ana[0]
        lines.append(f"  Samankaltaisinta ({best['name']}): " + "; ".join(best["similar"]))
        lines.append(f"  Erilaisinta:      " + "; ".join(best["different"]))
        lines.append("")
    if not hist.empty and len(hist) >= 2:
        h = hist.tail(52)
        lines.append(f"52 viikon vaihteluväli: {h['kuplariski'].min():.0f}–{h['kuplariski'].max():.0f}")
        lines.append("")
    lines.append("Vaihe 1: mukana pilarit A ja B. Pilarit C (spekulaatio), D (keskittyminen) ja E (ajoitus) lisätään myöhemmin.")
    if WARNINGS:
        lines.append("")
        lines.append("VAROITUKSET:")
        lines += [f"  - {w}" for w in WARNINGS]
    return "\n".join(lines)


def make_chart(composite: pd.Series, extreme: pd.Series):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = composite.dropna()
    s = s[s.index >= "1975-01-01"]
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.axhspan(75, 90, color="orange", alpha=0.15)
    ax.axhspan(90, 100, color="red", alpha=0.15)
    ax.plot(s.index, s.values, lw=1.6, color="#1f4e79", label="Kuplariski (pilarit A+B)")
    e = (extreme.reindex(s.index) * 100)
    ax.plot(e.index, e.values, lw=1, color="#888", alpha=0.8, label="Ääriarvojen osuus %")
    for name, d in PEAKS.items():
        ax.axvline(pd.Timestamp(d), color="k", ls=":", lw=0.9)
        ax.text(pd.Timestamp(d), 101, name.split()[0], ha="center", fontsize=8)
    ax.set_ylim(0, 105)
    ax.set_ylabel("0–100")
    ax.set_title("Kuplaindikaattori v0.1 — kuplariski suhteessa omaan historiaan")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    os.makedirs(OUT_DIR, exist_ok=True)
    fig.tight_layout()
    fig.savefig(CHART_PNG, dpi=130)
    plt.close(fig)


def send_email(subject: str, body: str, attachment: str | None):
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("EMAIL_TO", user)
    if not (user and pw and to):
        print("Sähköpostin ympäristömuuttujat puuttuvat — raportti vain tulostettu.")
        return
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment and os.path.exists(attachment):
        with open(attachment, "rb") as f:
            msg.add_attachment(f.read(), maintype="image", subtype="png", filename="kuplariski.png")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    print("Sähköposti lähetetty:", to)


# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Faktapaketti uutiskirjeelle (outputs/latest.json)
# ----------------------------------------------------------------------------
LATEST_JSON = os.path.join(OUT_DIR, "latest.json")

# Lukijalle esitettävät teemat: pilarien muuttujat ryhmiteltynä ymmärrettäviksi kokonaisuuksiksi.
# Kun pilarit C–E lisätään, niille lisätään omat teemansa tähän.
THEMES = {
    "arvostus":  {"nimi": "Arvostus",      "muuttujat": ["A1", "A4", "A6", "A8"]},
    "velkavipu": {"nimi": "Velkavipu",     "muuttujat": ["B1", "B2"]},
    "raha":      {"nimi": "Raha ja velka", "muuttujat": ["B3", "B4", "B5", "B6", "B7"]},
}

# Raaka-arvon muunnos lukijalle ymmärrettävään yksikköön: (muunnos, yksikkö, desimaalit)
DISPLAY = {
    "A1": (lambda x: x, "kertaa 10 vuoden keskitulos", 1),
    "A4": (lambda x: x * 100, "% BKT:sta (Wilshire 5000 / BKT, likiarvo)", 0),
    "A6": (lambda x: x, "%-yksikköä", 2),
    "A8": (lambda x: x, "Q-luku", 2),
    "B1": (lambda x: x * 0.1, "% BKT:sta", 2),
    "B2": (lambda x: (np.exp(x) - 1) * 100, "% / 12 kk", 1),
    "B3": (lambda x: x, "% / 12 kk inflaatiokorjattuna", 1),
    "B4": (lambda x: (np.exp(x) - 1) * 100, "% / 12 kk", 1),
    "B5": (lambda x: x, "%-yksikköä", 2),
    "B6": (lambda x: x, "%", 2),
    "B7": (lambda x: x, "%-yksikköä / 3 v", 1),
}


def status_label(score) -> tuple[str, str]:
    """Tilan nimi ja taso (good/warning/serious/critical) 0–100-lukemalle."""
    if score is None or pd.isna(score):
        return ("Ei dataa", "none")
    if score >= 90:
        return ("Äärimmäinen", "critical")
    if score >= 75:
        return ("Korkea", "serious")
    if score >= 40:
        return ("Koholla", "warning")
    return ("Rauhallinen", "good")


def _years_to_ranges(years: list[int]) -> str:
    """[1999, 2000, 2007] -> '1999–2000, 2007'"""
    if not years:
        return ""
    out, start, prev = [], years[0], years[0]
    for y in years[1:]:
        if y == prev + 1:
            prev = y
            continue
        out.append(f"{start}–{prev}" if start != prev else f"{start}")
        start = prev = y
    out.append(f"{start}–{prev}" if start != prev else f"{start}")
    return ", ".join(out)


def export_latest(frame, meta, pct, pillars, composite, cov, extreme, ana, asof, reasons) -> dict:
    """Kirjoittaa uutiskirjeen faktapaketin. Kaikki 'ennätys'-väitteet lasketaan tässä datasta,
    jotta kirjoittaja (AI tai pohja) ei voi pyöristää 99,6:tta ennätykseksi."""
    variables = {}
    for k in meta:
        f, unit, dec = DISPLAY.get(k, (lambda x: x, "", 2))
        s = frame[k].ffill(limit=meta[k].get("ffill", 7)).loc[:asof].dropna()
        p = pct.loc[asof, k] if k in pct.columns else np.nan
        entry = {
            "nimi": meta[k]["name"],
            "pilari": meta[k]["pillar"],
            "persentiili": None if pd.isna(p) else round(float(p), 1),
            "kuplamaisempi_kun_pienempi": bool(meta[k]["invert"]),
            "yksikko": unit,
        }
        if k in reasons:
            entry["puuttumisen_syy"] = reasons[k]
        if not s.empty:
            cur, hist = s.iloc[-1], s.iloc[:-1]
            if meta[k]["invert"]:
                more = hist[hist < cur]
                ext = (hist.min(), hist.idxmin()) if not hist.empty else (None, None)
            else:
                more = hist[hist > cur]
                ext = (hist.max(), hist.idxmax()) if not hist.empty else (None, None)
            entry.update({
                "arvo": round(float(f(cur)), dec),
                "arvon_kk": str(s.index[-1].date())[:7],
                "historia_alkaa": int(s.index[0].year),
                "on_ennatys": bool(len(more) == 0),
                "kuplamaisempi_kuin_nyt_vuosina": _years_to_ranges(sorted({d.year for d in more.index})),
                "historian_kuplamaisin_arvo": None if ext[0] is None else round(float(f(ext[0])), dec),
                "historian_kuplamaisin_kk": None if ext[1] is None else str(ext[1].date())[:7],
            })
        variables[k] = entry

    themes = {}
    for tk, t in THEMES.items():
        keys = [k for k in t["muuttujat"] if k in pct.columns and pd.notna(pct.loc[asof, k])]
        sc = round(float(np.mean([pct.loc[asof, k] for k in keys])), 1) if keys else None
        lab, lvl = status_label(sc)
        themes[tk] = {"nimi": t["nimi"], "pisteet": sc, "tila": lab, "taso": lvl, "muuttujat": keys}

    comp = float(composite.loc[asof])
    out = {
        "versio": VERSION,
        "luotu_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "datan_kk": str(asof.date())[:7],
        "kuplalukema": round(comp, 1),
        "vyohyke": interpret(comp),
        "taso": status_label(comp)[1],
        "kattavuus_pct": round(float(cov.loc[asof]) * 100, 1),
        "aariarvojen_osuus_pct": round(float(extreme.loc[asof]) * 100, 1),
        "pilarit": {p: {"nimi": PILLAR_NAMES[p], "paino": PILLAR_WEIGHTS[p],
                        "pisteet": None if pd.isna(pillars.loc[asof, p]) else round(float(pillars.loc[asof, p]), 1)}
                    for p in PILLAR_WEIGHTS},
        "teemat": themes,
        "muuttujat": variables,
        "analogiat": [{"huippu": a["name"], "samankaltaisuus_pct": round(a["score"]),
                       "yhteisia_muuttujia": a["n"], "samankaltaisinta": a["similar"],
                       "erilaisinta": a["different"]} for a in ana],
        "raakatasot": FACTS,
        "varoitukset": WARNINGS,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(LATEST_JSON, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=str)
    return out


def main():
    global DEMO
    # Rivipuskurointi: raportti näkyy Actions-lokissa heti oikeassa järjestyksessä
    # varoitusten kanssa, eikä katoa puskuriin jos jokin myöhemmin kaatuu.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="synteettinen data, ei verkkohakuja")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()
    DEMO = args.demo

    print("=" * 64, flush=True)
    print(f"KUPLAINDIKAATTORI {VERSION} — ajettava tiedosto: {os.path.abspath(__file__)}", flush=True)
    print(f"HTTP-aikaraja {HTTP_TIMEOUT}s, uusinnat {HTTP_RETRIES}, FRED-avain: {'ON' if FRED_API_KEY else 'EI'}", flush=True)
    print("=" * 64, flush=True)

    frame, meta = build_variables()
    if frame.empty:
        raise SystemExit("Yhtään muuttujaa ei saatu — ajo keskeytetään.")

    pct, z, pillars, coverage, composite, extreme, cov = compute(frame, meta)
    asof = composite.dropna().index[-1]
    comp = float(composite.loc[asof])
    ana = analogs(z, meta, asof)
    hist = load_history()

    reasons = na_reasons(frame, pct.loc[asof], asof)
    report = make_report(asof, comp, pillars.loc[asof], coverage.loc[asof], float(extreme.loc[asof]),
                         float(cov.loc[asof]), pct.loc[asof], meta, ana, hist, reasons)
    print("\n" + report + "\n", flush=True)   # flush: raportti varmasti lokiin ennen mitään myöhempää

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(REPORT_TXT, "w", encoding="utf-8") as f:
        f.write(report)
    try:
        export_latest(frame, meta, pct, pillars, composite, cov, extreme, ana, asof, reasons)
        print(f"Faktapaketti uutiskirjeelle: {LATEST_JSON}", flush=True)
    except Exception:  # noqa: BLE001
        print("VAROITUS: faktapaketin (latest.json) vienti epäonnistui:", flush=True)
        traceback.print_exc()

    # Kaavio, historian tallennus ja sähköposti eivät saa kaataa ajoa niin, että raportti
    # jää saamatta — raportti on jo tulostettu ja tallennettu yllä. Jokainen vaihe erikseen suojattu.
    try:
        make_chart(composite, extreme)
    except Exception:  # noqa: BLE001
        print("VAROITUS: kaavion piirto epäonnistui:", flush=True)
        traceback.print_exc()

    # Historia
    row = {
        "run_date": pd.Timestamp(dt.date.today()),
        "asof": asof,
        "kuplariski": round(comp, 2),
        **{f"pilari_{p}": round(float(pillars.loc[asof, p]), 2) if pd.notna(pillars.loc[asof, p]) else np.nan for p in PILLAR_WEIGHTS},
        "aariarvot_osuus": round(float(extreme.loc[asof]) * 100, 1),
        "kattavuus": round(float(cov.loc[asof]) * 100, 1),
        "analogi": ana[0]["name"] if ana else "",
        "analogi_pct": round(ana[0]["score"], 1) if ana else np.nan,
    }
    try:
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
        os.makedirs(DATA_DIR, exist_ok=True)
        hist.to_csv(HISTORY_CSV, index=False)
        # Koko kuukausisarja talteen omien analyysien pohjaksi
        pd.concat([pct.add_prefix("pct_"), pillars.add_prefix("pilari_"), composite.rename("kuplariski")], axis=1)\
            .to_csv(os.path.join(DATA_DIR, "kuplariski_kuukausittain.csv"))
    except Exception:  # noqa: BLE001
        print("VAROITUS: historian tallennus epäonnistui:", flush=True)
        traceback.print_exc()

    # Sähköposti
    prev_comp = hist.iloc[-2]["kuplariski"] if len(hist) >= 2 else None
    alert = comp >= ALERT_THRESHOLD and (prev_comp is None or prev_comp < ALERT_THRESHOLD)
    subject = f"Kuplaindikaattori vko {dt.date.today().isocalendar()[1]}: kuplariski {comp:.0f}"
    if prev_comp is not None:
        subject += f" ({comp - prev_comp:+.0f})"
    if alert:
        subject = "⚠ HÄLYTYS — " + subject
    elif comp >= ALERT_THRESHOLD:
        subject = "⚠ " + subject
    if not args.no_email and not DEMO:
        try:
            send_email(subject, report, CHART_PNG)
        except Exception:  # noqa: BLE001
            print("VAROITUS: sähköpostin lähetys epäonnistui (raportti on silti yllä ja tallennettu):", flush=True)
            traceback.print_exc()


if __name__ == "__main__":
    main()
