import json
import math
import urllib.request

global_assets = [
    # Global Indices
    ('^GDAXI', 'Germany DAX 40', 'Index', '3DELl_EQ', '3DESl_EQ'),
    ('^FTSE', 'UK FTSE 100', 'Index', '3UKLl_EQ', '3UKSl_EQ'),
    ('^N225', 'Japan Nikkei 225', 'Index', 'JPN3l_EQ', 'JP3Sl_EQ'),
    ('^HSI', 'Hong Kong Hang Seng', 'Index', '3HSl_EQ', '3SHSl_EQ'),
    ('EEM', 'MSCI Emerging Markets', 'Index', 'EEM', 'EUM'),
    ('INDA', 'MSCI India ETF', 'Index', 'INDA', 'None'),
    ('EWZ', 'MSCI Brazil ETF', 'Index', 'BRZU', 'BZQ'),
    
    # Global Commodities
    ('GC=F', 'Gold Spot/Futures', 'Commodity', '3GLEl_EQ', 'GDMSl_EQ'),
    ('SI=F', 'Silver Spot/Futures', 'Commodity', '3SLVl_EQ', 'SLVSl_EQ'),
    ('CL=F', 'WTI Crude Oil', 'Commodity', 'WTI3l_EQ', 'SWTOl_EQ'),
    ('HG=F', 'Copper Futures', 'Commodity', '3COPl_EQ', 'SCOPl_EQ'),
    ('URA', 'Global Uranium ETF', 'Commodity', 'URA', 'None'),
    
    # Global Titans
    ('TSM', 'Taiwan Semiconductor (TSMC)', 'Global Tech', '3TSMl_EQ', 'STSMl_EQ'),
    ('ASML', 'ASML Holding (Netherlands)', 'Global Tech', '3ASMl1_EQ', 'None'),
    ('NVO', 'Novo Nordisk (Denmark)', 'Healthcare', 'NVO', 'None'),
    ('SAP', 'SAP SE (Germany)', 'Enterprise Tech', '3SAPl_EQ', 'None'),
    ('BABA', 'Alibaba (China)', 'China Tech', '3BABl_EQ', '3SABl_EQ'),
    ('BHP', 'BHP Group (Global Mining)', 'Materials', 'BHP', 'None'),
    
    # Crypto / Digital Assets
    ('BTC-USD', 'Bitcoin', 'Crypto', 'BT3Ll_EQ', 'BT3Sl_EQ'),
    ('ETH-USD', 'Ethereum', 'Crypto', 'ET3Ll_EQ', 'ET3Sl_EQ'),
    ('SOL-USD', 'Solana', 'Crypto', 'SOL3l_EQ', 'None'),
]

print(f"Fetching live market data for {len(global_assets)} worldwide assets...")

