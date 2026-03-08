"""
config.py — Sport configuration registry
JOB-006 Sports Betting Model

Adding a new sport = adding one entry here.
All downstream code reads from this config — no hardcoded sport logic elsewhere.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SportConfig:
    sport_id: str               # canonical key: darts | snooker | tennis
    tour: str                   # PDC | WST | ATP | WTA
    display_name: str
    stat_field: str             # total_180s | total_centuries | total_aces
    stat_label: str             # "180s" | "centuries" | "aces"
    stat_per_unit: str          # "per leg" | "per frame" | "per service game"
    default_line: float         # typical O/U line for model output
    min_matches_for_form: int   # minimum match history to trust form score
    form_window: int            # last N matches for recency-weighted form
    data_sources: list[str]     # ordered by preference
    scraper_module: str         # dotted path to scraper class
    phase: int                  # 1 = build now, 2 = later


SPORTS: dict[str, SportConfig] = {

    "darts": SportConfig(
        sport_id="darts",
        tour="PDC",
        display_name="Darts (PDC)",
        stat_field="total_180s",
        stat_label="180s",
        stat_per_unit="per leg",
        default_line=5.5,
        min_matches_for_form=5,
        form_window=10,
        data_sources=["dartsdatabase", "pdc_website"],
        scraper_module="scrapers.darts.dartsdatabase",
        phase=1,
    ),

    "snooker": SportConfig(
        sport_id="snooker",
        tour="WST",
        display_name="Snooker (WST)",
        stat_field="total_centuries",
        stat_label="centuries",
        stat_per_unit="per frame",
        default_line=3.5,
        min_matches_for_form=5,
        form_window=10,
        data_sources=["cuetrackeR"],
        scraper_module="scrapers.snooker.cuetrackeR",
        phase=1,
    ),

    "tennis": SportConfig(
        sport_id="tennis",
        tour="ATP",
        display_name="Tennis (ATP/WTA)",
        stat_field="total_aces",
        stat_label="aces",
        stat_per_unit="per service game",
        default_line=12.5,
        min_matches_for_form=5,
        form_window=15,          # more matches needed — surface variance
        data_sources=["sackmann_atp", "sackmann_wta", "atp_website"],
        scraper_module="scrapers.tennis.sackmann",
        phase=1,
    ),

}


def get_sport(sport_id: str) -> SportConfig:
    if sport_id not in SPORTS:
        raise ValueError(f"Unknown sport: {sport_id!r}. Valid: {list(SPORTS)}")
    return SPORTS[sport_id]


def all_phase1_sports() -> list[SportConfig]:
    return [s for s in SPORTS.values() if s.phase == 1]
