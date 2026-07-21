# Quota-aware orchestration prompt

Paste the block below into an orchestrator agent's instructions (or a project
`CLAUDE.md`) to make it budget its own subagent launches.

It depends on `budget_check.py` from this repo, which returns a verdict and an
exit code (`0` GO, `1` CAUTION, `2` STOP, `3` UNKNOWN).

---

## Kvótatudatos munkavégzés

Ez a gép figyeli a Claude-előfizetés terhelését. Mielőtt nagy feladatba kezdesz,
kérdezd meg a keret állását, és igazodj hozzá. A parancs nem kerül tokenbe és egy
másodperc alatt lefut:

```sh
python3 ~/claude-usage-status/budget_check.py --brief
```

Példa válasz: `CAUTION | live | session 76% | weekly 13% | burn 1.6x (…)`

### Mindig friss adatból dolgozz

A parancs **kétféle forrásból** tud dolgozni, és a válaszban mindig kiírja, melyikből:

- `live` — élő lekérdezés a szervertől. Ez a jó eset, nincs teendőd.
- `CACHED ... m old` — nincs élő hozzáférés (jellemzően ha nem azon a gépen
  futsz, ahol a bejelentkezés van), ezért a repóba commitolt pillanatképet
  olvassa.

**Ha `CACHED`-et látsz, előbb frissíts, és csak utána dönts:**

```sh
git -C ~/claude-usage-status pull --quiet
python3 ~/claude-usage-status/budget_check.py --brief
```

A pillanatkép ötpercenként frissül a forrásgépen és felkerül a repóba, tehát egy
`git pull` szinte mindig friss adatot hoz. Ha a `git pull` után is `CACHED`
marad, az rendben van — a lényeg, hogy ne órákkal korábbi számból dolgozz.

A parancs magától is véd: ha a pillanatkép 5 órás ablaka időközben lejárt, nem
mondja meg a régi százalékot, hanem `session unknown (stale)` jelzést ad, és
sosem enged GO-t ilyenkor. Ha a pillanatkép 90 percnél régebbi, a verdikt
`UNKNOWN` lesz. **Ne értelmezd a régi számot friss adatként** — ha bizonytalan
vagy, kérdezd meg a felhasználót, mit mutat nála a `/usage`.

### Mikor ellenőrizd

- **Minden munkamenet elején**, mielőtt tervet készítesz.
- **Minden subagent-indítás előtt**, különösen ha többet indítanál párhuzamosan.
- **Fázisok között** (pl. felderítés → implementáció → tesztelés → review).
- **Ha egy subagent hibával tér vissza** — lásd lentebb, ez a legfontosabb pont.

### Mit jelentenek a verdiktek

**GO** — Nincs korlátozás. Indíthatsz párhuzamos subagenteket, futtathatsz teljes
repó-átvizsgálást, mehet a szokásos több-ágenses munkafolyamat.

**CAUTION** — Van keret, de fogy. Ilyenkor:
- Ne indíts párhuzamos fan-outot; egyszerre egy subagent dolgozzon.
- Szűkítsd a hatókört: konkrét fájlok a teljes repó helyett, célzott grep a
  mindent-beolvasás helyett.
- Mechanikus munkára (formázás, átnevezés, egyszerű teszt-scaffold) válassz
  olcsóbb modellt.
- Ami már fut, azt fejezd be — ne szakítsd félbe pánikszerűen.
- Halaszd a ráérős dolgokat (nagy refaktor, dokumentáció-átírás, teljes
  tesztlefedettség-emelés) a keret nullázódása utánra.

**STOP** — Ne kezdj új munkát. Ehelyett:
1. Hozd az éppen futó változtatást **konzisztens állapotba** — félbehagyott
   refaktor, importálatlan új fájl, felében átírt teszt ne maradjon.
2. Commitold a munkát egy WIP branchre beszédes üzenettel.
3. Írj egy rövid átadó összefoglalót: mi készült el, mi maradt hátra, mi a
   következő lépés.
4. Mondd meg a felhasználónak, mikor nullázódik a keret (a parancs kiírja), és
   állj meg. Ne indíts új subagentet, ne kezdj új fájlt.

**UNKNOWN** — A kvóta nem volt lekérdezhető, vagy a pillanatkép túl régi.
Először próbáld a fenti `git pull`-t. Ha utána is UNKNOWN, dolgozz CAUTION
szabályok szerint, és mondd meg a felhasználónak, hogy a keretfigyelés épp nem
lát — ilyenkor ő tud pontos számot adni a `/usage` parancsból.

### Ha egy subagent hibával tér vissza — FONTOS

Rate limit hiba **nem kódhiba**. Ha egy subagent elszáll, mielőtt bármit
javítanál, futtasd le a `budget_check.py`-t.

- Ha a verdikt **STOP**, vagy a hibaüzenetben `rate limit`, `usage limit`, `429`
  vagy `quota` szerepel: a kód valószínűleg **rendben van**. Ne írd át a
  forrást, ne lazíts a teszteken, ne kezdj újrapróbálkozási ciklusba — azzal
  csak tovább égeted a keretet, és elrontasz működő kódot egy nem létező hiba
  miatt. Ehelyett: checkpointolj a fenti STOP eljárás szerint, és szólj.
- Csak akkor kezdj hibakeresésbe, ha a keret rendben van, tehát a hiba tényleg a
  kódból jön.

### Menet közbeni megszakítás

Hosszú futásnál a keret elfogyhat menet közben. Ez megengedett, sőt elvárt:
**jobb rendezetten megállni, mint hibára futni.** Ha egy fázis végén STOP-ot
kapsz, ne kezdd el a következő fázist — zárd le tisztán a fentiek szerint. A
felhasználó a nullázódás után folytatni tudja onnan, ahol abbahagytad.

### Profil-címke a kimenetben

Ha a válaszban látsz egy `[conserve]` vagy `[greedy]` címkét, az a felhasználó
által beállított **költési politika**, nem az, hogy a fiók kimerülőben van. A
`conserve` szándékosan korán mond STOP-ot (a felhasználó tartalékot akar hagyni),
a `greedy` szándékosan sokáig enged. Címke nélkül a kiegyensúlyozott alapértelmezés
fut. A verdiktet mindig kövesd — a címke csak azt magyarázza, miért ott a küszöb.

Ne kérdezz rá minden ellenőrzés eredményére; csak akkor jelezz, ha a verdikt
CAUTION-re vagy STOP-ra vált, vagy ha emiatt megváltoztatod a tervet.
