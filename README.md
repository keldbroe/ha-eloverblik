# ElOverblik (DK)

Home Assistant integration for [Energinet ElOverblik](https://eloverblik.dk/welcome). It reads your own electricity metering points from the official Customer API and DataHub.

Use it to follow:

- **Forbrug** — electricity bought from the grid
- **Salg / eksport** — electricity delivered to the grid, for example from solar panels

The domain is `eloverblik_dk`, so it can be installed next to other ElOverblik integrations without taking over their entities.

Hourly values are imported as Home Assistant long-term statistics. In the Energy dashboard, add the consumption series under **Grid consumption** and the export series under **Return to grid**.

## Create a refresh token

1. Open [eloverblik.dk/welcome](https://eloverblik.dk/welcome) and sign in with MitID.
2. Open **Datadeling**.
3. Create a token and include the metering points Home Assistant should read. The token is valid for up to one year.
4. Copy the refresh token when it is shown. If you lose it, create a new one. The data-access token that the integration exchanges it for is kept in memory only and lasts about 24 hours.

The refresh token is stored in Home Assistant's config entry storage. It is not written to the log.

## Install from the Marketplace

In Home Assistant, go to **Settings → Marketplace → Add from link** and enter:

```text
keldbroe/ha-eloverblik
```

Home Assistant 2025.1.0 or newer is required. The integration targets Home Assistant 2026.11 and still loads on recent 2025 and 2026 releases.

## Install with HACS

1. In HACS, open **Integrations**.
2. Choose **Custom repositories** and add `https://github.com/keldbroe/ha-eloverblik` as an integration.
3. Download **ElOverblik (DK)**.
4. Restart Home Assistant.

## Configure

1. Go to **Settings → Devices & services → Add integration** and choose **ElOverblik (DK)**.
2. Paste the refresh token. It is checked against ElOverblik before you continue.
3. Select the metering points to monitor. Each row shows the address, whether it is consumption (**Forbrug**) or export (**Salg/eksport**), and the 18-digit metering point id.

When the refresh token expires, Home Assistant opens a reauthentication flow and asks for a new one.

### Which metering point should I select?

| What you want in the Energy dashboard | Typical DataHub type |
| --- | --- |
| Grid consumption | `D07` (forbrug fra net), or `E17` when that is the only consumption point |
| Return to grid | `D06` (leveret til net) or `E18` (production) |

A parent `E17` often has `D07` and `D06` children. Selecting the parent together with those children counts the same electricity twice. The setup form leaves that parent unselected when the children are present. You can still select it yourself.

### Options

- **Update interval:** default 6 hours, minimum 1 hour. ElOverblik data is delayed, so polling faster does not produce newer values and uses the rate limit.
- **Statistics history:** default 30 days, maximum 365. This is how far back the first statistics import goes.
- **Import Energy dashboard statistics:** on by default.

The year and month sensors always use the readings needed for the current calendar year, even when the statistics history is shorter.

## Energy dashboard

After the first successful update:

1. Open **Settings → Dashboards → Energy**.
2. Under **Electricity grid**, add grid consumption and choose the statistic whose name ends with **forbrug (grid consumption)**.
3. Add return to grid and choose the statistic whose name ends with **salg til net (return to grid)**.

Those statistics are hourly cumulative sums (`sum` and `state`) from the external statistics API. They are separate from the month and year sensors. The month and year sensors reset with the calendar and are meant for dashboards, not for the Energy dashboard.

ElOverblik usually publishes a day one to three days after it happens. The **Latest data date** sensor, and the `data_date` attribute on **Latest available day**, show the newest day DataHub has actually delivered. Today and yesterday are often empty. That is the DataHub delay, not a failed update.

Imported hours are appended. An hour that is already stored is not rewritten, so a later correction from DataHub does not move the cumulative sum. To rebuild history, delete the statistic under **Developer tools → Statistics** and reload the integration. Increasing the history option later does not backfill hours that are already recorded.

Removing the integration does not delete statistics that were already imported.

## Entities

Each metering point is its own device.

| Entity | Unit | Notes |
| --- | --- | --- |
| Latest available day | kWh | Total for the newest day that has data. Attributes: `data_date`, `metering_point_type` (`consumption` or `export`). |
| This month | kWh | Calendar month so far. `total_increasing`. Can be 0 at the start of a month while data is still delayed. |
| This year | kWh | Calendar year so far. `total_increasing`. |
| Latest hour | kWh | Newest hourly value. Attribute: `period_start`. |
| Latest data date | date | Diagnostic. The newest day present in the downloaded series. |

Quarter-hour metering points (`PT15M`) are requested as hourly series and, if DataHub returns 15-minute values, rolled up to clock hours. Gaps are left empty. A quality code of `A02` (not available) is ignored.

## How the API is used

- `GET /api/token` exchanges the refresh token for a data-access token. GetToken is limited to 2 calls per minute, so the token is cached until five minutes before it expires and is refreshed once after HTTP 401.
- `GET /api/meteringpoints/meteringpoints?includeAll=false` loads linked metering points, including child points.
- `POST /api/meterdata/gettimeseries/{from}/{to}/Hour` loads the series. The body is the swagger object `{"meteringPoints": {"meteringPoint": ["..."]}}`. Calls are batched at 10 metering points and 31 days.
- The client also keeps a local budget of 25 Customer API calls per minute. HTTP 429 and 503 raise a rate-limit error and are not retried in a loop. The coordinator keeps the last good sensor values and honors `Retry-After`.

Timestamps from DataHub are UTC. Day, month, and year totals use the time zone configured in Home Assistant, which for a Danish home is `Europe/Copenhagen`.

## Development

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
```

The tests use recorded `MyEnergyData_MarketDocument` payloads for hourly and 15-minute series. They do not call ElOverblik.

## License

MIT. See [LICENSE](LICENSE).
