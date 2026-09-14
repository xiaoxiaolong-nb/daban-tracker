"""
打板追踪器 — 早盘首板实时监测 API
GET /api/morning/now   → 当日涨停池（早盘优先/实时）
POST /api/morning/history → 3日复盘统计（早盘 vs 午后 吃肉/炸板吃面）
"""
import json, os, time, akshare as ak, datetime, pandas as pd
from flask import Blueprint, jsonify, request
from functools import wraps

bp = Blueprint('morning', __name__)
BASE = os.path.dirname(os.path.abspath(__file__))

CACHE_FILE = os.path.join(BASE, 'morning_cache.json')
STATS_FILE = os.path.join(BASE, 'morning_stats.json')

_morning_cache = {'data': [], 'ts': 0}
_stats_cache  = None
CACHE_TTL    = 30   # 秒

def is_morning(t):
    if not t: return False
    t = str(t).replace(':', '').zfill(6)
    try: v = int(t[:2])*60 + int(t[2:4])
    except: return False
    return v <= 10*60  # 9:25–10:00

def get_today_pool():
    global _morning_cache
    now = time.time()
    if _morning_cache['data'] and (now - _morning_cache['ts']) < CACHE_TTL:
        return _morning_cache['data']
    try:
        today = datetime.datetime.now().strftime('%Y%m%d')
        df = ak.stock_zt_pool_em(date=today)
        if df is None or df.empty:
            _morning_cache = {'data': [], 'ts': now}
            return []
        rows = []
        for _, r in df.iterrows():
            ft = str(r.get('首次封板时间', '') or '')
            zb  = int(r.get('炸板次数', 0) or 0)
            fb  = float(r.get('封板资金', 0) or 0) / 1e8
            amt = float(r.get('成交额', 0) or 0) / 1e8
            mkt = float(r.get('流通市值', 0) or 0) / 1e8
            rows.append({
                'code':      str(r.get('代码', '')),
                'name':      str(r.get('名称', '')),
                'pct':       float(r.get('涨跌幅', 0)),
                'price':     float(r.get('最新价', 0)),
                'turnover':  float(r.get('换手率', 0)),
                'mktcap':    round(mkt, 1),
                'fb_money':  round(fb, 2),
                'amount':    round(amt, 2),
                'first_time':ft,
                'last_time': str(r.get('最后封板时间', '') or ''),
                'zb_count':  zb,
                'boards':    int(r.get('连板数', 0) or 0),
                'industry':  str(r.get('所属行业', '') or ''),
                'morning':   is_morning(ft),
            })
        _morning_cache = {'data': rows, 'ts': now}
        return rows
    except Exception as e:
        print('[morning pool error]', e)
        return _morning_cache.get('data', [])

def get_history_stats():
    """从 day1to2.json 算近3日早盘 vs 午后的统计数据"""
    global _stats_cache
    if _stats_cache: return _stats_cache
    try:
        d = json.load(open(os.path.join(BASE, 'day1to2.json'), encoding='utf-8'))
        samples = [s for s in d.get('samples', []) if str(s.get('sig_date','')) >= '20260904']
    except:
        samples = []

    def calc(arr):
        n = len(arr)
        eat   = [s for s in arr if s.get('success')]
        zb    = [s for s in arr if (s.get('zb_count') or 0) > 0]
        eat_zb = [s for s in arr if s.get('success') and (s.get('zb_count') or 0) > 0]
        fail_zb= [s for s in arr if (s.get('zb_count') or 0) > 0 and not s.get('success')]
        no_zb  = [s for s in arr if (s.get('zb_count') or 0) == 0]
        eat_nz = [s for s in arr if s.get('success') and (s.get('zb_count') or 0) == 0]
        return {
            'n': n,
            'eat': len(eat), 'eat_rate': round(len(eat)/n*100, 1) if n else 0,
            'zb': len(zb),   'zb_rate':  round(len(zb)/n*100, 1) if n else 0,
            'eat_zb': len(eat_zb),  'eat_zb_rate': round(len(eat_zb)/n*100, 1) if n else 0,
            'fail_zb': len(fail_zb), 'fail_zb_rate': round(len(fail_zb)/n*100, 1) if n else 0,
            'no_zb': len(no_zb),
            'eat_nz': len(eat_nz), 'eat_nz_rate': round(len(eat_nz)/len(no_zb)*100, 1) if no_zb else 0,
        }

    morning   = [s for s in samples if is_morning(str(s.get('first_time', '')))]
    afternoon = [s for s in samples if not is_morning(str(s.get('first_time', '')))]

    _stats_cache = {
        'morning': calc(morning),
        'afternoon': calc(afternoon),
        'days': 3,
        'updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    return _stats_cache

@bp.route('/api/morning/now')
def morning_now():
    rows = get_today_pool()
    morning = sorted([r for r in rows if r['morning']], key=lambda x: x['first_time'])
    afternoon = sorted([r for r in rows if not r['morning']], key=lambda x: x['first_time'])
    now = datetime.datetime.now()
    is_trading = (9*60+25 <= now.hour*60+now.minute <= 15*60+5)
    return jsonify({
        'morning': morning, 'afternoon': afternoon,
        'morning_count': len(morning), 'total_count': len(rows),
        'time': now.strftime('%H:%M:%S'), 'is_trading': is_trading,
        'ts': _morning_cache['ts'],
    })

@bp.route('/api/morning/stats')
def morning_stats():
    return jsonify(get_history_stats())

@bp.route('/api/morning/minline')
def morning_minline():
    code = request.args.get('code', '')
    if not code:
        return jsonify({'error': 'code required'}), 400
    qcode = code_to_q(code)
    try:
        url = f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={qcode}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        text = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
        j = json.loads(text)
        data_list = j.get('data', {}).get('data', {}).get('data', [])
        prev_close = float(j.get('data', {}).get('data', {}).get('昨晚收盘', 0) or 0)
        rows = []
        for item in data_list:
            if isinstance(item, str):
                parts = item.split()
                if len(parts) >= 2:
                    rows.append({'time': parts[0], 'price': float(parts[1]), 'vol': int(parts[2]) if len(parts)>2 else 0})
        if not rows and prev_close == 0:
            # fallback: use close price as prev_close
            prev_close = rows[0]['price'] if rows else 0
        for r in rows:
            r['prev_close'] = prev_close
        return jsonify({'code': code, 'data': rows, 'prev_close': prev_close})
    except Exception as e:
        return jsonify({'error': str(e), 'data': []}), 500

def code_to_q(code):
    c = str(code).strip()
    if c.startswith(('6', '5', '7', '9')): return 'sh' + c
    if c.startswith(('8', '4')): return 'bj' + c
    return 'sz' + c

