"""
打板追踪器 — 早盘首板实时狙击
- 候选池构建（mktcap+板块）
- 实时异动拉升扫描
- 弹窗推送（浏览器原生 + 页面内强提醒）
"""
import json, os, time, urllib.request, threading, datetime, sys
import akshare as ak, pandas as pd
from flask import Blueprint, jsonify, request

bp = Blueprint('sniper', __name__)
BASE = os.path.dirname(os.path.abspath(__file__))

# ─── 配置 ────────────────────────────────────────────────
HOT_SECTOR_TOP_N   = 6      # 取涨停池 Top N 板块作"热点板块"
MKTCAP_MIN_YI      = 30
MKTCAP_MAX_YI      = 150
SCAN_INTERVAL_SEC  = 3
BATCH_SIZE         = 80
TRADE_MORNING_EARLY= (9*60+25, 10*60)   # 早盘黄金窗口
TRADE_MORNING_FULL = (9*60+25, 11*60+30) # 早盘全天
TRADE_NOON         = (13*60, 14*60)     # 午后才考虑
NOTIFY_DEDUPE_SEC  = 300    # 同票 5 分钟不重复推

# ─── 缓存 ────────────────────────────────────────────────
_pool        = {'codes': [], 'sector_map': {}, 'mktcap_map': {}, 'updated': 0}
_alerts      = []            # 最近 20 条 alert
_alert_seen  = {}            # code -> last alert ts (去重)
_pool_lock   = threading.Lock()
_zt_cache    = {'data': None, 'ts': 0}

# ─── 工具 ────────────────────────────────────────────────
def is_morning(t):
    if not t: return False
    t = str(t).replace(':','').zfill(6)
    try: v = int(t[:2])*60+int(t[2:4])
    except: return False
    return 9*60+25 <= v <= 10*60

def code_to_q(code):
    c = str(code).strip()
    if c.startswith(('6','5','7','9')): return 'sh'+c
    if c.startswith(('8','4')): return 'bj'+c
    return 'sz'+c

# ─── 个人微信推送（wechat-access）────────────────────────
_GW_PORT   = int(os.environ.get('OPENCLAW_GATEWAY_PORT', '57298'))
_GW_TOKEN  = os.environ.get('OPENCLAW_GATEWAY_TOKEN', '').strip()
_WX_TARGET = os.environ.get('WECHAT_TARGET', '1909613051').strip()
_WX_CHAN   = os.environ.get('WECHAT_CHANNEL', 'wechat-access').strip()

def _push_wechat(a):
    """触发时推送一条消息到个人微信（OpenClaw gateway /tools/invoke）。
    环境变量由 flask_watchdog.py 注入：OPENCLAW_GATEWAY_TOKEN / OPENCLAW_GATEWAY_PORT。
    """
    if not _GW_TOKEN:
        return  # 未配置 gateway token，跳过微信推送
    msg = (f"🎯 狙击触发 {a['name']}({a['code']})\n"
           f"+{a['change_pct']:.2f}% 量比{a['vol_ratio']:.2f} 市值{a['mktcap']}亿\n"
           f"行业：{a.get('industry','-')} 换手{a.get('turnover',0):.1f}%")
    body = {
        'tool': 'message', 'action': 'send',
        'args': {'action': 'send', 'channel': _WX_CHAN, 'target': _WX_TARGET, 'message': msg}
    }
    try:
        req = urllib.request.Request(
            f'http://127.0.0.1:{_GW_PORT}/tools/invoke',
            data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {_GW_TOKEN}'},
            method='POST')
        urllib.request.urlopen(req, timeout=8)
    except Exception as we:
        print('[wechat push err]', we)

def in_window(now=None):
    now = now or datetime.datetime.now()
    m = now.hour*60+now.minute
    if TRADE_MORNING_EARLY[0] <= m <= TRADE_MORNING_EARLY[1]: return 'morning_hot'
    if TRADE_MORNING_FULL[0]  <= m <= TRADE_MORNING_FULL[1]:  return 'morning'
    if TRADE_NOON[0]           <= m <= TRADE_NOON[1]:           return 'noon'
    return None

