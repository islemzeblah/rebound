# app.py - Flask Web Application for Crypto Rebound Scanner (Dark Theme with Filtering)

from flask import Flask, render_template, request, jsonify, send_file
import requests
from datetime import datetime, timedelta
import json
import os
import csv
import io
import time
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
import threading
import base64

app = Flask(__name__)

# Global variable to store latest scan results
latest_results = []
last_scan_time = None
scan_lock = threading.Lock()
scan_progress = {"current": 0, "total": 0, "status": "idle"}

@dataclass
class ReboundResult:
    symbol: str
    current_price: float
    rebound_pct: float
    rebound_hours: float
    drawdown_from_high: float
    drawdown_21d: float           # New: 21d drawdown from highest to current
    drawdown_flag: str             # New: Flag based on drawdown severity
    price_change_48h: float
    price_change_96h: float
    price_change_21d: float
    volume_24h: float
    low_price: float
    low_time: str
    high_price: float
    high_time: str
    low_48h_price: float
    low_48h_time: str
    high_48h_price: float
    high_48h_time: str
    low_96h_price: float
    low_96h_time: str
    high_96h_price: float
    high_96h_time: str
    low_21d_price: float
    low_21d_time: str
    high_21d_price: float
    high_21d_time: str
    high_21d_for_drawdown: float  # New: Highest price in 21d for drawdown calc
    high_21d_time_for_drawdown: str # New: Time of that high
    time_display: str
    candles_count: int
    scan_time: str

