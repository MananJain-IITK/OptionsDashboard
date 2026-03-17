import os
import re
import math
import glob
from datetime import datetime, date
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.conf import settings

try:
    import pandas as pd
    import numpy as np
    from scipy.stats import norm
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


# ─── Risk-Free Rate — Live 91-day T-bill yield ───────────────────────────────
# Waterfall: 1) FBIL website  2) RBI press release  3) hardcoded fallback
# Cached to <BASE_DIR>/rfr_cache.json for 24 hours.

import json
import time
import threading

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

RFR_CACHE_FILE    = os.path.join(settings.BASE_DIR, 'rfr_cache.json')
RFR_CACHE_TTL     = 24 * 3600          # 24 hours in seconds
RFR_FALLBACK      = 0.065              # 6.5% — last known Dec-2023 91d T-bill
_rfr_lock         = threading.Lock()


def _load_rfr_cache():
    """Return (rate, timestamp) from cache file, or (None, 0) if missing/stale."""
    try:
        with open(RFR_CACHE_FILE, 'r') as f:
            data = json.load(f)
        if time.time() - data.get('ts', 0) < RFR_CACHE_TTL:
            return float(data['rate']), data['ts']
    except Exception:
        pass
    return None, 0


def _save_rfr_cache(rate):
    try:
        with open(RFR_CACHE_FILE, 'w') as f:
            json.dump({'rate': rate, 'ts': time.time(),
                       'rate_pct': round(rate * 100, 4)}, f, indent=2)
    except Exception:
        pass


def _fetch_fbil():
    """
    Scrape FBIL's T-Bills page for the latest 91-day yield.
    FBIL publishes a daily table at https://www.fbil.org.in/#/tbills
    The actual data is served from their API endpoint.
    """
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36'),
        'Referer': 'https://www.fbil.org.in/',
    }
    # FBIL serves benchmark data through their REST API
    url = 'https://www.fbil.org.in/api/v1/benchmarks/tbills'
    r = requests.get(url, headers=headers, timeout=8)
    r.raise_for_status()
    data = r.json()
    # Response is typically a list; find the 91-day entry
    for item in (data if isinstance(data, list) else data.get('data', [])):
        tenor = str(item.get('tenor', item.get('Tenor', ''))).strip()
        if '91' in tenor:
            val = item.get('rate', item.get('Rate', item.get('yield', item.get('Yield', ''))))
            rate = float(str(val).replace('%', '').strip())
            return rate / 100   # convert percent → decimal
    return None


def _fetch_rbi_tbill():
    """
    Scrape RBI's weekly T-bill auction result page.
    RBI publishes results at https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx
    We search for the most recent 91-day cut-off yield.
    """
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36'),
    }
    # RBI press release search for T-bill auction results
    search_url = ('https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx'
                  '?prid=search&srchkw=91+day+treasury+bill')
    r = requests.get(search_url, headers=headers, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')

    # Find the most recent press release link
    links = soup.find_all('a', href=True)
    pr_url = None
    for link in links:
        href = link.get('href', '')
        text = link.get_text(strip=True).lower()
        if 'auction' in text and '91' in text and 'treasury' in text:
            pr_url = 'https://www.rbi.org.in' + href if href.startswith('/') else href
            break

    if not pr_url:
        return None

    # Fetch the press release and look for the yield
    r2 = requests.get(pr_url, headers=headers, timeout=10)
    soup2 = BeautifulSoup(r2.text, 'html.parser')
    text  = soup2.get_text(separator=' ')

    # Pattern: "Implicit Yield at Cut-Off Price" followed by a number
    import re as _re
    m = _re.search(
        r'(?:implicit yield at cut.off|cut.off yield|yield at cut.off)[^\d]*(\d+\.\d+)',
        text, _re.IGNORECASE
    )
    if m:
        return float(m.group(1)) / 100

    return None


def _fetch_rbi_direct():
    """
    Direct RBI DBIE-style URL for T-bill weekly auction data table.
    Falls back to scraping the RBI Money Market Operations page.
    """
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'),
    }
    # RBI publishes weekly auction results in a structured table
    url = 'https://www.rbi.org.in/Scripts/BS_ViewMonetaryOperations.aspx'
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')

    # Look for tables with T-bill data
    import re as _re
    tables = soup.find_all('table')
    for table in tables:
        text = table.get_text(separator=' ').lower()
        if '91' in text and ('yield' in text or 'cut' in text):
            # Find numeric yield values — typically between 5.0 and 9.0
            nums = _re.findall(r'\b([5-9]\.\d{2,4})\b', text)
            if nums:
                return float(nums[0]) / 100
    return None


