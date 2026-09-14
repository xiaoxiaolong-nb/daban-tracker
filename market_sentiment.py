"""
全市场情绪扫描 — akshare 官方涨停/炸板/跌停股池
数据源：东方财富官方（与同花顺/选股宝/通达信 L2 涨停复盘同源）
- 封板池：ak.stock_zt_pool_em(date) — 收盘价≥涨停价的"封死"涨停股
- 炸板池：ak.stock_zt_pool_zbgc_em(date) — 触板后炸开的股
- 跌停池：ak.stock_zt_pool_dtgc_em(date) — 跌停股
ST 名单缓存：qt.gtimg.cn 名称识别（保留原机制）
输出：market_sentiment.json
用法：python market_sentiment.py
"""
import os, sys, json, time
from datetime import datetime, timedelta
import urllib.request
import pandas as pd
import akshare as ak

# UTF-8 输出（gbk 终端安全）
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

OUT = os.path.join(os.path.dirname(__file__), 'market_sentiment.json')
ST_CACHE = os.path.join(os.path.dirname(__file__), 'st_codes.json')
ST_CACHE_DAYS = 7
HISTORY_DAYS = 15  # 近 3 周交易日（约 11~15 天有数据），akshare 窗口约 3 周
AVG_DAYS = 20  # 均值窗口；实际数据不足时动态取已获得的天数


