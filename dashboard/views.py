import json
import math
from datetime import datetime, date
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    from scipy.stats import norm
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# ─── Index Ticker Mapping ────────────────────────────────────────────────────
INDEX_MAP = {
    'NIFTY50': '^NSEI',
    'SENSEX': '^BSESN',
}

# ─── Black-Scholes Greeks ────────────────────────────────────────────────────

def black_scholes_greeks(S, K, T, r, sigma, option_type='call'):
    """Compute BS price and all Greeks."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {}
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if option_type.lower() == 'call':
            price  = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
            delta  = norm.cdf(d1)
            theta  = (-(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
                      - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365
            rho    = K * T * math.exp(-r * T) * norm.cdf(d2) / 100
        else:
            price  = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
            delta  = norm.cdf(d1) - 1
            theta  = (-(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
                      + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365
            rho    = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100

        gamma  = norm.pdf(d1) / (S * sigma * math.sqrt(T))
        vega   = S * norm.pdf(d1) * math.sqrt(T) / 100

        return {
            'price': round(price, 4),
            'delta': round(delta, 6),
            'gamma': round(gamma, 6),
            'theta': round(theta, 6),
            'vega':  round(vega, 6),
            'rho':   round(rho, 6),
            'd1':    round(d1, 6),
            'd2':    round(d2, 6),
        }
    except Exception:
        return {}


def implied_volatility(market_price, S, K, T, r, option_type='call', tol=1e-6, max_iter=200):
    """Newton-Raphson IV solver."""
    if T <= 0 or market_price <= 0:
        return None
    sigma = 0.3
    for _ in range(max_iter):
        res = black_scholes_greeks(S, K, T, r, sigma, option_type)
        if not res:
            return None
        price = res['price']
        vega  = res['vega'] * 100          # undo the /100 scaling
        diff  = price - market_price
        if abs(diff) < tol:
            break
        if abs(vega) < 1e-10:
            break
        sigma -= diff / vega
        if sigma <= 0:
            sigma = 1e-4
    return round(sigma, 6) if 0 < sigma < 10 else None


# ─── Home Page ───────────────────────────────────────────────────────────────

def home(request):
    indices = list(INDEX_MAP.keys())
    context = {
        'indices': indices,
        'yfinance_available': YFINANCE_AVAILABLE,
    }
    return render(request, 'dashboard/home.html', context)


# ─── API: Fetch strikes for a given index + option type ──────────────────────

@require_http_methods(["GET"])
def get_strikes(request):
    index     = request.GET.get('index', 'NIFTY50')
    opt_type  = request.GET.get('type', 'call').lower()
    ticker_sym = INDEX_MAP.get(index.upper())

    if not ticker_sym:
        return JsonResponse({'error': 'Invalid index'}, status=400)
    if not YFINANCE_AVAILABLE:
        return JsonResponse({'error': 'yfinance not installed'}, status=500)

    try:
        ticker = yf.Ticker(ticker_sym)
        exps   = ticker.options          # available expiry dates
        if not exps:
            return JsonResponse({'error': 'No option data available'}, status=404)

        # Collect unique strikes across nearest 3 expiries
        all_strikes = set()
        expiry_list = list(exps[:6])
        for exp in expiry_list:
            try:
                chain = ticker.option_chain(exp)
                df = chain.calls if opt_type == 'call' else chain.puts
                all_strikes.update(df['strike'].dropna().tolist())
            except Exception:
                continue

        sorted_strikes = sorted(all_strikes)
        return JsonResponse({
            'strikes':   sorted_strikes,
            'expiries':  expiry_list,
            'spot':      _get_spot(ticker),
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def _get_spot(ticker):
    try:
        info = ticker.fast_info
        return round(float(info.last_price), 2)
    except Exception:
        try:
            hist = ticker.history(period='1d')
            if not hist.empty:
                return round(float(hist['Close'].iloc[-1]), 2)
        except Exception:
            pass
    return None


# ─── API: Fetch option chain data for selected strike ────────────────────────

@require_http_methods(["GET"])
def get_option_data(request):
    index    = request.GET.get('index', 'NIFTY50')
    opt_type = request.GET.get('type', 'call').lower()
    strike   = request.GET.get('strike')
    expiry   = request.GET.get('expiry')

    ticker_sym = INDEX_MAP.get(index.upper())
    if not ticker_sym or not strike:
        return JsonResponse({'error': 'Missing params'}, status=400)
    if not YFINANCE_AVAILABLE:
        return JsonResponse({'error': 'yfinance not installed'}, status=500)

    try:
        strike = float(strike)
        ticker = yf.Ticker(ticker_sym)
        spot   = _get_spot(ticker)
        exps   = list(ticker.options)

        if expiry and expiry in exps:
            target_exps = [expiry]
        else:
            target_exps = exps[:6]

        results = []
        for exp in target_exps:
            try:
                chain = ticker.option_chain(exp)
                df = chain.calls if opt_type == 'call' else chain.puts
                row = df[df['strike'] == strike]
                if row.empty:
                    continue
                row = row.iloc[0]
                results.append({
                    'expiry':           exp,
                    'strike':           strike,
                    'lastPrice':        _safe(row, 'lastPrice'),
                    'bid':              _safe(row, 'bid'),
                    'ask':              _safe(row, 'ask'),
                    'volume':           _safe(row, 'volume'),
                    'openInterest':     _safe(row, 'openInterest'),
                    'impliedVolatility':_safe(row, 'impliedVolatility'),
                    'inTheMoney':       bool(row.get('inTheMoney', False)),
                })
            except Exception:
                continue

        return JsonResponse({'data': results, 'spot': spot})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def _safe(row, col):
    try:
        v = row[col]
        if pd.isna(v):
            return None
        return round(float(v), 4)
    except Exception:
        return None


# ─── Analysis Hub ────────────────────────────────────────────────────────────

def analysis_hub(request):
    index    = request.GET.get('index', '')
    opt_type = request.GET.get('type', '')
    strike   = request.GET.get('strike', '')
    expiry   = request.GET.get('expiry', '')
    context  = {
        'index': index,
        'opt_type': opt_type,
        'strike': strike,
        'expiry': expiry,
    }
    return render(request, 'dashboard/analysis_hub.html', context)


# ─── Greeks Calculator Page ──────────────────────────────────────────────────

def greeks_calculator(request):
    index    = request.GET.get('index', '')
    opt_type = request.GET.get('type', 'call')
    strike   = request.GET.get('strike', '')
    expiry   = request.GET.get('expiry', '')
    context  = {
        'index': index, 'opt_type': opt_type,
        'strike': strike, 'expiry': expiry,
        'indices': list(INDEX_MAP.keys()),
    }
    return render(request, 'dashboard/greeks.html', context)


# ─── IV Smile Page ───────────────────────────────────────────────────────────

def iv_smile(request):
    index    = request.GET.get('index', '')
    opt_type = request.GET.get('type', 'call')
    strike   = request.GET.get('strike', '')
    expiry   = request.GET.get('expiry', '')
    context  = {
        'index': index, 'opt_type': opt_type,
        'strike': strike, 'expiry': expiry,
        'indices': list(INDEX_MAP.keys()),
    }
    return render(request, 'dashboard/iv_smile.html', context)


# ─── API: Greeks Data ────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_greeks_data(request):
    index    = request.GET.get('index', 'NIFTY50')
    opt_type = request.GET.get('type', 'call').lower()
    strike   = request.GET.get('strike')
    expiry   = request.GET.get('expiry')
    r        = float(request.GET.get('r', '0.065'))   # risk-free rate

    if not YFINANCE_AVAILABLE:
        return JsonResponse({'error': 'yfinance not installed'}, status=500)
    if not strike or not expiry:
        return JsonResponse({'error': 'Missing strike or expiry'}, status=400)

    try:
        strike = float(strike)
        ticker_sym = INDEX_MAP.get(index.upper())
        ticker = yf.Ticker(ticker_sym)
        spot   = _get_spot(ticker)

        chain = ticker.option_chain(expiry)
        df    = chain.calls if opt_type == 'call' else chain.puts
        row   = df[df['strike'] == strike]
        if row.empty:
            return JsonResponse({'error': 'Strike not found in chain'}, status=404)
        row = row.iloc[0]

        market_price = _safe(row, 'lastPrice') or _safe(row, 'ask') or 0
        iv_yf        = _safe(row, 'impliedVolatility') or 0.2

        exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
        T        = max((exp_date - date.today()).days / 365, 1/365)

        # Use yf IV for BS calculation
        greeks = black_scholes_greeks(spot, strike, T, r, iv_yf, opt_type)

        # Also compute BS IV from market price
        bs_iv = implied_volatility(market_price, spot, strike, T, r, opt_type) if market_price > 0 else None

        # Greeks across a range of spot prices
        spot_range  = np.linspace(spot * 0.80, spot * 1.20, 80).tolist()
        delta_curve = []
        gamma_curve = []
        theta_curve = []
        for s in spot_range:
            g = black_scholes_greeks(s, strike, T, r, iv_yf, opt_type)
            delta_curve.append(round(g.get('delta', 0), 6))
            gamma_curve.append(round(g.get('gamma', 0), 6))
            theta_curve.append(round(g.get('theta', 0), 6))

        return JsonResponse({
            'spot':        spot,
            'strike':      strike,
            'expiry':      expiry,
            'opt_type':    opt_type,
            'T_days':      round(T * 365, 1),
            'iv_yf':       round(iv_yf * 100, 2),
            'bs_iv':       round(bs_iv * 100, 2) if bs_iv else None,
            'market_price':market_price,
            'greeks':      greeks,
            'spot_range':  [round(s, 2) for s in spot_range],
            'delta_curve': delta_curve,
            'gamma_curve': gamma_curve,
            'theta_curve': theta_curve,
            'open_interest': _safe(row, 'openInterest'),
            'volume':        _safe(row, 'volume'),
            'bid':           _safe(row, 'bid'),
            'ask':           _safe(row, 'ask'),
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ─── API: IV Smile Data ──────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_iv_smile_data(request):
    index    = request.GET.get('index', 'NIFTY50')
    opt_type = request.GET.get('type', 'call').lower()
    expiry   = request.GET.get('expiry')

    if not YFINANCE_AVAILABLE:
        return JsonResponse({'error': 'yfinance not installed'}, status=500)
    if not expiry:
        return JsonResponse({'error': 'Missing expiry'}, status=400)

    try:
        ticker_sym = INDEX_MAP.get(index.upper())
        ticker = yf.Ticker(ticker_sym)
        spot   = _get_spot(ticker)

        chain = ticker.option_chain(expiry)
        df = chain.calls if opt_type == 'call' else chain.puts
        df = df[df['impliedVolatility'].notna() & (df['impliedVolatility'] > 0)]
        df = df[(df['strike'] >= spot * 0.80) & (df['strike'] <= spot * 1.20)]

        strikes = df['strike'].tolist()
        ivs     = (df['impliedVolatility'] * 100).round(2).tolist()
        moneyness = [(k / spot) for k in strikes]

        return JsonResponse({
            'strikes':   strikes,
            'ivs':       ivs,
            'moneyness': [round(m, 4) for m in moneyness],
            'spot':      spot,
            'expiry':    expiry,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ─── API: IV Surface (3D) ────────────────────────────────────────────────────

@require_http_methods(["GET"])
def get_iv_surface_data(request):
    index    = request.GET.get('index', 'NIFTY50')
    opt_type = request.GET.get('type', 'call').lower()
    strike   = request.GET.get('strike')

    if not YFINANCE_AVAILABLE:
        return JsonResponse({'error': 'yfinance not installed'}, status=500)
    if not strike:
        return JsonResponse({'error': 'Missing strike'}, status=400)

    try:
        strike_val = float(strike)
        ticker_sym = INDEX_MAP.get(index.upper())
        ticker = yf.Ticker(ticker_sym)
        spot   = _get_spot(ticker)
        exps   = list(ticker.options[:8])

        # Build IV vs Spot vs Time surface for the selected strike
        surface_data = []
        today = date.today()

        for exp in exps:
            try:
                exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
                T_days   = (exp_date - today).days
                if T_days <= 0:
                    continue
                chain = ticker.option_chain(exp)
                df = chain.calls if opt_type == 'call' else chain.puts
                df = df[df['impliedVolatility'].notna() & (df['impliedVolatility'] > 0)]

                # Strikes within ±15% of spot
                df = df[(df['strike'] >= spot * 0.85) & (df['strike'] <= spot * 1.15)]

                for _, row in df.iterrows():
                    surface_data.append({
                        'expiry': exp,
                        'T_days': T_days,
                        'strike': round(float(row['strike']), 2),
                        'iv':     round(float(row['impliedVolatility']) * 100, 2),
                        'moneyness': round(float(row['strike']) / spot, 4),
                    })
            except Exception:
                continue

        # Selected-strike IV across time
        strike_iv_series = []
        for exp in exps:
            try:
                exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
                T_days   = (exp_date - today).days
                if T_days <= 0:
                    continue
                chain = ticker.option_chain(exp)
                df = chain.calls if opt_type == 'call' else chain.puts
                row = df[abs(df['strike'] - strike_val) < 1]
                if row.empty:
                    # nearest strike
                    idx = (df['strike'] - strike_val).abs().idxmin()
                    row = df.loc[[idx]]
                row = row.iloc[0]
                iv = _safe(row, 'impliedVolatility')
                if iv:
                    strike_iv_series.append({
                        'expiry': exp,
                        'T_days': T_days,
                        'iv':     round(iv * 100, 2),
                        'strike': round(float(row['strike']), 2),
                    })
            except Exception:
                continue

        return JsonResponse({
            'surface': surface_data,
            'strike_series': strike_iv_series,
            'spot': spot,
            'selected_strike': strike_val,
            'opt_type': opt_type,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