class WebScanner:
    def __init__(self, config, progress_callback=None):
        self.base_url = "https://api.binance.com"
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        # Increase timeout and add retries
        self.session.mount('https://', requests.adapters.HTTPAdapter(
            max_retries=3,
            pool_connections=10,
            pool_maxsize=10
        ))
        
        # Configuration from web inputs
        self.lookback_hours = float(config.get('lookback_hours', 12))
        self.lookback_48h_hours = float(config.get('lookback_48h_hours', 48))
        self.lookback_96h_hours = float(config.get('lookback_96h_hours', 96))
        self.lookback_21d_hours = float(config.get('lookback_21d_hours', 720))
        self.min_price_change = float(config.get('min_price_change', 5))
        self.min_volume_24h = float(config.get('min_volume_24h', 1400000))
        self.max_drawdown = float(config.get('max_drawdown', 4.2))
        self.timeframe = config.get('timeframe', '15m')
        self.timeframe_21d = config.get('timeframe_21d', '1h')
        self.filter_timeframe = config.get('filter_timeframe', '21d')
        self.filter_enabled = config.get('filter_enabled') == 'on'
        self.green_circle_min = float(config.get('green_circle_min', 17.0))
        self.green_circle_max = float(config.get('green_circle_max', 21.0))
        
        self.progress_callback = progress_callback
        
        # Calculate candle limits
        self.candles_per_hour_15m = 4 if self.timeframe == "15m" else 1 if self.timeframe == "1h" else 0.25
        self.candles_per_hour_1h = 1 if self.timeframe_21d == "1h" else 0.25 if self.timeframe_21d == "4h" else 1/24
        
        self.candle_limit_recent = int(self.lookback_hours * self.candles_per_hour_15m) + 10
        self.candle_limit_48h = int(self.lookback_48h_hours * self.candles_per_hour_15m) + 20
        self.candle_limit_96h = int(self.lookback_96h_hours * self.candles_per_hour_15m) + 40
        self.candle_limit_21d = int(self.lookback_21d_hours * self.candles_per_hour_1h) + 10
        
        self.results = []
    
    def get_all_usdt_pairs(self) -> List[str]:
        """Get all USDT spot trading pairs with timeout."""
        try:
            url = f"{self.base_url}/api/v3/exchangeInfo"
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            pairs = []
            for symbol in data['symbols']:
                if (symbol['quoteAsset'] == 'USDT' and 
                    symbol['status'] == 'TRADING' and
                    symbol['isSpotTradingAllowed']):
                    pairs.append(symbol['symbol'])
            
            return pairs
        except Exception as e:
            print(f"Error fetching pairs: {e}")
            return []
    
    def get_ticker_24hr(self, symbol: str) -> Optional[Dict]:
        """Get 24hr ticker data."""
        try:
            url = f"{self.base_url}/api/v3/ticker/24hr"
            response = self.session.get(url, params={"symbol": symbol}, timeout=15)
            if response.status_code == 200:
                return response.json()
            return None
        except:
            return None
    
    def get_klines(self, symbol: str, interval: str, limit: int) -> Optional[List]:
        """Get klines for specified interval and limit."""
        try:
            url = f"{self.base_url}/api/v3/klines"
            params = {"symbol": symbol, "interval": interval, "limit": limit}
            response = self.session.get(url, params=params, timeout=20)
            if response.status_code == 200:
                return response.json()
            return None
        except:
            return None
    
    def analyze_time_window(self, symbol: str, lookback_hours: int, interval: str) -> Optional[Dict]:
        """Analyze time window to find lowest and highest price."""
        try:
            # Calculate appropriate limit based on interval
            if interval == "15m":
                candles_per_hour = 4
            elif interval == "1h":
                candles_per_hour = 1
            elif interval == "4h":
                candles_per_hour = 0.25
            elif interval == "1d":
                candles_per_hour = 1/24
            else:
                candles_per_hour = 1
            
            limit = int(lookback_hours * candles_per_hour) + 10
            if limit > 1000:
                limit = 1000
            
            klines = self.get_klines(symbol, interval, limit)
            if not klines or len(klines) < 10:
                return None
            
            candles = []
            for k in klines:
                candle_time = datetime.fromtimestamp(int(k[0]) / 1000)
                candles.append({
                    'time': candle_time,
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4])
                })
            
            candles.sort(key=lambda x: x['time'])
            now = datetime.now()
            cutoff_time = now - timedelta(hours=lookback_hours)
            window_candles = [c for c in candles if c['time'] >= cutoff_time]
            
            if len(window_candles) < 5:
                return None
            
            absolute_low = min(window_candles, key=lambda x: x['low'])
            low_price = absolute_low['low']
            low_time = absolute_low['time']
            
            candles_after_low = [c for c in window_candles if c['time'] > low_time]
            if not candles_after_low:
                return None
            
            highest_after_low = max(candles_after_low, key=lambda x: x['high'])
            high_price = highest_after_low['high']
            high_time = highest_after_low['time']
            
            if low_price <= 0:
                return None
            
            price_change = ((high_price - low_price) / low_price) * 100
            
            # Format time
            if lookback_hours >= 24:
                low_time_str = low_time.strftime('%m/%d %H:%M')
                high_time_str = high_time.strftime('%m/%d %H:%M')
            else:
                low_time_str = low_time.strftime('%H:%M')
                high_time_str = high_time.strftime('%H:%M')
            
            return {
                'low_price': low_price,
                'low_time': low_time_str,
                'high_price': high_price,
                'high_time': high_time_str,
                'price_change': price_change
            }
        except Exception as e:
            return None
    
    def analyze_21d_with_drawdown(self, symbol: str, current_price: float) -> Optional[Dict]:
        """Analyze 21d window to find highest price for drawdown calculation."""
        try:
            # Calculate appropriate limit based on interval
            if self.timeframe_21d == "1h":
                candles_per_hour = 1
            elif self.timeframe_21d == "4h":
                candles_per_hour = 0.25
            elif self.timeframe_21d == "1d":
                candles_per_hour = 1/24
            else:
                candles_per_hour = 1
            
            limit = int(self.lookback_21d_hours * candles_per_hour) + 10
            if limit > 1000:
                limit = 1000
            
            klines = self.get_klines(symbol, self.timeframe_21d, limit)
            if not klines or len(klines) < 10:
                return None
            
            candles = []
            for k in klines:
                candle_time = datetime.fromtimestamp(int(k[0]) / 1000)
                candles.append({
                    'time': candle_time,
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4])
                })
            
            candles.sort(key=lambda x: x['time'])
            now = datetime.now()
            cutoff_time = now - timedelta(hours=self.lookback_21d_hours)
            window_candles = [c for c in candles if c['time'] >= cutoff_time]
            
            if len(window_candles) < 5:
                return None
            
            # Find the absolute highest price in 21d window (for drawdown calculation)
            absolute_high = max(window_candles, key=lambda x: x['high'])
            high_price = absolute_high['high']
            high_time = absolute_high['time']
            
            # Calculate drawdown from highest price to current
            if high_price <= 0:
                return None
            
            drawdown_21d = ((high_price - current_price) / high_price) * 100
            
            # Determine flag based on drawdown percentage
            if drawdown_21d >= 30:
                drawdown_flag = "🔴 CRITICAL"  # Red flag for severe drawdown
            elif drawdown_21d >= 20:
                drawdown_flag = "🟠 HIGH"      # Orange flag for high drawdown
            elif drawdown_21d >= 10:
                drawdown_flag = "🟡 MEDIUM"    # Yellow flag for medium drawdown
            elif drawdown_21d >= 5:
                drawdown_flag = "🟢 LOW"       # Green flag for low drawdown
            else:
                drawdown_flag = "⚪ MINIMAL"    # White flag for minimal drawdown
            
            # Format time
            high_time_str = high_time.strftime('%m/%d %H:%M')
            
            return {
                'high_21d_price': high_price,
                'high_21d_time': high_time_str,
                'drawdown_21d': drawdown_21d,
                'drawdown_flag': drawdown_flag
            }
        except Exception as e:
            return None
    
    def format_time_display(self, hours: float) -> str:
        """Format time difference for display."""
        if hours >= 24:
            days = int(hours // 24)
            remaining_hours = int(hours % 24)
            if remaining_hours > 0:
                return f"{days}d{remaining_hours}h"
            return f"{days}d"
        elif hours >= 1:
            whole_hours = int(hours // 1)
            minutes = int((hours % 1) * 60)
            if minutes > 0:
                return f"{whole_hours}h{minutes}m"
            return f"{whole_hours}h"
        else:
            minutes = int(hours * 60)
            return f"{minutes}m"
    
    def get_filter_price_change(self, result):
        """Get price change based on selected filter timeframe."""
        if self.filter_timeframe == "48h":
            return result.price_change_48h
        elif self.filter_timeframe == "96h":
            return result.price_change_96h
        elif self.filter_timeframe == "21d":
            return result.price_change_21d
        return result.price_change_48h
    
    def analyze_pair(self, symbol: str) -> Optional[ReboundResult]:
        """Analyze a single pair."""
        try:
            ticker = self.get_ticker_24hr(symbol)
            if not ticker:
                return None
            
            current_price = float(ticker['lastPrice'])
            volume_24h = float(ticker['quoteVolume'])
            
            if volume_24h < self.min_volume_24h:
                return None
            
            # Analyze time windows
            analysis_48h = self.analyze_time_window(symbol, self.lookback_48h_hours, self.timeframe)
            if not analysis_48h:
                return None
            price_change_48h = analysis_48h['price_change']
            
            analysis_96h = self.analyze_time_window(symbol, self.lookback_96h_hours, self.timeframe)
            if not analysis_96h:
                return None
            price_change_96h = analysis_96h['price_change']
            
            analysis_21d = self.analyze_time_window(symbol, self.lookback_21d_hours, self.timeframe_21d)
            if not analysis_21d:
                return None
            price_change_21d = analysis_21d['price_change']
            
            # Analyze 21d for drawdown calculation
            analysis_21d_drawdown = self.analyze_21d_with_drawdown(symbol, current_price)
            if not analysis_21d_drawdown:
                return None
            
            # Apply filter if enabled
            if self.filter_enabled:
                if self.filter_timeframe == "48h" and analysis_48h is None:
                    return None
                elif self.filter_timeframe == "96h" and analysis_96h is None:
                    return None
                elif self.filter_timeframe == "21d" and analysis_21d is None:
                    return None
            
            # Analyze recent window
            klines = self.get_klines(symbol, self.timeframe, self.candle_limit_recent)
            if not klines or len(klines) < 4:
                return None
            
            candles = []
            for k in klines:
                candle_time = datetime.fromtimestamp(int(k[0]) / 1000)
                candles.append({
                    'time': candle_time,
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4])
                })
            
            candles.sort(key=lambda x: x['time'])
            now = datetime.now()
            cutoff_time = now - timedelta(hours=self.lookback_hours)
            recent_candles = [c for c in candles if c['time'] >= cutoff_time]
            
            if len(recent_candles) < 4:
                return None
            
            recent_lows = [(c['low'], c['time']) for c in recent_candles]
            absolute_low = min(recent_lows, key=lambda x: x[0])
            low_price = absolute_low[0]
            low_time = absolute_low[1]
            
            candles_after_low = [c for c in recent_candles if c['time'] > low_time]
            if not candles_after_low:
                return None
            
            highest_after_low = max(candles_after_low, key=lambda x: x['high'])
            high_price = highest_after_low['high']
            high_time = highest_after_low['time']
            
            time_diff_hours = (high_time - low_time).total_seconds() / 3600
            if time_diff_hours > self.lookback_hours:
                return None
            
            if low_price <= 0:
                return None
            rebound_pct = ((high_price - low_price) / low_price) * 100
            
            if rebound_pct < self.min_price_change:
                return None
            
            if high_price <= 0:
                return None
            drawdown = ((high_price - current_price) / high_price) * 100
            
            if drawdown > self.max_drawdown:
                return None
            
            time_display = self.format_time_display(time_diff_hours)
            
            return ReboundResult(
                symbol=symbol,
                current_price=current_price,
                rebound_pct=rebound_pct,
                rebound_hours=time_diff_hours,
                drawdown_from_high=drawdown,
                drawdown_21d=analysis_21d_drawdown['drawdown_21d'],
                drawdown_flag=analysis_21d_drawdown['drawdown_flag'],
                price_change_48h=price_change_48h,
                price_change_96h=price_change_96h,
                price_change_21d=price_change_21d,
                volume_24h=volume_24h,
                low_price=low_price,
                low_time=low_time.strftime('%H:%M'),
                high_price=high_price,
                high_time=high_time.strftime('%H:%M'),
                low_48h_price=analysis_48h['low_price'],
                low_48h_time=analysis_48h['low_time'],
                high_48h_price=analysis_48h['high_price'],
                high_48h_time=analysis_48h['high_time'],
                low_96h_price=analysis_96h['low_price'],
                low_96h_time=analysis_96h['low_time'],
                high_96h_price=analysis_96h['high_price'],
                high_96h_time=analysis_96h['high_time'],
                low_21d_price=analysis_21d['low_price'],
                low_21d_time=analysis_21d['low_time'],
                high_21d_price=analysis_21d['high_price'],
                high_21d_time=analysis_21d['high_time'],
                high_21d_for_drawdown=analysis_21d_drawdown['high_21d_price'],
                high_21d_time_for_drawdown=analysis_21d_drawdown['high_21d_time'],
                time_display=time_display,
                candles_count=len(recent_candles),
                scan_time=datetime.now().strftime('%H:%M:%S')
            )
        except Exception as e:
            return None
    
    def scan(self) -> List[ReboundResult]:
        """Run the scan with progress tracking."""
        print("Fetching pairs...")
        pairs = self.get_all_usdt_pairs()
        if not pairs:
            return []
        
        total_pairs = len(pairs)
        print(f"Scanning {total_pairs} pairs...")
        
        if self.progress_callback:
            self.progress_callback(0, total_pairs, "Starting scan...")
        
        results = []
        processed = 0
        
        with ThreadPoolExecutor(max_workers=25) as executor:
            future_to_symbol = {executor.submit(self.analyze_pair, symbol): symbol for symbol in pairs}
            
            for future in as_completed(future_to_symbol):
                result = future.result()
                if result:
                    results.append(result)
                
                processed += 1
                if self.progress_callback and processed % 10 == 0:
                    self.progress_callback(processed, total_pairs, f"Scanning... {processed}/{total_pairs}")
        
        if self.progress_callback:
            self.progress_callback(total_pairs, total_pairs, "Sorting results...")
        
        # Sort by filter timeframe
        results.sort(key=lambda x: self.get_filter_price_change(x), reverse=True)
        
        self.results = results
        return results

# Flask Routes
@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html')

@app.route('/progress')
def get_progress():
    """Get current scan progress."""
    global scan_progress
    return jsonify(scan_progress)