def get_risk_free_rate():
    """
    Returns the current 91-day T-bill yield as a decimal (e.g., 0.0685 for 6.85%).
    Tries FBIL → RBI press release → RBI direct → fallback.
    Result is cached for 24 hours.
    """
    with _rfr_lock:
        # 1. Check cache
        cached_rate, _ = _load_rfr_cache()
        if cached_rate is not None:
            return cached_rate

        if not REQUESTS_AVAILABLE:
            return RFR_FALLBACK

        # 2. Try each source in order
        sources = [
            ('FBIL API',       _fetch_fbil),
            ('RBI Press Rel.', _fetch_rbi_tbill),
            ('RBI Direct',     _fetch_rbi_direct),
        ]
        last_error = None
        for name, fetcher in sources:
            try:
                rate = fetcher()
                if rate is not None and 0.03 <= rate <= 0.15:  # sanity: 3%–15%
                    _save_rfr_cache(rate)
                    return rate
            except Exception as e:
                last_error = f'{name}: {e}'
                continue

        # 3. All sources failed — use fallback and cache it briefly (1 hr)
        _save_rfr_cache(RFR_FALLBACK)
        return RFR_FALLBACK


def get_rfr_meta():
    """Return rate + metadata dict for the diagnostics/UI endpoint."""
    rate = get_risk_free_rate()
    cached, ts = _load_rfr_cache()
    return {
        'rate':        rate,
        'rate_pct':    round(rate * 100, 4),
        'source':      'cache' if cached else 'fallback',
        'cached_at':   datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else None,
        'next_refresh': datetime.fromtimestamp(ts + RFR_CACHE_TTL).strftime('%Y-%m-%d %H:%M:%S') if ts else None,
        'fallback':    RFR_FALLBACK,
    }

# ─── Data Directory Config ───────────────────────────────────────────────────
# Folder:  <BASE_DIR>/12Dec-Nifty/12Dec-Nifty/*.csv
# Files:   {strike}_{call|put}_{YYYY-MM-DD}.csv
# Spot:    nifty_underlying.csv  (in same folder)

DATA_ROOT       = os.path.join(settings.BASE_DIR, '12Dec-Nifty', '12Dec-Nifty')
UNDERLYING_FILE = os.path.join(DATA_ROOT, 'nifty_underlying.csv')


# ─── CSV Loader Helpers ──────────────────────────────────────────────────────

def _parse_filename(fname):
    """
    19900_call_2023-12-28.csv  →  (19900.0, 'call', '2023-12-28')
    Returns None if the filename doesn't match.
    """
    base = os.path.splitext(os.path.basename(fname))[0]
    m = re.match(r'^(\d+(?:\.\d+)?)_(call|put)_(\d{4}-\d{2}-\d{2})$', base, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1)), m.group(2).lower(), m.group(3)


def _load_csv(filepath):
    """Load one option CSV; normalise columns; parse datetime."""
    df = pd.read_csv(filepath)
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
    df['datetime'] = pd.to_datetime(df['datetime'], dayfirst=True, errors='coerce')
    df = df.dropna(subset=['datetime']).sort_values('datetime').reset_index(drop=True)
    return df


def _load_underlying():
    if not os.path.exists(UNDERLYING_FILE):
        return None
    df = pd.read_csv(UNDERLYING_FILE)
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
    for col in ('datetime', 'date', 'timestamp', 'time'):
        if col in df.columns:
            df['datetime'] = pd.to_datetime(df[col], dayfirst=True, errors='coerce')
            break
    return df.dropna(subset=['datetime']).sort_values('datetime').reset_index(drop=True)


