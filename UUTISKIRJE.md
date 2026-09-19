# Kuplamittari-uutiskirje 2.0 — käyttöönotto ja toiminta

Joka lauantaiaamu sähköpostiisi tulee **Kuplamittari**. Siinä on viikon pörssitapahtumat,
viikon nousijat ja laskijat, sektorikartta, **tutkan nousuehdokkaat ja kuplavaroitus** ensi
viikolle, **tuloskortti** viime viikon ennusteiden osumisesta sekä selkokielinen analyysi
siitä, mitä kuplamittari näyttää. Viikon kuplalukema (esim. 81/100) tulee lopussa.

Tekstit kirjoittaa tekoäly (Claude), joka hakee joka viikko tuoreet uutiset. **Kaikki luvut**
laskee kuitenkin Python: taulukot, mittarit, tuloskortin pisteet ja kuplalukeman. Tekoäly ei
voi muuttaa niitä, ei väittää ennätystä ilman dataa eikä pisteyttää omia ennusteitaan.

---

## Käyttöönotto (n. 20 min)

### 1. Anthropic API -avain (5 min)

1. Mene osoitteeseen **platform.claude.com** ja luo tili.
2. **Osta krediittiä:** Settings → Billing → **Buy credits**. **10 $ riittää noin 4 kuukaudeksi.**
   **Älä kytke automaattista lisälatausta (Auto-reload) päälle.** Silloin ostettu summa on
   samalla kulukatto.
3. **Luo avain:** Settings → API keys → **Create key**. Nimi `kuplamittari`, voimassaolo
   mahdollisimman pitkä. Kopioi avain heti (alkaa `sk-ant-`), koska se näytetään vain kerran.

### 2. Avain GitHubiin (1 min)

Repossa: **Settings → Secrets and variables → Actions → New repository secret**
- Name: `ANTHROPIC_API_KEY`, Value: kopioimasi avain.

### 3. Tiedostot repoon (5 min)

| Tiedosto | Mitä tehdä |
|---|---|
| `kupla.py` | **korvaa** (v0.9: vie luvut uutiskirjeelle) |
| `newsletter.py` | **korvaa** tai lisää (uutiskirje 2.0) |
| `tutka.py` | **uusi**, lisää juureen |
| `data/universe_sp500.csv` | **uusi**, lisää `data`-kansioon (S&P 500 -lista varmuuskopioksi) |
| `.github/workflows/weekly.yml` | **korvaa** (lauantaiaamu) |
| `tests/` | valinnainen: testit |

Juuren tiedostot: **Add file → Upload files**, raahaa `kupla.py`, `newsletter.py` ja `tutka.py`,
ja paina **Commit changes**. `universe_sp500.csv`: avaa ensin repon `data`-kansio ja lataa
tiedosto siellä samalla tavalla. Workflow: avaa `.github/workflows/weekly.yml` → kynäkuvake →
Ctrl+A → liitä uusi sisältö → **Commit changes**.

### 4. Ensimmäinen ajo (5 min)

**Actions** → vasemmalta **Kuplaindikaattori** → **Run workflow** → **Run workflow**.
(Älä käytä vanhan ajon Re-run-nappia, koska se ajaa vanhaa koodia.) Ajo kestää 5–10 minuuttia:
se hakee noin 530 osakkeen, ETF:n ja kryptovaluutan kurssit, ja tekoäly tekee kaksi
tutkimuskierrosta.

**Aja ensimmäinen kerta viikonloppuna.** Tutka tekee uusia ennusteita vain silloin, kun
pörssit ovat kiinni (ks. alla). Arkipäivän ajo tuottaa muuten täyden kirjeen, mutta tutkan
kohdalla lukee "julkaistaan lauantain numerossa".

---

## Kirjeen sisältö

1. **Otsikko ja ingressi**: viikon tarina ja lista siitä, mitä numerossa on
2. **Viikon luku**: yksi pysäyttävä luku tarinoineen
3. **Viikko lyhyesti**: 4–6 tärkeintä tapahtumaa
4. **Markkinat numeroina**: USA, Eurooppa, OMXH25, korko, VIX, EUR/USD, öljy, kulta, bitcoin
5. **Sektorit viikolla**: S&P 500:n 11 sektoria lämpökarttana
6. **Viikon nousijat ja laskijat**: S&P 500:n suurimmat liikkujat ja niiden syyt
7. **Tuloskortti**: viime viikon ennusteet ✓/✗, kertymä, tekoälyn itsearvio ja **opittu asia**
8. **Tutka**: 3 nousuehdokasta ja 1 kuplavaroitus ensi viikolle perusteluineen, laukaisijoineen ja riskeineen
9. **Mitä Kuplamittari näkee**: arvostus, velkavipu sekä raha ja velka
10. **Toistaako historia itseään?**: vertailu aiempiin kupliin
11. **Ensi viikolla seurataan**: kalenteri ja tulosjulkistukset
12. **Sijoittajan minuutti**: yksi käsite selitettynä, joka viikko eri aihe
13. **Viikon kuplalukema**: lopussa, muutos viime viikosta

---

## Tutka ja tuloskortti — näin rehellisyys on varmistettu

Nämä säännöt on koodattu `tutka.py`:hyn, eikä niitä voi ohittaa:

1. **Ennusteet jäädytetään ennen viikon alkua** tiedostoon `newsletter/ennusteet.json`, ja se
   tallentuu repoon. Gitin versiohistoria todistaa, milloin kukin ennuste tehtiin.
