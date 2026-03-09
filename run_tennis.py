from src.scrapers.tennis.sackmann import SackmannScraper

s = SackmannScraper()
s.load_year(2024, "ATP")
s.load_year(2025, "ATP")
s.load_year(2024, "WTA")
s.load_year(2025, "WTA")
s.promote_to_matches()