def _get_spot_from_underlying(as_of_dt=None):
    """
    Return the Nifty spot close at or just before as_of_dt.
    If as_of_dt is None, returns the last row in the underlying file.
    Underlying columns: open  high  low  close  volume  datetime
    """
    df = _load_underlying()
    if df is None or df.empty:
        return None
    for col in ('close', 'price', 'last', 'ltp', 'open'):
        if col in df.columns:
            if as_of_dt is not None:
                sub = df[df['datetime'] <= pd.Timestamp(as_of_dt)]
                row = sub.iloc[-1] if not sub.empty else df.iloc[0]
            else:
                row = df.iloc[-1]
            return round(float(row[col]), 2)
    return None


def _get_ref_dt(df):
    """Return the latest datetime in an option CSV as a Timestamp, or None."""
    if df is None or df.empty:
        return None
    return df['datetime'].iloc[-1]


def _scan_data_folder():
    """
    Returns:
        { expiry_str: { 'call': {strike: filepath}, 'put': {strike: filepath} } }
    """
    result = {}
    for fpath in glob.glob(os.path.join(DATA_ROOT, '*.csv')):
        parsed = _parse_filename(fpath)
        if parsed is None:
            continue
        strike, opttype, expiry = parsed
        result.setdefault(expiry, {'call': {}, 'put': {}})
        result[expiry][opttype][strike] = fpath
    return result


def _get_latest_row(df):
    if df is None or df.empty:
        return {}
    row = df.iloc[-1]
    return {k: _safe_val(row, k) for k in ('close', 'open', 'high', 'low', 'volume', 'open_interest')}


def _safe_val(row, col):
    try:
        v = row[col]
        return None if pd.isna(v) else round(float(v), 4)
    except Exception:
        return None


# ─── Black-Scholes Greeks ────────────────────────────────────────────────────

