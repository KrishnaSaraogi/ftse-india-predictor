"""
Checks SEC EDGAR for FLIN's N-PORT filing covering a given quarter.

Lowest-risk source in the pipeline (real US government API), so this
is the safest thing to run fully unattended. CIK 1655589 (Franklin
Templeton ETF Trust) confirmed directly from real filing URLs during
development - the trust files N-PORT for ~40 funds on the same day, so
this checks each candidate's series name rather than assuming the
first NPORT result found is the right fund.
"""

import re
import time

import pandas as pd
import requests

HEADERS = {"User-Agent": "FTSE India Predictor (automated) research@example.com"}
CIK = "1655589"
CIK_PADDED = CIK.zfill(10)
TARGET_SERIES_NAME = "Franklin FTSE India ETF"


def find_nport_filing(target_period: str, max_check: int = 80,
                      sleep_between: float = 0.2) -> pd.DataFrame | None:
    """target_period like '2026-09-30'. Returns a DataFrame with columns
    [name, isin, pctVal] if found, else None."""
    resp = requests.get(f"https://data.sec.gov/submissions/CIK{CIK_PADDED}.json",
                        headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    nport_filings = [(f, d, a) for f, d, a in
                     zip(recent.get("form", []), recent.get("filingDate", []),
                         recent.get("accessionNumber", []))
                     if "NPORT" in f]

    for f, d, a in nport_filings[:max_check]:
        acc_nodash = a.replace("-", "")
        index_url = f"https://www.sec.gov/Archives/edgar/data/{CIK}/{acc_nodash}/"
        idx_resp = requests.get(index_url, headers=HEADERS, timeout=20)
        if idx_resp.status_code != 200:
            continue
        xml_files = re.findall(r'href="([^"]+primary_doc\.xml)"', idx_resp.text)
        if not xml_files:
            xml_files = re.findall(r'href="([^"]+\.xml)"', idx_resp.text)
        if not xml_files:
            continue
        doc_url = index_url + xml_files[0].split("/")[-1]
        time.sleep(sleep_between)
        xml_resp = requests.get(doc_url, headers=HEADERS, timeout=30)
        if xml_resp.status_code != 200:
            continue

        series = re.search(r"<seriesName>(.*?)</seriesName>", xml_resp.text)
        period = re.search(r"<repPdDate>(.*?)</repPdDate>", xml_resp.text)
        series_name = series.group(1) if series else ""
        period_str = period.group(1) if period else ""

        if TARGET_SERIES_NAME.lower() in series_name.lower() and period_str == target_period:
            return _parse_holdings(xml_resp.text)

    return None


def _parse_holdings(xml_text: str) -> pd.DataFrame:
    blocks = re.findall(r"<invstOrSec>(.*?)</invstOrSec>", xml_text, re.S)
    rows = []
    for block in blocks:
        def grab(tag):
            m = re.search(f"<{tag}>(.*?)</{tag}>", block)
            return m.group(1) if m else None
        isin_m = re.search(r'<identifiers>.*?<isin\s+value="([^"]+)"', block, re.S)
        rows.append({"name": grab("name"),
                     "isin": isin_m.group(1) if isin_m else None,
                     "pctVal": grab("pctVal"), "assetCat": grab("assetCat")})
    df = pd.DataFrame(rows)
    return df[df["assetCat"] == "EC"][["name", "isin", "pctVal"]].reset_index(drop=True)
