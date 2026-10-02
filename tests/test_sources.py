"""Parsers for each country, fed small samples in the published formats."""

import pandas as pd
import pytest

from wastewater.sources import PATHOGENS, SOURCES, canada, germany, netherlands, new_zealand, scotland, usa
from wastewater.sources.base import RawSeries, slugify, week_ending, weighted_weekly_mean


def of(series, pathogen):
    return {s.region_id: s for s in series if s.pathogen == pathogen}


def by_id(series):
    return {s.region_id: s for s in series}


def test_registry_has_scotland_first_and_unique_ids():
    assert list(SOURCES)[0] == "scotland"
    assert len(set(SOURCES)) == len(SOURCES)
    for cls in SOURCES.values():
        assert cls.info.hemisphere in ("N", "S")
        assert "covid" in cls.info.signals
        assert set(cls.info.signals) <= set(PATHOGENS)
        for signal in cls.info.signals.values():
            assert signal.kind in ("wastewater", "lab tests") and signal.unit and signal.metric


def test_slugify_and_week_ending():
    assert slugify("Île-à-la-Crosse") == "ile-a-la-crosse"
    assert slugify("NHS Greater Glasgow and Clyde") == "nhs-greater-glasgow-and-clyde"
    ends = week_ending(pd.to_datetime(["2026-09-14", "2026-09-18", "2026-09-20"]))
    assert list(ends.dt.strftime("%Y-%m-%d")) == ["2026-09-20"] * 3


def test_raw_series_drops_missing_and_negative_and_averages_duplicates():
    s = pd.Series([1.0, None, -5.0, 3.0], index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-01"]))
    raw = RawSeries("x", "X", "site", "Sites", s)
    assert raw.values.to_dict() == {pd.Timestamp("2024-01-01"): 2.0}
    with pytest.raises(ValueError):
        RawSeries("x", "X", "planet", "Sites", s)
    with pytest.raises(ValueError):
        RawSeries("x", "X", "site", "Sites", s, pathogen="measles")


def test_weighted_weekly_mean_counts_each_site_once_per_week():
    df = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-03", "2024-01-02"],
            "value": [10.0, 30.0, 100.0],
            "pop": [1000, 1000, 3000],
            "site": ["a", "a", "b"],
        }
    )
    weekly = weighted_weekly_mean(df, "date", "value", "pop", "site")
    # site a weekly mean 20 (weight 1000), site b 100 (weight 3000)
    assert weekly.iloc[0] == pytest.approx((20 * 1000 + 100 * 3000) / 4000)


SCOT_NATIONAL = """Season,ISOyear,SevenDayEnding,Country,WastewaterRNA
2025/26,2026,20260914,S92000003,14.622152
2025/26,2026,20260915,S92000003,14.462503
"""

SCOT_HB = """Season,ISOyear,WeekBeginning,WeekEnding,HBcode,HBName,Average(Mgc),Average(Mgc)QF,PercentCoverage
2025/26,2026,20260905,20260911,S08000024,NHS Lothian,12.5,,90
2025/26,2026,20260912,20260918,S08000024,NHS Lothian,14.0,,90
2025/26,2026,20260912,20260918,S08000028,NHS Western Isles,NA,:,0
"""

SCOT_WWTW = """Season,ISOyear,WeekBeginning,WeekEnding,WastewaterTreatmentWork,Average(Mgc),Average(Mgc)QF,PercentCoverage
2025/26,2026,20260912,20260918,Seafield,15.2,,100
2025/26,2026,20260912,20260918,Perth,0,,100
"""


SCOT_POSITIVITY = """Season,ISOyear,ISOweek,WeekBeginning,WeekEnding,Country,Pathogen,PositiveCount,TotalSamples,PositivityPercentage
2025/26,2026,38,20260914,20260920,S92000003,Influenza (All),137,3807,3.6
2025/26,2026,38,20260914,20260920,S92000003,RSV,8,2304,0.3
2025/26,2026,38,20260914,20260920,S92000003,COVID-19,500,4000,12.5
"""

SCOT_CASES = """Season,ISOyear,ISOweek,WeekBeginning,WeekEnding,Pathogen,HBcode,HBName,HBQF,NumberCasesPerWeek,RateCasesPerWeek,Population
2025/26,2026,38,20260914,20260920,Influenza (All),S08000024,NHS Lothian,,40,4.3,933130
2025/26,2026,38,20260914,20260920,RSV,S08000024,NHS Lothian,,3,0.3,933130
2025/26,2026,38,20260914,20260920,Influenza (All),S92000003,Scotland,d,137,2.5,5543300
"""


def test_scotland_lab_signals():
    nat = {s.pathogen: s for s in scotland.parse_positivity(SCOT_POSITIVITY)}
    assert set(nat) == {"flu", "rsv"}  # COVID comes from wastewater, not lab tests
    assert nat["flu"].values.iloc[0] == pytest.approx(3.6) and nat["flu"].unit == "% positive"
    boards = scotland.parse_cases_by_board(SCOT_CASES)
    assert {(s.pathogen, s.region_id) for s in boards} == {("flu", "hb-s08000024"), ("rsv", "hb-s08000024")}
    assert all(s.unit == "cases/100k" for s in boards)
    assert scotland.Scotland.info.signals["flu"].kind == "lab tests"