def black_scholes_greeks(S, K, T, r, sigma, option_type='call'):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {}
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if option_type.lower() == 'call':
            price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
            delta = norm.cdf(d1)
            theta = (-(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
                     - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365
            rho   = K * T * math.exp(-r * T) * norm.cdf(d2) / 100
        else:
            price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
            delta = norm.cdf(d1) - 1
            theta = (-(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
                     + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365
            rho   = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100
        gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))
        vega  = S * norm.pdf(d1) * math.sqrt(T) / 100
        return {
            'price': round(price, 4), 'delta': round(delta, 6),
            'gamma': round(gamma, 6), 'theta': round(theta, 6),
            'vega':  round(vega, 6),  'rho':   round(rho, 6),
        }
    except Exception:
        return {}


def implied_volatility(market_price, S, K, T, r, option_type='call', tol=1e-6, max_iter=200):
    if T <= 0 or market_price <= 0:
        return None
    sigma = 0.3
    for _ in range(max_iter):
        res  = black_scholes_greeks(S, K, T, r, sigma, option_type)
        if not res:
            return None
        diff = res['price'] - market_price
        vega = res['vega'] * 100
        if abs(diff) < tol:
            break
        if abs(vega) < 1e-10:
            break
        sigma -= diff / vega
        if sigma <= 0:
            sigma = 1e-4
    return round(sigma, 6) if 0 < sigma < 10 else None


# ─── Pages ───────────────────────────────────────────────────────────────────

def home(request):
    return render(request, 'dashboard/home.html', {
        'indices':   ['NIFTY50'],
        'data_mode': 'local',
        'data_ok':   os.path.isdir(DATA_ROOT),
        'data_path': DATA_ROOT,
    })


def analysis_hub(request):
    return render(request, 'dashboard/analysis_hub.html', {
        'index':    request.GET.get('index', 'NIFTY50'),
        'opt_type': request.GET.get('type', 'call'),
        'strike':   request.GET.get('strike', ''),
        'expiry':   request.GET.get('expiry', ''),
    })


def greeks_calculator(request):
    return render(request, 'dashboard/greeks.html', {
        'index':    request.GET.get('index', 'NIFTY50'),
        'opt_type': request.GET.get('type', 'call'),
        'strike':   request.GET.get('strike', ''),
        'expiry':   request.GET.get('expiry', ''),
        'indices':  ['NIFTY50'],
    })


def iv_smile(request):
    return render(request, 'dashboard/iv_smile.html', {
        'index':    request.GET.get('index', 'NIFTY50'),
        'opt_type': request.GET.get('type', 'call'),
        'strike':   request.GET.get('strike', ''),
        'expiry':   request.GET.get('expiry', ''),
        'indices':  ['NIFTY50'],
    })


# ─── API: Strikes ─────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_strikes(request):
    opt_type = request.GET.get('type', 'call').lower()
    if not os.path.isdir(DATA_ROOT):
        return JsonResponse({'error': f'Data folder not found at: {DATA_ROOT}\n'
                                      f'Expected: options_platform/12Dec-Nifty/12Dec-Nifty/'}, status=404)
    try:
        chain_map = _scan_data_folder()
        if not chain_map:
            return JsonResponse({'error': 'No CSV files matched pattern strike_call/put_YYYY-MM-DD.csv'}, status=404)

        expiry_list = sorted(chain_map.keys())
        all_strikes = set()
        for exp_data in chain_map.values():
            all_strikes.update(exp_data.get(opt_type, {}).keys())

        if not all_strikes:
            return JsonResponse({'error': f'No {opt_type} option files found'}, status=404)

        return JsonResponse({
            'strikes':  sorted(all_strikes),
            'expiries': expiry_list,
            'spot':     _get_spot_from_underlying(),
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ─── API: Option data ─────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_option_data(request):
    opt_type = request.GET.get('type', 'call').lower()
    strike   = request.GET.get('strike')
    expiry   = request.GET.get('expiry')
    if not strike:
        return JsonResponse({'error': 'Missing strike'}, status=400)
    try:
        strike_f  = float(strike)
        chain_map = _scan_data_folder()
        spot      = _get_spot_from_underlying()
        exps      = [expiry] if expiry and expiry in chain_map else sorted(chain_map.keys())
        results   = []
        for exp in exps:
            fpath = chain_map.get(exp, {}).get(opt_type, {}).get(strike_f)
            if not fpath:
                continue
            row = _get_latest_row(_load_csv(fpath))
            results.append({
                'expiry': exp, 'strike': strike_f,
                'lastPrice': row.get('close'), 'volume': row.get('volume'),
                'openInterest': row.get('open_interest'),
                'high': row.get('high'), 'low': row.get('low'),
                'inTheMoney': (strike_f <= spot if opt_type == 'call' else strike_f >= spot) if spot else None,
            })
        return JsonResponse({'data': results, 'spot': spot})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ─── API: Greeks ──────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_greeks_data(request):
    opt_type = request.GET.get('type', 'call').lower()
    strike   = request.GET.get('strike')
    expiry   = request.GET.get('expiry')
    # Risk-free rate: live 91-day T-bill yield, user can override via ?r=
    _default_r = get_risk_free_rate()
    r        = float(request.GET.get('r', str(_default_r)))
    if not strike or not expiry:
        return JsonResponse({'error': 'Missing strike or expiry'}, status=400)
    try:
        strike_f  = float(strike)
        chain_map = _scan_data_folder()
        spot      = _get_spot_from_underlying()

        fpath = chain_map.get(expiry, {}).get(opt_type, {}).get(strike_f)
        if not fpath:
            available = list(chain_map.get(expiry, {}).get(opt_type, {}).keys())
            if not available:
                return JsonResponse({'error': f'No {opt_type} data found for expiry {expiry}'}, status=404)
            strike_f = min(available, key=lambda x: abs(x - strike_f))
            fpath    = chain_map[expiry][opt_type][strike_f]

        df           = _load_csv(fpath)
        latest       = _get_latest_row(df)
        market_price = latest.get('close') or 0

        # Match spot to the exact timestamp of the last row in this CSV
        ref_dt   = _get_ref_dt(df)
        spot     = _get_spot_from_underlying(as_of_dt=ref_dt)
        if not spot:
            spot = market_price * 1.05   # rough fallback if underlying missing

        ref_date = ref_dt.date() if ref_dt is not None else date.today()
        exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
        T        = max((exp_date - ref_date).days / 365, 1 / 365)

        bs_iv  = implied_volatility(market_price, spot, strike_f, T, r, opt_type) if market_price > 0 else None
        iv     = bs_iv if bs_iv else 0.20
        greeks = black_scholes_greeks(spot, strike_f, T, r, iv, opt_type)

        spot_range  = np.linspace(spot * 0.80, spot * 1.20, 80).tolist()
        delta_curve, gamma_curve, theta_curve = [], [], []
        for s in spot_range:
            g = black_scholes_greeks(s, strike_f, T, r, iv, opt_type)
            delta_curve.append(round(g.get('delta', 0), 6))
            gamma_curve.append(round(g.get('gamma', 0), 6))
            theta_curve.append(round(g.get('theta', 0), 6))

        return JsonResponse({
            'spot': spot, 'strike': strike_f, 'expiry': expiry,
            'opt_type': opt_type, 'T_days': round(T * 365, 1),
            'iv_computed': round(iv * 100, 2),  # Newton-Raphson IV from local market price
            'bs_iv': round(bs_iv * 100, 2) if bs_iv else None,
            'market_price': market_price, 'greeks': greeks,
            'spot_range': [round(s, 2) for s in spot_range],
            'delta_curve': delta_curve, 'gamma_curve': gamma_curve, 'theta_curve': theta_curve,
            'open_interest': latest.get('open_interest'),
            'volume': latest.get('volume'),
            'bid': None, 'ask': None,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ─── API: IV Smile ────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_iv_smile_data(request):
    opt_type = request.GET.get('type', 'call').lower()
    expiry   = request.GET.get('expiry')
    if not expiry:
        return JsonResponse({'error': 'Missing expiry'}, status=400)
    try:
        r         = get_risk_free_rate()
        chain_map = _scan_data_folder()
        spot      = _get_spot_from_underlying()
        strikes_d = chain_map.get(expiry, {}).get(opt_type, {})
        if not strikes_d:
            return JsonResponse({'error': f'No {opt_type} data for expiry {expiry}'}, status=404)

        exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
        smile_strikes, smile_ivs, smile_moneyness = [], [], []

        for strike_f, fpath in sorted(strikes_d.items()):
            if spot and not (spot * 0.80 <= strike_f <= spot * 1.20):
                continue
            try:
                df     = _load_csv(fpath)
                price  = (_get_latest_row(df).get('close') or 0)
                if price <= 0:
                    continue
                ref_dt = _get_ref_dt(df)
                spot   = _get_spot_from_underlying(as_of_dt=ref_dt) or spot
                ref    = ref_dt.date() if ref_dt else date.today()
                T      = max((exp_date - ref).days / 365, 1 / 365)
                iv     = implied_volatility(price, spot, strike_f, T, r, opt_type)
                if iv and 0.01 < iv < 5:
                    smile_strikes.append(strike_f)
                    smile_ivs.append(round(iv * 100, 2))
                    smile_moneyness.append(round(strike_f / spot, 4) if spot else None)
            except Exception:
                continue

        return JsonResponse({
            'strikes': smile_strikes, 'ivs': smile_ivs,
            'moneyness': smile_moneyness, 'spot': spot, 'expiry': expiry,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ─── API: IV Surface ──────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_iv_surface_data(request):
    opt_type     = request.GET.get('type', 'call').lower()
    strike_param = request.GET.get('strike')
    if not strike_param:
        return JsonResponse({'error': 'Missing strike'}, status=400)
    try:
        r          = get_risk_free_rate()
        strike_sel = float(strike_param)
        chain_map  = _scan_data_folder()
        spot       = _get_spot_from_underlying()
        surface_data, strike_series = [], []

        for expiry, type_map in sorted(chain_map.items()):
            try:
                exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
            except ValueError:
                continue
            strikes_d = type_map.get(opt_type, {})
            if not strikes_d:
                continue

            # Use last datetime in first file as reference date
            ref_date = date.today()
            for fp in list(strikes_d.values())[:1]:
                try:
                    df = _load_csv(fp)
                    if not df.empty:
                        ref_date = df['datetime'].iloc[-1].date()
                except Exception:
                    pass

            T_days = (exp_date - ref_date).days
            if T_days <= 0:
                continue
            T = T_days / 365

            # Surface points
            for strike_f, fpath in sorted(strikes_d.items()):
                if spot and not (spot * 0.85 <= strike_f <= spot * 1.15):
                    continue
                try:
                    df    = _load_csv(fpath)
                    price = _get_latest_row(df).get('close') or 0
                    if price <= 0:
                        continue
                    iv = implied_volatility(price, spot, strike_f, T, r, opt_type)
                    if iv and 0.01 < iv < 5:
                        surface_data.append({
                            'expiry': expiry, 'T_days': T_days,
                            'strike': strike_f, 'iv': round(iv * 100, 2),
                            'moneyness': round(strike_f / spot, 4) if spot else None,
                        })
                except Exception:
                    continue

            # Term structure for selected strike
            nearest = min(strikes_d.keys(), key=lambda x: abs(x - strike_sel))
            try:
                df    = _load_csv(strikes_d[nearest])
                price = _get_latest_row(df).get('close') or 0
                if price > 0:
                    iv = implied_volatility(price, spot, nearest, T, r, opt_type)
                    if iv and 0.01 < iv < 5:
                        strike_series.append({
                            'expiry': expiry, 'T_days': T_days,
                            'iv': round(iv * 100, 2), 'strike': nearest,
                        })
            except Exception:
                pass

        return JsonResponse({
            'surface': surface_data, 'strike_series': strike_series,
            'spot': spot, 'selected_strike': strike_sel, 'opt_type': opt_type,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ─── API: Diagnostics ─────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def diagnostics(request):
    """
    GET /api/diagnostics/
    Returns a full report of what data was found, so you can verify
    folder structure and CSV parsing before using the UI.
    """
    report = {
        'data_root':        DATA_ROOT,
        'data_root_exists': os.path.isdir(DATA_ROOT),
        'underlying_file':  UNDERLYING_FILE,
        'underlying_exists': os.path.exists(UNDERLYING_FILE),
        'underlying_spot':  None,
        'underlying_rows':  None,
        'underlying_cols':  None,
        'underlying_date_range': None,
        'expiries_found':   [],
        'total_call_files': 0,
        'total_put_files':  0,
        'sample_strikes':   [],
        'errors':           [],
    }

    # Underlying check
    try:
        df = _load_underlying()
        if df is not None and not df.empty:
            report['underlying_rows']      = len(df)
            report['underlying_cols']      = list(df.columns)
            report['underlying_spot']      = _get_spot_from_underlying()
            report['underlying_date_range'] = [
                str(df['datetime'].iloc[0]),
                str(df['datetime'].iloc[-1]),
            ]
    except Exception as e:
        report['errors'].append(f'Underlying load error: {e}')

    # Options chain check
    try:
        chain_map = _scan_data_folder()
        report['expiries_found'] = sorted(chain_map.keys())
        for exp, type_map in chain_map.items():
            report['total_call_files'] += len(type_map.get('call', {}))
            report['total_put_files']  += len(type_map.get('put', {}))
        # Sample: first expiry, first 5 call strikes
        if report['expiries_found']:
            first_exp = report['expiries_found'][0]
            call_strikes = sorted(chain_map[first_exp].get('call', {}).keys())[:5]
            report['sample_strikes'] = call_strikes
    except Exception as e:
        report['errors'].append(f'Chain scan error: {e}')

    # Risk-free rate check
    try:
        report['risk_free_rate'] = get_rfr_meta()
    except Exception as e:
        report['risk_free_rate'] = {'error': str(e)}

    return JsonResponse(report, json_dumps_params={'indent': 2})


# ─── API: Risk-Free Rate ──────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_rfr(request):
    """
    GET /api/risk-free-rate/
    Returns current 91-day T-bill yield + cache metadata.
    Pass ?refresh=1 to force a cache bust and re-fetch.
    """
    if request.GET.get('refresh') == '1':
        # Delete cache file to force a fresh fetch
        try:
            os.remove(RFR_CACHE_FILE)
        except FileNotFoundError:
            pass

    meta = get_rfr_meta()
    return JsonResponse(meta, json_dumps_params={'indent': 2})
