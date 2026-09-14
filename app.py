import os, json, struct, urllib.request, subprocess, sys, threading, time, asyncio
from datetime import datetime
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from morning_monitor import bp as morning_bp
from sniper import bp as sniper_bp, start_scanner

# ===== 腾讯 K线接口（实时含今日） =====
def code_to_qt(code):
    c = code.strip().lower()
    digits = ''.join(ch for ch in c if ch.isdigit())
    if not digits:
        return None
    if c.startswith(('sh','sz','bj')):
        mkt = c[:2]
    else:
        mkt = 'sh' if digits.startswith('6') else ('bj' if digits.startswith(('8','4')) else 'sz')
    return mkt + digits

def fetch_txfqkline(qcode, count=320):
    """腾讯 web.ifzq.gtimg.cn fqkline 接口（含今日实时 K 线）"""
    try:
        url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={qcode},day,,,{count},qfq'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
        text = urllib.request.urlopen(req, timeout=4).read().decode('utf-8')
        j = json.loads(text)
        for k, v in j.get('data', {}).items():
            if isinstance(v, dict) and v.get('qfqday'):
                bars = []
                for row in v['qfqday']:
                    if len(row) >= 6:
                        bars.append({
                            'date': row[0].replace('-', ''),
                            'open': round(float(row[1]), 2),
                            'close': round(float(row[2]), 2),
                            'high': round(float(row[3]), 2),
                            'low': round(float(row[4]), 2),
                            'vol': int(float(row[5])),
                        })
                return bars
    except Exception:
        return None
    return None



BASE = os.path.dirname(os.path.abspath(__file__))
VIP = r'__vipdoc_unavailable__'  # 云端不再依赖本地 C:/new_tdx/vipdoc，仅作无效占位
SIGNALS_FILE = os.path.join(BASE, 'signals.json')
SENTIMENT_FILE = os.path.join(BASE, 'market_sentiment.json')
HORIZONS = [1, 2, 3, 5, 10]

app = Flask(__name__)
app.config['SECRET_KEY'] = 'board-tracker-secret-2026'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')
app.register_blueprint(morning_bp)
app.register_blueprint(sniper_bp)
_name_cache = {}
_rescan_proc = None

