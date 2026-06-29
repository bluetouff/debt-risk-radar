"""
Debt Risk Radar source catalog.

The catalog is deliberately explicit: every monitored metric has a source,
a risk direction, a scoring bucket and a human-readable rationale.
"""

FRED_SERIES = {
    "fiscal": {
        "GFDEGDQ188S": {
            "name": "Federal debt, total public debt / GDP",
            "unit": "% GDP",
            "direction": "up",
            "weight": 1.20,
            "source": "FRED",
            "rationale": "Debt stock relative to economic base.",
        },
        "FYGFGDQ188S": {
            "name": "Federal debt held by the public / GDP",
            "unit": "% GDP",
            "direction": "up",
            "weight": 1.30,
            "source": "FRED",
            "rationale": "Market-facing federal debt burden.",
        },
        "FYFSGDA188S": {
            "name": "Federal surplus or deficit / GDP",
            "unit": "% GDP",
            "direction": "down",
            "weight": 1.10,
            "source": "FRED",
            "rationale": "A deeper deficit accelerates debt accumulation.",
        },
        "A091RC1Q027SBEA": {
            "name": "Federal government interest payments",
            "unit": "USD bn SAAR",
            "direction": "up",
            "weight": 1.00,
            "source": "FRED/BEA",
            "rationale": "Interest expense is the fiscal transmission channel.",
        },
    },
    "rates_market": {
        "DGS2": {
            "name": "2Y Treasury yield",
            "unit": "%",
            "direction": "up",
            "weight": 0.70,
            "source": "FRED/Treasury",
            "rationale": "Higher front-end rates raise near-term refinancing cost.",
        },
        "DGS10": {
            "name": "10Y Treasury yield",
            "unit": "%",
            "direction": "up",
            "weight": 1.00,
            "source": "FRED/Treasury",
            "rationale": "Benchmark long-rate for sovereign funding cost.",
        },
        "DGS30": {
            "name": "30Y Treasury yield",
            "unit": "%",
            "direction": "up",
            "weight": 0.80,
            "source": "FRED/Treasury",
            "rationale": "Long-duration credibility and inflation compensation signal.",
        },
        "T10Y2Y": {
            "name": "10Y minus 2Y Treasury spread",
            "unit": "pp",
            "direction": "down",
            "weight": 0.70,
            "source": "FRED/Treasury",
            "rationale": "Inversion can flag recession and fiscal revenue risk.",
        },
        "T10YIE": {
            "name": "10Y breakeven inflation",
            "unit": "%",
            "direction": "up",
            "weight": 0.50,
            "source": "FRED",
            "rationale": "Inflation compensation affects nominal debt dynamics.",
        },
        "BAMLC0A0CM": {
            "name": "US investment grade OAS",
            "unit": "%",
            "direction": "up",
            "weight": 0.80,
            "source": "FRED/ICE BofA",
            "rationale": "Corporate credit stress can feed fiscal and banking risk.",
        },
        "BAMLH0A0HYM2": {
            "name": "US high yield OAS",
            "unit": "%",
            "direction": "up",
            "weight": 1.00,
            "source": "FRED/ICE BofA",
            "rationale": "High-yield spreads capture market risk appetite.",
        },
    },
    "private_leverage": {
        "TDSP": {
            "name": "Household debt service payments / disposable income",
            "unit": "%",
            "direction": "up",
            "weight": 0.90,
            "source": "FRED/Fed",
            "rationale": "Household debt service can transmit rate stress to demand.",
        },
        "NCBCMDPMVCE": {
            "name": "Nonfinancial corporate debt securities and loans",
            "unit": "USD bn",
            "direction": "up",
            "weight": 0.80,
            "source": "FRED/Fed Z.1",
            "rationale": "Corporate leverage raises refinancing and default risk.",
        },
        "DRCCLACBS": {
            "name": "Credit card delinquency rate",
            "unit": "%",
            "direction": "up",
            "weight": 0.80,
            "source": "FRED/Fed",
            "rationale": "Consumer credit deterioration is an early fragility signal.",
        },
        "DRBLACBS": {
            "name": "Business loan delinquency rate",
            "unit": "%",
            "direction": "up",
            "weight": 0.80,
            "source": "FRED/Fed",
            "rationale": "Business loan stress can migrate into banks and employment.",
        },
    },
    "liquidity": {
        "NFCI": {
            "name": "Chicago Fed National Financial Conditions Index",
            "unit": "index",
            "direction": "up",
            "weight": 1.00,
            "source": "FRED/Chicago Fed",
            "rationale": "Broad financial conditions proxy.",
        },
        "STLFSI4": {
            "name": "St. Louis Fed Financial Stress Index",
            "unit": "index",
            "direction": "up",
            "weight": 0.90,
            "source": "FRED/St. Louis Fed",
            "rationale": "Fast-moving market stress composite.",
        },
        "WRESBAL": {
            "name": "Reserve balances with Federal Reserve Banks",
            "unit": "USD mn",
            "direction": "down",
            "weight": 0.70,
            "source": "FRED/Fed",
            "rationale": "Falling reserves can tighten money-market plumbing.",
        },
        "RRPONTSYD": {
            "name": "Overnight reverse repo facility",
            "unit": "USD bn",
            "direction": "down",
            "weight": 0.50,
            "source": "FRED/Fed",
            "rationale": "Shrinking RRP reduces an important liquidity buffer.",
        },
    },
}