def load_st_codes():
    """7 天内复用 qt.gtimg.cn 缓存的 ST 名单，否则重拉。"""
    if os.path.exists(ST_CACHE):
        try:
            cd = json.load(open(ST_CACHE, 'r', encoding='utf-8'))
            fetched = cd.get('fetched', '')
            if fetched:
                age = (datetime.now() - datetime.fromisoformat(fetched)).days
                if age < ST_CACHE_DAYS:
                    return set(cd.get('codes', []))
        except Exception:
            pass

    # 从本机 .day 列出全部股票代码
    vip = r'C:/new_tdx/vipdoc'
    codes = []
    for mkt in ('sh', 'sz', 'bj'):
        d = os.path.join(vip, mkt, 'lday')
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith('.day') and f.startswith(mkt):
                codes.append(f.replace('.day', '').lower())

    st = set()
    batch = 80
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        url = 'https://qt.gtimg.cn/q=' + ','.join(chunk)
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://stockapp.finance.qq.com/'
            })
            with urllib.request.urlopen(req, timeout=8) as r:
                text = r.read().decode('gbk', errors='ignore')
            for line in text.strip().split('\n'):
                if '="' not in line:
                    continue
                key, inner = line.split('="', 1)
                q = key.strip().lstrip('v_').lower()
                parts = inner.rstrip(';"\n').split('~')
                if len(parts) < 2:
                    continue
                name = parts[1].strip().upper()
                if 'ST' in name:
                    st.add(q)
        except Exception as e:
            print(f'  [ST 拉取警告] 第 {i}~{i+len(chunk)} 批: {e}')
        time.sleep(0.05)
        if (i // batch) % 10 == 0:
            print(f'  [ST] 进度 {i}/{len(codes)}')

    try:
        json.dump({'fetched': datetime.now().isoformat(), 'codes': sorted(st)},
                  open(ST_CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    except Exception:
        pass
    print(f'  [ST] 识别完成：共 {len(st)} 只（已缓存到 st_codes.json）')
    return st


def safe_call(fn, date_str, retries=3, sleep=1.0):
    """akshare 接口带重试，返回 DataFrame（空表表示无数据或全 ST）。"""
    for i in range(retries):
        try:
            df = fn(date=date_str)
            if df is None:
                return pd.DataFrame()
            return df
        except Exception as e:
            if i < retries - 1:
                time.sleep(sleep * (i + 1))
                continue
            return pd.DataFrame()
    return pd.DataFrame()


def scan_one(date_str, st_set):
    """扫描单个交易日，返回 stat dict。"""
    up = safe_call(ak.stock_zt_pool_em, date_str)        # 封板池
    zhangting = safe_call(ak.stock_zt_pool_zbgc_em, date_str)  # 炸板池
    dieting = safe_call(ak.stock_zt_pool_dtgc_em, date_str)    # 跌停池

    # 剔除 ST
    if len(up) and '代码' in up.columns:
        up = up[~up['代码'].astype(str).str.lower().isin(st_set)]
    if len(zhangting) and '代码' in zhangting.columns:
        zhangting = zhangting[~zhangting['代码'].astype(str).str.lower().isin(st_set)]
    if len(dieting) and '代码' in dieting.columns:
        dieting = dieting[~dieting['代码'].astype(str).str.lower().isin(st_set)]

    def cnt_by_board(df, board_val):
        if df is None or len(df) == 0 or '连板数' not in df.columns:
            return 0
        try:
            b = pd.to_numeric(df['连板数'], errors='coerce').fillna(0).astype(int)
            return int((b == board_val).sum())
        except Exception:
            return 0

    max_board = 0
    if len(up) and '连板数' in up.columns:
        try:
            b = pd.to_numeric(up['连板数'], errors='coerce').fillna(0).astype(int)
            max_board = int(b.max()) if len(b) else 0
        except Exception:
            pass

    return {
        'date': date_str,
        'limit_up': len(up),
        'zhangting': len(zhangting),
        'limit_down': len(dieting),
        'first_board': cnt_by_board(up, 1),
        'second_board': cnt_by_board(up, 2),
        'third_board': cnt_by_board(up, 3),
        'fourth_board': cnt_by_board(up, 4),
        'fifth_board': cnt_by_board(up, 5),
        'max_board': max_board,
    }


def get_recent_trading_days(n=HISTORY_DAYS, end_date=None):
    """从 akshare 交易日历取最近 n 个交易日。"""
    if end_date is None:
        end_date = datetime.now().date()
    try:
        cal = ak.tool_trade_date_hist_sina()
        cal['trade_date'] = pd.to_datetime(cal['trade_date']).dt.date
        # 取 end_date 之前的最近 n 个交易日
        cal = cal[cal['trade_date'] <= end_date].tail(n)
        return [d.strftime('%Y%m%d') for d in cal['trade_date']]
    except Exception as e:
        print(f'  [警告] 交易日历拉取失败，改用周末跳过: {e}')
        # fallback
        days = []
        d = end_date
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d.strftime('%Y%m%d'))
            d -= timedelta(days=1)
        return list(reversed(days))


def calc_adv(prev, curr):
    """1进2 / 2进3 / 3进4 / 4进5 晋级率（%）。"""
    out = {}
    for prev_b, curr_b, key in [
        ('first_board', 'second_board', 'adv_12'),
        ('second_board', 'third_board', 'adv_23'),
        ('third_board', 'fourth_board', 'adv_34'),
        ('fourth_board', 'fifth_board', 'adv_45'),
    ]:
        y = prev.get(prev_b, 0) if prev else 0
        t = curr.get(curr_b, 0) if curr else 0
        out[key] = round(t / y * 100, 1) if y > 0 else None
    return out


def main(end_date=None):
    print(f'[启动] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    st_set = load_st_codes()
    print(f'[ST] 共 {len(st_set)} 只 ST/*ST 已剔除')

    days = get_recent_trading_days(HISTORY_DAYS, end_date)
    print(f'[扫描] {len(days)} 个交易日（{days[0]} ~ {days[-1]}）')

    daily = []
    t0 = time.time()
    for i, d in enumerate(days):
        s = scan_one(d, st_set)
        # 全部为 0 视为无数据（节假日 akshare 返回空）；跳过
        if s['limit_up'] == 0 and s['limit_down'] == 0 and s['zhangting'] == 0:
            print(f'  [{i+1}/{len(days)}] {d}  无数据（节假日/未更新），跳过')
            continue
        daily.append(s)
        print(f'  [{i+1}/{len(days)}] {d}  封板{s["limit_up"]:>3} 炸板{s["zhangting"]:>2} 跌停{s["limit_down"]:>3} '
              f'1板{s["first_board"]:>3} 2板{s["second_board"]:>2} 3板{s["third_board"]:>2} '
              f'4板+{s["fourth_board"]+s["fifth_board"]:>2} 最高{s["max_board"]}板')
        time.sleep(0.4)  # 限速，避免被反爬

    # 计算 adv（每日相对前一日）
    for i in range(1, len(daily)):
        adv = calc_adv(daily[i - 1], daily[i])
        daily[i].update(adv)
    # 第一天 adv 留空
    if daily:
        daily[0].update({'adv_12': None, 'adv_23': None, 'adv_34': None, 'adv_45': None})

    # 聚合
    keys_num = ['limit_up', 'zhangting', 'limit_down',
                'first_board', 'second_board', 'third_board', 'fourth_board', 'fifth_board']
    keys_adv = ['adv_12', 'adv_23', 'adv_34', 'adv_45']

    def avg_of(k, n=AVG_DAYS):
        vals = [d[k] for d in daily[-n:] if d.get(k) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    avg_20 = {k: avg_of(k, AVG_DAYS) for k in keys_num + keys_adv}
    # max_board 在近20日的最大值
    if daily:
        avg_20['max_board'] = max((d['max_board'] for d in daily[-AVG_DAYS:]), default=0)

    # 精简字段
    def to_stat(d):
        out = {k: d.get(k) for k in (keys_num + keys_adv + ['max_board'])}
        out['date'] = d.get('date')
        return out

    out = {
        'updated': datetime.now().isoformat(timespec='seconds'),
        'st_excluded': len(st_set),
        'source': 'akshare (东方财富官方涨停/炸板/跌停股池)',
        'trading_days': len(daily),
        'latest': to_stat(daily[-1]) if daily else None,
        'prev': to_stat(daily[-2]) if len(daily) >= 2 else None,
        'avg_20': avg_20,
        'recent5': [to_stat(d) for d in daily[-5:]],
        'daily': [to_stat(d) for d in daily],  # 90 天明细
    }

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t0
    print(f'[完成] {len(daily)} 个交易日，耗时 {elapsed:.1f}s')
    print(f'  输出: {OUT}')
    if daily:
        last = daily[-1]
        print(f'  最新一日 ({last["date"]}): 封板 {last["limit_up"]} 炸板 {last["zhangting"]} '
              f'跌停 {last["limit_down"]} 最高 {last["max_board"]}板')
        print(f'  近 20 日均值: 封板 {avg_20["limit_up"]} 跌停 {avg_20["limit_down"]} '
              f'1进2 {avg_20["adv_12"]}% 2进3 {avg_20["adv_23"]}% 3进4 {avg_20["adv_34"]}%')


if __name__ == '__main__':
    # 允许指定历史结束日期：python market_sentiment.py 20260904
    end = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit() and len(sys.argv[1]) == 8:
        end = datetime.strptime(sys.argv[1], '%Y%m%d').date()
    main(end)
