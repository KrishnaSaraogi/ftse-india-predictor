#!/usr/bin/env python3
"""
Check SEC EDGAR for FLIN's N-PORT filing covering a given quarter.

CIK 1655589 (Franklin Templeton ETF Trust) confirmed directly from real
filing URLs during development - not guessed. This CIK covers the whole
trust, which files N-PORT for every Franklin ETF on the same day, so
this checks each candidate's SERIES NAME for "Franklin FTSE India ETF"
rather than assuming the first NPORT result is the right fund.

USAGE
    python scripts/check_flin_filing_template.py --period 2026-09-30

Writes data/flin_holdings_<period>.csv if found - feed that path
straight into update_backtest.py --new-holdings.

Typical filing lag: ~50-60 days after quarter end. If not found, it is
most likely just not filed yet - try again in a few days.
"""

import argparse
import re
import time
from pathlib import Path

import pandas as pd
import requests

HEADERS = {"User-Agent": "Research research@example.com"}
CIK = "1655589"
CIK_PADDED = CIK.zfill(10)
TARGET_SERIES_NAME = "Franklin FTSE India ETF"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", required=True,
                    help="quarter end date to look for, e.g. 2026-09-30")
    ap.add_argument("--max-check", type=int, default=80,
                    help="how many recent NPORT filings to check before "
                         "giving up (the trust files ~30-40 funds per "
                         "quarter, so this needs headroom)")
    args = ap.parse_args()

    filings_resp = requests.get(
        f"https://data.sec.gov/submissions/CIK{CIK_PADDED}.json",
        headers=HEADERS, timeout=20)
    filings_resp.raise_for_status()
    data = filings_resp.json()
    recent = data.get("filings", {}).get("recent", {})
    nport_filings = [(f, d, a) for f, d, a in
                     zip(recent.get("form", []), recent.get("filingDate", []),
                         recent.get("accessionNumber", []))
                     if "NPORT" in f]
    print(f"{len(nport_filings)} NPORT filings on record for this CIK")

    target_xml = None
    for f, d, a in nport_filings[:args.max_check]:
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
        time.sleep(0.2)
        xml_resp = requests.get(doc_url, headers=HEADERS, timeout=30)
        if xml_resp.status_code != 200:
            continue

        series = re.search(r"<seriesName>(.*?)</seriesName>", xml_resp.text)
        period = re.search(r"<repPdDate>(.*?)</repPdDate>", xml_resp.text)
        series_name = series.group(1) if series else "?"
        period_str = period.group(1) if period else "?"

        if TARGET_SERIES_NAME.lower() in series_name.lower():
            print(f"Found FLIN filing: period={period_str}")
            if period_str == args.period:
                target_xml = xml_resp.text
                break

    if target_xml is None:
        print(f"\nNo Franklin FTSE India ETF filing found for {args.period} "
              f"in the {args.max_check} most recent filings checked.")
        print("Most likely: not yet public. Try again in a few days.")
        return

    blocks = re.findall(r"<invstOrSec>(.*?)</invstOrSec>", target_xml, re.S)
    rows = []
    for block in blocks:
        def grab(tag):
            m = re.search(f"<{tag}>(.*?)</{tag}>", block)
            return m.group(1) if m else None
        isin_m = re.search(r'<identifiers>.*?<isin\s+value="([^"]+)"', block, re.S)
        rows.append({"name": grab("name"),
                     "isin": isin_m.group(1) if isin_m else None,
                     "pctVal": grab("pctVal"), "assetCat": grab("assetCat")})

    holdings = pd.DataFrame(rows)
    holdings = holdings[holdings["assetCat"] == "EC"]
    print(f"{len(holdings)} equity holdings parsed")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"flin_holdings_{args.period}.csv"
    holdings[["name", "isin", "pctVal"]].to_csv(out_path, index=False)
    print(f"Wrote {out_path}")
    print(f"\nNext: python scripts/update_backtest.py "
          f"--new-holdings {out_path} --review-date {args.period}")


if __name__ == "__main__":
    main()