@app.route('/scan', methods=['POST'])
def scan():
    """Run a scan with the provided configuration."""
    global latest_results, last_scan_time, scan_progress
    
    config = request.form.to_dict()
    
    def update_progress(current, total, status):
        global scan_progress
        scan_progress = {
            "current": current,
            "total": total,
            "status": status,
            "percentage": int((current / total) * 100) if total > 0 else 0
        }
    
    try:
        scan_progress = {"current": 0, "total": 0, "status": "starting", "percentage": 0}
        
        scanner = WebScanner(config, progress_callback=update_progress)
        results = scanner.scan()
        
        with scan_lock:
            latest_results = results
            last_scan_time = datetime.now()
        
        scan_progress = {"current": 0, "total": 0, "status": "complete", "percentage": 100}
        
        # Convert results to dictionaries for JSON response
        results_dict = []
        for r in results[:100]:  # Limit to 100 for response
            r_dict = asdict(r)
            # Format numbers for display
            r_dict['current_price'] = f"${r.current_price:.4f}"
            r_dict['rebound_pct'] = f"{r.rebound_pct:.2f}%"
            r_dict['price_change_48h'] = f"{r.price_change_48h:.2f}%"
            r_dict['price_change_96h'] = f"{r.price_change_96h:.2f}%"
            r_dict['price_change_21d'] = f"{r.price_change_21d:.2f}%"
            r_dict['drawdown_from_high'] = f"{r.drawdown_from_high:.2f}%"
            r_dict['drawdown_21d'] = f"{r.drawdown_21d:.2f}%"
            r_dict['volume_24h'] = f"${r.volume_24h:,.0f}"
            results_dict.append(r_dict)
        
        return jsonify({
            'success': True,
            'count': len(results),
            'results': results_dict,
            'scan_time': last_scan_time.strftime('%Y-%m-%d %H:%M:%S')
        })
    except Exception as e:
        scan_progress = {"current": 0, "total": 0, "status": "error", "percentage": 0}
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download/csv')
def download_csv():
    """Download results as CSV."""
    global latest_results
    
    if not latest_results:
        return "No results available", 404
    
    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow([
        'Symbol', 'Current_Price', 'Rebound_7h', 'Rebound_Hours',
        'Drawdown_7h', 'Drawdown_21d', 'Drawdown_Flag', 'Change_48h', 'Change_96h', 'Change_21d', 'Volume_24h',
        'Low_7h', 'Low_Time_7h', 'High_7h', 'High_Time_7h',
        'Low_48h', 'Low_Time_48h', 'High_48h', 'High_Time_48h',
        'Low_96h', 'Low_Time_96h', 'High_96h', 'High_Time_96h',
        'Low_21d', 'Low_Time_21d', 'High_21d', 'High_Time_21d',
        'High_21d_Drawdown', 'High_21d_Time_Drawdown', 'Scan_Time'
    ])
    
    # Write data
    for r in latest_results:
        writer.writerow([
            r.symbol, r.current_price, r.rebound_pct, r.rebound_hours,
            r.drawdown_from_high, r.drawdown_21d, r.drawdown_flag,
            r.price_change_48h, r.price_change_96h, r.price_change_21d, r.volume_24h,
            r.low_price, r.low_time, r.high_price, r.high_time,
            r.low_48h_price, r.low_48h_time, r.high_48h_price, r.high_48h_time,
            r.low_96h_price, r.low_96h_time, r.high_96h_price, r.high_96h_time,
            r.low_21d_price, r.low_21d_time, r.high_21d_price, r.high_21d_time,
            r.high_21d_for_drawdown, r.high_21d_time_for_drawdown, r.scan_time
        ])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'scan_results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    )

