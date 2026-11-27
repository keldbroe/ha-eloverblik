# Changelog

## 0.3.2

### Changed

- The metering point lists show the DataHub type, for example **Forbrug (D07)** and **Forbrug (E17)**. Before, a parent and its child looked the same.
- On the **Metering points** options page, a point that reports the same values as another is marked, for example **samme tal som D07**, so it is clear which one to remove.

## 0.3.1

### Fixed

- **Identical metering points are no longer shown twice without warning.** DataHub often sends the same hourly values for a parent and its child (`E17` and `D07`, `E18` and `D06`). When two selected points of the same kind match hour for hour for at least a week, a fixable warning appears under **Settings → Repairs**. **Fix** drops the redundant point and keeps the child. The warning is withdrawn if the points start to differ.
- **The metering points can be changed after setup.** **Configure** now has a **Metering points** page, and deleting a point's device in the UI deselects it. Before, the only way to drop a point was to remove the whole integration.

### Changed

- **Configure** opens a menu: **Metering points** or **Update interval and statistics**.
- Devices and sensors of deselected points are removed. Their imported statistics are kept.
- Diagnostics list which points are duplicates (by alias, without ids).

## 0.3.0

### Fixed

- **Production is no longer treated as return to grid.** `D01` (VE production) and `D09` (own production) were classified as export and preselected, so solar production could end up under **Return to grid**. They are now **Produktion** and not preselected. `D12`, `D14`, `D15` and other overlapping types are now **Andet** instead of grid consumption. `D10` (net from grid) is grid consumption and `D11` (net to grid) stays export. Entries saved by 0.2 are corrected when they load. Statistic ids and entity ids do not change, but check the Energy dashboard if you had selected a `D01`/`D09` point there.
- **DataHub corrections and late hours reach the statistics.** Each poll compares the last 14 days with the recorder and rewrites from the first changed or newly filled hour, keeping one continuous cumulative sum.
- A metering point can no longer be added by two entries, which would duplicate devices and write one statistic twice.
- "Today" in API requests follows Home Assistant's time zone instead of always using Europe/Copenhagen.

### Faster

- A restart no longer downloads the whole statistics history again. The window that has been written is stored per metering point in `.storage/eloverblik_dk.<entry_id>`. With 5 years of history that saves about 59 API calls and several minutes at every start.
- With statistics turned off, only the current year is fetched.
- Readings that are already written to statistics are dropped from memory, and later polls only process the trailing window.
- Checking whether a backfill is needed reads only the hours before the window, instead of every stored hour since 2000.
- The data-access token is shared between the setup form, setup and reloads.

### Added

- Diagnostics download, with the token, addresses and metering point ids redacted.
- `icons.json` for entity and action icons.
- Pinned CI actions and Dependabot updates for them.

## 0.2.1

- Statistics history up to 5 years (1825 days).

## 0.2.0

- Statistics history of 180 days by default, rebuilt with one continuous sum when it is increased.

## 0.1.0

- First release.