def fetch_chart(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        q = d["chart"]["result"][0]["indicators"]["quote"][0]
        candles = []
        for o, h, l, c, v in zip(q["open"], q["high"], q["low"], q["close"], q.get("volume", [0] * len(q["close"]))):
            if c is not None and o is not None and h is not None and l is not None:
                candles.append({"open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v or 0)})
        return candles
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return []

results = []
for sym, name, cat, l3x, s3x in global_assets:
    candles = fetch_chart(sym)
    if len(candles) < 60:
        continue
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    
    cur_close = closes[-1]
    
    # 1. Moving Averages
    n_sma200 = min(200, len(closes))
    sma200 = sum(closes[-n_sma200:]) / n_sma200
    sma50 = sum(closes[-50:]) / 50.0
    
    def calc_ema(vals, span):
        a = 2.0 / (span + 1.0)
        e = vals[0]
        for v in vals[1:]:
            e = a * v + (1 - a) * e
        return e
    
    ema50 = calc_ema(closes[-100:], 50)
    ema20 = calc_ema(closes[-60:], 20)
    ema9 = calc_ema(closes[-30:], 9)
    
    sma200_dist = ((cur_close - sma200) / sma200) * 100
    sma50_dist = ((cur_close - sma50) / sma50) * 100
    
    # 2. RSI(14)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-14:]) / 14.0
    avg_loss = sum(losses[-14:]) / 14.0
    rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    
    # 3. ATR(14)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[-14:]) / 14.0
    atr_pct = (atr / cur_close) * 100
    
    # 4. ADX(14)
    plus_dms, minus_dms = [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dms.append(up if up > down and up > 0 else 0.0)
        minus_dms.append(down if down > up and down > 0 else 0.0)
    smooth_tr = sum(trs[-14:])
    plus_di = (sum(plus_dms[-14:]) / smooth_tr * 100) if smooth_tr > 0 else 0
    minus_di = (sum(minus_dms[-14:]) / smooth_tr * 100) if smooth_tr > 0 else 0
    di_sum = plus_di + minus_di
    adx = (abs(plus_di - minus_di) / di_sum * 100) if di_sum > 0 else 0
    
    # 5. MACD (12, 26, 9)
    ema12 = calc_ema(closes[-60:], 12)
    ema26 = calc_ema(closes[-60:], 26)
    macd_line = ema12 - ema26
    
    # 6. Bollinger Bands
    mean20 = sum(closes[-20:]) / 20.0
    std20 = math.sqrt(sum((x - mean20) ** 2 for x in closes[-20:]) / 20.0)
    bb_upper = mean20 + (2.0 * std20)
    bb_lower = mean20 - (2.0 * std20)
    bb_pct_b = (cur_close - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
    
    # 7. 52-Week & 20-Day Range
    high_52w = max(highs)
    low_52w = min(lows)
    dist_52w_high = ((cur_close - high_52w) / high_52w) * 100
    high_20d = max(highs[-20:])
    near_20d_high = cur_close >= high_20d * 0.985
    
    # Scoring
    bull_score = 0
    bear_score = 0
    
    # Trend
    if cur_close > sma200:
        bull_score += 2.0
    else:
        bear_score += 2.0
        
    if ema20 > sma50:
        bull_score += 2.0
    else:
        bear_score += 2.0
        
    if ema9 > ema20:
        bull_score += 1.0
    else:
        bear_score += 1.0
        
    if macd_line > 0:
        bull_score += 1.5
    else:
        bear_score += 1.5
        
    # Momentum (Sweet spot: strong trending but not yet exhausted)
    if 52 <= rsi <= 68:
        bull_score += 2.5
    elif rsi > 70:
        bull_score += 1.0  # overbought penalty
    elif 32 <= rsi <= 48:
        bear_score += 2.5
    elif rsi < 30:
        bear_score += 1.0  # oversold penalty
        
    # Trend strength (ADX)
    if adx >= 22:
        if cur_close > sma200:
            bull_score += 1.5
        else:
            bear_score += 1.5
            
    if near_20d_high:
        bull_score += 1.5

    # Determine direction
    is_bull = bull_score > bear_score
    score = bull_score if is_bull else bear_score
    direction = "LONG" if is_bull else "SHORT"
    
    # Calculate Trade Levels
    if is_bull:
        sl_dist = max(1.5 * atr, cur_close * 0.015)
        sl = round(cur_close - sl_dist, 2)
        r_dist = cur_close - sl
        tp1 = round(cur_close + 1.0 * r_dist, 2)
        tp2 = round(cur_close + 2.0 * r_dist, 2)
        tp3 = round(cur_close + 3.0 * r_dist, 2)
        tsl = sl
    else:
        sl_dist = max(1.5 * atr, cur_close * 0.015)
        sl = round(cur_close + sl_dist, 2)
        r_dist = sl - cur_close
        tp1 = round(cur_close - 1.0 * r_dist, 2)
        tp2 = round(cur_close - 2.0 * r_dist, 2)
        tp3 = round(cur_close - 3.0 * r_dist, 2)
        tsl = sl

    results.append({
        "symbol": sym,
        "name": name,
        "category": cat,
        "close": round(cur_close, 2),
        "direction": direction,
        "score": round(score, 1),
        "sma200": round(sma200, 2),
        "sma200_dist": round(sma200_dist, 1),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "rsi": round(rsi, 1),
        "adx": round(adx, 1),
        "atr": round(atr, 2),
        "atr_pct": round(atr_pct, 2),
        "macd": round(macd_line, 2),
        "bb_pct_b": round(bb_pct_b, 2),
        "dist_52w_high": round(dist_52w_high, 1),
        "sl": sl,
        "sl_pct": round(abs(cur_close - sl) / cur_close * 100, 2),
        "tp1": tp1,
        "tp1_pct": round(abs(tp1 - cur_close) / cur_close * 100, 2),
        "tp2": tp2,
        "tp2_pct": round(abs(tp2 - cur_close) / cur_close * 100, 2),
        "tp3": tp3,
        "tp3_pct": round(abs(tp3 - cur_close) / cur_close * 100, 2),
        "long_3x": l3x,
        "short_3x": s3x,
    })

results.sort(key=lambda x: x["score"], reverse=True)

print("\n" + "=" * 100)
print(f"{'SYMBOL':<9} | {'NAME':<28} | {'DIR':<6} | {'SCORE':<5} | {'CLOSE':<9} | {'RSI':<5} | {'ADX':<5} | {'SMA200%':<8} | {'52W HIGH%':<10}")
print("=" * 100)
for r in results:
    icon = "🟢" if r["direction"] == "LONG" else "🔴"
    print(f"{r['symbol']:<9} | {r['name']:<28} | {icon} {r['direction']:<4} | {r['score']:>4.1f} | ${r['close']:<8.2f} | {r['rsi']:<5.1f} | {r['adx']:<5.1f} | {r['sma200_dist']:>+6.1f}% | {r['dist_52w_high']:>+7.1f}%")

with open("scratch/worldwide_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\nTop 5 Absolute Best Worldwide Setups:")
for idx, r in enumerate(results[:5], 1):
    print(f"\n#{idx}: {r['name']} ({r['symbol']}) - {r['direction']}")
    print(f"   Category: {r['category']} | Score: {r['score']}/12.0")
    print(f"   Price: ${r['close']} | RSI: {r['rsi']} | ADX: {r['adx']} (Trend Strength)")
    print(f"   Entry: ${r['close']} | SL: ${r['sl']} (-{r['sl_pct']}%)")
    print(f"   TP1: ${r['tp1']} (+{r['tp1_pct']}%) | TP2: ${r['tp2']} (+{r['tp2_pct']}%) | TP3: ${r['tp3']} (+{r['tp3_pct']}%)")
    print(f"   Trading 212 3x ETP: {r['long_3x'] if r['direction'] == 'LONG' else r['short_3x']}")