2. **Uusia ennusteita tehdään vain viikonloppuna.** Arkipäivänä viikon kurssiliikkeet olisivat
   jo tiedossa.
3. **Saman viikon ennusteita ei kirjoiteta koskaan yli.** Uusinta-ajo käyttää jo jäädytettyjä.
4. **Lähtöhinta on maanantain avauskurssi** (kryptoilla ensimmäinen avaus jäädytyksen jälkeen),
   eli se hinta, jonka lukija olisi oikeasti saanut. Näin viikonlopun uutiset, jotka tekoäly
   on voinut lukea, eivät paisuta tulosta.
5. **Python pisteyttää mekaanisesti.** Nousuehdokas osuu, jos se voittaa vertailunsa
   (S&P 500, kryptoilla bitcoin). Kuplavaroitus osuu, jos se häviää vertailulleen. Pelkkä
   nousu ei riitä, koska nousevassa markkinassa lähes kaikki nousee.
6. **Tekoälyä verrataan yksinkertaiseen sääntöön.** Joka viikko jäädytetään myös pelkän
   momentum-säännön valinnat, jotka lukija ei näe. Tuloskortti kertoo, voittaako tekoäly
   säännön. Se on paras mittari sille, tuoko tekoäly oikeasti lisäarvoa.

**Näin oppiminen toimii:** tekoäly saa joka viikko koko tuloshistorian, omat perustelunsa ja
tähänastiset opit. Se kirjoittaa itsearvion ja tiivistää yhden uuden opin. Opit (12 viimeisintä)
kulkevat mukana jokaisessa tulevassa ennusteessa.

**Rehellinen odotus:** kukaan ei ennusta viikon nousijoita luotettavasti. Yhden viikon tulos on
paljolti sattumaa, ja osumatarkkuus jää todennäköisesti lähelle 50 prosenttia. Menetelmän
arvo näkyy vasta kymmenien viikkojen kertymästä. Juuri siksi kaikki tulokset näytetään.

---

## Kun jokin menee pieleen

Kirje ei jää koskaan kokonaan tulematta:

| Tilanne | Mitä tapahtuu |
|---|---|
| Tekoäly ei vastaa tai krediitti loppuu | Tekstit tulevat ilmaisesta pohjasta ja tutkan valinnat pelkästä momentum-säännöstä. Alatunnisteessa lukee "koottu automaattipohjalla". |
| Tekoälyn vastaus on puutteellinen | Puuttuvat osat täydennetään pohjasta. Tuntemattomat tunnukset hylätään, ja jos kelvollisia valintoja on liian vähän, sääntö valitsee. |
| Kurssien haku epäonnistuu | Tutka- ja sektoriosiot jätetään pois, muu kirje lähtee. |
| Mittarin laskenta kaatuu | Saat virheilmoituksen. Vanhoja lukuja ei koskaan lähetetä uusina. |

Virheen tiedot löytyvät kohdasta Actions → viimeisin ajo → **Kirjoita ja lähetä uutiskirje**.

## Kulut

Kaksi tekoälykutsua viikossa: arviolta **0,4–0,8 $ per numero, eli noin 2–3,5 € kuukaudessa.**
Jokaisen numeron hinta kirjautuu tiedostoon `newsletter/history.json`. Tarkempi erittely löytyy
kohdasta platform.claude.com → Usage. Halvempi vaihtoehto: lisää salaisuus `ANTHROPIC_MODEL`
arvolla `claude-haiku-4-5-20251001`. Se on noin puolet halvempi, mutta analyysi on ohuempaa.

## Lukijoiden lisääminen myöhemmin

Muuta salaisuus `EMAIL_TO` pilkulla erotetuksi listaksi. Useammalle vastaanottajalle kirje
lähtee **piilokopiona**. Vaihda samalla workflow'ssa `LIITA_RAPORTTI: "0"`.

## Ennen kaupallista julkaisua (muistilista)

- **Eturistiriidat:** EU:n markkinoiden väärinkäyttöasetus (MAR) vaatii sijoitussuositusten
  julkaisijalta tekijän tiedot ja eturistiriitojen kertomisen, esimerkiksi omistatko itse
  mainittuja osakkeita. Aseta ilmoitus workflow'hun ympäristömuuttujaksi `TUTKA_ILMOITUS`.
  Oletusteksti on "Kirje on toistaiseksi laatijan omaan käyttöön."
- **Juridinen tarkistus:** kysy juristilta tai Finanssivalvonnalta ennen maksullista
  jakelua. (En ole juristi.)
- **Jakelu:** Gmail ei sovellu isolle tilaajamäärälle. Maksullinen uutiskirjealusta hoitaa
  maksut, tilausten hallinnan ja tietosuojan.
- **Hinnoittelu:** 1 €/kk -maksusta korttimaksun kiinteä kulu vie noin neljänneksen.
  Vuosihinta toimii paremmin.
- **Uskottavuus:** tuloskortti ja `ennusteet.json` ovat valmis, tarkastettava näyttö
  menetelmästä. Anna sen kertyä muutama kuukausi ennen julkaisua.

## Testit (valinnainen)

```
python tests/test_tutka.py            # pisteytyksen tarkat laskut
python tests/test_newsletter.py       # uutiskirjeen osat
python tests/simulate_two_weeks.py    # kahden viikon simulaatio alusta loppuun
```

## Arkisto

Jokainen numero tallentuu repoon `newsletter/arkisto/VVVV-KK-PP.html`.
