"""
Kuplamittarin tutka: viikon liikkujat, sektorilämpökartta, ehdokasseula sekä ennusteiden
jäädytys ja mekaaninen pisteytys.

Rehellisyyssäännöt (nämä ovat koko tuloskortin uskottavuuden perusta):
  1. Ennusteet jäädytetään tiedostoon newsletter/ennusteet.json ja repoon ENNEN viikon alkua.
     Uusia ennusteita tehdään vain viikonloppuna, kun pörssit ovat kiinni. Lähtöhinta on
     SEURAAVAN kaupankäyntipäivän (maanantain) avauskurssi – se hinta, jonka lukija olisi oikeasti
     saanut. Näin viikonlopun uutiset, jotka tekoäly on voinut lukea, eivät vääristä tulosta.
  2. Saman viikon ennusteita ei koskaan kirjoiteta yli. Uusintaajo käyttää jo jäädytettyjä.
  3. Pisteytys on mekaanista: osuma = kohde voitti vertailuindeksinsä (nousuehdokas) tai
     hävisi sille (kuplavaroitus). Tekoäly ei pisteytä itseään.
  4. Tekoälyn valintojen rinnalle jäädytetään joka viikko pelkän momentum-säännön valinnat.
     Näin nähdään, tuoko tekoäly lisäarvoa yksinkertaiseen sääntöön verrattuna.
"""
import datetime as dt
import io
import json
import os
import time

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
NL_DIR = os.path.join(HERE, "newsletter")
UNIVERSE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
UNIVERSE_CACHE = os.path.join(DATA_DIR, "universe_sp500.csv")
LEDGER = os.path.join(NL_DIR, "ennusteet.json")

BENCH_EQ = "^GSPC"          # osakkeiden ja ETF:ien vertailuindeksi
BENCH_CRYPTO = "BTC-USD"    # kryptojen vertailukohta (bitcoinin omana vertailuna S&P 500)
MAX_LESSONS = 12

SECTOR_FI = {
    "Information Technology": "Teknologia", "Communication Services": "Viestintäpalvelut",
    "Consumer Discretionary": "Syklinen kulutus", "Consumer Staples": "Päivittäistavarat",
    "Health Care": "Terveydenhuolto", "Financials": "Rahoitus", "Industrials": "Teollisuus",
    "Energy": "Energia", "Materials": "Materiaalit", "Utilities": "Sähkö ja vesi", "Real Estate": "Kiinteistöt",
}
SECTOR_ETFS = [("XLK", "Teknologia"), ("XLC", "Viestintäpalvelut"), ("XLY", "Syklinen kulutus"),
               ("XLP", "Päivittäistavarat"), ("XLV", "Terveydenhuolto"), ("XLF", "Rahoitus"),
               ("XLI", "Teollisuus"), ("XLE", "Energia"), ("XLB", "Materiaalit"),
               ("XLU", "Sähkö ja vesi"), ("XLRE", "Kiinteistöt")]
THEME_ETFS = [("SMH", "Puolijohteet"), ("IGV", "Ohjelmistot"), ("XBI", "Biotekniikka"),
              ("ARKK", "Spekulatiivinen innovaatio"), ("ITA", "Puolustusteollisuus"), ("KRE", "Aluepankit"),
              ("XHB", "Asuntorakentaminen"), ("URA", "Uraani"), ("TAN", "Aurinkoenergia"),
              ("GDX", "Kultakaivokset"), ("QQQ", "Nasdaq-100"), ("IWM", "Pienyhtiöt (Russell 2000)"),
              ("TLT", "Pitkät USA:n valtionlainat"), ("GLD", "Kulta")]
CRYPTO = [("BTC-USD", "Bitcoin"), ("ETH-USD", "Ether"), ("SOL-USD", "Solana"), ("XRP-USD", "XRP"),
          ("DOGE-USD", "Dogecoin")]


def _warn(msg: str) -> None:
    print("VAROITUS:", msg, flush=True)