def fetch_quotes_batch(codes):
    """批量 qt.gtimg.cn 拉行情；返回 {code: {price, prev_close, change_pct, mktcap, vol_ratio, name}}"""
    out = {}
    for i in range(0, len(codes), BATCH_SIZE):
        chunk = codes[i:i+BATCH_SIZE]
        qs = ','.join(code_to_q(c) for c in chunk)
        url = f'https://qt.gtimg.cn/q={qs}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            txt = urllib.request.urlopen(req, timeout=4).read().decode('gbk', errors='ignore')
            for line in txt.split('\n'):
                line = line.strip()
                if '="' not in line: continue
                body = line.split('="',1)[1].rstrip('";')
                parts = body.split('~')
                if len(parts) < 50: continue
                code = parts[2]
                try:
                    price = float(parts[3]) if parts[3] else 0
                    prev  = float(parts[4]) if parts[4] else 0
                    if price <= 0 or prev <= 0: continue
                    chg = (price/prev - 1) * 100
                    out[code] = {
                        'code': code,
                        'name': parts[1],
                        'price': price,
                        'prev_close': prev,
                        'change_pct': round(chg, 2),
                        'open': float(parts[5]) if parts[5] else 0,
                        'vol_ratio': float(parts[49]) if parts[49] else 0,
                        'mktcap':    float(parts[44]) if parts[44] else 0,
                        'amount':    float(parts[37]) if len(parts)>37 and parts[37] else 0,
                        'turnover':  float(parts[38]) if len(parts)>38 and parts[38] else 0,
                    }
                except (ValueError, IndexError): continue
        except Exception as e:
            print(f'[sniper batch err]', e)
            continue
    return out

def get_today_zt_pool():
    """今日涨停池（含板块+代码+现价），缓存 60 秒"""
    global _zt_cache
    now = time.time()
    if _zt_cache['data'] and (now - _zt_cache['ts']) < 60:
        return _zt_cache['data']
    try:
        today = datetime.datetime.now().strftime('%Y%m%d')
        df = ak.stock_zt_pool_em(date=today)
        rows = []
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                rows.append({
                    'code':  str(r.get('代码','')),
                    'name':  str(r.get('名称','')),
                    'industry': str(r.get('所属行业','') or ''),
                    'pct':   float(r.get('涨跌幅',0)),
                    'first_time': str(r.get('首次封板时间','') or ''),
                })
        _zt_cache = {'data': rows, 'ts': now}
        return rows
    except Exception as e:
        print('[sniper zt err]', e)
        return _zt_cache.get('data') or []

# ─── 候选池构建 ────────────────────────────────────────────
def _all_a_codes():
    """本地 .day 文件中的所有 A 股代码（包含上海深圳，不含北交所/ST）"""
    import glob
    seen, codes = set(), []
    for sub in ('sh','sz'):
        for fp in glob.glob(os.path.join(r'C:\new_tdx\vipdoc', sub, 'lday', '*.day')):
            base = os.path.basename(fp).replace('.day','')
            if not base.startswith(sub): continue
            code = base[2:]
            # 排除 ST、退市、停牌超 1 年的
            if code in seen: continue
            seen.add(code); codes.append(code)
    return codes

def build_pool(force=False):
    """构建候选池：全市场 30-150亿 流通市值"""
    now = time.time()
    if not force and _pool['codes'] and (now - _pool['updated']) < 600:
        return _pool

    codes = _all_a_codes()
    quotes = fetch_quotes_batch(codes)
    pool_codes, mktcap_map = [], {}
    for code in codes:
        q = quotes.get(code)
        if not q: continue
        mc = q['mktcap']
        if mc < MKTCAP_MIN_YI or mc > MKTCAP_MAX_YI: continue
        if 'ST' in q['name'] or '退' in q['name']: continue
        pool_codes.append(code)
        mktcap_map[code] = mc

    with _pool_lock:
        _pool['codes'] = pool_codes
        _pool['mktcap_map'] = mktcap_map
        _pool['updated'] = now
    print(f'[sniper pool] {len(pool_codes)} 只 in 30-150亿 range, scanned {len(codes)} total')
    return _pool

