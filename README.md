# OptionFlow — Professional Options Analytics Platform

A Django-based real-time options analytics dashboard for NSE (Nifty50) and BSE (Sensex).

## Features
- **Home** — Select index (Nifty50 / Sensex), option type (Call/Put), live strikes from yfinance
- **Analytics Hub** — 4-module navigation hub
- **Option Greeks** — Black-Scholes Δ Delta, Γ Gamma, Θ Theta, ν Vega, ρ Rho with interactive Plotly curves
- **IV Smile & 3D Surface** — IV Smile, 3D IV Surface (IV × Strike × Time), IV Term Structure
- **Open Interest Analytics** — Placeholder (coming soon)
- **Market Sentiment** — Placeholder (coming soon)

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the development server
```bash
cd options_platform
python manage.py runserver
```

### 3. Open in browser
```
http://127.0.0.1:8000/
```

## Project Structure
```
options_platform/
├── manage.py
├── requirements.txt
├── options_platform/          # Django project config
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── dashboard/                 # Main app
    ├── views.py               # All logic: yfinance, BS Greeks, IV solver
    ├── urls.py
    └── templates/dashboard/
        ├── base.html          # Dark terminal aesthetic, shared layout
        ├── home.html          # Index + strike selector
        ├── analysis_hub.html  # 4-module hub
        ├── greeks.html        # Greeks calculator + Plotly charts
        └── iv_smile.html      # IV Smile / 3D Surface / Term Structure
```

## API Endpoints
| Endpoint | Description |
|---|---|
| `GET /api/strikes/?index=NIFTY50&type=call` | Fetch available strikes + expiries |
| `GET /api/option-data/?index=NIFTY50&type=call&strike=24000&expiry=2025-01-30` | Option chain row |
| `GET /api/greeks-data/?index=NIFTY50&type=call&strike=24000&expiry=2025-01-30&r=0.065` | Greeks + sensitivity curves |
| `GET /api/iv-smile-data/?index=NIFTY50&type=call&expiry=2025-01-30` | IV smile data for one expiry |
| `GET /api/iv-surface-data/?index=NIFTY50&type=call&strike=24000` | Full IV surface + term structure |

## Notes
- Uses `^NSEI` (NSE Nifty 50) and `^BSESN` (BSE Sensex) tickers via yfinance
- All Greeks computed via Black-Scholes; IV solved via Newton-Raphson
- Plotly charts are fully interactive (zoom, pan, hover)
- No database required — fully stateless, data fetched live
