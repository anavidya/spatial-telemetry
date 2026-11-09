"""Small helpers for ERDDAP CSV requests."""
import io
from urllib.parse import quote

import pandas as pd
import requests


class ErddapNoData(Exception):
    """ERDDAP answered 'Your query produced no matching results'."""


def get_csv(url: str, timeout: float, units_row: bool = True) -> pd.DataFrame:
    """GET an ERDDAP .csv URL. In tabledap/info CSV, row 2 holds units, so skip it."""
    resp = requests.get(url, timeout=timeout)
    if resp.status_code == 404 and "no matching results" in resp.text.lower():
        raise ErddapNoData(url)
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text), skiprows=[1] if units_row else None)


def tabledap_url(server: str, dataset_id: str, variables: list, constraints: list) -> str:
    query = ",".join(variables) + "".join("&" + c for c in constraints)
    return f"{server}/tabledap/{dataset_id}.csv?{quote(query, safe='=&,!.:-_')}"