TREASURY_ENDPOINTS = {
    "debt_to_penny": {
        "name": "Debt to the Penny",
        "url": "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny",
        "fields": [
            "record_date",
            "debt_held_public_amt",
            "intragov_hold_amt",
            "tot_pub_debt_out_amt",
        ],
        "source": "US Treasury Fiscal Data",
    }
}


WORLD_BANK_INDICATORS = {
    "GC.DOD.TOTL.GD.ZS": {
        "name": "Central government debt, total / GDP",
        "unit": "% GDP",
        "direction": "up",
        "weight": 1.00,
        "source": "World Bank",
    },
    "GC.XPN.INTP.RV.ZS": {
        "name": "Interest payments / revenue",
        "unit": "% revenue",
        "direction": "up",
        "weight": 1.10,
        "source": "World Bank",
    },
    "NY.GDP.MKTP.KD.ZG": {
        "name": "Real GDP growth",
        "unit": "% YoY",
        "direction": "down",
        "weight": 0.80,
        "source": "World Bank",
    },
    "FP.CPI.TOTL.ZG": {
        "name": "Inflation, consumer prices",
        "unit": "% YoY",
        "direction": "up",
        "weight": 0.40,
        "source": "World Bank",
    },
}

BIS_COUNTRY_MAP = {
    "USA": "US",
    "FRA": "FR",
    "DEU": "DE",
    "JPN": "JP",
    "GBR": "GB",
    "ITA": "IT",
    "ESP": "ES",
    "CHN": "CN",
    "BRA": "BR",
    "IND": "IN",
}


BIS_BULK_FEEDS = {
    "credit_gap": {
        "name": "Credit-to-GDP gap",
        "url": "https://data.bis.org/static/bulk/WS_CREDIT_GAP_csv_flat.zip",
        "source": "BIS Data Portal",
    },
    "dsr": {
        "name": "Debt service ratios",
        "url": "https://data.bis.org/static/bulk/WS_DSR_csv_flat.zip",
        "source": "BIS Data Portal",
    },
}


CBO_DATASETS = {
    "long_term_budget": {
        "name": "Long-term budget projections",
        "url": "https://raw.githubusercontent.com/US-CBO/cbo-data/main/data/budget/long_term_budget/annual_fy_2026-02.csv",
        "source": "CBO Open Data",
        "variables": {
            "lt_debt_held_by_public_gdp_share": {
                "name": "Projected debt held by the public / GDP",
                "unit": "% GDP",
                "direction": "up",
                "weight": 1.20,
            },
            "lt_gross_federal_debt_gdp_share": {
                "name": "Projected gross federal debt / GDP",
                "unit": "% GDP",
                "direction": "up",
                "weight": 1.00,
            },
            "lt_deficit_total_gdp_share": {
                "name": "Projected federal deficit / GDP",
                "unit": "% GDP",
                "direction": "down",
                "weight": 1.00,
            },
            "lt_outlays_net_interest_gdp_share": {
                "name": "Projected net interest outlays / GDP",
                "unit": "% GDP",
                "direction": "up",
                "weight": 1.10,
            },
        },
    }
}


