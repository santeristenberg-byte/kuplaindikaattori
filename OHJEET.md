# Kuplaindikaattori — käyttöönotto (n. 15 min, ei maksullisia palveluita)

Lopputulos: joka maanantaiaamu sähköpostiisi tulee raportti ja kaavio. Historia
kertyy automaattisesti GitHubiin. Sinun ei tarvitse tehdä mitään käyttöönoton
jälkeen.

Tarvitset: GitHub-tilin (ilmainen) ja Gmail-osoitteen (lähettäjäksi; vastaanottaja
voi olla mikä tahansa osoite).

---

## 1. Luo GitHub-repo (3 min)

1. Mene osoitteeseen github.com ja luo tili, jos sinulla ei ole.
2. Oikea yläkulma **+ → New repository**.
3. Nimi: `kuplaindikaattori`. Valitse **Private**. Rastita **Add a README file**. **Create repository**.

## 2. Lataa tiedostot repoon (5 min)

Selaimessa, repon etusivulla:

1. **Add file → Upload files**. Raahaa ikkunaan nämä neljä tiedostoa:
   `kupla.py`, `requirements.txt`, `OHJEET.md`, `.gitignore`.
   Paina **Commit changes**.
2. Workflow-tiedosto pitää luoda käsin, koska selain ei lataa piilokansioita:
   **Add file → Create new file**. Kirjoita tiedostonimeksi täsmälleen

       .github/workflows/weekly.yml

   (GitHub luo kansiot automaattisesti kun kirjoitat kauttaviivat.)
   Avaa `weekly.yml` tästä paketista, kopioi koko sisältö laatikkoon, **Commit changes**.

## 3. Gmailin sovellussalasana (3 min)

Skripti lähettää postin Gmailin kautta. Gmail vaatii tähän erillisen
16-merkkisen *sovellussalasanan* — ei omaa salasanaasi.

1. Ota Google-tilillä käyttöön kaksivaiheinen tunnistus, jos ei ole:
   myaccount.google.com → Turvallisuus → Kaksivaiheinen vahvistus.
2. Mene osoitteeseen **myaccount.google.com/apppasswords**.
3. Anna nimeksi `kuplaindikaattori`, paina **Luo**. Kopioi 16-merkkinen koodi
   talteen (välilyönnit voi jättää pois).

## 4. Salaisuudet GitHubiin (2 min)

Repossa: **Settings → Secrets and variables → Actions → New repository secret**.
Luo kolme:

| Name | Value |
|---|---|
| `GMAIL_USER` | lähettävä Gmail-osoitteesi |
| `GMAIL_APP_PASSWORD` | 16-merkkinen sovellussalasana |
| `EMAIL_TO` | osoite, johon raportti tulee (voi olla sama) |
| `FRED_API_KEY` | *valinnainen* — ilmainen FRED-avain, ks. kohta "Jos jokin lähde ei vastaa" |

## 5. Ensimmäinen ajo (2 min)

1. Repossa **Actions**-välilehti. Jos GitHub kysyy, paina **I understand my workflows, go ahead and enable them**.
2. Vasemmalta **Kuplaindikaattori → Run workflow → Run workflow**.
3. Ajo kestää 1–2 min. Vihreä ruksi = raportti on sähköpostissasi.
   Punainen rasti = klikkaa ajoa auki, avaa vaihe *Aja indikaattori*, ja
   kopioi virheteksti minulle.

Tämän jälkeen ajo tapahtuu itsestään joka maanantai. Voit aina ajaa sen myös
käsin samasta napista.

---

## Mitä sähköpostissa lukee

```
KUPLARISKI        62.4 / 100   Kuplan esiaste  (+1.8 vs. edellinen ajo)
  Arvostus                 78.1   Vipu & likviditeetti 39.0
  Ääriarvoja               3 / 11 muuttujasta yli 90. persentiilin
MUUTTUJAT ...               persentiilit ja palkit
ANALOGIA ...                mitä huippua nykytila muistuttaa, ja miksi ei
```

- **Kuplariski** on painotettu keskiarvo muuttujien persentiileistä omaa
  historiaansa vasten. 60 = "kalliimpi kuin 60 % historiasta", ei "60 % todennäköisyys".
- **Otsikossa ⚠** = kuplariski on yli 75. **⚠ HÄLYTYS** = raja ylittyi juuri tällä viikolla.
- Liitteenä kaavio koko historiasta vuodesta 1975 ja kuplahuiput merkittyinä.
- Repon `data/`-kansioon kertyy `history.csv` (viikkorivit) ja
  `kuplariski_kuukausittain.csv` (koko kuukausisarja omia analyysejä varten).

## Jos jokin lähde ei vastaa

Skripti ei kaadu: puuttuva muuttuja jää pois, *datan kattavuus* laskee ja
raportin loppuun tulee VAROITUKSET-osio. Jokaisella lähteellä on kolme
tasoa: uudelleenyritys, varalähde ja välimuisti edellisestä onnistuneesta
ajosta (`data/fred_cache/`, `data/shiller_cache.csv`, `data/margin_debt_cache.csv`).
Välimuisti tallentuu repoon, joten yksittäinen katkos ei näy raportissa.

- **FRED aikakatkaisee** (yleistä kotiverkosta ja joistakin yritysverkoista):
  hanki ilmainen API-avain osoitteesta fred.stlouisfed.org/docs/api/api_key.html
  (vaatii FRED-tilin, 2 min) ja lisää se GitHubiin salaisuutena `FRED_API_KEY`.
  Avaimen kanssa skripti käyttää virallista rajapintaa, joka on luotettavin reitti.
- **Shiller**: CAPE lasketaan itse hinta-, tulos- ja CPI-sarakkeista, ja jos
  taulukko ei aukea, käytetään multpl.com:n kuukausitaulukkoa. Virhetilanteessa
  syntyy `outputs/shiller_debug.txt`, jonka voit lähettää minulle.
- **Ajo omalla koneella** (`python kupla.py --no-email`) toimii, mutta
  tarkoitettu ympäristö on GitHub Actions, jonka verkosta kaikki lähteet
  vastaavat yleensä ongelmitta.

## Muutettavat asetukset (kupla.py alussa)

- `ALERT_THRESHOLD` — hälytysraja (oletus 75)
- `PILLAR_WEIGHTS` — pilarien painot
- `MIN_HISTORY_MONTHS` — kuinka pitkä historia muuttujalla pitää olla (oletus 120 kk)
- Ajopäivä ja -aika: `.github/workflows/weekly.yml`, rivi `cron:`

## Kustannukset

Nolla. GitHub Actions antaa yksityisille repoille 2 000 minuuttia kuussa
ilmaiseksi; tämä käyttää noin 8. Kaikki datalähteet ovat julkisia.

Huom: GitHub pysäyttää ajastetun workflown, jos repossa ei ole tapahtunut
mitään 60 päivään. Bottikommitit pitävät sen hengissä, joten tämä ei
normaalisti koske sinua.