# ===== .day 数据接口（复用）=====
def load_signals():
    if not os.path.exists(SIGNALS_FILE): return []
    with open(SIGNALS_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_signals(sigs):
    with open(SIGNALS_FILE, 'w', encoding='utf-8') as f: json.dump(sigs, f, ensure_ascii=False, indent=2)

def code_to_path(code):
    code = code.strip().lower()
    digits = ''.join(c for c in code if c.isdigit())
    if code.startswith(('sh', 'sz', 'bj')): mkt, num = code[:2], digits
    elif code.endswith('.sh'): mkt, num = 'sh', digits
    elif code.endswith('.sz'): mkt, num = 'sz', digits
    elif code.endswith('.bj'): mkt, num = 'bj', digits
    else:
        num = digits
        mkt = 'sh' if num.startswith('6') else ('bj' if num.startswith(('8', '4')) else 'sz')
    return os.path.join(VIP, mkt, 'lday', f'{mkt}{num}.day')

def load_ohlcv(code):
    """读取 OHLCV 数据：优先腾讯实时接口（cloud 可用），本地 .day 作为 fallback"""
    qcode = code_to_qt(code)
    if qcode:
        bars = fetch_txfqkline(qcode, count=600)
        if bars:
            df = pd.DataFrame(bars)
            df = df[['date','open','close','high','low']].copy()
            df['date'] = df['date'].astype(str)
            return df
    # fallback 本地 .day（本地开发用，cloud 环境下 .day 通常不存在）
    f = code_to_path(code)
    if os.path.exists(f):
        with open(f, 'rb') as fh:
            data = fh.read()
        n = len(data) // 32
        recs = []
        for i in range(n):
            r = data[i * 32:(i + 1) * 32]
            d, o, h, l, c, amt, vol, res = struct.unpack('<iiiiifii', r)
            recs.append((str(d), o/100, h/100, l/100, c/100))
        return pd.DataFrame(recs, columns=['date', 'open', 'high', 'low', 'close'])
    return None

def compute(sig):
    code = sig['code']
    sig_date = str(sig['date']).replace('-', '').replace('/', '')
    df = load_ohlcv(code)
    res = {'found': False, 'ret': {}}
    if df is None: return res
    df = df.reset_index(drop=True)
    idx = df.index[df['date'] == sig_date]
    if len(idx) == 0: return res
    i = int(idx[0])
    res['found'] = True
    res['last_date'] = df['date'].iloc[-1]
    res['sig_close'] = df['close'].iloc[i]
    for h in HORIZONS:
        j = i + h
        res['ret'][f'T{h}'] = round((df['close'].iloc[j] / df['close'].iloc[i] - 1) * 100, 2) if j < len(df) else None
    j = i + 1
    res['next_open'] = round((df['open'].iloc[j] / df['close'].iloc[i] - 1) * 100, 2) if j < len(df) else None
    return res

def get_name(code):
    code_k = code.strip().lower()
    if code_k in _name_cache: return _name_cache[code_k]
    digits = ''.join(c for c in code_k if c.isdigit())
    if not digits: return ''
    mkt = 'sh' if digits.startswith('6') else ('bj' if digits.startswith(('8', '4')) else 'sz')
    q = mkt + digits
    try:
        req = urllib.request.Request(f'https://qt.gtimg.cn/q={q}',
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://stockapp.finance.qq.com/'})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = r.read().decode('gbk')
        if '="' in data:
            inner = data.split('="', 1)[1].rstrip(';"\n\r ')
            parts = inner.split('~')
            if len(parts) >= 3: _name_cache[code_k] = parts[1].strip()
    except: pass
    return _name_cache.get(code_k, '')

def summarize(items):
    stats = {}
    completed = [it for it in items if it.get('found')]
    for h in HORIZONS:
        key = f'T{h}'
        vals = [it['ret'].get(key) for it in completed if it.get('ret', {}).get(key) is not None]
        if vals:
            arr = np.array(vals)
            stats[key] = {'count': int(len(arr)), 'win_rate': round(float((arr>0).mean())*100,1),
                'mean': round(float(arr.mean()),2), 'median': round(float(np.median(arr)),2),
                'max': round(float(arr.max()),2), 'min': round(float(arr.min()),2)}
        else: stats[key] = {'count': 0}
    no = [it.get('next_open') for it in completed if it.get('next_open') is not None]
    if no:
        arr = np.array(no)
        stats['next_open'] = {'count': int(len(arr)), 'win_rate': round(float((arr>0).mean())*100,1),
            'mean': round(float(arr.mean()),2), 'median': round(float(np.median(arr)),2)}
    stats['total'] = len(items); stats['found'] = len(completed)
    return stats

# ===== 腾讯实时行情批量拉取 ======
def fetch_quotes_batch(codes):
    if not codes: return {}
    q_list, code_to_q = [], {}
    for c in codes:
        c_k = c.strip().lower()
        digits = ''.join(ch for ch in c_k if ch.isdigit())
        if not digits: continue
        if c_k.startswith(('sh','sz','bj')): mkt = c_k[:2]
        else: mkt = 'sh' if digits.startswith('6') else ('bj' if digits.startswith(('8','4')) else 'sz')
        q = mkt + digits
        q_list.append(q); code_to_q[q] = c_k
    if not q_list: return {}
    try:
        url = 'https://qt.gtimg.cn/q=' + ','.join(q_list)
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0','Referer':'https://stockapp.finance.qq.com/'})
        with urllib.request.urlopen(req, timeout=4) as r: text = r.read().decode('gbk')
        result = {}
        for line in text.strip().split('\n'):
            if '="' not in line: continue
            key, inner = line.split('="', 1)
            q = key.strip().lstrip('v_')
            inner = inner.rstrip(';\n\r "')
            parts = inner.split('~')
            if len(parts) < 5: continue
            try:
                price = float(parts[3]); prev_close = float(parts[4])
                chg_pct = round((price/prev_close - 1)*100, 2) if prev_close > 0 else None
                vol_ratio = None
                mktcap_yi = None
                try:
                    if len(parts) > 49 and parts[49]:
                        vol_ratio = float(parts[49])
                    if len(parts) > 44 and parts[44]:
                        mktcap_yi = float(parts[44])
                except Exception:
                    pass
                result[code_to_q.get(q, q)] = {'price': price, 'change_pct': chg_pct, 'name': parts[1], 'vol_ratio': vol_ratio, 'mktcap': mktcap_yi}
            except: continue
        return result
    except: return {}

# ===== WebSocket 实时推送线程 =====
_tracked_codes = set()
_push_running = False
_push_thread = None

def _push_loop():
    while _push_running:
        codes = list(_tracked_codes)
        if codes:
            quotes = fetch_quotes_batch(codes)
            if quotes:
                socketio.emit('quotes_update', quotes, namespace='/')
        time.sleep(1.5)  # 1.5秒推送一次

def start_push(codes):
    global _tracked_codes, _push_running, _push_thread
    for c in codes:
        _tracked_codes.add(c.strip().lower())
    if not _push_running:
        _push_running = True
        _push_thread = threading.Thread(target=_push_loop, daemon=True)
        _push_thread.start()
    print(f'[start_push] tracked={len(_tracked_codes)} codes', flush=True)

# ===== 页面路由 =====
@app.route('/page/morning')
def page_morning():
    resp = send_from_directory(BASE, 'page_morning.html')
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp

@app.after_request
def no_cache(resp):
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/')
def index(): return send_from_directory(BASE, 'page_board.html')
@app.route('/page/firstboard')
def page_firstboard():
    r = send_from_directory(BASE, 'page_firstboard.html')
    r.headers['Cache-Control'] = 'no-store, must-revalidate'
    r.headers['Pragma'] = 'no-cache'
    r.headers['Expires'] = '0'
    return r

@app.route('/page/leader')
def page_leader():
    r = send_from_directory(BASE, 'page_leader.html')
    r.headers['Cache-Control'] = 'no-store, must-revalidate'
    r.headers['Pragma'] = 'no-cache'
    r.headers['Expires'] = '0'
    return r

@app.route('/api/market/day1to2')
def api_day1to2():
    p = os.path.join(BASE, 'day1to2.json')
    if not os.path.exists(p):
        return jsonify({'updated': None, 'total': 0, 'samples': []})
    with open(p, 'r', encoding='utf-8') as f:
        d = json.load(f)
    # 不返 klines 详情（节省带宽），仅样本元数据
    samples = []
    for s in d.get('samples', []):
        samples.append({k: v for k, v in s.items() if k != 'klines'})
    return jsonify({'updated': d.get('updated'), 'total': d.get('total'), 'success_count': d.get('success_count'), 'history_days': d.get('history_days'), 'samples': samples})

@app.route('/api/market/day1to2/kline')
def api_day1to2_kline():
    """拿某个样本的 K 线（含首板日），走腾讯实时接口保证最新"""
    code = request.args.get('code', '')
    sig_date = request.args.get('sig_date', '')
    if not code or not sig_date:
        return jsonify({'error': 'missing'}), 400
    # 腾讯实时前复权日线（最高 640 根）
    qcode = ('sh' if code.startswith('6') else 'bj' if (code.startswith('8') or code.startswith('4')) else 'sz') + code
    bars = fetch_txfqkline(qcode, count=320)
    if not bars:
        return jsonify({'error': 'no kline'}), 404
    # 定位 sig_date
    sig_idx = -1
    for i, b in enumerate(bars):
        if str(b['date']) == str(sig_date):
            sig_idx = i
            break
    # 查样本 success（可选）
    p = os.path.join(BASE, 'day1to2.json')
    success = None
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                d = json.load(f)
            for s in d.get('samples', []):
                if s['code'] == code and s['sig_date'] == sig_date:
                    success = bool(s.get('success'))
                    break
        except Exception:
            pass
    return jsonify({'klines': bars, 'sig_idx_in_klines': sig_idx if sig_idx >= 0 else None, 'success': success})

_fb_proc = None

@app.route('/api/firstboard/rescan', methods=['POST'])
def firstboard_rescan():
    global _fb_proc
    if _fb_proc and _fb_proc.poll() is None:
        return jsonify({'status': 'running'})
    log_path = os.path.join(BASE, 'firstboard.log')
    env = dict(os.environ); env['PYTHONIOENCODING'] = 'utf-8'
    try:
        _fb_proc = subprocess.Popen([sys.executable, '-u', os.path.join(BASE, 'day1to2_scanner.py')],
            stdout=open(log_path, 'w', encoding='utf-8'), stderr=subprocess.STDOUT, env=env, cwd=BASE)
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500
    return jsonify({'status': 'started'})

@app.route('/api/firstboard/rescan/status')
def firstboard_rescan_status():
    global _fb_proc
    running = _fb_proc is not None and _fb_proc.poll() is None
    log_path = os.path.join(BASE, 'firstboard.log')
    last = ''
    if os.path.exists(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = [l for l in f.read().strip().split('\n') if l.strip()]
            last = lines[-1] if lines else ''
        except Exception:
            pass
    updated = None
    p = os.path.join(BASE, 'day1to2.json')
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                updated = json.load(f).get('updated')
        except Exception:
            pass
    return jsonify({'running': running, 'progress': last, 'updated': updated})

# ===== API 路由 =====

# 市场情绪
@app.route('/api/market/sentiment')
def market_sentiment():
    if not os.path.exists(SENTIMENT_FILE): return jsonify({'error':'未生成'}), 404
    with open(SENTIMENT_FILE, 'r', encoding='utf-8') as f: j = json.load(f)
    if 'latest' in j:
        return jsonify({'latest': j.get('latest'), 'prev': j.get('prev'), 'avg_20': j.get('avg_20'),
            'recent5': j.get('recent5',[]), 'updated': j.get('updated'), 'source': j.get('source',''),
            'trading_days': j.get('trading_days',''), 'st_excluded': j.get('st_excluded')})
    else:
        days = j.get('days', [])
        return jsonify({'latest': days[-1] if days else None, 'prev': days[-2] if len(days)>1 else None,
            'avg_20': j.get('avg_20'), 'recent5': days[-5:] if len(days)>=5 else days,
            'updated': j.get('updated'), 'source':'', 'trading_days': len(days), 'st_excluded': j.get('st_excluded')})

@app.route('/api/market/rescan', methods=['POST'])
def market_rescan():
    global _rescan_proc
    if _rescan_proc and _rescan_proc.poll() is None: return jsonify({'status':'running'})
    log_path = os.path.join(BASE, 'sentiment.log')
    env = dict(os.environ); env['PYTHONIOENCODING'] = 'utf-8'
    try:
        _rescan_proc = subprocess.Popen([sys.executable,'-u', os.path.join(BASE,'market_sentiment.py')],
            stdout=open(log_path,'w',encoding='utf-8'), stderr=subprocess.STDOUT, env=env)
    except Exception as e: return jsonify({'status':'error','msg':str(e)}), 500
    return jsonify({'status':'started'})

@app.route('/api/market/rescan/status')
def market_rescan_status():
    global _rescan_proc
    running = _rescan_proc is not None and _rescan_proc.poll() is None
    log_path = os.path.join(BASE, 'sentiment.log')
    last = ''
    if os.path.exists(log_path):
        try:
            with open(log_path,'r',encoding='utf-8') as f:
                lines = [l for l in f.read().strip().split('\n') if l.strip()]
            last = lines[-1] if lines else ''
        except: pass
    return jsonify({'running': running, 'progress': last})

# 信号
@app.route('/api/signals', methods=['GET'])
def list_signals():
    sigs = load_signals()
    out = []
    for s in sigs:
        c = compute(s)
        item = dict(s); item.update({'found': c['found'], 'last_date': c.get('last_date'),
            'ret': c.get('ret',{}), 'next_open': c.get('next_open'), 'sig_close': c.get('sig_close'),
            'name': get_name(s['code'])})
        item.setdefault('status','观察中'); item.setdefault('starred', False); item.setdefault('pinned', False); out.append(item)
    return jsonify({'signals': out, 'stats': summarize(out)})

@app.route('/api/signals', methods=['POST'])
def add_signal():
    data = request.json or {}; code = (data.get('code') or '').strip(); sdate = data.get('date')
    if not code or not sdate: return jsonify({'error':'code and date required'}), 400
    sigs = load_signals()
    sig = {'id': datetime.now().strftime('%Y%m%d%H%M%S%f'), 'code': code, 'date': str(sdate),
        'indicator': data.get('indicator',''), 'note': data.get('note',''), 'status':'观察中',
        'starred': data.get('starred', False), 'pinned': False, 'created': datetime.now().isoformat()}
    sigs.append(sig); save_signals(sigs)
    return jsonify({'ok':True, 'signal': sig})

@app.route('/api/signals/<sid>', methods=['PATCH'])
def patch_signal(sid):
    sigs = load_signals(); data = request.json or {}; found = False
    for s in sigs:
        if s.get('id') == sid:
            for k in ('status','note','indicator','pinned','starred'):
                if k in data: s[k] = data[k]
            found = True; break
    if not found: return jsonify({'error':'not found'}), 404
    save_signals(sigs); return jsonify({'ok':True})

@app.route('/api/signals/<sid>', methods=['DELETE'])
def del_signal(sid):
    sigs = load_signals(); sigs = [s for s in sigs if s.get('id') != sid]; save_signals(sigs)
    return jsonify({'ok':True})

# WebSocket 事件
@socketio.on('connect')
def on_connect():
    print('[WS] client connected')

@socketio.on('subscribe_quotes')
def on_subscribe(data):
    codes = data.get('codes', []) if isinstance(data, dict) else []
    if codes: start_push(codes)
    print(f'[on_subscribe] +{len(codes)} codes, total={len(_tracked_codes)}', flush=True)
    emit('subscribed', {'codes': list(_tracked_codes)})

@socketio.on('disconnect')
def on_disconnect():
    print('[WS] client disconnected')

# ===== 涨停池 API（akshare，今日数据）=====
_zt_cache = {'data': [], 'ts': 0}
_zt_cache_ttl = 300  # 5分钟缓存

# 板块行情缓存（akshare stock_board_industry_summary_ths）
_sector_cache = {'data': [], 'ts': 0}
_sector_ttl = 600  # 10 分钟

@app.route('/api/market/sector_summary')
def sector_summary():
    global _sector_cache
    now = time.time()
    if _sector_cache['data'] and (now - _sector_cache['ts']) < _sector_ttl:
        return jsonify({'data': _sector_cache['data'], 'cached': True, 'ts': _sector_cache['ts']})
    try:
        import akshare as ak
        df = ak.stock_board_industry_summary_ths()
        if df is None or df.empty:
            return jsonify({'data': _sector_cache['data'] or [], 'cached': False})
        rows = []
        for _, r in df.iterrows():
            try:
                rows.append({
                    'name': str(r.get('板块', '')),
                    'chg_pct': round(float(r.get('涨跌幅', 0) or 0), 2),
                    'fund_inflow': round(float(r.get('净流入', 0) or 0), 2),  # 亿
                    'amount': round(float(r.get('总成交额', 0) or 0), 2),    # 亿
                    'up_count': int(r.get('上涨家数', 0) or 0),
                    'down_count': int(r.get('下跌家数', 0) or 0),
                    'leader_name': str(r.get('领涨股', '')),
                    'leader_price': round(float(r.get('领涨股-最新价', 0) or 0), 2) if r.get('领涨股-最新价') else None,
                    'leader_chg': round(float(r.get('领涨股-涨跌幅', 0) or 0), 2) if r.get('领涨股-涨跌幅') else None,
                })
            except: continue
        # 涨跌幅降序
        rows.sort(key=lambda x: x['chg_pct'], reverse=True)
        _sector_cache = {'data': rows, 'ts': now}
        return jsonify({'data': rows, 'cached': False, 'ts': now})
    except Exception as e:
        return jsonify({'error': str(e), 'data': _sector_cache['data'] or []})


# ===== 板块领涨股 TOP3（新浪行业板块）=====
_sector_lead_cache = {'data': [], 'ts': 0}
_sector_lead_ttl = 120

@app.route('/api/market/sector_leaders')
def sector_leaders():
    global _sector_lead_cache
    now = time.time()
    if _sector_lead_cache['data'] and (now - _sector_lead_cache['ts']) < _sector_lead_ttl:
        return jsonify({'data': _sector_lead_cache['data'], 'cached': True, 'ts': _sector_lead_cache['ts']})
    try:
        import akshare as ak
        spot = ak.stock_sector_spot(indicator='新浪行业')
        rows = []
        for _, r in spot.iterrows():
            try:
                rows.append({
                    'name': str(r.get('板块', '')),
                    'label': str(r.get('label', '')),
                    'chg_pct': round(float(r.get('涨跌幅', 0) or 0), 2),
                    'leader_name': str(r.get('股票名称', '')),
                    'leader_chg': round(float(r.get('个股-涨跌幅', 0) or 0), 2),
                    'leaders': [],
                })
            except:
                continue
        rows.sort(key=lambda x: x['chg_pct'], reverse=True)
        for s in rows[:8]:
            try:
                det = ak.stock_sector_detail(sector=s['label'])
                lst = []
                for _, d in det.iterrows():
                    try:
                        lst.append({'name': str(d.get('name', '')), 'code': str(d.get('code', '')),
                                    'chg': round(float(d.get('changepercent', 0) or 0), 2)})
                    except:
                        continue
                lst.sort(key=lambda x: x['chg'], reverse=True)
                s['leaders'] = lst[:3]
            except Exception:
                s['leaders'] = ([{'name': s['leader_name'], 'code': '', 'chg': s['leader_chg']}]
                                if s['leader_name'] else [])
        out = rows[:8]
        _sector_lead_cache = {'data': out, 'ts': now}
        return jsonify({'data': out, 'cached': False, 'ts': now})
    except Exception as e:
        return jsonify({'data': _sector_lead_cache['data'] or [], 'cached': False, 'error': str(e)})


# ===== 个股资金流向（同花顺即时，code -> 净额亿）=====
_ff_cache = {'data': {}, 'ts': 0}
_ff_ttl = 120  # 2 分钟

def _parse_yi(v):
    """'5376.90万' / '9.84亿' / 0.5 / '0.5' -> 亿(float)"""
    try:
        if v is None: return None
        if isinstance(v, (int, float)):
            return round(float(v), 4)
        s = str(v).strip().replace(',', '')
        if not s or s in ('nan', 'None', '-', ''): return None
        mult = 1.0
        if s.endswith('亿'):
            s = s[:-1]; mult = 1.0
        elif s.endswith('万'):
            s = s[:-1]; mult = 0.0001
        return round(float(s) * mult, 4)
    except Exception:
        return None

@app.route('/api/market/fund_flow')
def market_fund_flow():
    global _ff_cache
    now = time.time()
    if _ff_cache['data'] and (now - _ff_cache['ts']) < _ff_ttl:
        return jsonify({'data': _ff_cache['data'], 'cached': True, 'ts': _ff_cache['ts']})
    try:
        import akshare as ak
        df = ak.stock_fund_flow_individual(symbol="即时")
        m = {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                code = str(r.get('股票代码', '')).strip()
                if not code: continue
                code = code.zfill(6)
                net = _parse_yi(r.get('净额'))
                if net is not None:
                    m[code] = net
        _ff_cache = {'data': m, 'ts': now}
        return jsonify({'data': m, 'cached': False, 'ts': now})
    except Exception as e:
        return jsonify({'error': str(e), 'data': _ff_cache['data'] or {}})


@app.route('/api/market/zt_today')
def zt_today():
    global _zt_cache
    now = time.time()
    if _zt_cache['data'] and (now - _zt_cache['ts']) < _zt_cache_ttl:
        return jsonify({'data': _zt_cache['data'], 'cached': True, 'ts': _zt_cache['ts']})
    try:
        import akshare as ak
        today = datetime.now().strftime('%Y%m%d')
        df = ak.stock_zt_pool_em(date=today)
        if df is None or df.empty:
            return jsonify({'data': [], 'cached': False})
        # 同步拉强势股池，取"入选理由"（涨停原因文字）
        reason_map = {}
        try:
            df_s = ak.stock_zt_pool_strong_em(date=today)
            if df_s is not None and not df_s.empty:
                for _, r in df_s.iterrows():
                    code = str(r.get('代码', ''))
                    reason = str(r.get('入选理由', '')).strip()
                    if reason and reason not in ('nan', 'None'):
                        reason_map[code] = reason
        except Exception as ex:
            print('[zt_today] strong pool err:', ex)
        rows = []
        for _, r in df.iterrows():
            try:
                code = str(r.get('代码',''))
                stat = str(r.get('涨停统计','')).strip()
                # 优先用入选理由，没有则用涨停统计（次数比）
                reason = reason_map.get(code, stat if stat not in ('nan','None','') else '—')
                rows.append({
                    'code': code,
                    'name': str(r.get('名称','')),
                    'industry': str(r.get('所属行业','')),
                    'reason': reason[:80],
                    'first_time': str(r.get('首次封板时间','')),
                    'last_time': str(r.get('最后封板时间','')),
                    'boards': int(r.get('连板数', 1) or 1),
                    'turn_rate': round(float(r.get('换手率', 0) or 0), 2),
                    'amount': round(float(r.get('成交额', 0) or 0) / 1e8, 2) if r.get('成交额') else None,
                    'fb_money': round(float(r.get('封板资金', 0) or 0) / 1e8, 2) if r.get('封板资金') else None,
                    'zb_count': int(r.get('炸板次数', 0) or 0),
                    'zt_stat': str(r.get('涨停统计','')).strip(),
                })
            except: continue
        rows.sort(key=lambda x: x['boards'], reverse=True)
        _zt_cache = {'data': rows, 'ts': now}
        return jsonify({'data': rows, 'cached': False, 'ts': now})
    except Exception as e:
        return jsonify({'error': str(e), 'data': _zt_cache['data'] or []})

@app.route('/api/market/zt_today/refresh', methods=['POST'])
def zt_today_refresh():
    global _zt_cache
    _zt_cache = {'data': [], 'ts': 0}  # 强制刷新
    return jsonify({'status': 'ok'})

# ===== 龙回头接口 =====
LH_FILE = os.path.join(BASE, 'dragon_back.json')

@app.route('/api/lh', methods=['GET'])
def get_lh():
    if not os.path.exists(LH_FILE): return jsonify([])
    with open(LH_FILE, 'r', encoding='utf-8') as f: return jsonify(json.load(f))

def _lh_load():
    if not os.path.exists(LH_FILE): return []
    with open(LH_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def _lh_save(items):
    with open(LH_FILE, 'w', encoding='utf-8') as f: json.dump(items, f, ensure_ascii=False, indent=2)

def _lh_digits(code):
    return ''.join(ch for ch in str(code or '') if ch.isdigit())

@app.route('/api/lh', methods=['POST'])
def add_lh():
    data = request.json or {}
    code = _lh_digits(data.get('code'))
    if not code: return jsonify({'error': 'code required'}), 400
    items = _lh_load()
    old = next((x for x in items if str(x.get('code','')) == code), None)
    items = [x for x in items if str(x.get('code','')) != code]
    items.append({
        'code': code,
        'name': (data.get('name') or (old or {}).get('name') or get_name(code)),
        'pinned': bool(data.get('pinned', (old or {}).get('pinned', False))),
        'added_date': (old or {}).get('added_date') or datetime.now().strftime('%Y-%m-%d %H:%M'),
    })
    _lh_save(items)
    return jsonify({'ok': True, 'code': code})

@app.route('/api/lh/<code>', methods=['PATCH'])
def patch_lh(code):
    data = request.json or {}
    d = _lh_digits(code)
    items = _lh_load()
    hit = False
    for x in items:
        if str(x.get('code','')) == d:
            if 'pinned' in data: x['pinned'] = bool(data['pinned'])
            if data.get('name'): x['name'] = data['name']
            hit = True
    _lh_save(items)
    return jsonify({'ok': hit})

@app.route('/api/lh/<code>', methods=['DELETE'])
def del_lh(code):
    d = _lh_digits(code)
    items = [x for x in _lh_load() if str(x.get('code','')) != d]
    _lh_save(items)
    return jsonify({'ok': True})

# ===== K线接口（日/周/月，从本地.day重采样）=====
def load_ohlcv_full(code):
    """读取带成交量的日线：优先腾讯接口，本地 .day 作为 fallback"""
    qcode = code_to_qt(code)
    if qcode:
        bars = fetch_txfqkline(qcode, count=600)
        if bars:
            df = pd.DataFrame(bars)
            df = df[['date','open','high','low','close','vol']].copy()
            df['date'] = df['date'].astype(str)
            return df
    # fallback 本地 .day
    f = code_to_path(code)
    if not os.path.exists(f):
        return None
    with open(f, 'rb') as fh:
        data = fh.read()
    n = len(data) // 32
    recs = []
    for i in range(n):
        r = data[i * 32:(i + 1) * 32]
        d, o, h, l, c, amt, vol, res = struct.unpack('<iiiiifii', r)
        recs.append((str(d), o/100, h/100, l/100, c/100, vol))
    return pd.DataFrame(recs, columns=['date','open','high','low','close','vol'])

def resample_bars(bars_or_df, period):
    import pandas as pd
    if isinstance(bars_or_df, list):
        df = pd.DataFrame(bars_or_df)
    else:
        df = bars_or_df.copy()
    if df.empty:
        return []
    df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    if period == 'weekly':
        grouper = df['date'].dt.isocalendar().week.astype(str) + '_' + df['date'].dt.year.astype(str)
    else:
        grouper = df['date'].dt.strftime('%Y-%m')
    grouped = df.groupby(grouper, sort=True)
    out = []
    for g, gdf in grouped:
        first = gdf.iloc[0]
        date_val = first['date']
        if hasattr(date_val, 'strftime'):
            date_str = date_val.strftime('%Y%m%d')
        else:
            date_str = str(int(float(str(date_val))))
        out.append({
            'date': date_str,
            'open': round(float(first['open']), 2),
            'high': round(float(gdf['high'].max()), 2),
            'low': round(float(gdf['low'].min()), 2),
            'close': round(float(gdf.iloc[-1]['close']), 2),
            'vol': int(gdf['vol'].sum()),
        })
    return out

@app.route('/api/kline')
def kline():
    code = request.args.get('code', '').strip()
    period = request.args.get('period', 'daily')
    days = max(20, min(int(request.args.get('days', '120')), 500))
    if period not in ('daily', 'weekly', 'monthly'):
        period = 'daily'

    bars = None
    qcode = code_to_qt(code)
    if qcode:
        n = days * 8 if period == 'weekly' else (days * 25 if period == 'monthly' else days)
        bars = fetch_txfqkline(qcode, n)

    if not bars:
        df = load_ohlcv_full(code)
        if df is None or df.empty:
            return jsonify({'error': 'not found'}), 404
        bars = []
        for _, r in df.iterrows():
            bars.append({
                'date': str(int(r['date'])),
                'open': round(float(r['open']), 2),
                'high': round(float(r['high']), 2),
                'low': round(float(r['low']), 2),
                'close': round(float(r['close']), 2),
                'vol': int(r['vol']),
            })

    if period == 'weekly':
        bars = resample_bars(bars, 'weekly')
    elif period == 'monthly':
        bars = resample_bars(bars, 'monthly')

    bars = bars[-days:]
    if len(bars) < 5:
        return jsonify({'error': 'too few bars'}), 400
    return jsonify({'code': code, 'name': get_name(code), 'period': period, 'bars': bars})


@socketio.on('subscribe_sniper')
def on_subscribe_sniper():
    print('[socketio] client subscribed sniper alerts')
    socketio.emit('sniper_subscribed', {'ok': True})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    # 本地默认 127.0.0.1，云端默认 0.0.0.0
    is_cloud = bool(os.environ.get('RENDER') or os.environ.get('PORT'))
    host = '0.0.0.0' if is_cloud else '127.0.0.1'
    debug = not is_cloud
    if is_cloud:
        print('[cloud mode] scanner disabled (use /api/sniper/refresh_pool to update pool)')
    else:
        start_scanner(app, socketio)
    socketio.run(app, host=host, port=port, debug=debug,
                 allow_unsafe_werkzeug=debug, log_output=False)
