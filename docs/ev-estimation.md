# EV charging power estimate

We estimate EV charging power at **2.169 kW**, using 17 accepted charging stops.

## Data

We used FusionSolar's total consumption (`usePower`) from **1 September–10 October 2026**: 40 days of five-minute readings, with all readings available for each day and its surrounding hours. Dates use Europe/Brussels; durations are calculated in UTC. We treat power readings as kW, although the source does not confirm their unit.

To find stops for calibration, we first select regions with consumption **>2.13 kW for ≥120 minutes**. Interruptions and missing readings split regions. We include two hours before and after each day to catch charging across midnight.

## How we estimate the power

We use stops because household activity makes charging starts less clear.

![Consumption around charging starts and stops, aligned without stretching time](images/ev-starts-and-endings.png)

*Each line is one candidate region, aligned at its start or stop. Both views show 120 minutes of charging and 60 minutes of surrounding consumption, without stretching time. The clearer stop drop is what we use to estimate EV power.*

Take **t=0** as the first interval below the threshold:

```text
before = mean(P at −15 and −10 minutes)
after  = mean(P at  +5 and +10 minutes)
drop   = before − after
EV power = mean(accepted drops), rounded to 0.001 kW
```

We skip the readings at −5 and 0 minutes because they may contain part of the charging stop. Assuming household consumption stays the same across the two clean windows, the difference estimates the load that switched off.

We keep stops with complete windows, where the region does not touch a gap or the edge of the available data. The drop must be positive, each window's highest and lowest readings must differ by **≤0.2 kW**, and the drop must not exceed the 10th percentile of total consumption in the region. Every accepted stop gets equal weight. **17/18 passed**; the 4 October 11:35 stop was excluded because consumption increased instead of dropping.

![Seventeen accepted charging stops give a mean EV power estimate of 2.169 kW](images/ev-ending-calibration.png)

*Each dot is the power drop at an accepted stop. The dashed line is their mean: 2.169 kW. The shaded band shows the 95% bootstrap interval for that mean, rather than variation between individual stops.*

## Results

| Check | Result |
| --- | --- |
| Mean of 17 accepted stops | **2.169 kW** |
| 95% bootstrap interval for the mean | **2.1291–2.2070 kW** |

The unrounded mean is 2.1688235 kW. We use **2.169 kW** for detection and energy. For the bootstrap interval, we resample whole accepted stops 5,000 times and recalculate the unrounded mean, using seed 20261011.

## Using the estimate

Daily detection requires consumption **>2.169 kW for ≥120 continuous minutes** and draws an EV block at **2.169 kW**. We subtract no baseload. Each reading covers the following five-minute interval, so energy is `2.169 × charging hours`, counting only the part inside the local day. Across the dataset, this gives **18 regions, 129.583 hours and 281.066 kWh**. The daily API matches all 40 daily investigation estimates.

The interval assumes independent stops that belong to the EV. Other appliances stopping, or household consumption changing at the same time, can affect the estimate. We have not confirmed whether the source readings are instantaneous or averaged. The two-hour rule also misses shorter charging sessions. Charger logs are needed to check which regions belong to the EV and how accurate the energy estimate is.

## Run the analysis

```powershell
uv run --locked home-energy investigate ev 2026-09-01 --end-date 2026-10-10 --fetch --output outputs/ev-investigation-2026-09-01_2026-10-10
```

The output directory contains the individual stops in `ending_estimates.csv` and the settings and results in `estimation.json`. The [report code](../src/home_energy/investigations/ev/report.py) draws the graphs from these measurements. The [EV detector](../src/home_energy/usage/ev.py) uses the estimate calculated by the [calibration code](../src/home_energy/investigations/ev/calibration.py).