# ----------------------------------------------------------------------------
# Universumi ja hinnat
# ----------------------------------------------------------------------------
def load_universe(offline: bool = False) -> pd.DataFrame:
    """Sarakkeet: tunnus (Yahoo-muoto), nimi, sektori, luokka (osake/sektori/teema/krypto)."""
    raw = None
    if not offline:
        try:
            r = requests.get(UNIVERSE_URL, timeout=60)
            r.raise_for_status()
            raw = pd.read_csv(io.StringIO(r.text))[["Symbol", "Security", "GICS Sector"]]
            if len(raw) > 400:
                os.makedirs(DATA_DIR, exist_ok=True)
                raw.to_csv(UNIVERSE_CACHE, index=False)
            else:
                raw = None
        except Exception as e:  # noqa: BLE001
            _warn(f"S&P 500 -listan haku epäonnistui, käytetään välimuistia: {str(e)[:120]}")
    if raw is None:
        raw = pd.read_csv(UNIVERSE_CACHE)
    stocks = pd.DataFrame({
        "tunnus": raw["Symbol"].astype(str).str.replace(".", "-", regex=False),
        "nimi": raw["Security"].astype(str),
        "sektori": raw["GICS Sector"].map(SECTOR_FI).fillna(raw["GICS Sector"]),
        "luokka": "osake",
    })
    extra = ([{"tunnus": t, "nimi": n, "sektori": n, "luokka": "sektori"} for t, n in SECTOR_ETFS]
             + [{"tunnus": t, "nimi": n, "sektori": n, "luokka": "teema"} for t, n in THEME_ETFS]
             + [{"tunnus": t, "nimi": n, "sektori": "Krypto", "luokka": "krypto"} for t, n in CRYPTO])
    return pd.concat([stocks, pd.DataFrame(extra)], ignore_index=True).drop_duplicates("tunnus")


