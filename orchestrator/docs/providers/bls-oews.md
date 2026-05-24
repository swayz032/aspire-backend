# BLS OEWS Provider — Occupational Employment and Wage Statistics

**Provider:** U.S. Bureau of Labor Statistics (BLS) OEWS Program  
**API Endpoint:** `POST https://api.bls.gov/publicAPI/v2/timeseries/data/`  
**Auth:** Optional `registrationkey` in JSON body  
**Cost:** Free (.gov public data)  
**Risk tier:** GREEN  
**Client:** `aspire_orchestrator/providers/bls_oews_client.py`

---

## Rate Limits

| Auth state | Requests/day | Series/request |
|---|---|---|
| Unauthenticated | 25 | 25 |
| With `ASPIRE_BLS_API_KEY` | 500 | 50 |

The Aspire client sends 7 series per call (7 measures for one SOC + area).
At 25 req/day unauthenticated that supports ~3 trade lookups/day before
the anonymous limit is hit. Set `ASPIRE_BLS_API_KEY` on Railway Ava-Brain for
production use (free registration at https://data.bls.gov/registrationEngine/).

---

## Series ID Format

```
OEUM{area_code_7digit}{soc_nodash_6digit}{measure_code_2digit}
```

### Area codes

| Area type | Format | Example |
|---|---|---|
| National | `0000000` | `0000000` |
| MSA | MSA numeric padded to 7 digits | Austin TX MSA 12420 → `0012420` |
| State | State FIPS padded to 7 digits | Texas = 48 → `0000048` *(not yet implemented)* |

### SOC codes (locked per plan §3.2)

| Trade | SOC code | SOC (no dash) |
|---|---|---|
| Electrician | 47-2111 | 472111 |
| Plumber | 47-2152 | 472152 |
| Drywaller | 47-2081 | 472081 |
| Painter | 47-2141 | 472141 |
| HVAC / refrigeration mechanic | 49-9021 | 499021 |

### Measure codes

| Code | Field | Description |
|---|---|---|
| `01` | `employment` | Total employment estimate |
| `03` | `mean_hourly` | Mean hourly wage |
| `04` | `mean_annual` | Mean annual wage |
| `08` | `median_hourly` | Median (50th pct) hourly wage |
| `09` | `median_annual` | Median annual wage |
| `11` | `pct_10_hourly` | 10th percentile hourly |
| `12` | `pct_25_hourly` | 25th percentile hourly |
| `13` | `pct_75_hourly` | 75th percentile hourly |
| `14` | `pct_90_hourly` | 90th percentile hourly |

### Example series IDs

```
# Electrician national median hourly wage
OEUM0000000472111 08

# Plumber Austin TX MSA (12420) mean hourly
OEUM0012420472152 03

# HVAC national 25th percentile hourly
OEUM0000000499021 12
```

---

## Request / Response Shape

### Request (POST body)

```json
{
  "seriesid": [
    "OEUM000000047211103",
    "OEUM000000047211108",
    "OEUM000000047211111",
    "OEUM000000047211112",
    "OEUM000000047211113",
    "OEUM000000047211114",
    "OEUM000000047211101"
  ],
  "registrationkey": "YOUR_KEY_HERE"
}
```

### Response (success)

```json
{
  "status": "REQUEST_SUCCEEDED",
  "Results": {
    "series": [
      {
        "seriesID": "OEUM000000047211108",
        "data": [
          { "year": "2024", "period": "A01", "periodName": "Annual", "value": "29.02" }
        ]
      }
    ]
  }
}
```

Data is most-recent-first. The client takes `data[0]` as the latest estimate.

### Suppressed data values

BLS suppresses data when sample sizes are too small. The client treats these
as `None` (not zero):

| Value | Meaning |
|---|---|
| `-` | Not available |
| `*` | Wage > $100/hr or employment > 99,999 (suppressed high) |
| `**` | Wage < $10/hr (suppressed low) |
| `#` | Not released |

---

## ZIP → MSA Mapping

`aspire_orchestrator/services/adam/zip_to_msa.py` provides a static
prefix-based mapping (first 3 ZIP digits → MSA numeric code). Coverage:
top ~75 US metros by construction volume. Unknown ZIPs return `None` and
the BLS client falls back to national estimates.

Add new MSA mappings to `_ZIP_PREFIX_TO_MSA` as Aspire expands to new markets.

---

## Law Compliance Notes

- **Law #1:** Client returns data; orchestrator/Drew decides how to present wage bands in estimates.
- **Law #2:** Every call (success + failure) produces a receipt via `make_receipt_data`.
- **Law #3:** Unknown trade → `UNKNOWN_TRADE` deny; empty series → `RESOURCE_NOT_FOUND` fail.
- **Law #9:** OEWS data is public government statistics — no PII risk. `registrationkey` is never logged or surfaced in receipts.

---

## Env Vars

| Var | Required | Notes |
|---|---|---|
| `ASPIRE_BLS_API_KEY` | Recommended for prod | Free registration at data.bls.gov. Without it: 25 req/day. |

Set on Railway Ava-Brain service.