@app.route('/download/json')
def download_json():
    """Download results as JSON."""
    global latest_results
    
    if not latest_results:
        return "No results available", 404
    
    data = [asdict(r) for r in latest_results]
    
    return send_file(
        io.BytesIO(json.dumps(data, indent=2, default=str).encode('utf-8')),
        mimetype='application/json',
        as_attachment=True,
        download_name=f'scan_results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    )

if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    if not os.path.exists('templates'):
        os.makedirs('templates')
    
    # Create the HTML template file with dark theme, filtering, and documentation
    html_content = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Crypto Rebound Scanner - Dark Theme</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0c10;
            color: #e5e9f0;
            line-height: 1.6;
        }

        .container {
            max-width: 1800px;
            margin: 0 auto;
            padding: 20px;
        }

        /* Header Styles */
        .header {
            background: #1a1e24;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            border: 1px solid #2d333b;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            position: relative;
        }

        .header-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }

        h1 {
            font-size: 1.8rem;
            font-weight: 600;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 8px;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }

        h1 svg {
            width: 32px;
            height: 32px;
            filter: drop-shadow(0 2px 4px rgba(0,0,0,0.3));
        }

        .btn-docs {
            background: #2d333b;
            color: #e5e9f0;
            border: 1px solid #3b82f6;
            padding: 10px 20px;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s;
        }

        .btn-docs:hover {
            background: #3b82f6;
            color: white;
            border-color: #3b82f6;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(59,130,246,0.3);
        }

        /* Documentation Modal */
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
            overflow-y: auto;
            backdrop-filter: blur(5px);
        }

        .modal-content {
            background: #1a1e24;
            margin: 40px auto;
            max-width: 1200px;
            border-radius: 16px;
            border: 1px solid #2d333b;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            animation: modalSlideIn 0.3s ease;
        }

        @keyframes modalSlideIn {
            from {
                transform: translateY(-50px);
                opacity: 0;
            }
            to {
                transform: translateY(0);
                opacity: 1;
            }
        }

        .modal-header {
            padding: 24px 30px;
            border-bottom: 1px solid #2d333b;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .modal-header h2 {
            font-size: 1.8rem;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .modal-close {
            background: none;
            border: none;
            color: #8b949e;
            font-size: 2rem;
            cursor: pointer;
            padding: 0 10px;
            transition: color 0.2s;
        }

        .modal-close:hover {
            color: #ef4444;
        }

        .modal-body {
            padding: 30px;
            max-height: 70vh;
            overflow-y: auto;
        }

        .modal-section {
            margin-bottom: 40px;
            background: #0d1117;
            border-radius: 12px;
            padding: 25px;
            border: 1px solid #2d333b;
        }

        .modal-section h3 {
            color: #3b82f6;
            font-size: 1.4rem;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .modal-section h4 {
            color: #e5e9f0;
            font-size: 1.1rem;
            margin: 20px 0 10px;
        }

        .doc-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }

        .doc-card {
            background: #1a1e24;
            border: 1px solid #2d333b;
            border-radius: 10px;
            padding: 20px;
        }

        .doc-card h5 {
            color: #3b82f6;
            font-size: 1rem;
            margin-bottom: 15px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .doc-card ul {
            list-style: none;
        }

        .doc-card li {
            margin-bottom: 12px;
            color: #8b949e;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .doc-card li strong {
            color: #e5e9f0;
            min-width: 100px;
        }

        .example-box {
            background: #0d1117;
            border: 1px solid #2d333b;
            border-radius: 8px;
            padding: 15px;
            margin: 15px 0;
            font-family: monospace;
            color: #3b82f6;
        }

        .color-sample {
            display: inline-block;
            width: 20px;
            height: 20px;
            border-radius: 4px;
            margin-right: 10px;
        }

        .flag-examples {
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            margin-top: 15px;
        }

        .flag-item {
            padding: 8px 15px;
            border-radius: 20px;
            background: #1a1e24;
            border: 1px solid #2d333b;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .step-guide {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        .step {
            display: flex;
            gap: 20px;
            align-items: flex-start;
        }

        .step-number {
            background: #3b82f6;
            color: white;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 1.2rem;
            flex-shrink: 0;
        }

        .step-content {
            flex: 1;
        }

        .step-content h4 {
            margin: 0 0 10px 0;
            color: #ffffff;
        }

        .step-content p {
            color: #8b949e;
        }

        /* Configuration Grid */
        .config-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }

        .config-item {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .config-item label {
            font-size: 0.85rem;
            font-weight: 500;
            color: #8b949e;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }

        .config-item input,
        .config-item select {
            padding: 10px 12px;
            border: 1px solid #2d333b;
            border-radius: 8px;
            font-size: 0.95rem;
            transition: all 0.2s;
            background: #0d1117;
            color: #e5e9f0;
        }

        .config-item input:focus,
        .config-item select:focus {
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59,130,246,0.2);
            background: #161b22;
        }

        .config-item.checkbox {
            flex-direction: row;
            align-items: center;
            gap: 8px;
        }

        .config-item.checkbox input {
            width: 18px;
            height: 18px;
            accent-color: #3b82f6;
        }

        /* Button Styles */
        .button-group {
            display: flex;
            gap: 12px;
            margin-top: 8px;
        }

        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 0.95rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }

        .btn-primary {
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white;
            border: 1px solid #3b82f6;
        }

        .btn-primary:hover:not(:disabled) {
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(59,130,246,0.3);
        }

        .btn-secondary {
            background: #1a1e24;
            color: #e5e9f0;
            border: 1px solid #2d333b;
        }

        .btn-secondary:hover:not(:disabled) {
            background: #2d333b;
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        /* Filter Bar */
        .filter-bar {
            background: #1a1e24;
            border-radius: 8px;
            padding: 15px 20px;
            margin: 20px 0;
            display: flex;
            align-items: center;
            gap: 15px;
            border: 1px solid #2d333b;
            flex-wrap: wrap;
        }

        .filter-label {
            color: #8b949e;
            font-size: 0.9rem;
            font-weight: 500;
        }

        .filter-badge {
            background: #0d1117;
            border: 1px solid #3b82f6;
            color: #3b82f6;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .filter-badge button {
            background: none;
            border: none;
            color: #8b949e;
            cursor: pointer;
            font-size: 1.1rem;
            padding: 0 4px;
        }

        .filter-badge button:hover {
            color: #ef4444;
        }

        .filter-input {
            display: flex;
            gap: 8px;
            align-items: center;
            flex: 1;
        }

        .filter-input input {
            padding: 8px 12px;
            border: 1px solid #2d333b;
            border-radius: 6px;
            background: #0d1117;
            color: #e5e9f0;
            width: 120px;
        }

        .filter-input select {
            padding: 8px 12px;
            border: 1px solid #2d333b;
            border-radius: 6px;
            background: #0d1117;
            color: #e5e9f0;
        }

        .filter-actions {
            display: flex;
            gap: 8px;
            margin-left: auto;
        }

        .btn-filter {
            padding: 8px 16px;
            background: #3b82f6;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.9rem;
        }

        .btn-filter:hover {
            background: #2563eb;
        }

        .btn-clear {
            padding: 8px 16px;
            background: #2d333b;
            color: #e5e9f0;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.9rem;
        }

        .btn-clear:hover {
            background: #404854;
        }

        /* Progress Bar */
        .progress-container {
            background: #1a1e24;
            border-radius: 12px;
            padding: 20px;
            margin: 20px 0;
            border: 1px solid #2d333b;
        }

        .progress-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 12px;
            color: #8b949e;
            font-size: 0.9rem;
        }

        .progress-bar-bg {
            width: 100%;
            height: 8px;
            background: #2d333b;
            border-radius: 4px;
            overflow: hidden;
        }

        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #3b82f6, #2563eb);
            transition: width 0.3s ease;
            border-radius: 4px;
        }

        .progress-status {
            margin-top: 8px;
            font-size: 0.9rem;
            color: #8b949e;
        }

        /* Status Bar */
        .status-bar {
            background: #1a1e24;
            border-radius: 8px;
            padding: 16px 20px;
            margin: 20px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border: 1px solid #2d333b;
            font-size: 0.95rem;
        }

        .status-message {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 8px #10b981;
        }

        .download-buttons {
            display: flex;
            gap: 8px;
        }

        .btn-small {
            padding: 8px 16px;
            background: #0d1117;
            border: 1px solid #2d333b;
            border-radius: 6px;
            font-size: 0.85rem;
            cursor: pointer;
            color: #e5e9f0;
            transition: all 0.2s;
        }

        .btn-small:hover {
            background: #2d333b;
        }

        /* Results Table */
        .results {
            background: #1a1e24;
            border-radius: 12px;
            padding: 20px;
            border: 1px solid #2d333b;
            margin-top: 20px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }

        .results h2 {
            font-size: 1.2rem;
            font-weight: 600;
            color: #ffffff;
            margin-bottom: 20px;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }

        .stat-card {
            background: #0d1117;
            border: 1px solid #2d333b;
            border-radius: 10px;
            padding: 16px;
        }

        .stat-card h3 {
            font-size: 0.8rem;
            color: #8b949e;
            text-transform: uppercase;
            letter-spacing: 0.3px;
            margin-bottom: 8px;
        }

        .stat-card .value {
            font-size: 1.4rem;
            font-weight: 600;
            color: #ffffff;
        }

        .table-container {
            overflow-x: auto;
            max-height: 600px;
            border: 1px solid #2d333b;
            border-radius: 8px;
            background: #0d1117;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
        }

        th {
            background: #1a1e24;
            color: #8b949e;
            font-weight: 500;
            padding: 14px 8px;
            text-align: left;
            border-bottom: 2px solid #2d333b;
            position: sticky;
            top: 0;
            z-index: 10;
            cursor: pointer;
            transition: background-color 0.2s;
        }

        th:hover {
            background: #2d333b;
            color: #ffffff;
        }

        th.filter-active {
            background: #3b82f6;
            color: white;
        }

        th.filter-active::after {
            content: " ▼";
            font-size: 0.8rem;
        }

        th.filter-asc::after {
            content: " ▲";
        }

        th.filter-desc::after {
            content: " ▼";
        }

        td {
            padding: 10px 8px;
            border-bottom: 1px solid #2d333b;
            color: #e5e9f0;
        }

        tr:hover {
            background: #2d333b;
        }

        .symbol {
            font-weight: 600;
            color: #3b82f6;
        }

        .green-circle {
            display: inline-block;
            margin-left: 6px;
            font-size: 0.9rem;
            filter: drop-shadow(0 0 4px #22c55e);
        }

        /* Drawdown flag colors */
        .flag-critical {
            color: #ef4444;
            font-weight: 600;
            background: rgba(239, 68, 68, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
        }
        
        .flag-high {
            color: #f97316;
            font-weight: 600;
            background: rgba(249, 115, 22, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
        }
        
        .flag-medium {
            color: #eab308;
            font-weight: 600;
            background: rgba(234, 179, 8, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
        }
        
        .flag-low {
            color: #22c55e;
            font-weight: 600;
            background: rgba(34, 197, 94, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
        }
        
        .flag-minimal {
            color: #9ca3af;
            font-weight: 600;
            background: rgba(156, 163, 175, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
        }

        /* Color classes for percentages - with background colors */
        .cell-red {
            background: linear-gradient(90deg, rgba(239, 68, 68, 0.2), rgba(239, 68, 68, 0.05));
            color: #f87171;
            font-weight: 500;
            border-left: 3px solid #ef4444;
        }
        
        .cell-orange {
            background: linear-gradient(90deg, rgba(249, 115, 22, 0.2), rgba(249, 115, 22, 0.05));
            color: #fb923c;
            font-weight: 500;
            border-left: 3px solid #f97316;
        }
        
        .cell-yellow {
            background: linear-gradient(90deg, rgba(234, 179, 8, 0.2), rgba(234, 179, 8, 0.05));
            color: #facc15;
            font-weight: 500;
            border-left: 3px solid #eab308;
        }
        
        .cell-green {
            background: linear-gradient(90deg, rgba(34, 197, 94, 0.2), rgba(34, 197, 94, 0.05));
            color: #4ade80;
            font-weight: 500;
            border-left: 3px solid #22c55e;
        }
        
        .cell-blue {
            background: linear-gradient(90deg, rgba(59, 130, 246, 0.2), rgba(59, 130, 246, 0.05));
            color: #60a5fa;
            font-weight: 500;
            border-left: 3px solid #3b82f6;
        }
        
        .cell-purple {
            background: linear-gradient(90deg, rgba(168, 85, 247, 0.2), rgba(168, 85, 247, 0.05));
            color: #c084fc;
            font-weight: 500;
            border-left: 3px solid #a855f7;
        }
        
        .cell-gray {
            background: #0d1117;
            color: #9ca3af;
            border-left: 3px solid #4b5563;
        }

        .loader {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 2px solid #2d333b;
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 0.6s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .hidden {
            display: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="header-top">
                <h1>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M12 2v20M17 5H9.5M17 12h-5M17 19h-5" stroke="#3b82f6"/>
                        <path d="M7 5h10v14H7V5z" stroke="#3b82f6" fill="none"/>
                    </svg>
                    Crypto Rebound Scanner
                </h1>
                <button class="btn-docs" onclick="openDocs()">
                    <span>📚</span> Documentation
                </button>
            </div>
            
            <form id="scanForm">
                <div class="config-grid">
                    <div class="config-item">
                        <label>Lookback Hours</label>
                        <input type="number" name="lookback_hours" value="12" step="1">
                    </div>
                    <div class="config-item">
                        <label>48h Lookback</label>
                        <input type="number" name="lookback_48h_hours" value="48" step="1">
                    </div>
                    <div class="config-item">
                        <label>96h Lookback</label>
                        <input type="number" name="lookback_96h_hours" value="96" step="1">
                    </div>
                    <div class="config-item">
                        <label>21d Lookback (hours)</label>
                        <input type="number" name="lookback_21d_hours" value="720" step="1">
                    </div>
                    <div class="config-item">
                        <label>Min Rebound %</label>
                        <input type="number" name="min_price_change" value="5" step="0.1">
                    </div>
                    <div class="config-item">
                        <label>Min Volume ($)</label>
                        <input type="number" name="min_volume_24h" value="1400000" step="100000">
                    </div>
                    <div class="config-item">
                        <label>Max Drawdown %</label>
                        <input type="number" name="max_drawdown" value="4.2" step="0.1">
                    </div>
                    <div class="config-item">
                        <label>Timeframe</label>
                        <select name="timeframe">
                            <option value="15m" selected>15m</option>
                            <option value="1h">1h</option>
                            <option value="4h">4h</option>
                        </select>
                    </div>
                    <div class="config-item">
                        <label>21d Timeframe</label>
                        <select name="timeframe_21d">
                            <option value="1h" selected>1h</option>
                            <option value="4h">4h</option>
                            <option value="1d">1d</option>
                        </select>
                    </div>
                    <div class="config-item">
                        <label>Filter By</label>
                        <select name="filter_timeframe">
                            <option value="48h">48h</option>
                            <option value="96h">96h</option>
                            <option value="21d" selected>21d</option>
                        </select>
                    </div>
                    <div class="config-item checkbox">
                        <input type="checkbox" name="filter_enabled" id="filter_enabled" checked>
                        <label for="filter_enabled">Enable Filter</label>
                    </div>
                    <div class="config-item">
                        <label>Green Circle Min %</label>
                        <input type="number" name="green_circle_min" value="17.0" step="0.1">
                    </div>
                    <div class="config-item">
                        <label>Green Circle Max %</label>
                        <input type="number" name="green_circle_max" value="21.0" step="0.1">
                    </div>
                </div>

                <div class="button-group">
                    <button type="submit" class="btn btn-primary" id="scanBtn">
                        <span>🔍</span> Run Scan
                    </button>
                    <button type="button" class="btn btn-secondary" id="resetBtn">
                        <span>↺</span> Reset
                    </button>
                </div>
            </form>
        </div>

        <!-- Progress Bar -->
        <div class="progress-container hidden" id="progressContainer">
            <div class="progress-header">
                <span id="progressStatus">Initializing...</span>
                <span id="progressPercent">0%</span>
            </div>
            <div class="progress-bar-bg">
                <div class="progress-bar-fill" id="progressBar" style="width: 0%"></div>
            </div>
            <div class="progress-status" id="progressDetail"></div>
        </div>

        <!-- Status Bar -->
        <div class="status-bar hidden" id="statusBar">
            <div class="status-message">
                <span class="status-dot"></span>
                <span id="statusMessage"></span>
            </div>
            <div class="download-buttons">
                <button class="btn-small" onclick="downloadCSV()">📥 CSV</button>
                <button class="btn-small" onclick="downloadJSON()">📥 JSON</button>
            </div>
        </div>

        <!-- Filter Bar -->
        <div class="filter-bar hidden" id="filterBar">
            <span class="filter-label">Filter by:</span>
            <div class="filter-input">
                <select id="filterColumn">
                    <option value="symbol">Symbol</option>
                    <option value="price">Price</option>
                    <option value="rebound">12h %</option>
                    <option value="48h">48h %</option>
                    <option value="96h">96h %</option>
                    <option value="21d">21d %</option>
                    <option value="time">Time</option>
                    <option value="drawdown">Drawdown 7h %</option>
                    <option value="drawdown21d">Drawdown 21d %</option>
                    <option value="drawdownflag">Drawdown Flag</option>
                    <option value="volume">Volume</option>
                </select>
                <select id="filterOperator">
                    <option value="contains">contains</option>
                    <option value="equals">equals</option>
                    <option value="greater">greater than</option>
                    <option value="less">less than</option>
                    <option value="between">between</option>
                </select>
                <input type="text" id="filterValue" placeholder="value">
                <input type="text" id="filterValue2" placeholder="max value" class="hidden" style="width:100px">
            </div>
            <div class="filter-actions">
                <button class="btn-filter" onclick="applyFilter()">Apply</button>
                <button class="btn-clear" onclick="clearFilter()">Clear</button>
            </div>
            <div id="activeFilter" class="hidden">
                <span class="filter-badge">
                    <span id="activeFilterText"></span>
                    <button onclick="clearFilter()">✕</button>
                </span>
            </div>
        </div>

        <!-- Results -->
        <div class="results hidden" id="results">
            <h2>📊 Scan Results</h2>
            <div class="stats-grid" id="stats"></div>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th onclick="sortTable('index')">#</th>
                            <th onclick="sortTable('symbol')">Symbol</th>
                            <th onclick="sortTable('price')">Price</th>
                            <th onclick="sortTable('rebound')">12h</th>
                            <th onclick="sortTable('48h')">48h</th>
                            <th onclick="sortTable('96h')">96h</th>
                            <th onclick="sortTable('21d')">21d</th>
                            <th onclick="sortTable('time')">Time</th>
                            <th onclick="sortTable('drawdown')">DD 7h</th>
                            <th onclick="sortTable('drawdown21d')">DD 21d</th>
                            <th>Flag</th>
                            <th>21d High</th>
                            <th>48h Range</th>
                            <th>96h Range</th>
                            <th>21d Range</th>
                            <th onclick="sortTable('volume')">Volume</th>
                        </tr>
                    </thead>
                    <tbody id="tableBody"></tbody>
                </table>
            </div>
            <div id="noResults" class="hidden" style="text-align: center; padding: 40px; color: #8b949e;">
                No results match the current filter
            </div>
        </div>
    </div>

    <!-- Documentation Modal -->
    <div id="docsModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>
                    <span>📚</span>
                    Crypto Rebound Scanner Documentation
                </h2>
                <button class="modal-close" onclick="closeDocs()">&times;</button>
            </div>
            <div class="modal-body">
                <!-- Quick Start Guide -->
                <div class="modal-section">
                    <h3>🚀 Quick Start Guide</h3>
                    <div class="step-guide">
                        <div class="step">
                            <div class="step-number">1</div>
                            <div class="step-content">
                                <h4>Configure Your Scan</h4>
                                <p>Adjust the input parameters in the configuration panel at the top. Each field controls different aspects of the scan.</p>
                            </div>
                        </div>
                        <div class="step">
                            <div class="step-number">2</div>
                            <div class="step-content">
                                <h4>Run the Scan</h4>
                                <p>Click the "Run Scan" button to start analyzing all USDT pairs on Binance. A progress bar will show the scan status.</p>
                            </div>
                        </div>
                        <div class="step">
                            <div class="step-number">3</div>
                            <div class="step-content">
                                <h4>Analyze Results</h4>
                                <p>View the results table with color-coded percentages. Use the filter bar to narrow down results and click column headers to sort.</p>
                            </div>
                        </div>
                        <div class="step">
                            <div class="step-number">4</div>
                            <div class="step-content">
                                <h4>Export Data</h4>
                                <p>Download results as CSV or JSON using the buttons in the status bar.</p>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Configuration Parameters -->
                <div class="modal-section">
                    <h3>⚙️ Configuration Parameters</h3>
                    <div class="doc-grid">
                        <div class="doc-card">
                            <h5>Time Windows</h5>
                            <ul>
                                <li><strong>Lookback Hours:</strong> Recent rebound window (default: 12h)</li>
                                <li><strong>48h Lookback:</strong> 2-day price change analysis</li>
                                <li><strong>96h Lookback:</strong> 4-day price change analysis</li>
                                <li><strong>21d Lookback:</strong> 3-week price change analysis</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Filters</h5>
                            <ul>
                                <li><strong>Min Rebound %:</strong> Minimum recent price increase (default: 5%)</li>
                                <li><strong>Min Volume:</strong> Minimum 24h trading volume (default: $1.4M)</li>
                                <li><strong>Max Drawdown:</strong> Maximum allowed drawdown (default: 4.2%)</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Timeframes</h5>
                            <ul>
                                <li><strong>Timeframe:</strong> Candle interval for recent analysis (15m/1h/4h)</li>
                                <li><strong>21d Timeframe:</strong> Candle interval for 21d analysis (1h/4h/1d)</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Special Features</h5>
                            <ul>
                                <li><strong>Filter By:</strong> Choose which timeframe to highlight</li>
                                <li><strong>Green Circle:</strong> Custom range for 🟢 indicator</li>
                            </ul>
                        </div>
                    </div>
                </div>

                <!-- Column Explanations -->
                <div class="modal-section">
                    <h3>📊 Column Explanations</h3>
                    <div class="doc-grid">
                        <div class="doc-card">
                            <h5>Basic Info</h5>
                            <ul>
                                <li><strong>#:</strong> Row number (sorted order)</li>
                                <li><strong>Symbol:</strong> Trading pair (e.g., BTCUSDT)</li>
                                <li><strong>Price:</strong> Current market price</li>
                                <li><strong>Volume:</strong> 24h trading volume</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Percentage Changes</h5>
                            <ul>
                                <li><strong>12h:</strong> Recent rebound percentage</li>
                                <li><strong>48h:</strong> Price change over 48 hours</li>
                                <li><strong>96h:</strong> Price change over 96 hours</li>
                                <li><strong>21d:</strong> Price change over 21 days</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Drawdowns</h5>
                            <ul>
                                <li><strong>DD 7h:</strong> Drawdown from recent high</li>
                                <li><strong>DD 21d:</strong> Drawdown from 21-day high</li>
                                <li><strong>Flag:</strong> Severity indicator (CRITICAL/HIGH/MEDIUM/LOW/MINIMAL)</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Range Information</h5>
                            <ul>
                                <li><strong>21d High:</strong> Highest price in 21 days with timestamp</li>
                                <li><strong>48h/96h/21d Range:</strong> Low → High price ranges with timestamps</li>
                            </ul>
                        </div>
                    </div>
                </div>

                <!-- Color Coding Legend -->
                <div class="modal-section">
                    <h3>🎨 Color Coding Legend</h3>
                    <div class="doc-grid">
                        <div class="doc-card">
                            <h5>Percentage Colors</h5>
                            <ul>
                                <li><span class="color-sample" style="background: #ef4444;"></span> <strong>Red:</strong> >30% (Extreme)</li>
                                <li><span class="color-sample" style="background: #f97316;"></span> <strong>Orange:</strong> 20-30% (Very High)</li>
                                <li><span class="color-sample" style="background: #eab308;"></span> <strong>Yellow:</strong> 10-20% (High)</li>
                                <li><span class="color-sample" style="background: #22c55e;"></span> <strong>Green:</strong> 5-10% (Moderate)</li>
                                <li><span class="color-sample" style="background: #3b82f6;"></span> <strong>Blue:</strong> 0-5% (Low)</li>
                                <li><span class="color-sample" style="background: #9ca3af;"></span> <strong>Gray:</strong> Negative</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>21d Special Colors</h5>
                            <ul>
                                <li><span class="color-sample" style="background: #a855f7;"></span> <strong>Purple:</strong> >42% (21d only)</li>
                                <li><span class="color-sample" style="background: #3b82f6;"></span> <strong>Blue:</strong> 35-42%</li>
                                <li><span class="color-sample" style="background: #22c55e;"></span> <strong>Green:</strong> 20-35%</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Volume Colors</h5>
                            <ul>
                                <li><span class="color-sample" style="background: #a855f7;"></span> <strong>Purple:</strong> >$10M</li>
                                <li><span class="color-sample" style="background: #3b82f6;"></span> <strong>Blue:</strong> $5-10M</li>
                                <li><span class="color-sample" style="background: #22c55e;"></span> <strong>Green:</strong> $2-5M</li>
                                <li><span class="color-sample" style="background: #eab308;"></span> <strong>Yellow:</strong> $1-2M</li>
                            </ul>
                        </div>
                    </div>
                </div>

                <!-- Drawdown Flags -->
                <div class="modal-section">
                    <h3>🚩 Drawdown Flags</h3>
                    <div class="flag-examples">
                        <div class="flag-item"><span class="flag-critical">🔴 CRITICAL</span> ≥30% drawdown</div>
                        <div class="flag-item"><span class="flag-high">🟠 HIGH</span> 20-30% drawdown</div>
                        <div class="flag-item"><span class="flag-medium">🟡 MEDIUM</span> 10-20% drawdown</div>
                        <div class="flag-item"><span class="flag-low">🟢 LOW</span> 5-10% drawdown</div>
                        <div class="flag-item"><span class="flag-minimal">⚪ MINIMAL</span> <5% drawdown</div>
                    </div>
                    <p style="margin-top: 20px; color: #8b949e;">Flags indicate how far the current price has fallen from the 21-day high. Higher flags suggest better entry opportunities but also higher risk.</p>
                </div>

                <!-- Green Circle Indicator -->
                <div class="modal-section">
                    <h3>🟢 Green Circle Indicator</h3>
                    <p>The green circle 🟢 appears next to symbols whose price change (in your selected filter timeframe) falls within your custom range.</p>
                    <div class="example-box">
                        Example: If Filter By = "21d" and Green Circle Min/Max = 17.0-21.0<br>
                        → Shows 🟢 for coins with 17-21% 21-day price change
                    </div>
                </div>

                <!-- Filtering and Sorting -->
                <div class="modal-section">
                    <h3>🔍 Filtering & Sorting</h3>
                    <div class="doc-grid">
                        <div class="doc-card">
                            <h5>Sorting</h5>
                            <ul>
                                <li>Click any column header to sort by that column</li>
                                <li>Click again to toggle ascending/descending</li>
                                <li>Blue header indicates active sort</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Filtering</h5>
                            <ul>
                                <li>Select column to filter</li>
                                <li>Choose operator (contains/equals/greater/less/between)</li>
                                <li>Enter value(s) and click Apply</li>
                                <li>Active filter shows as badge</li>
                            </ul>
                        </div>
                    </div>
                </div>

                <!-- Understanding the Data -->
                <div class="modal-section">
                    <h3>📈 Understanding the Data</h3>
                    <div class="doc-card">
                        <h5>Rebound Scanner Logic</h5>
                        <p>The scanner looks for coins that have:</p>
                        <ol style="color: #8b949e; margin-left: 20px;">
                            <li>Made a significant low in the recent lookback window</li>
                            <li>Rebounded strongly from that low (≥ Min Rebound %)</li>
                            <li>Not pulled back too much (≤ Max Drawdown %)</li>
                            <li>Sufficient trading volume (≥ Min Volume)</li>
                        </ol>
                        <p style="margin-top: 15px;">This identifies potential momentum plays that haven't yet retraced significantly.</p>
                    </div>
                </div>

                <!-- Tips & Tricks -->
                <div class="modal-section">
                    <h3>💡 Tips & Tricks</h3>
                    <ul style="color: #8b949e; margin-left: 20px;">
                        <li><strong>Lower Min Rebound</strong> → More results, less strict</li>
                        <li><strong>Higher Min Volume</strong> → More liquid, safer plays</li>
                        <li><strong>Lower Max Drawdown</strong> → Fresher rebounds, less pullback</li>
                        <li><strong>Use flags</strong> to identify potential entries (CRITICAL flags = deep discount)</li>
                        <li><strong>Export to CSV</strong> for further analysis in Excel</li>
                        <li><strong>Green circle</strong> helps spot coins in your target range</li>
                    </ul>
                </div>

                <!-- Technical Details -->
                <div class="modal-section">
                    <h3>🔧 Technical Details</h3>
                    <div class="doc-grid">
                        <div class="doc-card">
                            <h5>Data Source</h5>
                            <ul>
                                <li>Binance API (real-time)</li>
                                <li>All USDT spot pairs</li>
                                <li>Updates on each scan</li>
                            </ul>
                        </div>
                        <div class="doc-card">
                            <h5>Calculation Methods</h5>
                            <ul>
                                <li>Low → High price changes</li>
                                <li>Drawdown: (High - Current)/High</li>
                                <li>Timeframes: 15m/1h/4h candles</li>
                            </ul>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let progressInterval;
        let originalResults = [];
        let filteredResults = [];
        let currentSort = { column: null, direction: 'desc' };
        let currentFilter = { column: null, operator: null, value: null, value2: null };

        // Documentation functions
        function openDocs() {
            document.getElementById('docsModal').style.display = 'block';
            document.body.style.overflow = 'hidden';
        }

        function closeDocs() {
            document.getElementById('docsModal').style.display = 'none';
            document.body.style.overflow = 'auto';
        }

        // Close modal when clicking outside
        window.onclick = function(event) {
            const modal = document.getElementById('docsModal');
            if (event.target == modal) {
                closeDocs();
            }
        }

        // Close on Escape key
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                closeDocs();
            }
        });

        document.getElementById('scanForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const scanBtn = document.getElementById('scanBtn');
            const progressContainer = document.getElementById('progressContainer');
            const results = document.getElementById('results');
            const statusBar = document.getElementById('statusBar');
            const filterBar = document.getElementById('filterBar');
            
            scanBtn.disabled = true;
            progressContainer.classList.remove('hidden');
            results.classList.add('hidden');
            statusBar.classList.add('hidden');
            filterBar.classList.add('hidden');
            
            // Start polling for progress
            progressInterval = setInterval(updateProgress, 500);
            
            const formData = new FormData(this);
            
            try {
                const response = await fetch('/scan', {
                    method: 'POST',
                    body: formData
                });
                
                const data = await response.json();
                
                clearInterval(progressInterval);
                progressContainer.classList.add('hidden');
                
                if (data.success) {
                    originalResults = data.results;
                    filteredResults = [...originalResults];
                    currentFilter = { column: null, operator: null, value: null, value2: null };
                    displayResults();
                    statusBar.classList.remove('hidden');
                    filterBar.classList.remove('hidden');
                    document.getElementById('statusMessage').textContent = 
                        `Scan completed at ${data.scan_time} • Found ${data.count} rebounds`;
                } else {
                    alert('Error: ' + data.error);
                }
            } catch (error) {
                clearInterval(progressInterval);
                progressContainer.classList.add('hidden');
                alert('Error: ' + error.message);
            } finally {
                scanBtn.disabled = false;
            }
        });

        async function updateProgress() {
            try {
                const response = await fetch('/progress');
                const progress = await response.json();
                
                document.getElementById('progressStatus').textContent = progress.status;
                document.getElementById('progressPercent').textContent = progress.percentage + '%';
                document.getElementById('progressBar').style.width = progress.percentage + '%';
                document.getElementById('progressDetail').textContent = 
                    `Processed ${progress.current} / ${progress.total} pairs`;
            } catch (error) {
                console.error('Progress update error:', error);
            }
        }

        document.getElementById('resetBtn').addEventListener('click', function() {
            document.querySelectorAll('input[type="number"]').forEach(input => {
                if (input.name === 'lookback_hours') input.value = '12';
                if (input.name === 'lookback_48h_hours') input.value = '48';
                if (input.name === 'lookback_96h_hours') input.value = '96';
                if (input.name === 'lookback_21d_hours') input.value = '720';
                if (input.name === 'min_price_change') input.value = '5';
                if (input.name === 'min_volume_24h') input.value = '1400000';
                if (input.name === 'max_drawdown') input.value = '4.2';
                if (input.name === 'green_circle_min') input.value = '17.0';
                if (input.name === 'green_circle_max') input.value = '21.0';
            });
            
            document.querySelectorAll('select').forEach(select => {
                if (select.name === 'timeframe') select.value = '15m';
                if (select.name === 'timeframe_21d') select.value = '1h';
                if (select.name === 'filter_timeframe') select.value = '21d';
            });
            
            document.getElementById('filter_enabled').checked = true;
        });

        document.getElementById('filterOperator').addEventListener('change', function() {
            const value2 = document.getElementById('filterValue2');
            if (this.value === 'between') {
                value2.classList.remove('hidden');
            } else {
                value2.classList.add('hidden');
            }
        });

        function applyFilter() {
            const column = document.getElementById('filterColumn').value;
            const operator = document.getElementById('filterOperator').value;
            const value = document.getElementById('filterValue').value;
            const value2 = document.getElementById('filterValue2').value;
            
            if (!value) {
                alert('Please enter a filter value');
                return;
            }
            
            currentFilter = { column, operator, value, value2 };
            
            filteredResults = originalResults.filter(row => {
                let cellValue;
                switch(column) {
                    case 'symbol':
                        cellValue = row.symbol;
                        break;
                    case 'price':
                        cellValue = parseFloat(row.current_price.replace('$', ''));
                        break;
                    case 'rebound':
                        cellValue = parseFloat(row.rebound_pct);
                        break;
                    case '48h':
                        cellValue = parseFloat(row.price_change_48h);
                        break;
                    case '96h':
                        cellValue = parseFloat(row.price_change_96h);
                        break;
                    case '21d':
                        cellValue = parseFloat(row.price_change_21d);
                        break;
                    case 'time':
                        cellValue = row.time_display;
                        break;
                    case 'drawdown':
                        cellValue = parseFloat(row.drawdown_from_high);
                        break;
                    case 'drawdown21d':
                        cellValue = parseFloat(row.drawdown_21d);
                        break;
                    case 'drawdownflag':
                        cellValue = row.drawdown_flag;
                        break;
                    case 'volume':
                        cellValue = parseFloat(row.volume_24h.replace(/[$,]/g, ''));
                        break;
                    default:
                        cellValue = row.symbol;
                }
                
                const filterVal = isNaN(parseFloat(value)) ? value : parseFloat(value);
                const filterVal2 = value2 ? (isNaN(parseFloat(value2)) ? value2 : parseFloat(value2)) : null;
                
                switch(operator) {
                    case 'contains':
                        return String(cellValue).toLowerCase().includes(String(filterVal).toLowerCase());
                    case 'equals':
                        return String(cellValue) === String(filterVal);
                    case 'greater':
                        return parseFloat(cellValue) > parseFloat(filterVal);
                    case 'less':
                        return parseFloat(cellValue) < parseFloat(filterVal);
                    case 'between':
                        return parseFloat(cellValue) >= parseFloat(filterVal) && 
                               parseFloat(cellValue) <= parseFloat(filterVal2);
                    default:
                        return true;
                }
            });
            
            // Update filter badge
            const activeFilter = document.getElementById('activeFilter');
            const filterText = document.getElementById('activeFilterText');
            
            filterText.textContent = `${column} ${operator} ${value}${operator === 'between' ? ' and ' + value2 : ''}`;
            activeFilter.classList.remove('hidden');
            
            // Re-apply current sort
            if (currentSort.column) {
                sortTable(currentSort.column, true);
            } else {
                displayResults();
            }
        }

        function clearFilter() {
            currentFilter = { column: null, operator: null, value: null, value2: null };
            filteredResults = [...originalResults];
            document.getElementById('activeFilter').classList.add('hidden');
            document.getElementById('filterValue').value = '';
            document.getElementById('filterValue2').value = '';
            document.getElementById('filterValue2').classList.add('hidden');
            
            if (currentSort.column) {
                sortTable(currentSort.column, true);
            } else {
                displayResults();
            }
        }

        function sortTable(column, skipToggle = false) {
            if (!skipToggle) {
                if (currentSort.column === column) {
                    currentSort.direction = currentSort.direction === 'desc' ? 'asc' : 'desc';
                } else {
                    currentSort.column = column;
                    currentSort.direction = 'desc';
                }
            }
            
            // Update header styles
            document.querySelectorAll('th').forEach(th => {
                th.classList.remove('filter-active', 'filter-asc', 'filter-desc');
            });
            
            const headerIndex = {
                'index': 0,
                'symbol': 1,
                'price': 2,
                'rebound': 3,
                '48h': 4,
                '96h': 5,
                '21d': 6,
                'time': 7,
                'drawdown': 8,
                'drawdown21d': 9,
                'volume': 15
            }[currentSort.column];
            
            if (headerIndex !== undefined) {
                const header = document.querySelectorAll('th')[headerIndex];
                header.classList.add('filter-active');
                header.classList.add(currentSort.direction === 'desc' ? 'filter-desc' : 'filter-asc');
            }
            
            filteredResults.sort((a, b) => {
                let valA, valB;
                
                switch(currentSort.column) {
                    case 'index':
                        return 0;
                    case 'symbol':
                        valA = a.symbol;
                        valB = b.symbol;
                        break;
                    case 'price':
                        valA = parseFloat(a.current_price.replace('$', ''));
                        valB = parseFloat(b.current_price.replace('$', ''));
                        break;
                    case 'rebound':
                        valA = parseFloat(a.rebound_pct);
                        valB = parseFloat(b.rebound_pct);
                        break;
                    case '48h':
                        valA = parseFloat(a.price_change_48h);
                        valB = parseFloat(b.price_change_48h);
                        break;
                    case '96h':
                        valA = parseFloat(a.price_change_96h);
                        valB = parseFloat(b.price_change_96h);
                        break;
                    case '21d':
                        valA = parseFloat(a.price_change_21d);
                        valB = parseFloat(b.price_change_21d);
                        break;
                    case 'time':
                        valA = a.time_display;
                        valB = b.time_display;
                        break;
                    case 'drawdown':
                        valA = parseFloat(a.drawdown_from_high);
                        valB = parseFloat(b.drawdown_from_high);
                        break;
                    case 'drawdown21d':
                        valA = parseFloat(a.drawdown_21d);
                        valB = parseFloat(b.drawdown_21d);
                        break;
                    case 'volume':
                        valA = parseFloat(a.volume_24h.replace(/[$,]/g, ''));
                        valB = parseFloat(b.volume_24h.replace(/[$,]/g, ''));
                        break;
                    default:
                        return 0;
                }
                
                if (typeof valA === 'string' && typeof valB === 'string') {
                    return currentSort.direction === 'desc' 
                        ? valB.localeCompare(valA)
                        : valA.localeCompare(valB);
                } else {
                    return currentSort.direction === 'desc'
                        ? valB - valA
                        : valA - valB;
                }
            });
            
            displayResults();
        }

        function displayResults() {
            const results = document.getElementById('results');
            const tableBody = document.getElementById('tableBody');
            const statsDiv = document.getElementById('stats');
            const noResults = document.getElementById('noResults');
            
            results.classList.remove('hidden');
            
            if (filteredResults.length === 0) {
                noResults.classList.remove('hidden');
                tableBody.innerHTML = '';
                statsDiv.innerHTML = `
                    <div class="stat-card">
                        <h3>Total Results</h3>
                        <div class="value">0</div>
                    </div>
                    <div class="stat-card">
                        <h3>Filtered Results</h3>
                        <div class="value">0</div>
                    </div>
                    <div class="stat-card">
                        <h3>Filter Active</h3>
                        <div class="value">Yes</div>
                    </div>
                `;
                return;
            }
            
            noResults.classList.add('hidden');
            
            // Calculate stats
            const filterChanges = filteredResults.map(r => {
                const filterTimeframe = document.querySelector('select[name="filter_timeframe"]').value;
                if (filterTimeframe === '48h') return parseFloat(r.price_change_48h);
                if (filterTimeframe === '96h') return parseFloat(r.price_change_96h);
                return parseFloat(r.price_change_21d);
            });
            
            const reboundPcts = filteredResults.map(r => parseFloat(r.rebound_pct));
            const drawdown21d = filteredResults.map(r => parseFloat(r.drawdown_21d));
            
            statsDiv.innerHTML = `
                <div class="stat-card">
                    <h3>Total Results</h3>
                    <div class="value">${originalResults.length}</div>
                </div>
                <div class="stat-card">
                    <h3>Filtered Results</h3>
                    <div class="value">${filteredResults.length}</div>
                </div>
                <div class="stat-card">
                    <h3>${document.querySelector('select[name="filter_timeframe"]').value} Range</h3>
                    <div class="value">${Math.min(...filterChanges).toFixed(1)}% - ${Math.max(...filterChanges).toFixed(1)}%</div>
                </div>
                <div class="stat-card">
                    <h3>Rebound Range</h3>
                    <div class="value">${Math.min(...reboundPcts).toFixed(1)}% - ${Math.max(...reboundPcts).toFixed(1)}%</div>
                </div>
                <div class="stat-card">
                    <h3>21d Drawdown Range</h3>
                    <div class="value">${Math.min(...drawdown21d).toFixed(1)}% - ${Math.max(...drawdown21d).toFixed(1)}%</div>
                </div>
            `;
            
            // Populate table with colorized cells
            tableBody.innerHTML = filteredResults.map((r, index) => {
                const filterTimeframe = document.querySelector('select[name="filter_timeframe"]').value;
                let filterValue;
                if (filterTimeframe === '48h') filterValue = parseFloat(r.price_change_48h);
                else if (filterTimeframe === '96h') filterValue = parseFloat(r.price_change_96h);
                else filterValue = parseFloat(r.price_change_21d);
                
                const greenCircleMin = parseFloat(document.querySelector('input[name="green_circle_min"]').value);
                const greenCircleMax = parseFloat(document.querySelector('input[name="green_circle_max"]').value);
                
                const hasGreenCircle = filterValue >= greenCircleMin && filterValue <= greenCircleMax;
                
                // Determine flag class
                let flagClass = '';
                if (r.drawdown_flag.includes('CRITICAL')) flagClass = 'flag-critical';
                else if (r.drawdown_flag.includes('HIGH')) flagClass = 'flag-high';
                else if (r.drawdown_flag.includes('MEDIUM')) flagClass = 'flag-medium';
                else if (r.drawdown_flag.includes('LOW')) flagClass = 'flag-low';
                else flagClass = 'flag-minimal';
                
                return `
                    <tr>
                        <td>${index + 1}</td>
                        <td class="symbol">${r.symbol}${hasGreenCircle ? '<span class="green-circle">🟢</span>' : ''}</td>
                        <td>${r.current_price}</td>
                        <td class="${getCellClass(parseFloat(r.rebound_pct))}">${r.rebound_pct}</td>
                        <td class="${getCellClass(parseFloat(r.price_change_48h))}">${r.price_change_48h}</td>
                        <td class="${getCellClass(parseFloat(r.price_change_96h))}">${r.price_change_96h}</td>
                        <td class="${getCellClass(parseFloat(r.price_change_21d), true)}">${r.price_change_21d}</td>
                        <td>${r.time_display}</td>
                        <td class="${getCellClass(parseFloat(r.drawdown_from_high))}">${r.drawdown_from_high}</td>
                        <td class="${getCellClass(parseFloat(r.drawdown_21d))}">${r.drawdown_21d}</td>
                        <td><span class="${flagClass}">${r.drawdown_flag}</span></td>
                        <td><small>${r.high_21d_time_for_drawdown}<br>$${r.high_21d_for_drawdown.toFixed(4)}</small></td>
                        <td><small>${r.low_48h_time}<br>↓<br>${r.high_48h_time}</small></td>
                        <td><small>${r.low_96h_time}<br>↓<br>${r.high_96h_time}</small></td>
                        <td><small>${r.low_21d_time}<br>↓<br>${r.high_21d_time}</small></td>
                        <td class="${getVolumeCellClass(r.volume_24h)}">${r.volume_24h}</td>
                    </tr>
                `;
            }).join('');
        }

        function getCellClass(value, is21d = false) {
            if (is21d) {
                if (value >= 42) return 'cell-purple';
                if (value >= 35) return 'cell-blue';
                if (value >= 20) return 'cell-green';
                if (value >= 10) return 'cell-yellow';
                return 'cell-gray';
            } else {
                if (value >= 30) return 'cell-red';
                if (value >= 20) return 'cell-orange';
                if (value >= 10) return 'cell-yellow';
                if (value >= 5) return 'cell-green';
                if (value >= 0) return 'cell-blue';
                return 'cell-gray';
            }
        }

        function getVolumeCellClass(volumeStr) {
            const volume = parseFloat(volumeStr.replace(/[$,]/g, ''));
            if (volume >= 10000000) return 'cell-purple';
            if (volume >= 5000000) return 'cell-blue';
            if (volume >= 2000000) return 'cell-green';
            if (volume >= 1000000) return 'cell-yellow';
            return 'cell-gray';
        }

        function downloadCSV() {
            window.location.href = '/download/csv';
        }

        function downloadJSON() {
            window.location.href = '/download/json';
        }
    </script>
</body>
</html>'''
    
    with open('templates/index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print("=" * 50)
    print("🚀 Crypto Rebound Scanner Started!")
    print("=" * 50)
    print("📱 Open your browser and go to: http://localhost:5000")
    print("🌙 Dark Theme Enabled")
    print("🎨 Colorized Cells Active")
    print("🔍 Column Filtering Available")
    print("📉 21d Drawdown with Flags Added")
    print("📚 Full Documentation Available (Click the Documentation button)")
    print("=" * 50)
    print("\nDocumentation includes:")
    print("• Quick Start Guide with step-by-step instructions")
    print("• Complete parameter explanations")
    print("• Column-by-column breakdown")
    print("• Color coding legend with examples")
    print("• Drawdown flag meanings")
    print("• Filtering and sorting guide")
    print("• Tips & Tricks for best results")
    print("• Technical details about data sources")
    print("=" * 50)
    
    app.run(debug=True, host='0.0.0.0', port=5000)