def download_prices(tickers: list[str], batch: int = 100) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(päätöskurssit, avauskurssit), osinko- ja splittikorjatut; rivit = päivät, sarakkeet = tunnukset."""
    import yfinance as yf
    frames = []
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        for attempt in range(2):
            try:
                df = yf.download(chunk, period="1y", interval="1d", auto_adjust=True,
                                 progress=False, threads=True, group_by="column")
                close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": chunk[0]})
                opn = df["Open"] if isinstance(df.columns, pd.MultiIndex) else df[["Open"]].rename(columns={"Open": chunk[0]})
                frames.append((close, opn))
                break
            except Exception as e:  # noqa: BLE001
                _warn(f"hintahaku erä {i // batch + 1} epäonnistui ({str(e)[:80]}), yritetään uudelleen")
                time.sleep(5)
        time.sleep(1)
    if not frames:
        raise RuntimeError("yhtään hintaerää ei saatu")
    def tidy(df: pd.DataFrame) -> pd.DataFrame:
        idx = pd.to_datetime(df.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        df.index = idx.normalize()
        df = df.loc[:, ~df.columns.duplicated()].sort_index()
        return df.apply(pd.to_numeric, errors="coerce")
    return tidy(pd.concat([c for c, _ in frames], axis=1)), tidy(pd.concat([o for _, o in frames], axis=1))


def _close_on(prices: pd.DataFrame, when: pd.Timestamp) -> pd.Series:
    """Kunkin tunnuksen viimeisin päätöskurssi annettuna päivänä tai sitä ennen."""
    p = prices[prices.index <= when]
    return p.ffill().iloc[-1] if not p.empty else pd.Series(dtype=float)


def _fresh(prices: pd.DataFrame, when: pd.Timestamp, max_age_days: int = 4) -> pd.Index:
    """Tunnukset, joilla on hinta enintään max_age_days vanhana ennen päivää when."""
    p = prices[(prices.index <= when) & (prices.index > when - pd.Timedelta(days=max_age_days + 1))]
    return p.columns[p.notna().any()]


# ----------------------------------------------------------------------------
# Viikon liikkujat, sektorit ja ehdokasseula
# ----------------------------------------------------------------------------
def features(prices: pd.DataFrame, week_end: dt.date) -> pd.DataFrame:
    end = pd.Timestamp(week_end)
    p = prices[prices.index <= end].ffill()
    ok = _fresh(prices, end)
    p = p[ok]
    last = p.iloc[-1]

    def ret(days):
        base = _close_on(p, end - pd.Timedelta(days=days))
        return (last / base - 1) * 100

    year = p[p.index > end - pd.Timedelta(days=365)]
    ma200 = p.rolling(200, min_periods=150).mean().iloc[-1]
    daily = p.pct_change(fill_method=None).iloc[-21:]
    f = pd.DataFrame({
        "hinta": last,
        "r1w": ret(7), "r4w": ret(28), "r12w": ret(84),
        "huipusta": (last / year.max() - 1) * 100,
        "yli_200pv": (last / ma200 - 1) * 100,
        "vol20": daily.std() * np.sqrt(252) * 100,
    })
    return f.replace([np.inf, -np.inf], np.nan)


def movers(feat: pd.DataFrame, uni: pd.DataFrame, n: int = 5) -> dict:
    s = uni[uni["luokka"] == "osake"].set_index("tunnus")
    f = feat.join(s[["nimi", "sektori"]], how="inner").dropna(subset=["r1w"])
    row = lambda t, r: {"tunnus": t, "nimi": r["nimi"], "sektori": r["sektori"],
                        "viikko_pct": round(float(r["r1w"]), 1), "hinta": round(float(r["hinta"]), 2)}
    up = f.sort_values("r1w", ascending=False).head(n)
    down = f.sort_values("r1w", ascending=True).head(n)
    return {"nousijat": [row(t, r) for t, r in up.iterrows()], "laskijat": [row(t, r) for t, r in down.iterrows()]}


def sectors(feat: pd.DataFrame) -> list[dict]:
    out = []
    for t, name in SECTOR_ETFS:
        if t in feat.index and pd.notna(feat.loc[t, "r1w"]):
            out.append({"tunnus": t, "nimi": name, "viikko_pct": round(float(feat.loc[t, "r1w"]), 1),
                        "kk_pct": None if pd.isna(feat.loc[t, "r4w"]) else round(float(feat.loc[t, "r4w"]), 1)})
    return sorted(out, key=lambda x: -x["viikko_pct"])


def _item(t: str, r: pd.Series, meta: pd.DataFrame) -> dict:
    m = meta.loc[t] if t in meta.index else {"nimi": t, "sektori": "", "luokka": ""}
    d = {"tunnus": t, "nimi": m["nimi"], "sektori": m["sektori"], "luokka": m["luokka"]}
    for k in ("r1w", "r4w", "r12w", "huipusta", "yli_200pv", "vol20"):
        if pd.notna(r.get(k)):
            d[k] = round(float(r[k]), 1)
    return d


def candidates(feat: pd.DataFrame, uni: pd.DataFrame) -> dict:
    """Seulotut ehdokkaat tekoälylle. Tekoäly saa valita myös muita universumin kohteita."""
    meta = uni.set_index("tunnus")
    f = feat.join(meta[["luokka"]], how="inner")
    st = f[f["luokka"] == "osake"].dropna(subset=["r12w", "yli_200pv"])
    mom = st[(st["r12w"] > 0) & (st["yli_200pv"] > 0) & (st["r1w"] < 25)].sort_values("r12w", ascending=False).head(15)
    near = st[(st["huipusta"] > -2) & (st["r4w"] > 5)].sort_values("r4w", ascending=False).head(8)
    hot = f.dropna(subset=["yli_200pv"]).sort_values("yli_200pv", ascending=False).head(10)
    etf = f[f["luokka"].isin(["sektori", "teema"])]
    cry = f[f["luokka"] == "krypto"]
    return {
        "momentum": [_item(t, r, meta) for t, r in mom.iterrows()],
        "lahella_vuoden_huippua": [_item(t, r, meta) for t, r in near.iterrows()],
        "ylikuumentuneet": [_item(t, r, meta) for t, r in hot.iterrows()],
        "sektorit_ja_teemat": [_item(t, r, meta) for t, r in etf.sort_values("r4w", ascending=False).iterrows()],
        "krypto": [_item(t, r, meta) for t, r in cry.iterrows()],
    }


def rule_picks(feat: pd.DataFrame, uni: pd.DataFrame) -> list[dict]:
    """Pelkkä momentum-sääntö (vertailukohta tekoälylle, ja varavalinnat jos tekoäly ei vastaa):
    3 vahvinta 12 viikon momentum-osaketta eri sektoreilta + ylikuumentunein osake varoitukseksi."""
    meta = uni.set_index("tunnus")
    st = feat.join(meta, how="inner")
    st = st[st["luokka"] == "osake"].dropna(subset=["r12w", "yli_200pv", "r1w"])
    picks, used = [], set()
    for t, r in st[(st["yli_200pv"] > 0) & (st["r1w"] < 25)].sort_values("r12w", ascending=False).iterrows():
        if r["sektori"] in used:
            continue
        used.add(r["sektori"])
        picks.append({"tunnus": t, "tyyppi": "nousu",
                      "perustelu": f"Vahva 12 viikon nousu ({fmt_pct(r['r12w'])}) ja kurssi "
                                   f"{fmt_pct(r['yli_200pv'], sign=False)} yli 200 päivän keskiarvon.",
                      "laukaisija": "Trendin jatkuminen.", "riski": "Momentum voi kääntyä nopeasti."})
        if len(picks) == 3:
            break
    hot = st[~st.index.isin([p["tunnus"] for p in picks])].sort_values("yli_200pv", ascending=False)
    if not hot.empty:
        t, r = hot.index[0], hot.iloc[0]
        picks.append({"tunnus": t, "tyyppi": "varoitus",
                      "perustelu": f"Kurssi on {fmt_pct(r['yli_200pv'], sign=False)} yli 200 päivän keskiarvon, "
                                   "mikä on universumin ylikuumentunein lukema.",
                      "laukaisija": "Voitonotot.", "riski": "Ylikuumentunut kurssi voi silti jatkaa nousuaan."})
    return picks


def fmt_pct(x: float, dec: int = 1, sign: bool = True) -> str:
    s = f"{abs(x):.{dec}f}".replace(".", ",")
    pre = ("+" if x > 0 else ("−" if x < 0 else "")) if sign else ("−" if x < 0 else "")
    return f"{pre}{s} %"


# ----------------------------------------------------------------------------
# Ennusteiden kirjanpito
# ----------------------------------------------------------------------------
def load_ledger() -> dict:
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("ennusteet", [])
        d.setdefault("viikot", {})
        d.setdefault("opit", [])
        return d
    return {"ennusteet": [], "viikot": {}, "opit": []}


def save_ledger(led: dict) -> None:
    os.makedirs(NL_DIR, exist_ok=True)
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=1)


def benchmark_for(ticker: str, luokka: str) -> str:
    if luokka == "krypto":
        return BENCH_EQ if ticker == BENCH_CRYPTO else BENCH_CRYPTO
    return BENCH_EQ


def _first_open_after(opens: pd.DataFrame | None, closes: pd.DataFrame, t: str, after: pd.Timestamp):
    """(hinta, päivä): ensimmäinen avauskurssi jäädytyspäivän jälkeen. Jos avauskursseja ei ole
    (testidata), käytetään saman päivän päätöskurssia."""
    src = opens if (opens is not None and t in opens.columns) else closes
    s = src[t].loc[src.index > after].dropna() if t in src.columns else pd.Series(dtype=float)
    return (None, None) if s.empty else (float(s.iloc[0]), s.index[0])


def score_open(led: dict, prices: pd.DataFrame, opens: pd.DataFrame | None = None) -> list[dict]:
    """Pisteyttää avoimet ennusteet, joiden arviointiperjantai on saavutettu. Lähtöhinta on
    jäädytystä seuraavan kaupankäyntipäivän avaus, loppuhinta arviointiperjantain päätös."""
    closed = []
    last_day = prices.index.max()
    for e in led["ennusteet"]:
        if e.get("tila") != "auki":
            continue
        ev = pd.Timestamp(e["arvioidaan"])
        if last_day < ev:
            continue                                    # arviointiviikko ei ole vielä päättynyt
        made = pd.Timestamp(e["tehty"])
        t, b = e["tunnus"], e["vertailu"]
        (p0, d0), (b0, db0) = _first_open_after(opens, prices, t, made), _first_open_after(opens, prices, b, made)
        end = _close_on(prices, ev)
        fresh = _fresh(prices, ev)
        if (p0 is None or b0 is None or d0 > ev or db0 > ev or t not in fresh or b not in fresh
                or pd.isna(end.get(t)) or pd.isna(end.get(b))):
            e.update({"tila": "mitatoity", "huom": "hintaa ei saatu lähtö- tai arviointipäivälle"})
            continue
        r = (float(end[t]) / p0 - 1) * 100
        rb = (float(end[b]) / b0 - 1) * 100
        ex = r - rb
        hit = ex > 0 if e["tyyppi"] == "nousu" else ex < 0
        e.update({"tila": "suljettu", "lahtohinta": round(p0, 4), "lahto_pvm": str(d0.date()),
                  "vertailun_lahto": round(b0, 4), "loppuhinta": round(float(end[t]), 4),
                  "tulos_pct": round(r, 2), "vertailu_pct": round(rb, 2), "ylituotto_pct": round(ex, 2),
                  "osuma": bool(hit)})
        closed.append(e)
    return closed


def freeze(led: dict, picks: list[dict], prices: pd.DataFrame, uni: pd.DataFrame, wk: dict,
           week_id: str, number: int, run_date: dt.date, source: str) -> list[dict]:
    """Jäädyttää viikon ennusteet. Ei koskaan kirjoita olemassa olevia yli, eikä tee uusia arkipäivänä
    (silloin viikon kurssiliikkeet olisivat jo tiedossa, mutta lähtöhinta olisi perjantailta)."""
    existing = [e for e in led["ennusteet"] if e["viikko_id"] == week_id and e.get("lahde") == source]
    if existing:
        return existing
    if run_date.weekday() < 5:
        _warn("uusia ennusteita ei jäädytetä arkipäivänä (pörssit auki) — tutka julkaistaan lauantain numerossa")
        return []
    end = pd.Timestamp(wk["loppu"])
    close = _close_on(prices, end)
    fresh = _fresh(prices, end)
    meta = uni.set_index("tunnus")
    out = []
    for p in picks:
        t = p["tunnus"]
        if t not in meta.index or t not in fresh or pd.isna(close.get(t)):
            _warn(f"ennuste {t} hylätty: ei universumissa tai ei tuoretta hintaa")
            continue
        luokka = meta.loc[t, "luokka"]
        b = benchmark_for(t, luokka)
        if b not in fresh or pd.isna(close.get(b)):
            _warn(f"ennuste {t} hylätty: vertailuindeksin {b} hinta puuttuu")
            continue
        e = {"numero": number, "viikko_id": week_id, "lahde": source, "tehty": run_date.isoformat(),
             "tunnus": t, "nimi": meta.loc[t, "nimi"], "sektori": meta.loc[t, "sektori"], "luokka": luokka,
             "tyyppi": p["tyyppi"], "perustelu": p.get("perustelu", ""), "laukaisija": p.get("laukaisija", ""),
             "riski": p.get("riski", ""), "kurssi_jaadytettaessa": round(float(close[t]), 4),
             "lahto": "seuraavan kaupankäyntipäivän avaus", "vertailu": b,
             "arvioidaan": (wk["loppu"] + dt.timedelta(days=7)).isoformat(), "tila": "auki"}
        led["ennusteet"].append(e)
        out.append(e)
    return out


def stats(led: dict, source: str | None = "ai") -> dict:
    rows = [e for e in led["ennusteet"] if e.get("tila") == "suljettu" and (source is None or e.get("lahde") == source)]
    if not rows:
        return {"n": 0}
    hits = sum(e["osuma"] for e in rows)
    by = {}
    for typ in ("nousu", "varoitus"):
        r = [e for e in rows if e["tyyppi"] == typ]
        if r:
            by[typ] = {"n": len(r), "osumat": sum(e["osuma"] for e in r),
                       "keskim_ylituotto_pct": round(float(np.mean([e["ylituotto_pct"] for e in r])), 2)}
    best = max(rows, key=lambda e: e["ylituotto_pct"] if e["tyyppi"] == "nousu" else -e["ylituotto_pct"])
    return {"n": len(rows), "osumat": hits, "osumaprosentti": round(100 * hits / len(rows), 1),
            "viikkoja": len({e["viikko_id"] for e in rows}), "tyypeittain": by,
            "paras": {k: best[k] for k in ("tunnus", "nimi", "tyyppi", "ylituotto_pct", "viikko_id")}}


def add_lesson(led: dict, text: str, number: int, run_date: dt.date) -> None:
    text = (text or "").strip()
    if not text:
        return
    led["opit"] = [o for o in led["opit"] if o.get("numero") != number] + [
        {"numero": number, "pvm": run_date.isoformat(), "oppi": text}]
    led["opit"] = led["opit"][-MAX_LESSONS:]
