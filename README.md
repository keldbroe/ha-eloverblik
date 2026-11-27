# ElOverblik (DK)

**[Dansk](#dansk) · [English](#english)**

## Dansk

Home Assistant-integration til [Energinet ElOverblik](https://eloverblik.dk/welcome). Den læser dine egne målepunkter fra den officielle Customer API og DataHub.

Brug den til at følge:

- **Forbrug** — strøm købt fra nettet
- **Salg / eksport** — strøm leveret til nettet, for eksempel fra solceller

Domænet er `eloverblik_dk`, så den kan installeres ved siden af andre ElOverblik-integrationer uden at overtage deres enheder.

Måledata hentes i DataHubs egen opløsning (`Actual`: time for time `PT1H`, og kvarter for kvarter `PT15M`, hvor målepunktet leverer det). Intervallerne lægges sammen til hele UTC-timer og importeres som langtidsstatistik i Home Assistant. I Energi-dashboardet tilføjer du forbruget under **Forbrug fra nettet** og salget under **Retur til nettet**.

### Opret et refresh-token

1. Åbn [eloverblik.dk/welcome](https://eloverblik.dk/welcome), og log ind med MitID.
2. Åbn **Datadeling**.
3. Opret et token, og medtag de målepunkter, Home Assistant skal læse. Tokenet gælder i op til et år.
4. Kopier refresh-tokenet, når det vises. Mister du det, så opret et nyt. Det adgangstoken, integrationen bytter det til, gemmes kun i hukommelsen og gælder cirka et døgn.

Refresh-tokenet gemmes i Home Assistants opsætning. Det skrives ikke i loggen.

### Installer fra Marketplace

I Home Assistant: gå til **Indstillinger → Marketplace → Tilføj fra link**, og skriv:

```text
keldbroe/ha-eloverblik
```

Kræver Home Assistant 2025.1.0 eller nyere. Integrationen er lavet til Home Assistant 2026.11 og virker også med nyere 2025- og 2026-versioner.

### Installer med HACS

1. Åbn **Integrationer** i HACS.
2. Vælg **Custom repositories**, og tilføj `https://github.com/keldbroe/ha-eloverblik` som integration.
3. Download **ElOverblik (DK)**.
4. Genstart Home Assistant.

### Opsætning

1. Gå til **Indstillinger → Enheder & tjenester → Tilføj integration**, og vælg **ElOverblik (DK)**.
2. Indsæt refresh-tokenet. Det tjekkes hos ElOverblik, før du går videre.
3. Vælg de målepunkter, der skal følges. Hver linje viser adressen, typen, for eksempel **Forbrug (D07)** eller **Salg/eksport (D06)**, og det 18-cifrede målepunktsnummer.

Når refresh-tokenet udløber, beder Home Assistant om et nyt.

#### Hvilket målepunkt skal jeg vælge?

| Det du vil have i Energi-dashboardet | Typisk DataHub-type | Vises som |
| --- | --- | --- |
| Forbrug fra nettet | `D07` (forbrug fra net), `D10` (net fra net), eller `E17` når det er det eneste forbrugsmålepunkt | Forbrug |
| Retur til nettet | `D06` (leveret til net), `D11` (net til net) eller `E18` (produktionsmålepunkt) | Salg/eksport |
| Solproduktion | `D01` (VE-produktion), `D09` (egenproduktion) | Produktion |

Kun netmålepunkter (Forbrug og Salg/eksport) er valgt på forhånd.

- **Produktion** er alt, hvad anlægget producerer, også det huset selv bruger. Det er ikke det samme som retur til nettet. Brug det under **Solpaneler** i Energi-dashboardet, ikke under **Retur til nettet**.
- Alle andre typer, for eksempel `D12` (samlet forbrug), `D14` (elvarme) eller `D15` (nettoforbrug), vises som **Andet (type)**. De overlapper netmålepunkterne, så vælger du dem ved siden af `D07`, tælles strømmen to gange.

Et overordnet `E17` har ofte undermålepunkterne `D07` og `D06`. Vælger du både det overordnede målepunkt og dets undermålepunkter, tælles den samme strøm to gange. Opsætningen lader derfor det overordnede målepunkt være fravalgt, når undermålepunkterne findes. Du kan stadig selv vælge det.

Et målepunkt, som en anden ElOverblik-opsætning allerede følger, vises ikke igen.

#### Skift målepunkter senere

**Indstillinger → Enheder & tjenester → ElOverblik (DK) → Konfigurer → Målepunkter** viser alle målepunkter, tokenet kan se. Her tilføjer eller fjerner du målepunkter. Du kan også åbne et målepunkts enhed og vælge **Slet**, så fravælges det. Et fjernet målepunkt mister sin enhed og sine sensorer. Den statistik, der allerede er importeret, bevares, og vælger du det igen, fortsætter statistikken.

#### To målepunkter med de samme tal

DataHub sender ofte præcis de samme timeværdier for et overordnet målepunkt og dets undermålepunkt, for eksempel `E17` og dets `D07`, eller `E18` og `D06`. Så viser to enheder de samme tal, og vælger du begge i Energi-dashboardet, tælles den samme strøm to gange.

**Normalt skal du ikke vælge begge.** Ved opsætning og under **Målepunkter** henter integrationen den seneste måned (ét ekstra kald) og skjuler et målepunkt, der har præcis de samme tal som et andet. Formularen fortæller, hvilke der er skjult. Sæt kun hak i **Vis alle målepunkter (normalt ikke nødvendigt)**, hvis du ved, at du har brug for netop det målepunkt. Så vises de skjulte igen, markeret "samme tal som D07" (eller D06).

Målepunkter, der allerede er valgt, sammenlignes også over den seneste måned. Er to af samme slags ens time for time i mindst en uge, kommer der en besked under **Indstillinger → Reparationer**. Den fortæller, hvilket målepunkt der skal beholdes, og hvilket der kan fjernes. **Ret** fjerner det. Undermålepunktet (`D07`, `D06`) beholdes, fordi det er den konkrete netmåling og typisk har længst historik. Begynder tallene senere at være forskellige, forsvinder beskeden af sig selv. Timer uden salg, som vinternætter, tæller ikke som ens.

#### Indstillinger

**Konfigurer** åbner en menu med **Målepunkter** (se ovenfor) og **Opdateringsinterval og statistik**:

- **Opdateringsinterval:** standard 6 timer, mindst 1 time. ElOverblik-data er forsinket, så hyppigere opdatering giver ikke nyere tal og bruger bare af grænsen for kald.
- **Statistikhistorik:** standard 180 dage (et halvt år), højst 1825 (5 år, inden for DataHubs opbevaring af de seneste fem kalenderår plus indeværende år). Det er, hvor langt tilbage statistikken skrives. Sætter du tallet op senere, hentes de ældre dage, og den akkumulerede sum skrives igen. Den første import af flere år hentes med 25 kald i minuttet, så den tager et par minutter. Det sker kun én gang: hvilken periode der er skrevet, huskes i `.storage`, så en genstart kun henter indeværende år og timerne efter den nyeste gemte. Sætter du tallet ned, bevares de timer, der allerede er gemt.
- **Importér statistik til Energi-dashboardet:** slået til som standard.

Sensorerne for måned og år bruger altid de data, der skal til for indeværende kalenderår, også når statistikhistorikken er kortere.

### Energi-dashboardet

Efter den første vellykkede opdatering:

1. Åbn **Indstillinger → Dashboards → Energi**.
2. Under **Elnet** tilføjer du forbrug fra nettet og vælger den statistik, hvis navn slutter med **forbrug (grid consumption)**.
3. Tilføj retur til nettet, og vælg den statistik, hvis navn slutter med **salg til net (return to grid)**.

Den statistik er time-for-time akkumulerede summer (`sum` og `state`) fra den eksterne statistik. Den er adskilt fra sensorerne for måned og år. Måned- og årssensorerne nulstilles med kalenderen og er til dashboards, ikke til Energi-dashboardet.

ElOverblik offentliggør normalt et døgn én til tre dage efter. Sensoren **Seneste datadato** og attributten `data_date` på **Seneste tilgængelige døgn** viser det nyeste døgn, DataHub faktisk har leveret. I dag og i går er ofte tomme. Det er DataHubs forsinkelse, ikke en fejl.

Når den valgte periode er hentet, tilføjer senere opdateringer nye hele timer. De sammenligner også de seneste 14 dage med det gemte. Har DataHub rettet en time eller leveret en time, der manglede, skrives serien igen fra den time til den nyeste med én sammenhængende sum. Ældre rettelser kræver **Genopbyg statistik**. En gemt time, som DataHub ikke længere leverer, beholder sin gemte værdi.

Sætter du **Statistikhistorik** op, hentes de manglende ældre måneder. Integrationen sletter den eksterne statistik og skriver én sammenhængende akkumuleret sum fra starten af den nye periode til den seneste hele time. Det samme kan gøres manuelt:

**Udviklerværktøjer → Handlinger → ElOverblik (DK): Genopbyg statistik** (`eloverblik_dk.rebuild_statistics`).

Handlingen bruger den valgte historik (som standard 180 dage). Fjerner du integrationen, slettes den importerede statistik ikke.

### Enheder

Hvert målepunkt er sin egen enhed.

| Sensor | Enhed | Bemærkninger |
| --- | --- | --- |
| Seneste tilgængelige døgn | kWh | Summen for det nyeste døgn med data. Attributter: `data_date`, `metering_point_type` (`consumption`, `export`, `production` eller `other`). |
| Denne måned | kWh | Kalendermåneden indtil nu. `total_increasing`. Kan være 0 i starten af en måned, mens data stadig er forsinket. |
| I år | kWh | Kalenderåret indtil nu. `total_increasing`. |
| Seneste interval | kWh | Det nyeste interval i målerens egen opløsning (`PT1H` eller `PT15M`). Attributter: `resolution`, `period_start`, `period_end`. Unikt id: `{metering_point_id}_latest_hour`. |
| Seneste datadato | dato | Diagnostik. Det nyeste døgn i de hentede data. |

Tidsserierne hentes med aggregeringen `Actual`. DataHub leverer `PT1H` for timemålere og `PT15M`, hvor der offentliggøres kvartersværdier. Kommer der en hel døgnblok (`P1D`), fordeles den jævnt over timerne. Kvarterer lægges sammen i den UTC-time, de starter i (`XX:00:00`), som er det format, Home Assistants recorder accepterer for langtidsstatistik. Huller lades være tomme. Kvalitetskoden `A02` (ikke tilgængelig) springes over.

### Sådan bruges API'et

- `GET /api/token` bytter refresh-tokenet til et adgangstoken. GetToken er begrænset til 2 kald i minuttet, så tokenet gemmes i hukommelsen indtil fem minutter før det udløber, og fornyes én gang efter HTTP 401. Opsætningen, første start og senere genindlæsninger deler det, så en ændret indstilling ikke bruger et GetToken-kald.
- `GET /api/meteringpoints/meteringpoints?includeAll=false` henter de tilknyttede målepunkter, også undermålepunkter.
- `POST /api/meterdata/gettimeseries/{from}/{to}/Actual` henter serien i målerens egen opløsning. Kaldene samles i bundter af 10 målepunkter og 31 dage. 180 dage er nogle få kald, 730 dage er 24 kald, og 1825 dage er 59 kald.
- DataHub leverer tidsserier for de seneste fem kalenderår plus indeværende år, fra 1. januar i det første år. En tidligere start flyttes frem til den dag. En periode, der slutter før, springes over.
- Klienten holder også et lokalt loft på 25 kald i minuttet. En import, der kræver flere, venter til næste minut, med et sekunds margen. En femårsimport på cirka 59 kald tager derfor et par minutter. HTTP 429 og 503 giver en begrænsningsfejl og forsøges ikke igen i en løkke. Sensorerne beholder de sidste gode værdier, og `Retry-After` respekteres.

Tidsstempler fra DataHub er UTC. Døgn-, måneds- og årssummer, og "i dag", som et kald begrænses til, bruger Home Assistants tidszone, som for et dansk hjem er `Europe/Copenhagen`.

### Fejlfinding

**Indstillinger → Enheder & tjenester → ElOverblik (DK) → ⋮ → Download diagnostik** giver en rapport, du kan vedhæfte et issue. Refresh-tokenet, adressen og målepunktsnumrene er fjernet fra den.

## English

Home Assistant integration for [Energinet ElOverblik](https://eloverblik.dk/welcome). It reads your own electricity metering points from the official Customer API and DataHub.

Use it to follow:

- **Forbrug** — electricity bought from the grid
- **Salg / eksport** — electricity delivered to the grid, for example from solar panels

The domain is `eloverblik_dk`, so it can be installed next to other ElOverblik integrations without taking over their entities.

Metering data is requested at DataHub's native resolution (`Actual`: hourly `PT1H` today, and quarter-hour `PT15M` when the metering point provides it). Those intervals are summed into exact UTC clock hours and imported as Home Assistant long-term statistics. In the Energy dashboard, add the consumption series under **Grid consumption** and the export series under **Return to grid**.

### Create a refresh token

1. Open [eloverblik.dk/welcome](https://eloverblik.dk/welcome) and sign in with MitID.
2. Open **Datadeling**.
3. Create a token and include the metering points Home Assistant should read. The token is valid for up to one year.
4. Copy the refresh token when it is shown. If you lose it, create a new one. The data-access token that the integration exchanges it for is kept in memory only and lasts about 24 hours.

The refresh token is stored in Home Assistant's config entry storage. It is not written to the log.

### Install from the Marketplace

In Home Assistant, go to **Settings → Marketplace → Add from link** and enter:

```text
keldbroe/ha-eloverblik
```

Home Assistant 2025.1.0 or newer is required. The integration targets Home Assistant 2026.11 and still loads on recent 2025 and 2026 releases.

### Install with HACS

1. In HACS, open **Integrations**.
2. Choose **Custom repositories** and add `https://github.com/keldbroe/ha-eloverblik` as an integration.
3. Download **ElOverblik (DK)**.
4. Restart Home Assistant.

### Configure

1. Go to **Settings → Devices & services → Add integration** and choose **ElOverblik (DK)**.
2. Paste the refresh token. It is checked against ElOverblik before you continue.
3. Select the metering points to monitor. Each row shows the address, whether it is consumption (**Forbrug**) or export (**Salg/eksport**), and the 18-digit metering point id.

When the refresh token expires, Home Assistant opens a reauthentication flow and asks for a new one.

#### Which metering point should I select?

| What you want in the Energy dashboard | Typical DataHub type | Shown as |
| --- | --- | --- |
| Grid consumption | `D07` (forbrug fra net), `D10` (net fra net), or `E17` when that is the only consumption point | Forbrug |
| Return to grid | `D06` (leveret til net), `D11` (net til net) or `E18` (produktionsmålepunkt) | Salg/eksport |
| Solar production | `D01` (VE-produktion), `D09` (egenproduktion) | Produktion |

Only grid points (Forbrug and Salg/eksport) are preselected.

- **Produktion** is everything the installation produces, including what the home uses itself. It is not the same as return to grid. Use it under **Solar panels** in the Energy dashboard, not under **Return to grid**.
- Every other type, such as `D12` (samlet forbrug), `D14` (elvarme) or `D15` (nettoforbrug), is shown as **Andet (type)**. These overlap the grid points, so adding them next to `D07` counts electricity twice.

A parent `E17` often has `D07` and `D06` children. Selecting the parent together with those children counts the same electricity twice. The setup form leaves that parent unselected when the children are present. You can still select it yourself.

A metering point that another ElOverblik entry already monitors is not offered again.

#### Change the metering points later

**Settings → Devices & services → ElOverblik (DK) → Configure → Metering points** lists every point the token can see. Add or remove points there. You can also open a point's device and choose **Delete**, which removes it from the selection. A removed point loses its device and sensors. Statistics already imported for it are kept, and selecting it again continues them.

#### Two points with the same numbers

DataHub often reports exactly the same hourly values for a parent and its child, for example `E17` and its `D07`, or `E18` and `D06`. Two devices then show the same numbers, and adding both to the Energy dashboard counts the same electricity twice.

**Normally you should not select both.** At setup, and on the **Metering points** options page, the integration downloads the last month (one extra request) and hides a point that reports exactly the same values as another one. The form says which points are hidden. Tick **Show all metering points** only if you know you need that specific point. The hidden points are then listed again, marked "same values as D07" (or D06).

For points that are already selected, the integration also compares the selected points of the same kind over the last month. When two of them match hour for hour for at least a week, a warning appears under **Settings → Repairs**. It names the point to keep and the one to drop. **Fix** drops it. The child point (`D07`, `D06`) is kept, because it is the specific grid measurement and usually has the longer history. If the points later start to differ, the warning goes away on its own. Hours with no export, such as winter nights, are not counted as a match.

#### Options

**Configure** opens a menu with **Metering points** (see above) and **Update interval and statistics**:

- **Update interval:** default 6 hours, minimum 1 hour. ElOverblik data is delayed, so polling faster does not produce newer values and uses the rate limit.
- **Statistics history:** default 180 days (half a year), maximum 1825 (5 years, inside DataHub's retention of the previous five calendar years plus the current year). This is how far back statistics are written. Raising the number later backfills the older days and rewrites the cumulative sum. The first multi-year import is paced at 25 API calls per minute, so it takes a few minutes. It happens once: which window has been written is remembered in `.storage`, so a restart only fetches the current year and the hours after the newest stored one. Lowering the number leaves hours that were already stored.
- **Import Energy dashboard statistics:** on by default.

The year and month sensors always use the readings needed for the current calendar year, even when the statistics history is shorter.

### Energy dashboard

After the first successful update:

1. Open **Settings → Dashboards → Energy**.
2. Under **Electricity grid**, add grid consumption and choose the statistic whose name ends with **forbrug (grid consumption)**.
3. Add return to grid and choose the statistic whose name ends with **salg til net (return to grid)**.

Those statistics are hourly cumulative sums (`sum` and `state`) from the external statistics API. They are separate from the month and year sensors. The month and year sensors reset with the calendar and are meant for dashboards, not for the Energy dashboard.

ElOverblik usually publishes a day one to three days after it happens. The **Latest data date** sensor, and the `data_date` attribute on **Latest available day**, show the newest day DataHub has actually delivered. Today and yesterday are often empty. That is the DataHub delay, not a failed update.

Once the configured window is loaded, later polls append completed hours. They also compare the last 14 days with what is stored. When DataHub has corrected an hour, or delivered an hour that was missing, the series is rewritten from that hour to the newest one with one continuous sum. Older corrections need **Rebuild statistics**. A stored hour that DataHub no longer reports keeps its stored value.

Increasing **Statistics history** backfills the missing older months. The integration clears the external statistic and writes one continuous cumulative sum from the start of the new window through the latest completed hour. The same rewrite is available on demand:

**Developer tools → Actions → ElOverblik (DK): Rebuild statistics** (`eloverblik_dk.rebuild_statistics`).

That action uses the configured history (180 days by default). Removing the integration does not delete statistics that were already imported.

### Entities

Each metering point is its own device.

| Entity | Unit | Notes |
| --- | --- | --- |
| Latest available day | kWh | Total for the newest day that has data. Attributes: `data_date`, `metering_point_type` (`consumption`, `export`, `production` or `other`). |
| This month | kWh | Calendar month so far. `total_increasing`. Can be 0 at the start of a month while data is still delayed. |
| This year | kWh | Calendar year so far. `total_increasing`. |
| Latest interval | kWh | Newest native interval (`PT1H` or `PT15M`). Attributes: `resolution`, `period_start`, `period_end`. Unique id: `{metering_point_id}_latest_hour`. |
| Latest data date | date | Diagnostic. The newest day present in the downloaded series. |

The time series is requested with aggregation `Actual`. DataHub returns `PT1H` for hourly meters and `PT15M` where quarter-hour values are published. A daily `P1D` block, if one is ever returned, is spread evenly across the clock hours it covers. Quarter-hours are summed into the UTC hour they start in (`XX:00:00`), which is the slot Home Assistant's recorder accepts for long-term statistics. Gaps are left empty. A quality code of `A02` (not available) is ignored.

### How the API is used

- `GET /api/token` exchanges the refresh token for a data-access token. GetToken is limited to 2 calls per minute, so the token is cached in memory until five minutes before it expires and is refreshed once after HTTP 401. The cache is shared by the setup form, the first setup and later reloads, so changing an option does not spend a GetToken call.
- `GET /api/meteringpoints/meteringpoints?includeAll=false` loads linked metering points, including child points.
- `POST /api/meterdata/gettimeseries/{from}/{to}/Actual` loads the series at the meter's own resolution. The body is the swagger object `{"meteringPoints": {"meteringPoint": ["..."]}}`. Calls are batched at 10 metering points and 31 days. A 180-day window is several requests, 730 days is 24 requests, and 1825 days is 59 requests.
- DataHub returns time series for the previous five calendar years plus the current year, from 1 January of that first year. The client moves an earlier start forward to that day. A range that ends before it is skipped.
- The client also keeps a local budget of 25 Customer API calls per minute. A backfill that needs more than 25 requests waits until the next minute opens, with one extra second so the next burst is in the following window. A five-year import of about 59 requests therefore finishes across a few minutes. HTTP 429 and 503 raise a rate-limit error and are not retried in a loop. The coordinator keeps the last good sensor values and honors `Retry-After`.

Timestamps from DataHub are UTC. Day, month, and year totals, and the "today" a request is capped at, use the time zone configured in Home Assistant, which for a Danish home is `Europe/Copenhagen`.

### Troubleshooting

**Settings → Devices & services → ElOverblik (DK) → ⋮ → Download diagnostics** gives a report you can attach to an issue. The refresh token, the address and the metering point ids are removed from it.

## Development / Udvikling

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
```

The tests use recorded `MyEnergyData_MarketDocument` payloads for hourly and 15-minute series. They do not call ElOverblik.

## License / Licens

MIT. Se / see [LICENSE](LICENSE).