def test_scotland():
    nat = scotland.parse_national(SCOT_NATIONAL)[0]
    assert nat.level == "national" and len(nat.values) == 2
    hb = by_id(scotland.parse_weekly(SCOT_HB, level="region", group="Health boards", id_prefix="hb", name_col="HBName", code_col="HBcode"))
    assert set(hb) == {"hb-s08000024"}  # Western Isles only had a missing value
    assert hb["hb-s08000024"].name == "NHS Lothian"
    assert hb["hb-s08000024"].values.iloc[-1] == 14.0
    sites = by_id(scotland.parse_weekly(SCOT_WWTW, level="site", group="Treatment works", id_prefix="wwtw", name_col="WastewaterTreatmentWork"))
    assert set(sites) == {"wwtw-seafield", "wwtw-perth"}
    assert sites["wwtw-perth"].values.iloc[0] == 0.0  # zeros are kept


def test_usa():
    nat = usa.parse_national(pd.DataFrame({"week_end": ["2026-09-19", "2026-09-26"], "wval": ["3.12", "2.525"], "n_sites": ["942", "836"]}))
    assert nat.values.iloc[-1] == pytest.approx(2.525)
    flu = usa.parse_national(pd.DataFrame({"week_end": ["2026-09-26"], "wval": ["1"], "n_sites": ["5"]}), "flu")
    assert flu.pathogen == "flu"
    assert set(usa.TARGETS) == {"covid", "flu", "rsv"}
    states = by_id(usa.parse_states(pd.DataFrame({
        "state_territory": ["New York", "New York", "Ohio"],
        "week_end": ["2026-09-19", "2026-09-26", "2026-09-26"],
        "wval": ["4.0", "5.0", "1.0"],
        "n_sites": ["10", "11", "5"],
    })))
    assert set(states) == {"new-york", "ohio"}
    assert states["new-york"].level == "region"


CANADA = """Location,site,city,province,country,EpiYear,EpiWeek,weekstart,measureid,w_avg,min,max,populationcoverage,pruid
Canada,,,,Canada,2026,37,2026-09-13,covN2,19.9,19.9,45.8,0.31,
Alberta,,,Alberta,Canada,2026,37,2026-09-13,covN2,21.4,1,30,0.44,48
Calgary,,Calgary,Alberta,Canada,2026,37,2026-09-13,covN2,25.0,1,30,0.9,48
Calgary Bonnybrook,Calgary Bonnybrook,Calgary,Alberta,Canada,2026,37,2026-09-13,covN2,26.0,1,30,,48
Canada,,,,Canada,2026,37,2026-09-13,fluA,5.0,1,9,0.31,
Canada,,,,Canada,2026,37,2026-09-13,fluB,2.5,1,9,0.31,
Canada,,,,Canada,2026,37,2026-09-13,rsv,7.0,1,9,0.31,
Alberta,,,Alberta,Canada,2026,37,2026-09-13,fluA,3.0,1,9,0.44,48
"""


def test_canada():
    parsed = canada.parse(CANADA)
    series = of(parsed, "covid")
    assert set(series) == {"canada", "alberta", "city-calgary"}
    flu = of(parsed, "flu")
    assert flu["canada"].values.iloc[0] == pytest.approx(7.5)  # flu A + flu B
    assert flu["alberta"].values.iloc[0] == pytest.approx(3.0)  # only A reported
    assert of(parsed, "rsv")["canada"].values.iloc[0] == pytest.approx(7.0)
    assert series["canada"].level == "national"
    assert series["alberta"].level == "region"
    assert series["city-calgary"].parent == "Alberta"
    assert series["canada"].values.index[0] == pd.Timestamp("2026-09-19")  # week start + 6 days


GERMANY_AGG = "datum\tn\tanteil_bev\tviruslast\tviruslast_normalisiert\tvorhersage\tobere_schranke\tuntere_schranke\ttyp\n" \
    "2026-09-16\t61\t0.24\t23277.81\t19721.27\t20468\t23931\t17506\tSARS-CoV-2\n" \
    "2026-09-23\t63\t0.24\t27898.37\t26195.95\t27038\t32905\t22217\tSARS-CoV-2\n" \
    "2026-09-23\t63\t0.24\t999\t999\t999\t999\t999\tInfluenza A\n" \
    "2026-09-23\t63\t0.24\t500\t450\t1\t1\t1\tInfluenza A+B\n" \
    "2026-09-23\t63\t0.24\t300\t280\t1\t1\t1\tRSV A/B\n" \
    "2026-09-23\t63\t0.24\t800\t810\t1\t1\t1\tRSV A+B\n"