# ─── 扫描 ────────────────────────────────────────────────
def scan_once():
    """一轮扫描，返回新增 alert 列表"""
    if in_window() is None: return []

    # 已有涨停票跳过
    zt_codes = {r['code'] for r in get_today_zt_pool()}

    # 已涨停/涨幅≥9.5% 跳过（它们不是"异动拉升"，已是涨停状态）
    snap = []
    codes = _pool.get('codes') or []
    if not codes:
        build_pool(force=True)
        codes = _pool.get('codes') or []
    quotes = fetch_quotes_batch(codes)

    new_alerts = []
    now_ts = time.time()
    for code, q in quotes.items():
        # 跳过已涨停 / 已接近涨停
        if code in zt_codes: continue
        if q['change_pct'] >= 9.0: continue
        # 触发条件：拉升 + 量比 + 成交
        pull_up = q['change_pct'] >= 3.0  # 距昨收涨 3%+
        vol     = q['vol_ratio'] >= 1.8
        amt     = q['amount']  >= 5000   # 万
        if not (pull_up and vol and amt): continue
        # 去重
        last = _alert_seen.get(code, 0)
        if now_ts - last < NOTIFY_DEDUPE_SEC: continue
        _alert_seen[code] = now_ts

        alert = {
            'code':     code,
            'name':     q['name'],
            'price':    q['price'],
            'change_pct': q['change_pct'],
            'vol_ratio': q['vol_ratio'],
            'mktcap':   q['mktcap'],
            'sector':   _pool.get('sector_map', {}).get(code, ''),
            'amount':   q['amount'],
            'turnover': q.get('turnover', 0),
            'time':     datetime.datetime.now().strftime('%H:%M:%S'),
            'ts':       int(now_ts),
            'reason':   f"价升{q['change_pct']}% + 量比{q['vol_ratio']} + 成交{q['amount']/10000:.1f}亿",
        }
        new_alerts.append(alert)
        _alerts.insert(0, alert)
        if len(_alerts) > 30: _alerts.pop()

    return new_alerts

# ─── 扫描线程 ──────────────────────────────────────────────
_scanner_thread = None
_scanner_running = False
_socketio = None
def scanner_loop(app):
    global _scanner_running
    _scanner_running = True
    while _scanner_running:
        try:
            win = in_window()
            if win:
                if not _pool.get('codes') or time.time()-_pool['updated'] > 300:
                    build_pool(force=True)
                alerts = scan_once()
                for a in alerts:
                    try:
                        if _socketio:
                            _socketio.emit('sniper_alert', a, namespace='/')
                        print(f'[sniper alert] {a["name"]} {a["code"]} +{a["change_pct"]:.2f}% vol_ratio={a["vol_ratio"]:.2f}')
                        # 微信推送已关闭（用户要求：早盘监测不再推微信，仅页面内 / 浏览器提醒）
                        # _push_wechat(a)
                    except Exception as e:
                        print('[emit err]', e)
                print(f'[sniper {datetime.datetime.now().strftime("%H:%M:%S")}] {win} pool={len(_pool["codes"])} alerts={len(alerts)}')
        except Exception as e:
            print('[scanner err]', e)
        time.sleep(SCAN_INTERVAL_SEC)

def start_scanner(app, socketio):
    global _socketio, _scanner_thread
    _socketio = socketio
    if _scanner_thread and _scanner_thread.is_alive():
        return
    _scanner_thread = threading.Thread(target=scanner_loop, args=(app,), daemon=True)
    _scanner_thread.start()
    print('[sniper] scanner thread started')

# ─── API ──────────────────────────────────────────────────
@bp.route('/api/sniper/pool')
def sniper_pool():
    p = _pool
    return jsonify({
        'codes': p['codes'], 'count': len(p['codes']),
        'sectors': dict(list(p['sector_map'].items())[:200]),
        'updated': int(p['updated']),
        'window': in_window(),
    })

@bp.route('/api/sniper/alerts')
def sniper_alerts():
    return jsonify({'alerts': _alerts[:20], 'window': in_window(), 'pool_size': len(_pool['codes'])})

@bp.route('/api/sniper/refresh_pool', methods=['POST'])
def sniper_refresh():
    build_pool(force=True)
    return jsonify({'ok': True, 'count': len(_pool['codes'])})