EUROSTAT_GEOS = {
    "EA20": "Euro area 20",
    "EU27_2020": "European Union 27",
    "FR": "France",
    "DE": "Germany",
    "IT": "Italy",
    "ES": "Spain",
    "NL": "Netherlands",
    "BE": "Belgium",
    "PT": "Portugal",
    "EL": "Greece",
    "IE": "Ireland",
}


EUROSTAT_SERIES = {
    "maastricht_debt": {
        "name": "Maastricht gross government debt / GDP",
        "url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/gov_10dd_edpt1",
        "params": {"sector": "S13", "na_item": "GD", "unit": "PC_GDP", "freq": "A", "lang": "en"},
        "unit": "% GDP",
        "direction": "up",
        "weight": 1.10,
        "source": "Eurostat",
    },
    "government_balance": {
        "name": "General government balance / GDP",
        "url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/gov_10dd_edpt1",
        "params": {"sector": "S13", "na_item": "B9", "unit": "PC_GDP", "freq": "A", "lang": "en"},
        "unit": "% GDP",
        "direction": "down",
        "weight": 0.80,
        "source": "Eurostat",
    },
}


MASSIVE_MARKET_SERIES = {
    "TLT": {
        "name": "iShares 20+ Year Treasury Bond ETF",
        "unit": "USD",
        "direction": "down",
        "weight": 0.80,
        "source": "Massive Market Data",
        "rationale": "Long-duration Treasury proxy; drawdowns flag rate stress.",
    },
    "HYG": {
        "name": "iShares iBoxx High Yield Corporate Bond ETF",
        "unit": "USD",
        "direction": "down",
        "weight": 1.00,
        "source": "Massive Market Data",
        "rationale": "High-yield price proxy; falling price flags credit stress.",
    },
    "LQD": {
        "name": "iShares iBoxx Investment Grade Corporate Bond ETF",
        "unit": "USD",
        "direction": "down",
        "weight": 0.80,
        "source": "Massive Market Data",
        "rationale": "Investment-grade credit price proxy.",
    },
    "SHY": {
        "name": "iShares 1-3 Year Treasury Bond ETF",
        "unit": "USD",
        "direction": "down",
        "weight": 0.50,
        "source": "Massive Market Data",
        "rationale": "Short Treasury proxy used in duration stress ratios.",
    },
    "SPY": {
        "name": "SPDR S&P 500 ETF Trust",
        "unit": "USD",
        "direction": "down",
        "weight": 0.50,
        "source": "Massive Market Data",
        "rationale": "Equity risk appetite proxy.",
    },
}


BUCKET_LABELS = {
    "fiscal": "Fiscal solvency",
    "rates_market": "Rates and market stress",
    "private_leverage": "Private leverage",
    "liquidity": "Liquidity plumbing",
    "treasury_daily": "Treasury daily debt",
    "world_bank": "Global comparables",
    "global_credit": "BIS global credit",
    "cbo_projection": "CBO projections",
    "euro_maastricht": "Eurostat Maastricht",
    "market_prices": "Massive market prices",
}


BUCKET_WEIGHTS = {
    "fiscal": 0.22,
    "rates_market": 0.18,
    "private_leverage": 0.12,
    "liquidity": 0.10,
    "treasury_daily": 0.08,
    "world_bank": 0.04,
    "global_credit": 0.10,
    "cbo_projection": 0.08,
    "euro_maastricht": 0.04,
    "market_prices": 0.04,
}


WATCH_LEVEL = 65
STRESS_LEVEL = 80
ZSCORE_WINDOW_YEARS = 5