GERMANY_SITES = "standort\tbundesland\tdatum\tviruslast\tviruslast_normalisiert\tvorhersage\tobere_schranke\tuntere_schranke\teinwohner\tlaborwechsel\ttyp\tunter_bg\n" \
    "Aachen\tNW\t2026-09-21\t100\t100\t1\t1\t1\t1000\tnein\tSARS-CoV-2\tnein\n" \
    "Aachen\tNW\t2026-09-22\tNA\tNA\t1\t1\t1\t1000\tnein\tSARS-CoV-2\tNA\n" \
    "Bonn\tNW\t2026-09-23\t400\t400\t1\t1\t1\t3000\tnein\tSARS-CoV-2\tnein\n" \
    "Bonn\tNW\t2026-09-23\t5\t5\t1\t1\t1\t3000\tnein\tInfluenza A\tnein\n" \
    "Bonn\tNW\t2026-09-16\t40\t40\t1\t1\t1\t3000\tnein\tRSV A+B\tnein\n" \
    "Bonn\tNW\t2026-09-23\t20\t20\t1\t1\t1\t3000\tnein\tRSV A/B\tnein\n"


def test_germany():
    nats = {s.pathogen: s for s in germany.parse_national(GERMANY_AGG)}
    assert len(nats["covid"].values) == 2 and nats["covid"].values.iloc[-1] == pytest.approx(26195.95)
    assert nats["flu"].values.iloc[-1] == pytest.approx(450)  # Influenza A+B, not A alone
    assert nats["rsv"].values.iloc[-1] == pytest.approx(280)  # the current RSV A/B assay, never spliced
    parsed = germany.parse_sites(GERMANY_SITES)
    series = of(parsed, "covid")
    assert set(series) == {"land-nw", "site-aachen", "site-bonn"}
    rsv = of(parsed, "rsv")
    assert set(rsv) == {"site-bonn"}  # no state curves for RSV
    assert len(rsv["site-bonn"].values) == 1  # one assay per plant: the most recent
    assert series["land-nw"].values.iloc[0] == pytest.approx((100 * 1000 + 400 * 3000) / 4000)
    assert series["site-bonn"].parent == "North Rhine-Westphalia"


NL_NATIONAL = """Version;Date_of_report;Date_measurement;RNA_flow_per_100000
2;2026-09-30;2026-09-25;26049319451696
2;2026-09-30;2026-09-26;28632298076632
"""
NL_SITES = """Version;Date_of_report;Date_measurement;RWZI_AWZI_code;RWZI_AWZI_name;RNA_flow_per_100000
2;2026-09-30;2026-09-21;1008;Stadskanaal;6551650598065
2;2026-09-30;2026-09-22;32002;Tilburg;
2;2026-09-30;2026-09-23;32002;Tilburg;34621959326395
"""


def test_netherlands():
    nat = netherlands.parse_national(NL_NATIONAL)
    assert nat.values.iloc[-1] == pytest.approx(28632298076632)
    sites = by_id(netherlands.parse_sites(NL_SITES))
    assert set(sites) == {"rwzi-1008", "rwzi-32002"}
    assert sites["rwzi-32002"].name == "Tilburg" and len(sites["rwzi-32002"].values) == 1


def test_new_zealand():
    nat = new_zealand.parse_national("week_end_date,national_pop,copies_per_day_per_person\n2026-09-13,2292000,871774\n2026-09-20,2292000,891951\n")
    assert nat.values.iloc[-1] == 891951
    regions = by_id(new_zealand.parse_regions(
        "week_end_date,Region,copies_per_day_per_person,population_covered,n_sites\n"
        "2026-09-20,Hawke's Bay,100,1,1\n2026-09-20,Otago,200,1,1\n"
    ))
    assert set(regions) == {"hawke-s-bay", "otago"}
    sites = by_id(new_zealand.parse_sites(
        "week_end_date,SampleLocation,copies_per_day_per_person\n2026-09-20,WG_MoaPoint,5\n2026-09-20,AU_Central,7\n",
        "SampleLocation,DisplayName,SampleType,Latitude,Longitude,Population,region_id,Region,shp_label\n"
        "AU_Central,Auckland Central,Autosampler,0,0,315000,2,Auckland,x\n",
    ))
    assert sites["site-au-central"].name == "Auckland Central"
    assert sites["site-au-central"].population == 315000
    assert sites["site-wg-moapoint"].name == "Moa Point"
    assert sites["site-wg-moapoint"].parent == "Wellington"


def test_raw_series_drops_implausible_values_and_dates():
    import numpy as np
    import pandas as pd

    from wastewater.sources.base import RawSeries

    idx = pd.to_datetime(["1999-01-03", "2024-01-07", "2024-01-14", "2024-01-21", "2024-01-28", "2024-02-04", "2099-12-27"])
    raw = RawSeries("x", "X", "region", "R", pd.Series([5, 1, np.inf, -3, "n/a", 2, 9], index=idx, dtype=object), population="lots")
    assert list(raw.values.index.strftime("%Y-%m-%d")) == ["2024-01-07", "2024-02-04"]
    assert list(raw.values) == [1.0, 2.0]
    assert raw.population is None
    assert RawSeries("y", "Y", "site", "S", pd.Series([1.0], index=idx[1:2]), population="1200").population == 1200.0
    assert RawSeries("z", "Z", "site", "S", pd.Series([1.0], index=idx[1:2]), population=float("inf")).population is None
