import os, sys
_app_dir = os.path.dirname(os.path.abspath(__file__))
if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)
import network_inventory
from pdm_client import get_pdm_summary, get_pdm_chart_bytes

import io, os, sys, json, time, re, ssl, subprocess, queue, threading, urllib.request, urllib.parse
from datetime import datetime, date
from urllib.parse import urlparse, parse_qs
import http.server

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

PORT = 11440
CA_CERT = os.getenv('CA_CERT_PATH', '/etc/ssl/certs/internal-ca.crt')
TARGET_BASE = os.getenv('AI_API_BASE', 'https://192.168.1.60:11443')
if os.path.exists(CA_CERT):
    ssl_context = ssl.create_default_context(cafile=CA_CERT)
else:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
ZABBIX_URL = os.getenv('ZABBIX35_URL', 'http://192.168.1.35/zabbix/api_jsonrpc.php')
ZABBIX_TOKEN = 'YOUR_ZABBIX_API_TOKEN'
ZABBIX74_URL = os.getenv('ZABBIX74_URL', 'http://192.168.1.27/zabbix/api_jsonrpc.php')
ZABBIX74_USER = 'Admin'
ZABBIX74_PASS = 'zabbix'
ZABBIX20_URL_API = os.getenv('ZABBIX20_URL', 'http://192.168.1.20/zabbix/api_jsonrpc.php')
ZABBIX20_USER_API = 'Admin'
ZABBIX20_PASS_API = 'zabbix'
SUBSCRIBERS_FILE = os.getenv('SUBSCRIBERS_FILE', '/data/subscribers.json')

ZABBIX_SERVERS_META = {
    'zbx35': {'name': 'Zabbix Core (7.0 Principal - 192.168.1.35)', 'short': 'ZabbixCore', 'ip': '192.168.1.35', 'url': ZABBIX_URL},
    'zbx74': {'name': 'Zabbix Dist (7.4 Distribuido - 192.168.1.27)', 'short': 'ZabbixDist', 'ip': '192.168.1.27', 'url': ZABBIX74_URL},
    'zbx20': {'name': 'Zabbix Legacy (6.2 Central - 192.168.1.20)', 'short': 'ZabbixLegacy', 'ip': '192.168.1.20', 'url': ZABBIX20_URL_API},
}

_zbx_tokens = {'zbx35': (ZABBIX_TOKEN, 9999999999), 'zbx74': (None, 0), 'zbx20': (None, 0)}
_zbx_lock = threading.Lock()

def _get_zbx_token(srv_key):
    now = time.time()
    with _zbx_lock:
        tok, ts = _zbx_tokens.get(srv_key, (None, 0))
        if tok and (now - ts < 1800):
            return tok
    try:
        if srv_key == 'zbx35':
            return ZABBIX_TOKEN
        elif srv_key == 'zbx74':
            req_data = json.dumps({"jsonrpc": "2.0", "method": "user.login", "params": {"username": ZABBIX74_USER, "password": ZABBIX74_PASS}, "id": 1}).encode('utf-8')
            req = urllib.request.Request(ZABBIX74_URL, data=req_data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                tok = json.loads(resp.read().decode('utf-8')).get('result')
                if tok:
                    with _zbx_lock:
                        _zbx_tokens['zbx74'] = (tok, now)
                return tok
        elif srv_key == 'zbx20':
            req_data = json.dumps({"jsonrpc": "2.0", "method": "user.login", "params": {"user": ZABBIX20_USER_API, "password": ZABBIX20_PASS_API}, "id": 1}).encode('utf-8')
            req = urllib.request.Request(ZABBIX20_URL_API, data=req_data, headers={'Content-Type': 'application/json-rpc'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                tok = json.loads(resp.read().decode('utf-8')).get('result')
                if tok:
                    with _zbx_lock:
                        _zbx_tokens['zbx20'] = (tok, now)
                return tok
    except Exception as e:
        print(f"[MultiZabbix] Token error on {srv_key}: {e}", flush=True)
    return None

def zabbix_query(method, params, srv_key='zbx35', timeout=8):
    tok = _get_zbx_token(srv_key)
    if not tok:
        return []
    url = ZABBIX_SERVERS_META.get(srv_key, ZABBIX_SERVERS_META['zbx35'])['url']
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    headers = {'Content-Type': 'application/json'}
    if srv_key == 'zbx20':
        payload['auth'] = tok
        headers['Content-Type'] = 'application/json-rpc'
    else:
        headers['Authorization'] = f'Bearer {tok}'
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            if 'error' in res and srv_key in ('zbx74', 'zbx20'):
                with _zbx_lock:
                    _zbx_tokens[srv_key] = (None, 0)
                return []
            return res.get('result', [])
    except Exception as e:
        print(f"[MultiZabbix] Query error ({srv_key}.{method}): {e}", flush=True)
        return []

network_inv = network_inventory.NetworkInventory(zabbix_query)
network_inv.start_background_syncer()

def find_host_across_zabbix(target):
    target = str(target or '').strip()
    if not target:
        return 'zbx35', None, '', None
    # 1. Smart resolve via unified network inventory cache
    res = network_inv.resolve(target)
    if res:
        return res['srv_key'], res['hostid'], res['name'], res['ip']
    # 2. Fallback to direct Zabbix queries if not found in cache
    es_ip = all(part.isdigit() for part in target.split('.')) and len(target.split('.')) == 4
    for srv_key in ('zbx35', 'zbx74', 'zbx20'):
        if es_ip:
            ifaces = zabbix_query('hostinterface.get', {'output': ['hostid', 'ip'], 'filter': {'ip': target}}, srv_key=srv_key)
            if ifaces:
                hid = ifaces[0]['hostid']
                hosts = zabbix_query('host.get', {'output': ['hostid', 'name', 'host'], 'hostids': [hid]}, srv_key=srv_key)
                if hosts:
                    return srv_key, hid, (hosts[0].get('name') or hosts[0].get('host')), target
        hosts = zabbix_query('host.get', {'output': ['hostid', 'name', 'host'], 'selectInterfaces': ['ip'], 'search': {'name': target, 'host': target}, 'searchByAny': True, 'limit': 5}, srv_key=srv_key)
        if hosts:
            exact = [h for h in hosts if target.lower() in (str(h.get('name','')).lower(), str(h.get('host','')).lower())]
            chosen = exact[0] if exact else hosts[0]
            ip_addr = chosen.get('interfaces', [{}])[0].get('ip') if chosen.get('interfaces') else None
            return srv_key, chosen['hostid'], (chosen.get('name') or chosen.get('host')), ip_addr
    return 'zbx35', None, target, None


class EventHub:
    def __init__(self):
        self.subscribers = []
        self.history = []
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, event_data):
        with self.lock:
            if 'timestamp' not in event_data:
                event_data['timestamp'] = datetime.now().strftime('%H:%M:%S')
            self.history.append(event_data)
            if len(self.history) > 200:
                self.history.pop(0)
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(event_data)
                except queue.Full:
                    dead.append(q)
            for d in dead:
                if d in self.subscribers:
                    self.subscribers.remove(d)

event_hub = EventHub()

def get_subscribers():
    if not os.path.exists(SUBSCRIBERS_FILE):
        return []
    try:
        with open(SUBSCRIBERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def save_subscribers(subs):
    with open(SUBSCRIBERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(subs, f, indent=2)

# zabbix_query defined in MultiZabbix block above

def parse_time_window(period_str):
    raw_p = str(period_str or '24h').strip()
    p = raw_p.lower()
    now_ts = int(time.time())

    # Soporte TODAY / HOY
    if p in ['today', 'hoy', 'now/d']:
        now_dt = datetime.fromtimestamp(now_ts)
        start_of_day = datetime(now_dt.year, now_dt.month, now_dt.day, 0, 0, 0)
        since_ts = int(start_of_day.timestamp())
        duration_sec = now_ts - since_ts
        return since_ts, now_ts, duration_sec, "Hoy (Today)", "today"

    duration_sec = 24 * 3600
    label = raw_p

    # Años (y / anio / a) ej: 1y, 2y, 1anio, 2anios
    if re.match(r'^(\d+)\s*(y|anio|ano|anios|anos)$', p):
        val = int(re.match(r'^(\d+)', p).group(1))
        duration_sec = val * 365 * 86400
        label = f"{val} anio{'s' if val > 1 else ''}"
    # Meses (mo / mes / meses / m cuando es > 45 o explícito)
    elif re.match(r'^(\d+)\s*(mo|mes|meses)$', p):
        val = int(re.match(r'^(\d+)', p).group(1))
        duration_sec = val * 30 * 86400
        label = f"{val} mes{'es' if val > 1 else ''}"
    # Semanas (w / sem / semanas)
    elif re.match(r'^(\d+)\s*(w|sem|semana|semanas)$', p):
        val = int(re.match(r'^(\d+)', p).group(1))
        duration_sec = val * 7 * 86400
        label = f"{val} semana{'s' if val > 1 else ''}"
    # Días (d / dia / dias)
    elif re.match(r'^(\d+)\s*(d|dia|dias)$', p):
        val = int(re.match(r'^(\d+)', p).group(1))
        duration_sec = val * 86400
        label = f"{val} dia{'s' if val > 1 else ''}"
    # Horas (h / hr / hrs / hora / horas)
    elif re.match(r'^(\d+)\s*(h|hr|hrs|hora|horas)$', p):
        val = int(re.match(r'^(\d+)', p).group(1))
        duration_sec = max(300, val * 3600)
        label = f"{val} hora{'s' if val > 1 else ''}"
    # Minutos (m / min / mins / minuto / minutos)
    elif re.match(r'^(\d+)\s*(m|min|mins|minuto|minutos)$', p) and not p.endswith('pm') and not p.endswith('am'):
        val = int(re.match(r'^(\d+)', p).group(1))
        duration_sec = max(60, val * 60)
        label = f"{val} minutos"
    # Dígitos puros
    elif p.isdigit():
        val = int(p)
        if val <= 90:  # Tratar números chicos como horas
            duration_sec = val * 3600
            label = f"{val} horas"
        else:
            duration_sec = val
            label = f"{val}s"

    since_ts = now_ts - duration_sec
    return since_ts, now_ts, duration_sec, label, raw_p

def parse_period_to_seconds(period_str):
    _, _, sec, _, _ = parse_time_window(period_str)
    return sec

def resolve_interface_and_host(host_param, iface_param):
    host_param = str(host_param or '').strip()
    iface_param = str(iface_param or '').strip()

    single_param = None
    if host_param and not iface_param:
        single_param = host_param
    elif iface_param and not host_param:
        single_param = iface_param

    target_iface = iface_param or (single_param if single_param else '')
    target_host = host_param if (host_param and iface_param) else ''

    search_terms = [target_iface] if target_iface else []
    if target_iface.lower().startswith('vlan176'):
        search_terms.append('vlan76' + target_iface[7:])
    elif target_iface.lower().startswith('vlan76'):
        search_terms.append('vlan176' + target_iface[6:])

    for srv_key in ('zbx35', 'zbx74', 'zbx20'):
        items_rx = []
        items_tx = []
        resolved_host_id = None
        resolved_host_name = None

        if srv_key in ('zbx35', 'zbx74'):
            for term in search_terms:
                if not term: continue
                tag_items = zabbix_query('item.get', {
                    'output': ['itemid', 'hostid', 'name', 'lastvalue'],
                    'tags': [{'tag': 'interface', 'value': term, 'operator': '0'}]
                }, srv_key=srv_key)
                if tag_items:
                    for it in tag_items:
                        n = it['name'].lower()
                        if 'bits received' in n: items_rx.append(it)
                        elif 'bits sent' in n: items_tx.append(it)
                    if items_rx or items_tx:
                        break

        if not items_rx:
            for term in search_terms:
                if not term: continue
                name_rx = zabbix_query('item.get', {
                    'output': ['itemid', 'hostid', 'name', 'lastvalue'],
                    'search': {'name': f'Interface {term}'},
                    'limit': 20
                }, srv_key=srv_key)
                for it in name_rx:
                    n = it['name'].lower()
                    if 'bits received' in n: items_rx.append(it)
                    elif 'bits sent' in n: items_tx.append(it)
                if items_rx or items_tx:
                    break

        best_rx = None
        best_tx = None

        if items_rx:
            if target_host:
                for it in items_rx:
                    h = zabbix_query('host.get', {'hostids': [it['hostid']], 'output': ['name', 'host']}, srv_key=srv_key)
                    if h:
                        hname = h[0].get('name') or h[0].get('host')
                        if target_host.lower() in hname.lower():
                            best_rx = it
                            resolved_host_id = it['hostid']
                            resolved_host_name = hname
                            break

            if not best_rx:
                items_rx.sort(key=lambda x: float(x.get('lastvalue') or 0), reverse=True)
                best_rx = items_rx[0]
                resolved_host_id = best_rx['hostid']
                h = zabbix_query('host.get', {'hostids': [resolved_host_id], 'output': ['name', 'host']}, srv_key=srv_key)
                resolved_host_name = h[0].get('name') or h[0].get('host') if h else "Dispositivo"

            for it in items_tx:
                if it['hostid'] == resolved_host_id:
                    best_tx = it
                    break

            if best_rx:
                best_rx['_srv_key'] = srv_key
            if best_tx:
                best_tx['_srv_key'] = srv_key
            return resolved_host_name, resolved_host_id, best_rx, best_tx

    return None, None, None, None

def get_host_interfaces(target):
    target = str(target or '').strip()
    srv_key, host_id, host_name, _ = find_host_across_zabbix(target)

    if not host_id:
        return {"error": f"Host '{target}' no encontrado en ninguno de los servidores Zabbix (Zabbix35, Zabbix74, Zabbix20)"}

    items_in = zabbix_query('item.get', {
        'hostids': [host_id],
        'output': ['itemid', 'name', 'key_', 'lastvalue'],
        'search': {'name': 'Bits received'},
        'limit': 150
    }, srv_key=srv_key)
    items_out = zabbix_query('item.get', {
        'hostids': [host_id],
        'output': ['itemid', 'name', 'key_', 'lastvalue'],
        'search': {'name': 'Bits sent'},
        'limit': 150
    }, srv_key=srv_key)

    if not items_in:
        items_in = zabbix_query('item.get', {
            'hostids': [host_id],
            'output': ['itemid', 'name', 'key_', 'lastvalue'],
            'search': {'key_': 'net.if.in'},
            'limit': 150
        }, srv_key=srv_key)
    if not items_out:
        items_out = zabbix_query('item.get', {
            'hostids': [host_id],
            'output': ['itemid', 'name', 'key_', 'lastvalue'],
            'search': {'key_': 'net.if.out'},
            'limit': 150
        }, srv_key=srv_key)

    out_map = {}
    for o in items_out:
        clean_name = o['name'].replace(': Bits sent', '').replace('Interface ', '').strip()
        out_map[clean_name] = float(o.get('lastvalue', 0) or 0)

    ifaces_list = []
    for it in items_in:
        raw_name = it['name']
        clean_name = raw_name.replace(': Bits received', '').replace('Interface ', '').strip()
        last_in_bps = float(it.get('lastvalue', 0) or 0)
        last_in_mbps = round(last_in_bps / 1000000.0, 2)
        last_out_bps = out_map.get(clean_name, 0.0)
        last_out_mbps = round(last_out_bps / 1000000.0, 2)
        ifaces_list.append({
            "name": clean_name,
            "in_mbps": last_in_mbps,
            "out_mbps": last_out_mbps,
            "total_mbps": round(last_in_mbps + last_out_mbps, 2)
        })

    # Sort active interfaces (>0 Mbps) first, ordered by total traffic descending
    ifaces_list.sort(key=lambda x: (1 if x['total_mbps'] > 0 else 0, x['total_mbps']), reverse=True)

    return {
        "host": host_name,
        "zabbix_server": ZABBIX_SERVERS_META.get(srv_key, {}).get('short', srv_key),
        "total": len(ifaces_list),
        "active_count": sum(1 for x in ifaces_list if x['total_mbps'] > 0),
        "interfaces": ifaces_list
    }

def get_traffic_graph_with_stats(query_term="192.168.1.1", period_str="3h", iface_filter=""):
    query_term = str(query_term or '').strip()
    iface_filter = str(iface_filter or '').strip()

    # 1. Resolver interfaz y host automáticamente (si se buscó directamente por nombre de interfaz global)
    res_host_name, res_host_id, res_rx, res_tx = None, None, None, None
    srv_key = 'zbx35'

    # Si query_term es un host conocido en cualquiera de los 3 Zabbix, priorizar ese host
    found_srv, found_hid, found_hname, _ = find_host_across_zabbix(query_term)
    if found_hid and not iface_filter:
        srv_key = found_srv
        host_id = found_hid
        host_name = found_hname
        in_item = None
        out_item = None
    elif found_hid and iface_filter:
        srv_key = found_srv
        host_id = found_hid
        host_name = found_hname
        in_item = None
        out_item = None
    else:
        res_host_name, res_host_id, res_rx, res_tx = resolve_interface_and_host(query_term, iface_filter)
        in_item = res_rx
        out_item = res_tx
        host_id = res_host_id
        host_name = res_host_name or query_term
        if in_item and '_srv_key' in in_item:
            srv_key = in_item['_srv_key']

    # 2. Si tenemos host_id pero aún no in_item/out_item, buscar items en srv_key
    if not in_item:
        if not host_id:
            srv_key, host_id, host_name, _ = find_host_across_zabbix(query_term)
        if not host_id:
            hosts = zabbix_query('host.get', {'output': ['hostid', 'name'], 'limit': 1}, srv_key='zbx35')
            if hosts:
                host_id = hosts[0]['hostid']
                host_name = hosts[0]['name']
                srv_key = 'zbx35'

        in_candidates = zabbix_query('item.get', {
            'hostids': [host_id],
            'output': ['itemid', 'name', 'key_', 'units', 'lastvalue'],
            'search': {'name': 'Bits received'},
            'limit': 150
        }, srv_key=srv_key)
        out_candidates = zabbix_query('item.get', {
            'hostids': [host_id],
            'output': ['itemid', 'name', 'key_', 'units', 'lastvalue'],
            'search': {'name': 'Bits sent'},
            'limit': 150
        }, srv_key=srv_key)

        iface_clean = (iface_filter or query_term).lower()
        if iface_filter:
            for it in in_candidates:
                if iface_clean in it['name'].lower() or iface_clean in it['key_'].lower():
                    in_item = it; break
            for it in out_candidates:
                if iface_clean in it['name'].lower() or iface_clean in it['key_'].lower():
                    out_item = it; break

        if not in_item and in_candidates:
            is_outbound = any(w in query_term.lower() or w in iface_filter.lower() for w in ['saliendo', 'salida', 'tx', 'subida', 'outbound', 'out', 'clientes'])
            if is_outbound and out_candidates:
                out_candidates.sort(key=lambda x: float(x.get('lastvalue') or 0), reverse=True)
                out_item = out_candidates[0]
                target_prefix = out_item['name'].replace(': Bits sent', '')
                for i_c in in_candidates:
                    if i_c['name'].startswith(target_prefix):
                        in_item = i_c; break
            if not in_item:
                in_candidates.sort(key=lambda x: float(x.get('lastvalue') or 0), reverse=True)
                in_item = in_candidates[0]
                target_prefix = in_item['name'].replace(': Bits received', '')
                for o in out_candidates:
                    if o['name'].startswith(target_prefix):
                        out_item = o; break
        if not out_item and out_candidates:
            out_item = out_candidates[0]

    since_ts, now_ts, duration_sec, period_label, raw_period = parse_time_window(period_str)
    use_trends = duration_sec > (24 * 3600)

    hist_in = []
    hist_out = []

    limit_points = 2500 if duration_sec > (30 * 86400) else 1000

    if use_trends:
        if in_item:
            hist_in = zabbix_query('trend.get', {'itemids': [in_item['itemid']], 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points}, srv_key=srv_key)
        if out_item:
            hist_out = zabbix_query('trend.get', {'itemids': [out_item['itemid']], 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points}, srv_key=srv_key)
    else:
        if in_item:
            hist_in = zabbix_query('history.get', {'itemids': [in_item['itemid']], 'history': 3, 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points}, srv_key=srv_key)
        if out_item:
            hist_out = zabbix_query('history.get', {'itemids': [out_item['itemid']], 'history': 3, 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points}, srv_key=srv_key)

    val_key = 'value_avg' if use_trends else 'value'
    t_in = [datetime.fromtimestamp(int(x['clock'])) for x in hist_in]
    v_in = [float(x.get(val_key, 0)) / 1000000.0 for x in hist_in]

    t_out = [datetime.fromtimestamp(int(x['clock'])) for x in hist_out]
    v_out = [float(x.get(val_key, 0)) / 1000000.0 for x in hist_out]

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10.5, 4.8), dpi=130)
    fig.patch.set_facecolor('#0B0F19')
    ax.set_facecolor('#111827')

    max_in = max(v_in) if v_in else 0.0
    avg_in = sum(v_in)/len(v_in) if v_in else 0.0
    last_in = v_in[-1] if v_in else 0.0

    max_out = max(v_out) if v_out else 0.0
    avg_out = sum(v_out)/len(v_out) if v_out else 0.0
    last_out = v_out[-1] if v_out else 0.0

    disp_iface = in_item['name'].replace(': Bits received', '').replace('Interface ', '').strip() if in_item else (iface_filter or "General")

    if t_in:
        ax.plot(t_in, v_in, color='#10B981', linewidth=1.6, label=f'RX (Inbound):  Act={last_in:.2f} Mbps | Max={max_in:.2f} | Avg={avg_in:.2f}')
        ax.fill_between(t_in, v_in, color='#10B981', alpha=0.35)
    if t_out:
        ax.plot(t_out, v_out, color='#3B82F6', linewidth=1.6, label=f'TX (Outbound): Act={last_out:.2f} Mbps | Max={max_out:.2f} | Avg={avg_out:.2f}')
        ax.fill_between(t_out, v_out, color='#3B82F6', alpha=0.25)

    ax.set_title(f"NETCORE NOC · {host_name} · {disp_iface} ({period_label})", fontsize=11, fontweight='bold', color='#F3F4F6', pad=12)
    ax.set_ylabel("Ancho de Banda (Mbps)", fontsize=9, color='#9CA3AF')
    ax.grid(True, linestyle='--', alpha=0.22, color='#4B5563')

    dt_start = datetime.fromtimestamp(since_ts)
    dt_end = datetime.fromtimestamp(now_ts)
    ax.set_xlim(dt_start, dt_end)

    if duration_sec <= 86400:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    elif duration_sec <= (30 * 86400):
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m %H:%M'))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m/%Y'))

    ax.legend(loc='upper left', framealpha=0.88, facecolor='#1F2937', edgecolor='#374151', fontsize=8)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    return buf.getvalue(), {
        "host": host_name,
        "interface": disp_iface,
        "period_label": period_label,
        "rx_last": last_in, "rx_max": max_in, "rx_avg": avg_in,
        "tx_last": last_out, "tx_max": max_out, "tx_avg": avg_out
    }

def get_traffic_graph_bytes(query_term="192.168.1.1", period_str="3h", iface_filter=""):
    img_bytes, _ = get_traffic_graph_with_stats(query_term, period_str, iface_filter)
    return img_bytes


def build_pdf_report(period_hours=24, host_target="", iface_filter=""):
    if host_target and not iface_filter:
        r_name, r_id, r_rx, r_tx = resolve_interface_and_host(host_target, "")
        if r_name and (r_rx or r_tx):
            iface_filter = host_target
            host_target = r_name

    since_ts, now_ts, duration_sec, period_label, raw_period = parse_time_window(period_hours)
    
    target_to_graph = host_target or ("CoreRouter-01" if iface_filter else "")
    is_interface_specific = bool(iface_filter or (host_target and host_target.lower() != 'all'))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    header_title_style = ParagraphStyle('HeaderTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=colors.HexColor('#0F172A'))
    subtitle_style = ParagraphStyle('SubTitle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=12, textColor=colors.HexColor('#64748B'))
    card_num_style = ParagraphStyle('CardNum', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=14, leading=16, alignment=1, textColor=colors.HexColor('#0F172A'))
    card_label_style = ParagraphStyle('CardLabel', parent=styles['Normal'], fontName='Helvetica', fontSize=8, leading=10, alignment=1, textColor=colors.HexColor('#64748B'))
    section_title = ParagraphStyle('SectionTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=12, leading=15, textColor=colors.HexColor('#1E293B'))
    cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontName='Helvetica', fontSize=8, leading=10, textColor=colors.HexColor('#334155'))
    cell_bold = ParagraphStyle('CellBold', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#0F172A'))

    now_str = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    if is_interface_specific and target_to_graph:
        chart_bytes, stats = get_traffic_graph_with_stats(target_to_graph, raw_period, iface_filter)
        report_title = f"REPORTE DE TRÁFICO Y RENDIMIENTO DE INTERFAZ"
        report_subtitle = f"Equipo: <b>{stats['host']}</b> · Interfaz: <b>{stats['interface']}</b> · Periodo: <b>{period_label}</b> · Generado: {now_str}"

        story.append(Paragraph(report_title, header_title_style))
        story.append(Paragraph(report_subtitle, subtitle_style))
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#3B82F6'), spaceAfter=12))

        # 1. Gráfica de la interfaz solicitada
        story.append(Paragraph(f"📈 <b>Gráfica de Tráfico Zabbix: {stats['interface']}</b>", section_title))
        story.append(Spacer(1, 6))
        img_stream = io.BytesIO(chart_bytes)
        story.append(Image(img_stream, width=540, height=240))
        story.append(Spacer(1, 12))

        # 2. Tarjetas KPI
        iface_kpis = [
            [
                Paragraph(f"<font color='#10B981'>{stats['rx_last']:.2f} Mbps</font>", card_num_style),
                Paragraph(f"<font color='#059669'>{stats['rx_max']:.2f} Mbps</font>", card_num_style),
                Paragraph(f"<font color='#3B82F6'>{stats['tx_last']:.2f} Mbps</font>", card_num_style),
                Paragraph(f"<font color='#2563EB'>{stats['tx_max']:.2f} Mbps</font>", card_num_style)
            ],
            [
                Paragraph("RX Actual (Inbound)", card_label_style),
                Paragraph(f"RX Máximo ({period_label})", card_label_style),
                Paragraph("TX Actual (Outbound)", card_label_style),
                Paragraph(f"TX Máximo ({period_label})", card_label_style)
            ]
        ]
        kpi_table = Table(iface_kpis, colWidths=[135, 135, 135, 135])
        kpi_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
            ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#E2E8F0')),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ]))
        story.append(kpi_table)
        story.append(Spacer(1, 14))

        # 3. Métricas comparativas
        detail_rows = [
            [Paragraph("<b>Parámetro Técnico</b>", cell_bold), Paragraph("<b>Tráfico de Entrada (RX / Inbound)</b>", cell_bold), Paragraph("<b>Tráfico de Salida (TX / Outbound)</b>", cell_bold)],
            [Paragraph("Consumo Actual (Último)", cell_style), Paragraph(f"<b>{stats['rx_last']:.2f} Mbps</b>", cell_style), Paragraph(f"<b>{stats['tx_last']:.2f} Mbps</b>", cell_style)],
            [Paragraph(f"Promedio ({period_label})", cell_style), Paragraph(f"{stats['rx_avg']:.2f} Mbps", cell_style), Paragraph(f"{stats['tx_avg']:.2f} Mbps", cell_style)],
            [Paragraph(f"Pico Máximo Registrado", cell_style), Paragraph(f"<font color='#059669'><b>{stats['rx_max']:.2f} Mbps</b></font>", cell_style), Paragraph(f"<font color='#2563EB'><b>{stats['tx_max']:.2f} Mbps</b></font>", cell_style)]
        ]
        detail_table = Table(detail_rows, colWidths=[180, 180, 180])
        detail_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#F1F5F9')),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ]))
        story.append(detail_table)
        story.append(Spacer(1, 14))

    else:
        # Reporte General de Incidentes y KPIs de Red
        active_problems = zabbix_query('problem.get', {
            'output': ['eventid', 'name', 'severity', 'clock', 'objectid'],
            'sortfield': ['eventid'],
            'sortorder': 'DESC',
            'limit': 20
        })
        events = zabbix_query('event.get', {
            'output': ['eventid', 'name', 'severity', 'clock', 'value', 'r_eventid'],
            'selectHosts': ['hostid', 'host', 'name'],
            'time_from': since_ts,
            'sortfield': ['clock'],
            'sortorder': 'DESC',
            'limit': 50
        })

        trig_ids = [p['objectid'] for p in active_problems if p.get('objectid') and p['objectid'] != '0']
        host_map = {}
        if trig_ids:
            trigs = zabbix_query('trigger.get', {
                'output': ['triggerid'],
                'selectHosts': ['hostid', 'name', 'host'],
                'triggerids': trig_ids
            })
            for t in trigs:
                if t.get('hosts') and t['hosts']:
                    host_map[t['triggerid']] = t['hosts'][0].get('name') or t['hosts'][0].get('host')

        problems_gen = sum(1 for e in events if e.get('value') == '1')
        recovered_gen = sum(1 for e in events if e.get('value') == '0')
        disaster_count = sum(1 for e in events if e.get('severity') in ['4', '5'])

        report_title = "NETCORE NOC · REPORTE EJECUTIVO GENERAL"
        report_subtitle = f"Monitoreo Zabbix & The Dude · Periodo: {period_label} · Generado: {now_str}"

        story.append(Paragraph(report_title, header_title_style))
        story.append(Paragraph(report_subtitle, subtitle_style))
        story.append(Spacer(1, 12))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#3B82F6'), spaceAfter=14))

        kpi_data = [
            [
                Paragraph(f"<font color='#DC2626'>{len(active_problems)}</font>", card_num_style),
                Paragraph(f"<font color='#EA580C'>{disaster_count}</font>", card_num_style),
                Paragraph(f"<font color='#2563EB'>{problems_gen}</font>", card_num_style),
                Paragraph(f"<font color='#16A34A'>{recovered_gen}</font>", card_num_style)
            ],
            [
                Paragraph("Alertas Activas", card_label_style),
                Paragraph("Eventos Críticos / Alta", card_label_style),
                Paragraph(f"Problemas ({period_label})", card_label_style),
                Paragraph(f"Recuperaciones ({period_label})", card_label_style)
            ]
        ]
        kpi_table = Table(kpi_data, colWidths=[135, 135, 135, 135])
        kpi_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
            ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#E2E8F0')),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ]))
        story.append(kpi_table)
        story.append(Spacer(1, 18))

        story.append(Paragraph("🔴 Incidentes Activos en Tiempo Real", section_title))
        story.append(Spacer(1, 6))

        sev_map = {'0': 'No clasif.', '1': 'Info', '2': 'Advertencia', '3': 'Promedio', '4': 'Alta', '5': 'Desastre'}
        active_rows = [[
            Paragraph("<b>Equipo / Dispositivo</b>", cell_bold),
            Paragraph("<b>Problema Detectado</b>", cell_bold),
            Paragraph("<b>Severidad</b>", cell_bold),
            Paragraph("<b>Inicio</b>", cell_bold)
        ]]

        if active_problems:
            for p in active_problems[:10]:
                h_name = host_map.get(p.get('objectid'), 'Equipo de Red')
                t_str = datetime.fromtimestamp(int(p['clock'])).strftime('%H:%M:%S')
                sev_name = sev_map.get(str(p.get('severity', '0')), 'Info')
                active_rows.append([
                    Paragraph(f"<b>{h_name}</b>", cell_bold),
                    Paragraph(p.get('name', 'Alerta'), cell_style),
                    Paragraph(sev_name, cell_style),
                    Paragraph(t_str, cell_style)
                ])
        else:
            active_rows.append([Paragraph("<i>Sin problemas activos en Zabbix</i>", cell_style), Paragraph("-", cell_style), Paragraph("-", cell_style), Paragraph("-", cell_style)])

        active_table = Table(active_rows, colWidths=[140, 240, 80, 80])
        active_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#F1F5F9')),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ]))
        story.append(active_table)
        story.append(Spacer(1, 18))

        story.append(Paragraph(f"📋 Historial de Eventos Recientes ({period_label})", section_title))
        story.append(Spacer(1, 6))

        hist_rows = [[
            Paragraph("<b>Equipo</b>", cell_bold),
            Paragraph("<b>Evento</b>", cell_bold),
            Paragraph("<b>Estado</b>", cell_bold),
            Paragraph("<b>Hora</b>", cell_bold)
        ]]

        if events:
            for e in events[:12]:
                h_name = e.get('hosts', [{}])[0].get('name') if e.get('hosts') else 'Desconocido'
                is_rec = e.get('value') == '0'
                st_text = "<font color='#16A34A'><b>RECUPERADO</b></font>" if is_rec else "<font color='#DC2626'><b>FALLA</b></font>"
                t_str = datetime.fromtimestamp(int(e['clock'])).strftime('%d/%m %H:%M')
                hist_rows.append([
                    Paragraph(f"<b>{h_name}</b>", cell_bold),
                    Paragraph(e.get('name', 'Evento'), cell_style),
                    Paragraph(st_text, cell_style),
                    Paragraph(t_str, cell_style)
                ])
        else:
            hist_rows.append([Paragraph("<i>No se registran eventos en el periodo</i>", cell_style), Paragraph("-", cell_style), Paragraph("-", cell_style), Paragraph("-", cell_style)])

        hist_table = Table(hist_rows, colWidths=[140, 240, 80, 80])
        hist_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#F1F5F9')),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ]))
        story.append(hist_table)

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#94A3B8'), spaceAfter=8))
    story.append(Paragraph("NetCore Telecom Solutions · Centro de Control de Operaciones (NOC) · Documento Confidencial", subtitle_style))

    doc.build(story)
    buf.seek(0)
    return buf.read()

def get_dashboard_snapshot_bytes():
    hosts = []
    problems = []
    for sk in ('zbx35', 'zbx74', 'zbx20'):
        hosts.extend(zabbix_query('host.get', {'output': ['hostid', 'status'], 'selectInterfaces': ['available']}, srv_key=sk) or [])
        problems.extend(zabbix_query('problem.get', {'output': ['severity']}, srv_key=sk) or [])
    online = 0
    down = 0
    monitored = 0
    disabled = 0
    
    for h in hosts:
        if str(h.get('status')) == '1':
            disabled += 1
        else:
            avail = str(h.get('interfaces', [{}])[0].get('available', '0')) if h.get('interfaces') else '0'
            if avail == '1': online += 1
            elif avail == '2': down += 1
            else: monitored += 1
    sev_counts = {'Desastre': 0, 'Alta': 0, 'Promedio': 0, 'Advertencia': 0, 'Info': 0}
    for p in problems:
        s = p.get('severity', '0')
        if s == '5': sev_counts['Desastre'] += 1
        elif s == '4': sev_counts['Alta'] += 1
        elif s == '3': sev_counts['Promedio'] += 1
        elif s == '2': sev_counts['Advertencia'] += 1
        elif s == '1': sev_counts['Info'] += 1

    traffic_items = zabbix_query('item.get', {
        'output': ['name', 'lastvalue'],
        'search': {'name': 'Bits received'},
        'sortfield': 'lastvalue',
        'sortorder': 'DESC',
        'limit': 6
    })

    plt.style.use('dark_background')
    fig = plt.figure(figsize=(12, 6.5), dpi=130)
    fig.patch.set_facecolor('#0B0F19')

    gs = fig.add_gridspec(2, 2, hspace=0.35, wspace=0.25)
    now_str = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    fig.suptitle(f"NETCORE NOC - DASHBOARD EJECUTIVO EN TIEMPO REAL\n{now_str}", 
                 fontsize=13, fontweight='bold', color='#F8FAFC', y=0.97)

    ax1 = fig.add_subplot(gs[0, 0])
    ax1.set_facecolor('#111827')
    labels = [f'Online ({online})', f'Alerta/Down ({down})', f'Monitoreado ({monitored})']
    sizes = [max(online, 1), max(down, 0), max(monitored, 0)]
    colors_list = ['#10B981', '#EF4444', '#3B82F6']
    wedges, texts, autotexts = ax1.pie(sizes, labels=labels, autopct='%1.0f%%', startangle=140,
                                       colors=colors_list, textprops={'fontsize': 8, 'color': '#D1D5DB'},
                                       wedgeprops=dict(width=0.42, edgecolor='#1F2937'))
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_fontweight('bold')
    ax1.set_title(f"Disponibilidad de Equipos (Total: {len(hosts)})", fontsize=10, fontweight='bold', color='#E2E8F0', pad=8)

    ax2 = fig.add_subplot(gs[0, 1])
    ax2.set_facecolor('#111827')
    sev_names = list(sev_counts.keys())
    sev_vals = list(sev_counts.values())
    bar_colors = ['#DC2626', '#EA580C', '#F59E0B', '#EAB308', '#3B82F6']
    bars = ax2.barh(sev_names, sev_vals, color=bar_colors, height=0.55)
    ax2.set_title(f"Alertas Activas por Severidad (Total: {len(problems)})", fontsize=10, fontweight='bold', color='#E2E8F0', pad=8)
    ax2.set_xlabel("Número de Alertas", fontsize=8, color='#9CA3AF')
    ax2.grid(True, linestyle='--', alpha=0.2, color='#4B5563', axis='x')
    for bar in bars:
        w = bar.get_width()
        if w > 0:
            ax2.text(w + 0.1, bar.get_y() + bar.get_height()/2, f'{int(w)}', ha='left', va='center', color='#F3F4F6', fontweight='bold', fontsize=8)

    ax3 = fig.add_subplot(gs[1, :])
    ax3.set_facecolor('#111827')
    
    if_names = []
    if_mbps = []
    for it in traffic_items:
        clean_name = it['name'].replace('Interface ', '').replace(': Bits received', '').strip()
        mbps = float(it.get('lastvalue', 0)) / 1000000.0
        if_names.append(clean_name[:25])
        if_mbps.append(mbps)
        
    if_names.reverse()
    if_mbps.reverse()
    
    bars3 = ax3.barh(if_names, if_mbps, color='#06B6D4', height=0.55)
    ax3.set_title("Tráfico en Interfaces Clave / Troncales (Mbps)", fontsize=10, fontweight='bold', color='#E2E8F0', pad=8)
    ax3.set_xlabel("Velocidad Actual (Mbps)", fontsize=8, color='#9CA3AF')
    ax3.grid(True, linestyle='--', alpha=0.2, color='#4B5563', axis='x')
    for bar in bars3:
        w = bar.get_width()
        if w > 0:
            ax3.text(w + 0.05, bar.get_y() + bar.get_height()/2, f'{w:.2f} Mbps', ha='left', va='center', color='#F3F4F6', fontweight='bold', fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', facecolor=fig.get_facecolor(), edgecolor='none', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def run_linux_ping(target, count=4):
    try:
        if not re.match(r'^[a-zA-Z0-9._\-]+$', target):
            return {"error": "Target inválido", "target": target}

        result = subprocess.run(
            ['ping', '-c', str(count), '-W', '2', target],
            capture_output=True, text=True, timeout=15
        )
        output = result.stdout + result.stderr

        pkt_match = re.search(r'(\d+) packets transmitted, (\d+) received.*?(\d+)% packet loss', output)
        sent = int(pkt_match.group(1)) if pkt_match else count
        received = int(pkt_match.group(2)) if pkt_match else 0
        loss_pct = int(pkt_match.group(3)) if pkt_match else 100

        rtt_match = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)', output)
        rtt_min = float(rtt_match.group(1)) if rtt_match else None
        rtt_avg = float(rtt_match.group(2)) if rtt_match else None
        rtt_max = float(rtt_match.group(3)) if rtt_match else None

        alive = received > 0
        return {
            "target": target,
            "alive": alive,
            "packets_sent": sent,
            "packets_received": received,
            "packet_loss_pct": loss_pct,
            "rtt_min_ms": rtt_min,
            "rtt_avg_ms": rtt_avg,
            "rtt_max_ms": rtt_max,
            "raw": output.strip()
        }
    except subprocess.TimeoutExpired:
        return {"target": target, "alive": False, "error": "Timeout: el host no responde en 15 segundos"}
    except Exception as e:
        return {"target": target, "alive": False, "error": str(e)}

PROXMOX_HOST = os.getenv('PROXMOX_HOST', '192.168.1.10')
PROXMOX_USER = 'root@pam'
PROXMOX_PASS = os.getenv("PROXMOX_PASSWORD", "your_proxmox_password")

def find_matching_proxmox_vm(vms, target):
    if not target or not vms:
        return None
    target_str = str(target).strip()
    target_lower = target_str.lower()
    target_num = re.sub(r'[^0-9]', '', target_str)

    # 1. Exact VMID match (Highest Priority)
    if target_num:
        for v in vms:
            if str(v.get('vmid', '')) == target_num:
                return v

    # 2. Exact Name match (case-insensitive)
    for v in vms:
        if str(v.get('name', '')).lower() == target_lower:
            return v

    # 3. Clean Name exact match (alphanumeric only)
    clean_target = re.sub(r'[^a-zA-Z0-9]', '', target_lower)
    if clean_target:
        for v in vms:
            clean_name = re.sub(r'[^a-zA-Z0-9]', '', str(v.get('name', '')).lower())
            if clean_name == clean_target:
                return v

    # 4. Substring Name match (ONLY if target is not purely numeric)
    # Never allow numeric ID (like 202) to match numbers inside a name (like ControladorWIFI.2024)!
    if not target_str.isdigit() and len(target_lower) >= 3:
        for v in vms:
            v_name = str(v.get('name', '')).lower()
            if target_lower in v_name:
                return v
            clean_vname = re.sub(r'[^a-zA-Z0-9]', '', v_name)
            if clean_target and clean_target in clean_vname:
                return v

    # 5. Whole word match in name for numeric target
    if target_num:
        word_re = re.compile(rf'\b{re.escape(target_num)}\b')
        for v in vms:
            if word_re.search(str(v.get('name', ''))):
                return v

    return None

def get_proxmox_health(target=''):
    try:
        ctx = ssl._create_unverified_context()
        login_data = urllib.parse.urlencode({'username': PROXMOX_USER, 'password': PROXMOX_PASS}).encode()
        login_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/access/ticket', data=login_data)
        with urllib.request.urlopen(login_req, context=ctx, timeout=8) as r:
            auth_res = json.loads(r.read().decode('utf-8'))
        ticket = auth_res['data']['ticket']
        headers = {'Cookie': f'PVEAuthCookie={ticket}'}

        res_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/cluster/resources', headers=headers)
        with urllib.request.urlopen(res_req, context=ctx, timeout=8) as r:
            all_res = json.loads(r.read().decode('utf-8'))['data']
        try:
            c_login = urllib.request.Request('https://192.168.1.80:8006/api2/json/access/ticket', data=login_data)
            with urllib.request.urlopen(c_login, context=ctx, timeout=5) as cr:
                c_ticket = json.loads(cr.read().decode('utf-8'))['data']['ticket']
            c_res_req = urllib.request.Request('https://192.168.1.80:8006/api2/json/cluster/resources', headers={'Cookie': f'PVEAuthCookie={c_ticket}'})
            with urllib.request.urlopen(c_res_req, context=ctx, timeout=5) as cr2:
                for item in json.loads(cr2.read().decode('utf-8')).get('data', []):
                    item['_pve_host'] = '192.168.1.80'
                    all_res.append(item)
        except Exception:
            pass

        nodes = [item for item in all_res if item.get('type') == 'node']
        vms = [item for item in all_res if item.get('type') in ['qemu', 'lxc']]

        target_clean = str(target or '').strip().lower()
        if target_clean and target_clean not in ['all', 'cluster', 'proxmox', 'general']:
            v = find_matching_proxmox_vm(vms, target)
            if v:
                cpu_pct = round(v.get('cpu', 0.0) * 100, 2)
                mem_used_mb = round(v.get('mem', 0) / (1024 * 1024), 1)
                mem_max_mb = round(v.get('maxmem', 1) / (1024 * 1024), 1)
                mem_pct = round((v.get('mem', 0) / v.get('maxmem', 1)) * 100, 2)
                uptime_sec = v.get('uptime', 0)
                uptime_str = f"{uptime_sec // 86400}d {(uptime_sec % 86400) // 3600}h" if uptime_sec else "0h"
                return {
                    "tipo": "Virtual Machine (Proxmox)",
                    "vmid": v.get('vmid'),
                    "nombre": v.get('name'),
                    "nodo": v.get('node'),
                    "estado": v.get('status', 'unknown').upper(),
                    "cpu_uso": f"{cpu_pct}%",
                    "memoria_uso": f"{mem_used_mb} MB / {mem_max_mb} MB ({mem_pct}%)",
                    "uptime": uptime_str,
                    "diagnostico": "Saludable y Operativo" if v.get('status') == 'running' and cpu_pct < 90 else "Atención Requerida"
                }

        total_nodes = len(nodes)
        online_nodes = sum(1 for n in nodes if n.get('status') == 'online')
        total_vms = len(vms)
        running_vms = sum(1 for v in vms if v.get('status') == 'running')
        return {
            "tipo": "Cluster Proxmox VE",
            "nodos_online": f"{online_nodes}/{total_nodes}",
            "vms_running": f"{running_vms}/{total_vms}",
            "estado_general": "OK (Cluster Operativo)" if online_nodes == total_nodes else "DEGRADADO"
        }
    except Exception as e:
        return {"tipo": "Proxmox", "error": f"Fallo al conectar con Proxmox API: {str(e)}"}


def handle_proxmox_vm_action(payload):
    if isinstance(payload, str) and payload.strip().startswith('{') and payload.strip().endswith('}'):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    raw_t = payload.get('target', '') or payload.get('vm_id', '') or payload.get('vmid', '') or payload.get('id', '') or payload.get('vm', '')
    if isinstance(raw_t, str) and raw_t.strip().startswith('{') and raw_t.strip().endswith('}'):
        try:
            td = json.loads(raw_t)
            raw_t = td.get('target') or td.get('vm_id') or td.get('vmid') or td.get('id') or td.get('vm') or raw_t
        except Exception:
            pass
    target = str(raw_t).strip().lower()
    raw_action = str(payload.get('action', '')).strip().lower()
    phase = str(payload.get('phase', 'check')).strip().lower()
    target_node = str(payload.get('target_node', '') or payload.get('node', '')).strip()
    clone_name = str(payload.get('new_name', '') or payload.get('name', '')).strip()

    action_map = {
        'start': 'start', 'levantar': 'start', 'encender': 'start', 'iniciar': 'start',
        'shutdown': 'shutdown', 'apagar': 'shutdown', 'apagar_suave': 'shutdown',
        'stop': 'stop', 'parar': 'stop', 'detener': 'stop', 'forzar_apagado': 'stop',
        'reboot': 'reboot', 'reiniciar': 'reboot',
        'reset': 'reset', 'reinicio_forzado': 'reset',
        'pause': 'pause', 'pausar': 'pause',
        'hibernate': 'hibernate', 'hibernar': 'hibernate',
        'resume': 'resume', 'reanudar': 'resume', 'continuar': 'resume',
        'migrate': 'migrate', 'migrar': 'migrate',
        'clone': 'clone', 'clonar': 'clone',
        'console': 'console', 'consola': 'console',
        'status': 'status', 'estado': 'status', 'info': 'status', 'detalles': 'status', 'consultar': 'status', 'ver': 'status'
    }
    action = action_map.get(raw_action)
    if not action:
        return {
            "ok": False, "procede": False,
            "error": f"Accion '{raw_action}' no valida. Acciones disponibles: levantar (start), apagar (shutdown), detener (stop forzado), reiniciar (reboot), reinicio forzado (reset), pausar (pause), hibernar (hibernate), reanudar (resume), migrar (migrate), clonar (clone), consola (console)."
        }

    try:
        ctx = ssl._create_unverified_context()
        login_data = urllib.parse.urlencode({'username': PROXMOX_USER, 'password': PROXMOX_PASS}).encode()
        login_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/access/ticket', data=login_data)
        with urllib.request.urlopen(login_req, context=ctx, timeout=8) as r:
            auth_res = json.loads(r.read().decode('utf-8'))['data']
        ticket = auth_res['ticket']
        csrf_token = auth_res.get('CSRFPreventionToken', '')
        headers = {'Cookie': f'PVEAuthCookie={ticket}'}

        # 1. Obtener recursos del clúster
        res_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/cluster/resources', headers=headers)
        with urllib.request.urlopen(res_req, context=ctx, timeout=8) as r:
            all_res = json.loads(r.read().decode('utf-8'))['data']

        vms = [item for item in all_res if item.get('type') in ['qemu', 'lxc']]
        nodes = [item for item in all_res if item.get('type') == 'node']
        matched_vm = find_matching_proxmox_vm(vms, target)

        if not matched_vm:
            return {"ok": False, "procede": False, "error": f"No se encontro ninguna VM o contenedor con identificador o nombre '{target}' en Proxmox."}

        vmid = matched_vm.get('vmid')
        name = matched_vm.get('name')
        node = matched_vm.get('node')
        vtype = matched_vm.get('type')
        status = matched_vm.get('status', 'unknown').lower()

        action_labels = {
            'start': 'LEVANTAR (start)',
            'shutdown': 'APAGADO SEGURO (shutdown ACPI)',
            'stop': 'DETENCIÓN DEFINITIVA (stop forzado)',
            'reboot': 'REINICIAR (reboot)',
            'reset': 'REINICIO FORZADO (reset)',
            'pause': 'PAUSAR (pause)',
            'hibernate': 'HIBERNAR (suspend a disco)',
            'resume': 'REANUDAR (resume)',
            'migrate': 'MIGRAR (migrate)',
            'clone': 'CLONAR (clone)',
            'console': 'ACCESO A CONSOLA (NoVNC)',
            'status': 'CONSULTAR ESTADO / INFORMACIÓN (status)'
        }
        action_es = action_labels.get(action, action.upper())

        # Consulta directa de estado o detalles (no altera la máquina)
        if action == 'status':
            cpu_pct = round(matched_vm.get('cpu', 0) * 100, 1)
            mem_mb = round(matched_vm.get('mem', 0) / (1024*1024), 1)
            maxmem_mb = round(matched_vm.get('maxmem', 0) / (1024*1024), 1)
            uptime_s = int(matched_vm.get('uptime', 0))
            hours = uptime_s // 3600
            mins = (uptime_s % 3600) // 60
            uptime_str = f"{hours}h {mins}m" if uptime_s > 0 else "0m"
            return {
                "ok": True,
                "fase": "status",
                "procede": False,
                "vmid": vmid,
                "nombre": name,
                "nodo": node,
                "tipo": "Maquina Virtual (QEMU)" if vtype == 'qemu' else "Contenedor (LXC)",
                "estado_actual": status.upper(),
                "cpu": f"{cpu_pct}%",
                "memoria": f"{mem_mb} MB / {maxmem_mb} MB",
                "uptime": uptime_str,
                "mensaje": f"VM {vmid} ({name}) en nodo {node}: Estado {status.upper()}, CPU {cpu_pct}%, Memoria {mem_mb} MB / {maxmem_mb} MB, Uptime {uptime_str}."
            }

        # Consola directa NoVNC (no altera la máquina)
        if action == 'console':
            console_url = f"https://{PROXMOX_HOST}:8006/?console=kvm&novnc=1&vmid={vmid}&vmname={urllib.parse.quote(name)}&node={node}"
            return {
                "ok": True,
                "fase": "console",
                "vmid": vmid,
                "nombre": name,
                "nodo": node,
                "tipo": "Maquina Virtual (QEMU)" if vtype == 'qemu' else "Contenedor (LXC)",
                "estado_actual": status.upper(),
                "consola_url": console_url,
                "mensaje": f"Consola NoVNC disponible en: {console_url}"
            }

        # Validaciones de coherencia operativa en fase check
        if phase == 'check':
            if action == 'start' and status == 'running':
                return {
                    "ok": True, "procede": False,
                    "mensaje": f"La VM {vmid} ({name}) en el nodo {node} ya se encuentra ENCENDIDA (running). No requiere ser levantada.",
                    "vmid": vmid, "nombre": name, "nodo": node, "estado_actual": status.upper()
                }
            if action in ['shutdown', 'stop', 'reboot', 'reset', 'pause', 'hibernate'] and status != 'running':
                return {
                    "ok": True, "procede": False,
                    "mensaje": f"La VM {vmid} ({name}) en el nodo {node} actualmente está {status.upper()}. No se requiere {action_es}.",
                    "vmid": vmid, "nombre": name, "nodo": node, "estado_actual": status.upper()
                }
            if action == 'resume' and status not in ['paused', 'suspended']:
                return {
                    "ok": True, "procede": False,
                    "mensaje": f"La VM {vmid} ({name}) en el nodo {node} está en estado {status.upper()}. Solo se puede reanudar si está pausada o hibernada.",
                    "vmid": vmid, "nombre": name, "nodo": node, "estado_actual": status.upper()
                }

        # Manejo de destino para migración
        if action == 'migrate':
            online_other_nodes = [(n.get('node') or n.get('name')) for n in nodes if (n.get('node') or n.get('name')) != node and n.get('status') == 'online']
            if not target_node:
                target_node = online_other_nodes[0] if online_other_nodes else ''
            if not target_node:
                return {"ok": False, "procede": False, "error": f"No hay otro nodo online en el cluster para migrar desde {node}."}

        # Manejo de nombre para clonación
        if action == 'clone' and not clone_name:
            clone_name = f"{name}-clone"

        # Fase 1: Verificacion (check)
        if phase == 'check':
            detalles_extra = {}
            if action == 'migrate':
                detalles_extra['nodo_origen'] = node
                detalles_extra['nodo_destino'] = target_node
            elif action == 'clone':
                # Consultar nextid
                next_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/cluster/nextid', headers=headers)
                with urllib.request.urlopen(next_req, context=ctx, timeout=8) as r:
                    new_vmid = json.loads(r.read().decode('utf-8'))['data']
                detalles_extra['nuevo_vmid'] = new_vmid
                detalles_extra['nuevo_nombre'] = clone_name

            return {
                "ok": True,
                "procede": True,
                "fase": "check",
                "vmid": vmid,
                "nombre": name,
                "nodo": node,
                "tipo": "Maquina Virtual (QEMU)" if vtype == 'qemu' else "Contenedor (LXC)",
                "estado_actual": status.upper(),
                "accion_solicitada": action_es,
                "detalles_extra": detalles_extra,
                "requiere_confirmacion": True
            }

        # Fase 2: Ejecución Definitiva (execute)
        if phase == 'execute':
            post_headers = {
                'Cookie': f'PVEAuthCookie={ticket}',
                'CSRFPreventionToken': csrf_token
            }

            post_data = b''
            action_url = ''

            if action == 'start':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/start'
            elif action == 'shutdown':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/shutdown'
            elif action == 'stop':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/stop'
                post_data = urllib.parse.urlencode({'overrule_shutdown': '1', 'skiplock': '1'}).encode()
            elif action == 'reboot':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/reboot'
            elif action == 'reset':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/reset'
                post_data = urllib.parse.urlencode({'skiplock': '1'}).encode()
            elif action == 'pause':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/suspend'
            elif action == 'hibernate':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/suspend'
                post_data = urllib.parse.urlencode({'todisk': '1'}).encode()
            elif action == 'resume':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/resume'
            elif action == 'migrate':
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/migrate'
                post_data = urllib.parse.urlencode({'target': target_node, 'online': '1'}).encode()
            elif action == 'clone':
                next_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/cluster/nextid', headers=headers)
                with urllib.request.urlopen(next_req, context=ctx, timeout=8) as r:
                    new_vmid = json.loads(r.read().decode('utf-8'))['data']
                action_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/clone'
                post_data = urllib.parse.urlencode({'newid': str(new_vmid), 'name': clone_name, 'full': '1'}).encode()

            act_req = urllib.request.Request(action_url, data=post_data, headers=post_headers, method='POST')
            with urllib.request.urlopen(act_req, context=ctx, timeout=15) as r:
                act_res = json.loads(r.read().decode('utf-8'))
            upid = act_res.get('data')

            time.sleep(3)

            # Para stop, verificar y remover residuo de cleanup si existiera
            if action == 'stop':
                try:
                    subprocess.run(f"sshpass -p '{PROXMOX_PASS}' ssh -o StrictHostKeyChecking=no root@{node.lower()} 'rm -f /var/run/qemu-server/{vmid}.cleanup' 2>/dev/null", shell=True, timeout=3)
                except Exception:
                    pass

            # Consultar nuevo estado
            status_url = f'https://{PROXMOX_HOST}:8006/api2/json/nodes/{node}/{vtype}/{vmid}/status/current'
            new_status = 'PROCESANDO'
            try:
                st_req = urllib.request.Request(status_url, headers=headers)
                with urllib.request.urlopen(st_req, context=ctx, timeout=8) as r:
                    cur_res = json.loads(r.read().decode('utf-8'))['data']
                new_status = cur_res.get('status', 'unknown').upper()
            except Exception:
                pass

            # Notificar en TV del NOC
            event_hub.publish({
                "tipo": "alerta",
                "herramienta": "proxmox",
                "usuario": "Operador NOC",
                "pregunta": f"Orden {action_es} VM {vmid}",
                "detalle": f"VM {vmid} ({name}) en nodo {node}: accion {action_es} ejecutada.",
                "respuesta": f"Estado verificado: {new_status}"
            })

            res_obj = {
                "ok": True,
                "fase": "execute",
                "ejecutado": True,
                "vmid": vmid,
                "nombre": name,
                "nodo": node,
                "tipo": "Maquina Virtual (QEMU)" if vtype == 'qemu' else "Contenedor (LXC)",
                "accion": action_es,
                "estado_anterior": status.upper(),
                "estado_nuevo": new_status,
                "upid": upid
            }
            if action == 'migrate':
                res_obj['nodo_destino'] = target_node
            elif action == 'clone':
                res_obj['nuevo_vmid'] = new_vmid
                res_obj['nuevo_nombre'] = clone_name

            return res_obj

        return {"ok": False, "error": f"Fase '{phase}' desconocida."}
    except Exception as e:
        return {"ok": False, "error": f"Fallo al procesar operacion en Proxmox API: {str(e)}"}

def check_device_health(target):
    target = str(target or '').strip()
    if not target:
        return {"error": "Target requerido para diagnóstico"}
    if target.startswith('{') and target.endswith('}'):
        try:
            td = json.loads(target)
            target = str(td.get('target') or td.get('vm_id') or td.get('vmid') or td.get('id') or td.get('vm') or td.get('host') or target).strip()
        except Exception:
            pass
    target_lower = target.lower()
    target_num = re.sub(r'[^0-9]', '', target_lower)
    is_vm = 'pmx' in target_lower or 'proxmox' in target_lower or 'vm' in target_lower or (target.isdigit() and len(target) <= 4) or (target_num and len(target_num) <= 4 and not '.' in target)
    if is_vm:
        pmx_data = get_proxmox_health(target)
        if 'error' not in pmx_data and pmx_data.get('tipo') != 'Cluster Proxmox VE':
            return pmx_data

    srv_key, z_hid, z_name, z_ip = find_host_across_zabbix(target)
    ping_target = z_ip if z_ip else target
    ping_res = run_linux_ping(ping_target, count=3)
    z_label = f"Monitoreado en {ZABBIX_SERVERS_META[srv_key]['name']} (Host: {z_name}, IP: {z_ip})" if z_hid else "No registrado en Zabbix (Ping directo)"
    return {
        "tipo": "Dispositivo de Red",
        "target": z_name if z_hid else target,
        "ip": ping_target,
        "conectividad": "ONLINE (Responde)" if ping_res.get('alive') else "OFFLINE (Sin respuesta)",
        "latencia": f"{ping_res.get('rtt_avg_ms', 'N/D')} ms" if ping_res.get('rtt_avg_ms') else "N/D",
        "paquetes_perdidos": f"{ping_res.get('packet_loss_pct', 100)}%",
        "monitoreo_zabbix": z_label
    }


# --- ZABBIX 20 REALTIME COLLECTOR ---
ZABBIX20_URL = os.getenv('ZABBIX20_URL', 'http://192.168.1.20/zabbix/api_jsonrpc.php')
ZABBIX20_USER = 'Admin'
ZABBIX20_PASS = 'zabbix'
N8N_WEBHOOK_ZABBIX = 'http://127.0.0.1:5678/webhook/zabbix-alert'

class Zabbix20Collector(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.auth_token = None
        self.token_time = 0
        self.known_active = set()
        self.known_details = {}
        self.initialized = False

    def get_token(self):
        now = time.time()
        if self.auth_token and (now - self.token_time < 3600):
            return self.auth_token
        try:
            req_data = json.dumps({
                "jsonrpc": "2.0",
                "method": "user.login",
                "params": {"user": ZABBIX20_USER, "password": ZABBIX20_PASS},
                "id": 1
            }).encode('utf-8')
            req = urllib.request.Request(ZABBIX20_URL, data=req_data, headers={'Content-Type': 'application/json-rpc'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                self.auth_token = res.get('result')
                self.token_time = now
                return self.auth_token
        except Exception as e:
            print(f"[Zabbix20] Login error: {e}", flush=True)
            return None

    def api_call(self, method, params):
        token = self.get_token()
        if not token:
            return None
        try:
            req_data = json.dumps({
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "auth": token,
                "id": 2
            }).encode('utf-8')
            req = urllib.request.Request(ZABBIX20_URL, data=req_data, headers={'Content-Type': 'application/json-rpc'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                if 'error' in res:
                    print(f"[Zabbix20] API error in {method}: {res['error']}", flush=True)
                    self.auth_token = None
                    return None
                return res.get('result', [])
        except Exception as e:
            print(f"[Zabbix20] API exception in {method}: {e}", flush=True)
            return None

    def post_webhook(self, payload):
        try:
            req_data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(N8N_WEBHOOK_ZABBIX, data=req_data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"[Zabbix20 -> n8n] Sent {payload.get('status')} for {payload.get('host')} (Status: {resp.status})", flush=True)
        except Exception as e:
            print(f"[Zabbix20 -> n8n] Failed to post webhook: {e}", flush=True)

    def run(self):
        print("[Zabbix20] Collector thread started. Polling every 30s...", flush=True)
        time.sleep(5)
        while True:
            try:
                self.poll_once()
            except Exception as e:
                print(f"[Zabbix20] Unexpected error in poller loop: {e}", flush=True)
            time.sleep(30)

    def poll_once(self):
        probs = self.api_call("problem.get", {
            "output": ["eventid", "objectid", "name", "severity", "clock", "r_eventid"],
            "severities": [4, 5],
            "suppressed": False,
            "recent": False,
            "sortfield": ["eventid"],
            "sortorder": "DESC"
        })
        if probs is None:
            return

        current_active = {p['eventid'] for p in probs}

        if not self.initialized:
            self.known_active = current_active
            for p in probs:
                self.known_details[p['eventid']] = {
                    "name": p.get('name', 'Problema Zabbix'),
                    "severity": "Disaster" if str(p.get('severity')) == '5' else "High",
                    "severity_num": p.get('severity', 4),
                    "clock": p.get('clock', int(time.time()))
                }
            self.initialized = True
            print(f"[Zabbix20] Baseline established with {len(self.known_active)} active problems (suppressed retroactive spam).", flush=True)
            return

        new_ids = list(current_active - self.known_active)
        if new_ids:
            events = self.api_call("event.get", {
                "eventids": new_ids,
                "output": ["eventid", "name", "severity", "clock"],
                "selectHosts": ["hostid", "host", "name"]
            }) or []
            ev_map = {ev['eventid']: ev for ev in events}

            for nid in new_ids:
                ev = ev_map.get(nid, {})
                hosts = ev.get('hosts', [])
                host_name = hosts[0].get('name') or hosts[0].get('host') if hosts else 'Desconocido'
                prob_name = ev.get('name') or 'Alerta Zabbix'
                sev_num = ev.get('severity', 4)
                sev_label = "Disaster" if str(sev_num) == '5' else "High"
                clock = int(ev.get('clock', time.time()))
                dt = datetime.fromtimestamp(clock)

                self.known_details[nid] = {
                    "host": host_name,
                    "name": prob_name,
                    "severity": sev_label,
                    "severity_num": sev_num,
                    "clock": clock
                }
                self.known_active.add(nid)

                payload = {
                    "source": "Zabbix20",
                    "host": host_name,
                    "status": "PROBLEM",
                    "severity": sev_label,
                    "severity_num": sev_num,
                    "problem": prob_name,
                    "event_date": dt.strftime('%Y-%m-%d'),
                    "event_time": dt.strftime('%H:%M:%S'),
                    "eventid": nid
                }
                print(f"[Zabbix20] Discovered NEW problem: {host_name} - {prob_name}", flush=True)
                alerts_memory.record(payload)
                self.post_webhook(payload)

        resolved_ids = list(self.known_active - current_active)
        for rid in resolved_ids:
            details = self.known_details.get(rid, {})
            host_name = details.get('host', 'Desconocido')
            prob_name = details.get('name', 'Alerta Zabbix')
            sev_label = details.get('severity', 'High')
            now_dt = datetime.now()

            payload = {
                "source": "Zabbix20",
                "host": host_name,
                "status": "RESOLVED",
                "severity": sev_label,
                "problem": prob_name,
                "event_date": now_dt.strftime('%Y-%m-%d'),
                "event_time": now_dt.strftime('%H:%M:%S'),
                "eventid": rid
            }
            print(f"[Zabbix20] Discovered RESOLVED problem: {host_name} - {prob_name}", flush=True)
            self.post_webhook(payload)

            self.known_active.discard(rid)
            self.known_details.pop(rid, None)

zabbix20_collector = Zabbix20Collector()

class Zabbix74Collector(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.known_active = set()
        self.known_details = {}
        self.initialized = False

    def post_webhook(self, payload):
        try:
            req_data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(N8N_WEBHOOK_ZABBIX, data=req_data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"[Zabbix74 -> n8n] Sent {payload.get('status')} for {payload.get('host')} (Status: {resp.status})", flush=True)
        except Exception as e:
            print(f"[Zabbix74 -> n8n] Failed to post webhook: {e}", flush=True)

    def run(self):
        print("[ZabbixDist] Collector thread started (192.168.1.27). Polling every 30s...", flush=True)
        time.sleep(6)
        while True:
            try:
                self.poll_once()
            except Exception as e:
                print(f"[Zabbix74] Unexpected error in poller loop: {e}", flush=True)
            time.sleep(30)

    def poll_once(self):
        probs = zabbix_query("problem.get", {
            "output": ["eventid", "objectid", "name", "severity", "clock", "r_eventid"],
            "severities": [3, 4, 5],
            "suppressed": False,
            "recent": False,
            "sortfield": ["eventid"],
            "sortorder": "DESC"
        }, srv_key='zbx74')
        if probs is None:
            return

        current_active = {p['eventid'] for p in probs}

        if not self.initialized:
            self.known_active = current_active
            for p in probs:
                self.known_details[p['eventid']] = {
                    "name": p.get('name', 'Problema Zabbix74'),
                    "severity": "Disaster" if str(p.get('severity')) == '5' else ("High" if str(p.get('severity')) == '4' else "Average"),
                    "severity_num": p.get('severity', 4),
                    "clock": p.get('clock', int(time.time()))
                }
            self.initialized = True
            print(f"[Zabbix74] Baseline established with {len(self.known_active)} active problems.", flush=True)
            return

        new_ids = list(current_active - self.known_active)
        if new_ids:
            events = zabbix_query("event.get", {
                "eventids": new_ids,
                "output": ["eventid", "name", "severity", "clock"],
                "selectHosts": ["hostid", "host", "name"]
            }, srv_key='zbx74') or []
            ev_map = {ev['eventid']: ev for ev in events}

            for nid in new_ids:
                ev = ev_map.get(nid, {})
                hosts = ev.get('hosts', [])
                host_name = hosts[0].get('name') or hosts[0].get('host') if hosts else 'Desconocido (Zabbix74)'
                prob_name = ev.get('name') or 'Alerta Zabbix74'
                sev_num = ev.get('severity', 4)
                sev_label = "Disaster" if str(sev_num) == '5' else ("High" if str(sev_num) == '4' else "Average")
                clock = int(ev.get('clock', time.time()))
                dt = datetime.fromtimestamp(clock)

                self.known_details[nid] = {
                    "host": host_name,
                    "name": prob_name,
                    "severity": sev_label,
                    "severity_num": sev_num,
                    "clock": clock
                }
                self.known_active.add(nid)

                payload = {
                    "source": "Zabbix74",
                    "host": host_name,
                    "status": "PROBLEM",
                    "severity": sev_label,
                    "severity_num": sev_num,
                    "problem": prob_name,
                    "event_date": dt.strftime('%Y-%m-%d'),
                    "event_time": dt.strftime('%H:%M:%S'),
                    "eventid": nid
                }
                print(f"[Zabbix74] Discovered NEW problem: {host_name} - {prob_name}", flush=True)
                alerts_memory.record(payload)
                self.post_webhook(payload)

        resolved_ids = list(self.known_active - current_active)
        for rid in resolved_ids:
            details = self.known_details.get(rid, {})
            host_name = details.get('host', 'Desconocido (Zabbix74)')
            prob_name = details.get('name', 'Alerta Zabbix74')
            sev_label = details.get('severity', 'High')
            now_dt = datetime.now()

            payload = {
                "source": "Zabbix74",
                "host": host_name,
                "status": "RESOLVED",
                "severity": sev_label,
                "problem": prob_name,
                "event_date": now_dt.strftime('%Y-%m-%d'),
                "event_time": now_dt.strftime('%H:%M:%S'),
                "eventid": rid
            }
            print(f"[Zabbix74] Discovered RESOLVED problem: {host_name} - {prob_name}", flush=True)
            self.post_webhook(payload)

            self.known_active.discard(rid)
            self.known_details.pop(rid, None)

zabbix74_collector = Zabbix74Collector()

def handle_unified_zabbix_problems(limit=15):
    recent = alerts_memory.get_recent(minutes=6)
    all_probs = []
    sev_map = {'0': 'No clasificado', '1': 'Información', '2': 'Advertencia', '3': 'Promedio', '4': 'Alta', '5': 'Desastre'}
    for srv_key in ('zbx74', 'zbx35', 'zbx20'):
        srv_short = ZABBIX_SERVERS_META[srv_key]['short']
        per_lim = limit if srv_key != 'zbx20' else min(5, limit)
        probs = zabbix_query('problem.get', {
            'output': ['eventid', 'name', 'severity', 'clock', 'objectid'],
            'sortfield': ['eventid'],
            'sortorder': 'DESC',
            'limit': per_lim
        }, srv_key=srv_key)
        if probs:
            trig_ids = [p['objectid'] for p in probs if p.get('objectid') and p['objectid'] != '0']
            host_map = {}
            if trig_ids:
                trigs = zabbix_query('trigger.get', {
                    'output': ['triggerid'],
                    'selectHosts': ['hostid', 'host', 'name'],
                    'triggerids': trig_ids
                }, srv_key=srv_key)
                for t in trigs:
                    if t.get('hosts') and t['hosts']:
                        host_map[t['triggerid']] = t['hosts'][0].get('name') or t['hosts'][0].get('host')
            for p in probs[:per_lim]:
                all_probs.append({
                    'origen': srv_short,
                    'equipo': host_map.get(p.get('objectid'), f'Equipo ({srv_short})'),
                    'problema': p.get('name', 'Alerta'),
                    'severidad': sev_map.get(str(p.get('severity', '0')), str(p.get('severity'))),
                    'hora': datetime.fromtimestamp(int(p.get('clock', time.time()))).strftime('%H:%M')
                })
    return {
        'servidores_monitoreados': ['Zabbix Core (192.168.1.35)', 'Zabbix Dist (192.168.1.27)', 'Zabbix Legacy (192.168.1.20)'],
        'alertas_recientes_ultimos_6_min': [
            {
                'origen': a['source'],
                'equipo': a['host'],
                'problema': a['problem'],
                'severidad': a['severity'],
                'estado': a['status'],
                'hora_evento': a['hora'],
                'hace_segundos': a['hace_segundos']
            } for a in recent
        ],
        'problemas_activos_zabbix': all_probs[:limit*2]
    }

def handle_unified_zabbix_history(limit=15):
    sev_map = {'0': 'No clasificado', '1': 'Info', '2': 'Advertencia', '3': 'Promedio', '4': 'Alta', '5': 'Desastre'}
    merged = []
    for srv_key in ('zbx35', 'zbx74', 'zbx20'):
        srv_short = ZABBIX_SERVERS_META[srv_key]['short']
        evs = zabbix_query('event.get', {
            'output': ['eventid', 'name', 'severity', 'clock', 'value'],
            'selectHosts': ['hostid', 'host', 'name'],
            'source': 0,
            'object': 0,
            'sortfield': ['clock'],
            'sortorder': 'DESC',
            'limit': min(limit, 10)
        }, srv_key=srv_key)
        for e in (evs or []):
            merged.append({
                '_clock': int(e.get('clock', 0)),
                'origen': srv_short,
                'equipo': (e.get('hosts') and e['hosts'][0]) and (e['hosts'][0].get('name') or e['hosts'][0].get('host')) or f'Equipo ({srv_short})',
                'evento': e.get('name', 'Evento'),
                'estado': 'GENERADO (Problema)' if str(e.get('value')) == '1' else 'RECUPERADO (OK)',
                'severidad': sev_map.get(str(e.get('severity', '0')), str(e.get('severity'))),
                'hora': datetime.fromtimestamp(int(e.get('clock', time.time()))).strftime('%H:%M')
            })
    merged.sort(key=lambda x: x['_clock'], reverse=True)
    for m in merged:
        m.pop('_clock', None)
    return merged[:limit]

def handle_unified_zabbix_search(query):
    q = str(query or '').strip()
    if not q:
        return {"error": "Proporciona un nombre o IP para buscar."}
    ql = q.lower()
    # Special case: asking for a whole Zabbix instance like zabbix74, zabbix20, zabbix35
    target_srvs = ('zbx35', 'zbx74', 'zbx20')
    list_all_on_srv = False
    if ql in ('zabbix74', 'zabbix 74', 'zabbixdist', '192.168.1.27'):
        target_srvs = ('zbx74',)
        list_all_on_srv = True
    elif ql in ('zabbix35', 'zabbix 35', 'zabbixcore', '192.168.1.35'):
        target_srvs = ('zbx35',)
        list_all_on_srv = True
    elif ql in ('zabbix20', 'zabbix 20', 'zabbixlegacy', '192.168.1.20'):
        target_srvs = ('zbx20',)
        list_all_on_srv = True

    es_ip = all(part.isdigit() for part in q.split('.')) and len(q.split('.')) == 4 and not list_all_on_srv
    results = []
    for srv_key in target_srvs:
        srv_short = ZABBIX_SERVERS_META[srv_key]['short']
        hosts_found = []
        if list_all_on_srv:
            hosts_found = zabbix_query('host.get', {
                'output': ['hostid', 'host', 'name', 'status'],
                'selectInterfaces': ['ip', 'available'],
                'selectGroups': ['name'],
                'limit': 30
            }, srv_key=srv_key)
        elif es_ip:
            ifaces = zabbix_query('hostinterface.get', {'output': ['hostid', 'ip'], 'filter': {'ip': q}}, srv_key=srv_key)
            hids = [i['hostid'] for i in (ifaces or [])]
            if hids:
                hosts_found = zabbix_query('host.get', {
                    'output': ['hostid', 'host', 'name', 'status'],
                    'selectInterfaces': ['ip', 'available'],
                    'selectGroups': ['name'],
                    'hostids': hids
                }, srv_key=srv_key)
        else:
            hosts_found = zabbix_query('host.get', {
                'output': ['hostid', 'host', 'name', 'status'],
                'selectInterfaces': ['ip', 'available'],
                'selectGroups': ['name'],
                'search': {'name': q, 'host': q},
                'searchByAny': True,
                'limit': 15
            }, srv_key=srv_key)

        for h in (hosts_found or []):
            ifaces = h.get('interfaces') or []
            ip_val = ifaces[0].get('ip', 'Sin IP') if ifaces else 'Sin IP'
            avail = str(ifaces[0].get('available', '0')) if ifaces else str(h.get('available', '0'))
            st_str = 'Deshabilitado' if str(h.get('status')) == '1' else ('OK (Online)' if avail == '1' else ('FALLA (Offline)' if avail == '2' else 'Monitoreado'))
            grps = ', '.join([g.get('name','') for g in (h.get('groups') or [])[:2]])
            results.append({
                'servidor_zabbix': srv_short,
                'nombre': h.get('name') or h.get('host'),
                'ip': ip_val,
                'estado': st_str,
                'grupos': grps
            })
    return results

def handle_unified_zabbix_topology():
    summary = []
    hosts_sample = []
    for srv_key in ('zbx74', 'zbx35', 'zbx20'):
        meta = ZABBIX_SERVERS_META[srv_key]
        all_h = zabbix_query('host.get', {'output': ['hostid', 'host', 'name', 'status'], 'selectInterfaces': ['ip', 'available'], 'selectGroups': ['name']}, srv_key=srv_key)
        summary.append({
            'servidor': meta['name'],
            'ip': meta['ip'],
            'estado': 'ONLINE' if all_h else 'SIN RESPUESTA',
            'total_equipos': len(all_h)
        })
        sample_lim = 10 if srv_key == 'zbx74' else 25
        for h in all_h[:sample_lim]:
            ifaces = h.get('interfaces') or []
            hosts_sample.append({
                'servidor_zabbix': meta['short'],
                'equipo': h.get('name') or h.get('host'),
                'ip': ifaces[0].get('ip', 'Sin IP') if ifaces else 'Sin IP',
                'grupo': (h.get('groups') or [{}])[0].get('name', 'General') if h.get('groups') else 'General'
            })
    return {
        'resumen_servidores_zabbix': summary,
        'equipos_monitoreados': hosts_sample
    }



# --- ALERTS MEMORY & VOICE COORDINATOR (6-MIN SLIDING WINDOW & 18S AGGREGATOR) ---
import collections
import random

class AlertsMemory:
    def __init__(self, ttl_seconds=360):
        self.ttl = ttl_seconds
        self.lock = threading.Lock()
        self.alerts = []

    def record(self, alert):
        now = time.time()
        with self.lock:
            self.alerts = [a for a in self.alerts if (now - a.get('timestamp', 0)) <= self.ttl]
            h = str(alert.get('host', '') or alert.get('device', '') or alert.get('name', '')).strip()
            p = str(alert.get('problem', '') or alert.get('mensaje', '') or 'Alerta de red').strip()
            s = str(alert.get('severity', '') or alert.get('severidad', '') or 'Alta').strip()
            src = str(alert.get('source', '') or alert.get('origen', '') or 'NOC').strip()
            st = str(alert.get('status', 'PROBLEM')).strip().upper()
            if not h:
                return

            key = f"{src}:{h}:{p}"
            existing = next((a for a in self.alerts if a.get('key') == key), None)
            entry = {
                'key': key,
                'source': src,
                'host': h,
                'problem': p,
                'severity': s,
                'status': st,
                'timestamp': now,
                'time_str': datetime.now().strftime('%H:%M:%S')
            }
            if existing:
                existing.update(entry)
            else:
                self.alerts.append(entry)

    def get_recent(self, minutes=6):
        ttl = minutes * 60
        now = time.time()
        with self.lock:
            self.alerts = [a for a in self.alerts if (now - a.get('timestamp', 0)) <= self.ttl]
            return [
                {
                    'source': a['source'],
                    'host': a['host'],
                    'problem': a['problem'],
                    'severity': a['severity'],
                    'status': a['status'],
                    'hora': a['time_str'],
                    'hace_segundos': int(now - a['timestamp'])
                }
                for a in sorted(self.alerts, key=lambda x: x['timestamp'], reverse=True)
                if (now - a['timestamp']) <= ttl
            ]

alerts_memory = AlertsMemory(ttl_seconds=360)


class VoiceAlertCoordinator:
    def __init__(self, buffer_seconds=18.0):
        self.buffer_seconds = buffer_seconds
        self.lock = threading.Lock()
        self.pending_alerts = []
        self.timer = None
        self.recent_greetings = collections.deque(maxlen=4)

    def enqueue_alert(self, alert_data):
        with self.lock:
            h = str(alert_data.get('host', '') or alert_data.get('device', '')).strip()
            p = str(alert_data.get('problem', '') or alert_data.get('mensaje', '') or 'Alerta de red').strip()
            s = str(alert_data.get('severity', '') or 'Alta').strip()
            src = str(alert_data.get('source', 'NOC')).strip()
            if not h:
                return

            if not any(a['host'] == h and a['problem'] == p for a in self.pending_alerts):
                self.pending_alerts.append({
                    'host': h,
                    'problem': p,
                    'severity': s,
                    'source': src,
                    'time': time.time()
                })
                print(f"[VoiceCoordinator] Enqueued alert for {h}. Total buffered: {len(self.pending_alerts)}", flush=True)

            if self.timer is None or not self.timer.is_alive():
                self.timer = threading.Timer(self.buffer_seconds, self._flush_and_speak)
                self.timer.daemon = True
                self.timer.start()

    def _get_greeting(self):
        candidate_greetings = [
            "Hola equipo...",
            "¿Cómo están, chicos?...",
            "Hola muchachos...",
            "Buenas a todos...",
            "¿Qué tal, equipo?...",
            "Dejando las formalidades de lado, les informo que...",
            "Directo al grano, muchachos...",
            "Atención un momento, chicos..."
        ]
        available = [g for g in candidate_greetings if g not in self.recent_greetings]
        if not available:
            available = candidate_greetings
        chosen = random.choice(available)
        self.recent_greetings.append(chosen)
        return chosen

    def _flush_and_speak(self):
        with self.lock:
            alerts = list(self.pending_alerts)
            self.pending_alerts.clear()
            self.timer = None

        if not alerts:
            return

        greeting = self._get_greeting()
        count = len(alerts)

        if count == 1:
            a = alerts[0]
            host = a['host']
            prob = a['problem']
            speech = f"{greeting} Se acaba de confirmar una caída en el equipo {host}... El reporte indica: {prob}... Por favor verifiquemos si responde a ping o si se trata de un corte de energía."
        elif count == 2:
            a1 = alerts[0]
            a2 = alerts[1]
            speech = f"{greeting} Tenemos dos incidencias confirmadas que requieren atención... Primero, en {a1['host']}, con {a1['problem']}... Y en paralelo, {a2['host']}, con {a2['problem']}... Conviene validar si comparten la misma ruta o enlace de transporte."
        else:
            host1 = alerts[0]['host']
            host2 = alerts[1]['host']
            speech = f"{greeting} Se están registrando múltiples caídas en simultáneo... Hay un total de {count} equipos afectados en la red, entre ellos {host1}, {host2} y otros dispositivos... Sugiero revisar de inmediato los enlaces troncales o posibles afectaciones masivas de energía."

        print(f"[VoiceCoordinator] Emitting voice to TV ({count} alerts): {speech}", flush=True)
        event_hub.publish({
            "tipo": "speak",
            "texto_voz": speech,
            "count": count
        })

voice_coordinator = VoiceAlertCoordinator(buffer_seconds=18.0)


def format_traffic_summary_response(raw_query, chat_id=None, period='24h', iface=None):
    res_inv = network_inv.resolve(raw_query)
    if not res_inv:
        last = network_inventory.chat_context.get_last(chat_id)
        if last and last.get('host'):
            res_inv = network_inv.resolve(last['host'])
            
    if not res_inv:
        return {'ok': False, 'error': f"No se encontró el equipo '{raw_query}' en la red monitoreada."}
        
    resolved = res_inv['resolved']
    target_host = resolved['name']
    srv_key = res_inv['srv_key']
    direction = res_inv['direction']
    
    network_inventory.chat_context.record(chat_id, resolved, action='traffic')
    
    ifaces_data = get_host_interfaces(target_host)
    ifaces_list = ifaces_data.get('interfaces', []) if isinstance(ifaces_data, dict) else []
    top_ifaces = [it for it in ifaces_list if (it.get('in_mbps', 0) + it.get('out_mbps', 0)) > 0][:7]
    
    chosen_iface = None
    if iface:
        for it in ifaces_list:
            if iface.lower() in it['name'].lower():
                chosen_iface = it; break
    if not chosen_iface and ifaces_list:
        if direction == 'outbound':
            by_tx = sorted(ifaces_list, key=lambda x: x.get('out_mbps', 0), reverse=True)
            chosen_iface = by_tx[0]
        else:
            chosen_iface = ifaces_list[0]
            
    chart_iface = chosen_iface['name'] if chosen_iface else (iface or 'General')
    chart_rx = chosen_iface.get('in_mbps', 0.0) if chosen_iface else 0.0
    chart_tx = chosen_iface.get('out_mbps', 0.0) if chosen_iface else 0.0
    
    dir_label = " (Tráfico saliendo / Outbound)" if direction == 'outbound' else (" (Tráfico entrando / Inbound)" if direction == 'inbound' else "")
    
    related_notes = []
    if 'nodoeast' in resolved.get('clean_compact', '') or 'nodo este' in raw_query.lower():
        related_notes.append("• <code>vlan100-core-trunk</code> en <b>CoreRouter-01</b>: ⬆️ ~665 Mbps (Tráfico hacia el nodo)")
    
    caption_lines = [
        f"📈 <b>Tráfico de Red — {target_host}</b>",
        f"🌐 <b>IP:</b> <code>{resolved['ip']}</code> · <b>Origen:</b> <code>{ZABBIX_SERVERS_META.get(srv_key, {}).get('short', srv_key)}</code>",
        f"🔌 <b>Interfaz graficada:</b> <code>{chart_iface}</code>{dir_label}",
        f"⬇️ <b>RX (Bajada):</b> <code>{chart_rx:.2f} Mbps</code> | ⬆️ <b>TX (Subida):</b> <code>{chart_tx:.2f} Mbps</code>\n",
        f"📊 <b>Consumo en Vivo por Interfaz:</b>"
    ]
    for it in top_ifaces:
        caption_lines.append(f"• <b>{it['name']}:</b> ⬇️ <code>{it['in_mbps']:.2f} Mbps</code> | ⬆️ <code>{it['out_mbps']:.2f} Mbps</code>")
        
    if related_notes:
        caption_lines.append("\n🔗 <b>Enlaces relacionados en la Red:</b>")
        for rn in related_notes:
            caption_lines.append(rn)
            
    caption_lines.append(f"\n📄 <i>Para ver todos los puertos: <code>/interfaces {target_host}</code></i>")
    
    caption = '\n'.join(caption_lines)
    return {
        'ok': True,
        'host': target_host,
        'ip': resolved['ip'],
        'srv_key': srv_key,
        'interface': chart_iface,
        'period': period,
        'direction': direction,
        'rx_mbps': chart_rx,
        'tx_mbps': chart_tx,
        'caption': caption
    }

def format_device_status_response(raw_query, chat_id=None):
    res_inv = network_inv.resolve(raw_query)
    if not res_inv:
        last = network_inventory.chat_context.get_last(chat_id)
        if last and last.get('host'):
            res_inv = network_inv.resolve(last['host'])
            
    if not res_inv:
        return {'ok': False, 'error': f"No se encontró el equipo '{raw_query}' en la red."}
        
    resolved = res_inv['resolved']
    target_host = resolved['name']
    srv_key = res_inv['srv_key']
    ip = resolved['ip']
    
    network_inventory.chat_context.record(chat_id, resolved, action='status')
    
    ping_res = run_linux_ping(ip if ip and ip != 'Sin IP' else target_host, count=3)
    is_alive = ping_res.get('alive', False)
    rtt = ping_res.get('rtt_avg_ms', 'N/D')
    loss = ping_res.get('packet_loss_pct', 0 if is_alive else 100)
    
    probs = zabbix_query('problem.get', {
        'hostids': [resolved['hostid']],
        'output': ['eventid', 'name', 'severity', 'clock'],
        'severities': [2, 3, 4, 5],
        'recent': False,
        'limit': 5
    }, srv_key=srv_key) or []
    
    ifaces_data = get_host_interfaces(target_host)
    ifaces_list = ifaces_data.get('interfaces', []) if isinstance(ifaces_data, dict) else []
    top_ifaces = [it for it in ifaces_list if (it.get('in_mbps', 0) + it.get('out_mbps', 0)) > 0][:6]
    
    status_emoji = "🟢" if is_alive else "🔴"
    status_label = "ONLINE (Operativo y respondiendo)" if is_alive else "OFFLINE (Sin respuesta a ping)"
    
    lines = [
        f"{status_emoji} <b>Estado Operativo — {target_host}</b>",
        f"🌐 <b>IP:</b> <code>{ip}</code> · <b>Monitoreo:</b> <code>{ZABBIX_SERVERS_META.get(srv_key, {}).get('short', srv_key)}</code>",
        f"📶 <b>Conectividad ICMP:</b> {status_emoji} <b>{status_label}</b>",
        f"⚡ <b>Latencia RTT:</b> <code>{rtt} ms</code> | <b>Pérdida de paquetes:</b> <code>{loss}%</code>\n"
    ]
    
    if probs:
        lines.append(f"⚠️ <b>Alertas Activas en el Equipo ({len(probs)}):</b>")
        for p in probs:
            lines.append(f"• 🔴 <b>{p.get('name', 'Problema')}</b>")
        lines.append("")
    else:
        lines.append("✅ <b>Alertas en Zabbix:</b> Sin incidentes activos reportados.\n")
        
    if top_ifaces:
        lines.append("📊 <b>Tráfico en Vivo (Principales Puertos):</b>")
        for it in top_ifaces:
            lines.append(f"• <b>{it['name']}:</b> ⬇️ <code>{it['in_mbps']:.2f} Mbps</code> | ⬆️ <code>{it['out_mbps']:.2f} Mbps</code>")
        lines.append("")
        
    lines.append(f"📈 <i>Para ver gráfica: <code>/trafico {target_host}</code> | Interfaces: <code>/interfaces {target_host}</code></i>")
    
    return {
        'ok': True,
        'host': target_host,
        'ip': ip,
        'alive': is_alive,
        'rtt': rtt,
        'loss': loss,
        'text': '\n'.join(lines)
    }

def format_interfaces_response(raw_query, chat_id=None):
    cid = str(chat_id or 'global')
    norm = network_inventory.normalize_text(raw_query)
    
    is_pagination = any(w in norm for w in ['paginado', 'paginada', 'pagina', 'página', 'siguiente', 'anterior', 'mas interfaces', 'resto de interfaces'])
    
    page_req = 1
    m_page = re.search(r'(?:pagina|página)\s*(\d+)', norm)
    if m_page:
        page_req = int(m_page.group(1))
    elif 'siguiente' in norm:
        last_entry = network_inventory.chat_context.get_last(cid)
        page_req = (last_entry.get('page', 1) or 1) + 1
    elif 'anterior' in norm:
        last_entry = network_inventory.chat_context.get_last(cid)
        page_req = max(1, (last_entry.get('page', 1) or 1) - 1)
        
    target_host = None
    resolved = None
    srv_key = 'zbx35'
    
    if not is_pagination:
        res_inv = network_inv.resolve(raw_query)
        if res_inv:
            resolved = res_inv['resolved']
            target_host = resolved['name']
            srv_key = res_inv['srv_key']
            
    if not target_host:
        last_entry = network_inventory.chat_context.get_last(cid)
        if last_entry and last_entry.get('host'):
            res_inv = network_inv.resolve(last_entry['host'])
            if res_inv:
                resolved = res_inv['resolved']
                target_host = resolved['name']
                srv_key = res_inv['srv_key']
                
    if not target_host:
        target_host = 'CoreRouter-01'
        res_inv = network_inv.resolve('CoreRouter-01')
        if res_inv:
            resolved = res_inv['resolved']
            srv_key = res_inv['srv_key']

    network_inventory.chat_context.record(cid, resolved or {'name': target_host, 'srv_key': srv_key}, action='interfaces')
    
    ifaces_data = get_host_interfaces(target_host)
    ifaces_list = ifaces_data.get('interfaces', []) if isinstance(ifaces_data, dict) else []
    
    page_size = 10
    total = len(ifaces_list)
    active_count = sum(1 for x in ifaces_list if (x.get('in_mbps', 0) + x.get('out_mbps', 0)) > 0)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page_req, total_pages))
    network_inventory.chat_context.set_page(cid, page)
    
    start_idx = (page - 1) * page_size
    page_items = ifaces_list[start_idx : start_idx + page_size]
    
    lines = [
        f"🔌 <b>Interfaces de Red — {target_host} (Página {page} de {total_pages})</b>",
        f"Total monitoreadas: <b>{total}</b> ({active_count} con tráfico activo)\n"
    ]
    
    for idx, it in enumerate(page_items, start_idx + 1):
        lines.append(f"<b>{idx}. {it['name']}</b>\n   ⬇️ IN: <code>{it['in_mbps']} Mbps</code> | ⬆️ OUT: <code>{it['out_mbps']} Mbps</code>")
        
    lines.append("")
    if page < total_pages:
        lines.append(f"📄 <i>Mostrando interfaces {start_idx + 1} al {min(start_idx + page_size, total)} de {total}. Escribe <b>\"página {page + 1}\"</b> o <b>\"siguiente\"</b> para continuar.</i>\n")
    else:
        lines.append(f"✅ <i>Fin de la lista de interfaces ({total} en total).</i>\n")
        
    lines.append(f"📊 <i>Para ver gráfica: <code>/trafico {target_host} [interfaz]</code></i>")
    
    return {
        'ok': True,
        'host': target_host,
        'page': page,
        'total_pages': total_pages,
        'text': '\n'.join(lines)
    }


# ==============================================================================
# TV WALLBOARD TELEMETRY (OLTs, Border Routers & SSL Certificates)
# ==============================================================================
_tv_cache = {"data": None, "ts": 0}
_tv_lock = threading.Lock()


#
# ==============================================================================
# SENTRY VOICE INTELLIGENCE (LLM Genuine Thoughts & Event Alerts for NOC TV)
# ==============================================================================
import random, uuid

class SentryVoiceBrain:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_routine_time = time.time()
        # Next routine comment in 60 to 120 minutes (1 a 2 horas)
        self.next_routine_interval = random.randint(3600, 7200)
        self.last_alert_time = 0
        self.last_comments = []
        self.active_speech = None
        self.border_history = {}
        self.pmx_node_states = {}
        self.focus_idx = 0
        self.alert_cooldowns = {}

    def ask_llm(self, system_prompt, user_prompt):
        try:
            req_data = {
                'model': 'agent.tools',
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ],
                'temperature': 0.75,
                'max_tokens': 75
            }
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer YOUR_LLM_BEARER_TOKEN'
            }
            req = urllib.request.Request(
                'https://192.168.1.60:11443/v1/chat/completions',
                data=json.dumps(req_data).encode('utf-8'),
                headers=headers
            )
            with urllib.request.urlopen(req, context=ssl_context, timeout=8) as resp:
                d = json.loads(resp.read().decode('utf-8'))
                text = d['choices'][0]['message']['content'].strip()
                return text.strip('"\'«»')
        except Exception as e:
            print(f"[SentryVoiceBrain] LLM error: {e}", flush=True)
            return None

    def evaluate_and_update(self, telemetry_data):
        now = time.time()
        with self.lock:
            # 1. ALERTAS REACTIVAS ANTE CAÍDAS O RECUPERACIONES
            borders = telemetry_data.get("borders", [])
            for b in borders:
                bname = b.get("short", b.get("name", "Router"))
                ip = b.get("ip", "")
                is_down = (b.get("status") == "OFFLINE" or b.get("ping_loss", 0) >= 100)
                
                prev = self.border_history.get(bname)
                self.border_history[bname] = {"is_down": is_down, "rx": b.get("rx_mbps", 0)}

                if prev is not None:
                    # Caída de router de borde
                    if is_down and not prev["is_down"]:
                        cool_key = f"down_{bname}"
                        if (now - self.alert_cooldowns.get(cool_key, 0) > 1800) and (now - self.last_alert_time > 300):
                            self.alert_cooldowns[cool_key] = now
                            self.last_alert_time = now
                            user_p = f"ALERTA OPERATIVA INMEDIATA: Se detectó caída o desconexión en el router de borde {bname} ({ip}). Da un aviso breve y directo al equipo en la sala NOC."
                            text = self.ask_llm(
                                "Eres Marco, coordinador del NOC de NetCore NOC. Da una alerta técnica breve y directa en voz alta (máximo 16 palabras).",
                                user_p
                            )
                            if text:
                                self._set_speech(text, "alert", now)
                                return

                    # Recuperación de router de borde
                    elif not is_down and prev["is_down"]:
                        cool_key = f"up_{bname}"
                        if (now - self.alert_cooldowns.get(cool_key, 0) > 1800) and (now - self.last_alert_time > 300):
                            self.alert_cooldowns[cool_key] = now
                            self.last_alert_time = now
                            user_p = f"RECUPERACIÓN: El router {bname} ({ip}) se ha restablecido y vuelve a responder en línea. Da un aviso breve de normalización."
                            text = self.ask_llm(
                                "Eres Marco, coordinador del NOC de NetCore NOC. Da un aviso breve de recuperación técnica en voz alta (máximo 14 palabras).",
                                user_p
                            )
                            if text:
                                self._set_speech(text, "recovery", now)
                                return

            # Caída de nodo Proxmox
            pmx = telemetry_data.get("proxmox", {})
            nodes = pmx.get("nodes", [])
            for n in nodes:
                nname = n.get("node", "nodo")
                status = n.get("status", "online")
                prev_st = self.pmx_node_states.get(nname)
                self.pmx_node_states[nname] = status
                if prev_st == "online" and status != "online":
                    cool_key = f"pmx_down_{nname}"
                    if (now - self.alert_cooldowns.get(cool_key, 0) > 1800) and (now - self.last_alert_time > 300):
                        self.alert_cooldowns[cool_key] = now
                        self.last_alert_time = now
                        user_p = f"ALERTA CRÍTICA: El hipervisor {nname} del clúster Proxmox ha pasado a estado OFFLINE. Da una advertencia operativa rápida."
                        text = self.ask_llm(
                            "Eres Marco, coordinador del NOC de NetCore NOC. Da una advertencia técnica urgente y concisa en voz alta (máximo 16 palabras).",
                            user_p
                        )
                        if text:
                            self._set_speech(text, "alert", now)
                            return

            # 2. COMENTARIOS RUTINARIOS GENUINOS (Cada 1 a 2 horas)
            if (now - self.last_routine_time) >= self.next_routine_interval:
                self._generate_routine_speech(telemetry_data, now)

    def _generate_routine_speech(self, telemetry_data, now):
        pmx = telemetry_data.get("proxmox", {})
        pbs = pmx.get('pbs', {})
        borders = telemetry_data.get("borders", [])
        mk = telemetry_data.get('mk_health', [])
        tot_rx = sum(b.get('rx_mbps', 0) for b in borders)
        tot_tx = sum(b.get('tx_mbps', 0) for b in borders)

        focus_topics = ['proxmox_vms', 'core_traffic', 'hardware_health', 'pbs_backups']
        topic = focus_topics[self.focus_idx % len(focus_topics)]
        self.focus_idx += 1

        if topic == 'proxmox_vms':
            mem_u = pmx.get('tot_mem_used_gb', 536)
            mem_m = pmx.get('tot_mem_max_gb', 1133)
            mem_p = round((mem_u / mem_m) * 100, 1) if mem_m else 47.0
            ctx = f"Clúster Proxmox totalmente estable con {pmx.get('vms_running', 38)} de {pmx.get('vms_total', 41)} VMs activas y memoria utilizada al {mem_p}% ({mem_u} GB de {mem_m} GB de capacidad física)."
        elif topic == 'core_traffic':
            ctx = f"Tráfico troncal de borde con {round(tot_rx/1000, 1)} Gbps de bajada y {round(tot_tx/1000, 1)} Gbps de subida; enlaces WAN Troncal Principal, Enlace de Respaldo y Red de Distribución operando sin saturación."
        elif topic == 'hardware_health':
            temps = [f"{m.get('short')}: {m.get('temp_c')}°C" for m in mk if m.get('temp_c')]
            ctx = f"Salud física de los MikroTik núcleo: temperaturas operativas estables ({', '.join(temps[:3])}), procesadores bajo 10% y enlace troncal de distribución normal."
        else: # pbs_backups
            ctx = f"Servidor PBS de respaldos con {pbs.get('respaldos_exitosos', 28)} de 28 respaldos diarios íntegros y cero fallos en datastores."

        hist_clause = ""
        if self.last_comments:
            hist_clause = f"IMPORTANTE: No repitas ideas ni palabras de lo dicho antes: {'; '.join(self.last_comments[-3:])}."

        sys_p = (
            "Eres Marco, coordinador de guardia del NOC de NetCore NOC. Hablas en voz alta en la sala de operaciones "
            "como un ingeniero de redes senior, de forma espontánea, técnica y concisa. "
            "Da una sola frase breve (máximo 16 palabras) reflexionando sobre el estado actual de la infraestructura. "
            "La infraestructura opera de forma normal y estable. Habla con calma y realismo técnico, sin alarmismos. "
            "No saludes, no digas 'buenos días', 'hola equipo' ni fórmulas prefabricadas."
        )
        user_p = f"SITUACIÓN ACTUAL: {ctx}. {hist_clause} Di tu observación en una sola frase breve."

        text = self.ask_llm(sys_p, user_p)
        if text:
            self.last_comments.append(text)
            if len(self.last_comments) > 15:
                self.last_comments.pop(0)
            self.last_routine_time = now
            self.next_routine_interval = random.randint(3600, 7200) # Próximo en 1 a 2 horas (60 a 120 min)
            self._set_speech(text, "routine", now)
            return text
        return None

    def force_routine_comment(self, telemetry_data):
        now = time.time()
        with self.lock:
            return self._generate_routine_speech(telemetry_data, now)

    def _set_speech(self, text, stype, ts):
        self.active_speech = {
            "id": str(uuid.uuid4()),
            "text": text,
            "type": stype,
            "ts": ts
        }
        print(f"[SentryVoiceBrain] Generated ({stype}): {text}", flush=True)
        try:
            with _tv_lock:
                if _tv_cache.get("data"):
                    _tv_cache["data"]["sentry_speech"] = self.active_speech
        except Exception:
            pass
        try:
            event_hub.publish({
                "tipo": "speak",
                "texto_voz": text,
                "speech_id": self.active_speech["id"],
                "detalle": f"Locución Marco ({stype}): {text}",
                "usuario": "Marco (Sentry)",
                "herramienta": "sentry"
            })
        except Exception as e:
            print(f"[SentryVoiceBrain] Broadcast error: {e}", flush=True)

    def get_speech_for_tv(self):
        with self.lock:
            if not self.active_speech:
                return None
            if time.time() - self.active_speech["ts"] < 45:
                return self.active_speech
            return None

sentry_voice_brain = SentryVoiceBrain()

# ==============================================================================
# WHATSAPP WEBHOOK & LLM TICKET CLASSIFIER (WITH OPERATOR MENTIONS)
# ==============================================================================
import unicodedata

NOC_OPERATORS = [
    {
        "nombre": "Operador NOC 1",
        "jid": "584120000001@c.us",
        "phone": "584120000001",
        "alias": ["operador1", "guardia1"]
    },
    {
        "nombre": "Operador NOC 2",
        "jid": "584240000002@c.us",
        "phone": "584240000002",
        "alias": ["operador2", "guardia2"]
    },
    {
        "nombre": "Operador NOC 3",
        "jid": "584140000003@c.us",
        "phone": "584140000003",
        "alias": ["operador3", "guardia3"]
    },
    {
        "nombre": "Operador NOC 4",
        "jid": "584240000004@c.us",
        "phone": "584240000004",
        "alias": ["operador4", "guardia4"]
    }
]

def strip_accents_text(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def detect_mentioned_operators(data, body_text):
    mentioned = []
    # 1. Native WhatsApp mentions (mentionedJidList / mentionedJids)
    raw_jids = data.get("mentionedJidList") or data.get("mentionedJids") or []
    if isinstance(raw_jids, list):
        for jid in raw_jids:
            clean_jid = str(jid).lower().strip()
            for op in NOC_OPERATORS:
                if op["jid"].lower() in clean_jid or op["phone"] in clean_jid:
                    if op["nombre"] not in mentioned:
                        mentioned.append(op["nombre"])

    # 2. Text mentions (@alias or whole-word alias, including guardia, operador, etc.)
    clean_text = strip_accents_text(body_text.lower())
    for op in NOC_OPERATORS:
        if op["nombre"] in mentioned:
            continue
        for alias in op["alias"]:
            alias_clean = strip_accents_text(alias.lower())
            pattern = rf"(?:@\s*{re.escape(alias_clean)}|\b{re.escape(alias_clean)}\b)"
            if re.search(pattern, clean_text):
                mentioned.append(op["nombre"])
                break
                
    return mentioned

_last_processed_wa_msg_ids = set()

def classify_whatsapp_ticket(body_text, mentioned_ops=None):
    ops_instruction = ""
    if mentioned_ops:
        ops_names = " y ".join(mentioned_ops)
        ops_instruction = (
            f"\nIMPORTANTE: El mensaje menciona o va dirigido a: {ops_names}. "
            f"Debes iniciar la locución llamando la atención de esa persona "
            f"(ej: 'Atención {ops_names}: nuevo requerimiento...' o '{ops_names}, solicitan apoyo con...')."
        )

    prompt = (
        "Eres el clasificador y sintetizador de voz de IA del NOC de NetCore NOC para el grupo de WhatsApp 'Soporte Interno TI'.\n"
        "Tu misión es procesar mensajes entrantes para generar la locución de voz clara, formal y perfectamente entendible que se anunciará a los operadores por altavoz en el centro de operaciones.\n"
        f"{ops_instruction}\n"
        "REGLAS DE CLASIFICACIÓN:\n"
        "1. NUEVO REQUERIMIENTO O FALLA:\n"
        "   Si el mensaje reporta una nueva falla, problema, solicitud técnica o requerimiento de soporte interno, o si pide apoyo técnico a un miembro del NOC:\n"
        "   Responde exactamente en JSON: {\"es_ticket\": true, \"locucion\": \"...\"}\n"
        "   REGLAS PARA LA 'locucion':\n"
        "   - Estructura y tono: Debe sonar natural, pausada y clara para síntesis de voz (TTS). Máximo 32 palabras.\n"
        "   - Formato recomendado:\n"
        "     \"Nuevo requerimiento de [Departamento u Oficina]: [Nombre Completo del Solicitante] solicita [Descripción clara del problema o requerimiento].\"\n"
        "     Ejemplo: \"Nuevo requerimiento de Soporte Corporativo, oficina 4-B: María Fernández solicita habilitación de acceso VPN corporativo.\"\n"
        "   - Si no hay nombre de persona: \"Nuevo requerimiento de [Departamento/Oficina]: [descripción del problema].\"\n"
        "   - Si no hay departamento: \"Nuevo requerimiento solicitado por [Nombre]: [descripción del problema].\"\n"
        "   - NUNCA inventes, cambies ni omitas el nombre de la persona (ej: 'María' no debe cambiarse por nada).\n"
        "   - NUNCA incluyas números de cédula (C.I.), números telefónicos, saludos ni despedidas en la locución.\n"
        "   - Usa puntuación clara (puntos y comas) para generar pausas naturales al hablar.\n\n"
        "2. MENSAJES QUE NO DEBEN HABLARSE:\n"
        "   - Si es una respuesta o confirmación del NOC ('ticket creado', 'revisando', 'en proceso', 'solucionado'): {\"es_ticket\": false, \"locucion\": null}\n"
        "   - Si es un seguimiento general ('alguna novedad?', 'cómo va mi caso?'): {\"es_ticket\": false, \"locucion\": null}\n"
        "   - Si es un simple saludo o agradecimiento sin solicitud: {\"es_ticket\": false, \"locucion\": null}\n\n"
        "Responde ÚNICAMENTE el objeto JSON sin explicaciones adicionales ni backticks."
    )
    payload = {
        "model": "agent.tools",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"MENSAJE DE WHATSAPP:\n{body_text}"}
        ],
        "temperature": 0.1,
        "max_tokens": 200
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer YOUR_LLM_BEARER_TOKEN"
    }
    req = urllib.request.Request(
        "https://192.168.1.60:11443/v1/chat/completions",
        data=json.dumps(payload).encode('utf-8'),
        headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_context) as r:
            res = json.loads(r.read().decode('utf-8'))
            raw = res["choices"][0]["message"]["content"].strip()
            if "```" in raw:
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m: raw = m.group(0)
            return json.loads(raw)
    except Exception as e:
        print(f"[WhatsApp Ticket Classifier] LLM error: {e}", flush=True)
        return {"es_ticket": False, "locucion": None}

def process_whatsapp_webhook(payload):
    try:
        event = payload.get("event")
        if event != "message.received":
            return
        
        data = payload.get("data", {})
        msg_id = data.get("id")
        if msg_id:
            if msg_id in _last_processed_wa_msg_ids:
                return
            _last_processed_wa_msg_ids.add(msg_id)
            if len(_last_processed_wa_msg_ids) > 1000:
                _last_processed_wa_msg_ids.pop()

        from_me = data.get("fromMe", False)
        if from_me:
            return

        body = (data.get("body") or "").strip()
        if not body or len(body) < 3:
            return

        chat_id = data.get("chatId", "")
        author = data.get("author", "")
        print(f"[WhatsApp Webhook] Inbound from {chat_id} (author {author}): {body[:60]}...", flush=True)

        mentioned_ops = detect_mentioned_operators(data, body)
        if mentioned_ops:
            print(f"[WhatsApp Mention Detected]: {', '.join(mentioned_ops)}", flush=True)

        res = classify_whatsapp_ticket(body, mentioned_ops)
        loc = None
        if res and res.get("es_ticket") and res.get("locucion"):
            loc = res["locucion"]
        elif mentioned_ops:
            ops_str = " y ".join(mentioned_ops)
            if len(mentioned_ops) > 1:
                loc = f"Atención {ops_str}: los mencionaron en el grupo Soporte Interno TI."
            else:
                loc = f"Atención {ops_str}: te mencionaron en el grupo Soporte Interno TI."

        if loc:
            print(f"[WhatsApp Ticket Detected] Speaking on TV: {loc}", flush=True)
            sentry_voice_brain._set_speech(loc, "ticket", time.time())
        else:
            print(f"[WhatsApp Message Filtered] Ignored (not a new ticket or mention)", flush=True)
    except Exception as e:
        print(f"[WhatsApp Webhook Error]: {e}", flush=True)

def fetch_tv_telemetry_live():
    # 1. OLTs (Zabbix 35)
    olt_configs = [
        {"name": "OLT REGION SOUTH", "host": "SW_CORE_SOUTH", "color_rx": "#ec4899", "color_tx": "#3b82f6", "iface": "sfp-sfpplus2"},
        {"name": "OLT REGION CENTRAL", "host": "MK_CORE_CENTRAL", "color_rx": "#06b6d4", "color_tx": "#1d4ed8", "iface": "sfp-sfpplus8"},
        {"name": "OLT REGION NORTH", "host": "SW_CORE_NORTH", "color_rx": "#00ffff", "color_tx": "#22c55e", "iface": "sfp-sfpplus8"},
        {"name": "OLT REGION WEST", "host": "MK_CORE_WEST", "color_rx": "#ef4444", "color_tx": "#f87171", "iface": "sfp-sfpplus1"},
        {"name": "OLT REGION EAST", "host": "SW_CORE_EAST", "color_rx": "#f59e0b", "color_tx": "#ea580c", "iface": "sfp-sfpplus1"},
        {"name": "OLT REGION METRO", "host": "MK_CORE_METRO", "color_rx": "#c084fc", "color_tx": "#9333ea", "iface": "sfp-sfpplus1"},
    ]

    olt_hosts = [c["host"] for c in olt_configs]
    hosts_res = zabbix_query("host.get", {"output": ["hostid", "name", "host"], "filter": {"host": olt_hosts}}, srv_key='zbx35') or []
    hmap = {h["host"]: h["hostid"] for h in hosts_res}

    olts = []
    for c in olt_configs:
        hid = hmap.get(c["host"])
        rx = 0.0
        tx = 0.0
        if hid:
            items = zabbix_query("item.get", {
                "hostids": [hid],
                "output": ["name", "key_", "lastvalue"],
                "search": {"key_": f"[{c['iface']}]", "name": c["iface"]},
                "searchByAny": True
            }, srv_key='zbx35') or []
            for it in items:
                nl = it.get("name", "").lower()
                try:
                    val = float(it.get("lastvalue", 0)) / 1000000.0
                    if "received" in nl or "in" in it.get("key_", "").lower():
                        rx = round(val, 2)
                    elif "sent" in nl or "out" in it.get("key_", "").lower():
                        tx = round(val, 2)
                except:
                    pass
        olts.append({
            "name": c["name"],
            "host": c["host"],
            "iface": c["iface"],
            "rx_mbps": rx,
            "tx_mbps": tx,
            "color_rx": c["color_rx"],
            "color_tx": c["color_tx"]
        })

    # 2. Routers de Borde & Núcleo (5 Routers en Zabbix 35 - Medición limpia de interfaz WAN/Tránsito real)
    border_configs = [
        {
            "name": "BORDER-ROUTER-NORTH", "short": "Edge-North-1072", "model": "CCR1072", "hostid": "10654", "ip": "192.168.10.1", "tag": "Core Distribution / 5009", "color_rx": "#38bdf8", "color_tx": "#60a5fa",
            "rx_key": "net.if.in[ifHCInOctets.2]", "tx_key": "net.if.out[ifHCOutOctets.2]", "iface_match": "sfp-sfpplus2"
        },
        {
            "name": "BORDER-ROUTER-SOUTH", "short": "Edge-South-2216", "model": "CCR2216", "hostid": "12277", "ip": "192.168.20.1", "tag": "Trunk Backbone / Uplink", "color_rx": "#818cf8", "color_tx": "#a78bfa",
            "rx_key": "net.if.in[ifHCInOctets.12]", "tx_key": "net.if.out[ifHCOutOctets.12]", "iface_match": "sfp28-2"
        },
        {
            "name": "COREROUTER-01", "short": "CoreRouter", "model": "CCR2216", "hostid": "12535", "ip": "198.51.100.1", "tag": "Core WAN Principal", "color_rx": "#34d399", "color_tx": "#4ade80",
            "rx_key": "net.if.in[ifHCInOctets.26]", "tx_key": "net.if.out[ifHCOutOctets.26]", "iface_match": "wan-mds"
        },
        {
            "name": "DIST-ROUTER-EAST", "short": "Dist-East-5009", "model": "RB5009", "hostid": "10648", "ip": "192.168.30.1", "tag": "Bonding / Trunk", "color_rx": "#fbbf24", "color_tx": "#f59e0b",
            "rx_key": "net.if.in[ifHCInOctets.18]", "tx_key": "net.if.out[ifHCOutOctets.18]", "iface_match": "bonding-sag"
        },
        {
            "name": "ACCESS-ROUTER-WEST", "short": "Access-West-750", "model": "RB750Gr3", "hostid": "10676", "ip": "192.168.40.1", "tag": "Hotspot Gateway / Office", "color_rx": "#f472b6", "color_tx": "#ec4899",
            "rx_key": "net.if.in[ifHCInOctets.1]", "tx_key": "net.if.out[ifHCOutOctets.1]", "iface_match": "ether1"
        }
    ]
    borders = []
    mk_health = []
    for bc in border_configs:
        items = zabbix_query("item.get", {
            "hostids": [bc["hostid"]],
            "output": ["name", "key_", "lastvalue"]
        }, srv_key='zbx35') or []

        real_rx = 0.0
        real_tx = 0.0
        found_rx = False
        found_tx = False
        cpu_vals = []
        temp = None
        mem_pct = None
        ping_ms = None

        for it in items:
            nm = it.get("name", "")
            k = it.get("key_", "")
            vl = it.get("lastvalue", "0")

            # Medición exacta de la interfaz WAN/Tránsito sin sumar VLANs/puentes duplicados
            if k == bc.get("rx_key"):
                try:
                    real_rx = float(vl) / 1000000.0
                    found_rx = True
                except: pass
            elif k == bc.get("tx_key"):
                try:
                    real_tx = float(vl) / 1000000.0
                    found_tx = True
                except: pass
            elif not found_rx and bc.get("iface_match") and bc["iface_match"] in nm.lower() and ("received" in nm.lower() or "in" in k.lower()):
                try:
                    real_rx = float(vl) / 1000000.0
                    found_rx = True
                except: pass
            elif not found_tx and bc.get("iface_match") and bc["iface_match"] in nm.lower() and ("sent" in nm.lower() or "out" in k.lower()):
                try:
                    real_tx = float(vl) / 1000000.0
                    found_tx = True
                except: pass

            if "CPU utilization" in nm:
                try: cpu_vals.append(float(vl))
                except: pass
            elif "CPU: Temperature" in nm or "Device: Temperature" in nm:
                try:
                    t_val = float(vl)
                    if t_val > 0 and (temp is None or t_val > temp):
                        temp = round(t_val, 1)
                except: pass
            elif "Memory utilization" in nm:
                try: mem_pct = round(float(vl), 1)
                except: pass
            elif "ICMP response time" in nm:
                try: ping_ms = round(float(vl) * 1000.0, 2)
                except: pass

        cpu_avg = round(sum(cpu_vals)/len(cpu_vals), 1) if cpu_vals else 0.0

        border_entry = {
            "name": bc["name"],
            "short": bc["short"],
            "model": bc["model"],
            "ip": bc["ip"],
            "tag": bc["tag"],
            "color_rx": bc["color_rx"],
            "color_tx": bc["color_tx"],
            "rx_mbps": round(real_rx, 3) if real_rx < 0.1 else round(real_rx, 2),
            "tx_mbps": round(real_tx, 3) if real_tx < 0.1 else round(real_tx, 2),
            "status": "ONLINE"
        }
        borders.append(border_entry)

        mk_health.append({
            "name": bc["name"],
            "short": bc["short"],
            "model": bc["model"],
            "cpu_pct": cpu_avg,
            "temp_c": temp,
            "mem_pct": mem_pct,
            "ping_ms": ping_ms,
            "status": "ONLINE"
        })

    # 3. Certificates & Web Monitoring (Zabbix 20 Dashboard 192)
    dash192 = zabbix_query("dashboard.get", {"dashboardids": ["192"], "selectPages": "extend"}, srv_key='zbx20')
    cert_map = {}
    if dash192 and isinstance(dash192, list) and len(dash192) > 0:
        try:
            for w in dash192[0]["pages"][0]["widgets"]:
                flds = {f["name"]: f["value"] for f in w.get("fields", [])}
                if "itemid" in flds:
                    cert_map[flds["itemid"]] = {
                        "title": w.get("name") or "Servicio",
                        "bg_color": flds.get("bg_color", "00BFFF")
                    }
        except Exception as e:
            print(f"[TV-Telemetry] Error parsing dash 192: {e}", flush=True)

    certificates = []
    if cert_map:
        c_items = zabbix_query("item.get", {
            "itemids": list(cert_map.keys()),
            "output": ["itemid", "name", "key_", "lastvalue", "lastclock"]
        }, srv_key='zbx20') or []
        for it in c_items:
            meta = cert_map.get(it["itemid"], {})
            try:
                days = int(float(it.get("lastvalue", 0)))
            except:
                days = 0
            title = meta.get("title") or it.get("name", "Servicio")
            has_data = days > 0
            is_critical = has_data and days <= 7
            is_warning = has_data and (7 < days <= 20)
            certificates.append({
                "title": title,
                "days": days,
                "has_data": has_data,
                "is_critical": is_critical,
                "is_warning": is_warning,
                "bg_color": meta.get("bg_color", "00BFFF")
            })

    certificates.sort(key=lambda x: (not x["is_critical"], not x["is_warning"], not x["has_data"], x["days"]))

    # 4. Proxmox Clúster & Hipervisores (Zabbix/PVE & PDM)
    proxmox_data = {
        "ok": False,
        "nodes": [],
        "vms_running": 0,
        "vms_total": 0,
        "vms_stopped": 0,
        "tot_mem_used_gb": 0,
        "tot_mem_max_gb": 0,
        "tot_cores": 0,
        "pbs": {}
    }
    try:
        ctx_pmx = ssl.create_default_context()
        ctx_pmx.check_hostname = False
        ctx_pmx.verify_mode = ssl.CERT_NONE
        l_data = urllib.parse.urlencode({'username': PROXMOX_USER, 'password': PROXMOX_PASS}).encode()
        l_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/access/ticket', data=l_data)
        t_res = json.loads(urllib.request.urlopen(l_req, context=ctx_pmx, timeout=3).read())['data']
        pve_ticket = t_res['ticket']
        pve_headers = {'Cookie': 'PVEAuthCookie=' + pve_ticket}
        r_req = urllib.request.Request(f'https://{PROXMOX_HOST}:8006/api2/json/cluster/resources', headers=pve_headers)
        pve_items = json.loads(urllib.request.urlopen(r_req, context=ctx_pmx, timeout=3).read())['data']

        pmx_nodes = []
        vms_running = 0
        vms_total = 0
        tot_mem_u = 0.0
        tot_mem_m = 0.0
        tot_c = 0
        for it in pve_items:
            itype = it.get('type')
            if itype == 'node':
                m_u = round(it.get('mem', 0) / (1024**3), 1)
                m_m = round(it.get('maxmem', 0) / (1024**3), 1)
                m_pct = round((it.get('mem', 0) / it.get('maxmem', 1)) * 100, 1) if it.get('maxmem') else 0
                c_pct = round(it.get('cpu', 0) * 100, 1)
                cores = it.get('maxcpu', 0)
                tot_mem_u += m_u
                tot_mem_m += m_m
                tot_c += cores
                pmx_nodes.append({
                    "node": it.get('node'),
                    "status": it.get('status', 'offline'),
                    "cpu_pct": c_pct,
                    "mem_used_gb": m_u,
                    "mem_max_gb": m_m,
                    "mem_pct": m_pct,
                    "cores": cores,
                    "uptime": it.get('uptime', 0)
                })
            elif itype in ['qemu', 'lxc']:
                vms_total += 1
                if it.get('status') == 'running':
                    vms_running += 1

        pmx_nodes.sort(key=lambda x: x['node'].upper())

        # PDM PBS summary if available
        pbs_info = {}
        try:
            pdm_s = get_pdm_summary()
            if pdm_s and pdm_s.get('ok'):
                pbs_raw = pdm_s.get('pbs_backup', {})
                pbs_info = {
                    "nodo": pbs_raw.get('nodo_pbs', 'pbs-backup.netcore-isp.net'),
                    "estado": pbs_raw.get('estado', 'Online y Operativo'),
                    "datastores": pbs_raw.get('datastores_online', '3 / 3 (datastore-local, datastore-remote, archive)'),
                    "almacenamiento": pbs_raw.get('almacenamiento', '4.82 TB / 16.0 TB (30.1%)'),
                    "respaldos_exitosos": pbs_raw.get('respaldos_exitosos', 28),
                    "respaldos_fallidos": pbs_raw.get('respaldos_fallidos', 0),
                    "trabajos": "cluster-primary (Site-A) + cluster-secondary (Site-B): OK"
                }
        except Exception:
            pass

        proxmox_data = {
            "ok": True,
            "nodes": pmx_nodes,
            "vms_running": vms_running,
            "vms_total": vms_total,
            "vms_stopped": vms_total - vms_running,
            "tot_mem_used_gb": round(tot_mem_u, 1),
            "tot_mem_max_gb": round(tot_mem_m, 1),
            "tot_cores": tot_c,
            "pbs": pbs_info
        }
    except Exception as e_pmx:
        print(f"[TV-Telemetry] Proxmox fetch error: {e_pmx}", flush=True)

# mk_health already populated in unified border loop

    res = {
        "ok": True,
        "olts": olts,
        "borders": borders,
        "certificates": certificates,
        "proxmox": proxmox_data,
        "mk_health": mk_health,
        "critical_count": sum(1 for c in certificates if c["is_critical"]),
        "timestamp": time.strftime("%H:%M:%S")
    }
    try:
        sentry_voice_brain.evaluate_and_update(res)
        res["sentry_speech"] = sentry_voice_brain.get_speech_for_tv()
    except Exception as e_sb:
        print(f"[SentryVoiceBrain] Evaluate error: {e_sb}", flush=True)
        res["sentry_speech"] = None
    return res

def get_tv_telemetry_cached():
    global _tv_cache
    now = time.time()
    with _tv_lock:
        if _tv_cache["data"] and (now - _tv_cache["ts"] < 15):
            return _tv_cache["data"]

    try:
        fresh = fetch_tv_telemetry_live()
        with _tv_lock:
            _tv_cache = {"data": fresh, "ts": now}
        return fresh
    except Exception as e:
        print(f"[TV-Telemetry] Live fetch exception: {e}", flush=True)
        with _tv_lock:
            if _tv_cache["data"]:
                return _tv_cache["data"]
            return {"ok": False, "error": str(e), "olts": [], "borders": [], "certificates": []}

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "nginx/1.22.0"
    sys_version = "" 
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/api/tts':
            try:
                qs = parse_qs(parsed.query)
                text = qs.get('text', [''])[0].strip()
                voice = qs.get('voice', ['es-VE-SebastianNeural'])[0].strip()
                rate = qs.get('rate', ['-6%'])[0].strip()
                if not text:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Missing text parameter")
                    return
                import hashlib
                h = hashlib.md5(f"{text}:{voice}:{rate}".encode('utf-8')).hexdigest()
                cache_file = f"/tmp/tts_{h}.mp3"
                if not os.path.exists(cache_file):
                    import asyncio, edge_tts
                    async def _gen():
                        comm = edge_tts.Communicate(text, voice, rate=rate)
                        await comm.save(cache_file)
                    asyncio.run(_gen())
                with open(cache_file, 'rb') as f:
                    audio_bytes = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.send_header('Content-Length', str(len(audio_bytes)))
                self.end_headers()
                self.wfile.write(audio_bytes)
            except Exception as e:
                err_b = str(e).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(err_b)))
                self.end_headers()
                self.wfile.write(err_b)
            return

        if parsed.path == '/api/sentry-test-speech':
            try:
                qs = parse_qs(parsed.query)
                custom_txt = qs.get('text', [None])[0]
                if custom_txt:
                    sentry_voice_brain._set_speech(custom_txt, "ticket", time.time())
                    txt = custom_txt
                else:
                    latest = get_tv_telemetry_cached()
                    txt = sentry_voice_brain.force_routine_comment(latest)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "generated": txt, "speech": sentry_voice_brain.active_speech}).encode('utf-8'))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode('utf-8'))
            return

        if parsed.path == '/api/tv-telemetry':
            try:
                data = get_tv_telemetry_cached()
                resp = json.dumps(data).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err_resp = json.dumps({"ok": False, "error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(err_resp)
            return

        if parsed.path in ['/whatsapp', '/whatsapp-qr']:
            try:
                html = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Vinculación de WhatsApp - NetCore NOC</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0b0f19; color: #f1f5f9; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 16px; box-sizing: border-box; }
    .card { background: #131b2e; border: 1px solid #1e293b; border-radius: 18px; padding: 28px 24px; max-width: 440px; width: 100%; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.6); }
    h1 { font-size: 20px; font-weight: 700; margin: 0 0 6px 0; color: #38bdf8; }
    .subtitle { color: #94a3b8; font-size: 13px; margin: 0 0 18px 0; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px; border-radius: 9999px; font-size: 12px; font-weight: 600; background: #1e293b; color: #94a3b8; margin-bottom: 16px; border: 1px solid #334155; }
    .badge.ready { background: #064e3b; color: #34d399; border-color: #059669; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #38bdf8; animation: blink 1.5s infinite; }
    .badge.ready .dot { background: #34d399; animation: none; }
    @keyframes blink { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.3; transform: scale(0.85); } }
    .qr-box { background: white; padding: 12px; border-radius: 14px; display: inline-block; margin-bottom: 16px; box-shadow: 0 8px 16px rgba(0,0,0,0.4); }
    .qr-img { width: 230px; height: 230px; display: block; border-radius: 6px; }
    .pairing-box { background: #0b0f19; border: 1px dashed #334155; border-radius: 12px; padding: 14px; margin-top: 14px; text-align: center; }
    .pairing-title { font-size: 11px; text-transform: uppercase; color: #64748b; letter-spacing: 0.05em; font-weight: 700; }
    .pairing-code { font-family: monospace; font-size: 26px; font-weight: 800; letter-spacing: 4px; color: #38bdf8; margin: 8px 0; }
    .btn { background: #0284c7; color: white; border: none; border-radius: 8px; padding: 8px 14px; font-size: 12px; font-weight: 600; cursor: pointer; transition: 0.2s; margin-top: 6px; }
    .btn:hover { background: #0369a1; }
    .hint { font-size: 11px; color: #64748b; margin-top: 8px; line-height: 1.4; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Vincular Bot de WhatsApp</h1>
    <div class="subtitle">Número asignado: <strong>+5804XXXXXXXXX</strong></div>

    <div id="badge" class="badge">
      <span class="dot"></span>
      <span id="badge-txt">Generando código en vivo...</span>
    </div>

    <div class="qr-box">
      <img id="qr-img" class="qr-img" src="" alt="Cargando QR...">
    </div>

    <div class="pairing-box">
      <div class="pairing-title">¿Prefieres código numérico?</div>
      <div id="p-code" class="pairing-code">---</div>
      <button class="btn" onclick="genPairing()">Solicitar nuevo código</button>
      <div class="hint">En WhatsApp toca: <em>Ajustes &gt; Dispositivos vinculados &gt; Vincular con el número de teléfono</em> e introduce este código.</div>
    </div>
  </div>

  <script>
    async function loadQR() {
      try {
        const r = await fetch('/api/whatsapp-qr-live');
        const d = await r.json();
        if (d.status === 'ready') {
          document.getElementById('badge').className = 'badge ready';
          document.getElementById('badge-txt').innerText = '✅ Conectado con éxito: ' + (d.phone || '+5804XXXXXXXXX');
          document.getElementById('qr-img').style.opacity = '0.2';
          return;
        }
        if (d.qrCode) {
          document.getElementById('qr-img').src = d.qrCode;
          document.getElementById('badge-txt').innerText = 'Código QR activo (en vivo)';
        }
      } catch (e) {
        console.error(e);
      }
    }

    async function genPairing() {
      document.getElementById('p-code').innerText = '...';
      try {
        const r = await fetch('/api/whatsapp-pairing-code', { method: 'POST' });
        const d = await r.json();
        if (d.pairingCode) {
          document.getElementById('p-code').innerText = d.pairingCode;
        }
      } catch (e) {
        document.getElementById('p-code').innerText = 'Error';
      }
    }

    loadQR();
    genPairing();
    setInterval(loadQR, 3000);
  </script>
</body>
</html>"""
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(html.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(html.encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                return

        if parsed.path == '/api/whatsapp-qr-live':
            try:
                headers = {'x-api-key': 'YOUR_OPENWA_API_KEY'}
                req_sess = urllib.request.Request('http://127.0.0.1:2785/api/sessions/YOUR_OPENWA_SESSION_ID', headers=headers)
                with urllib.request.urlopen(req_sess, timeout=4) as r_s:
                    sess_info = json.loads(r_s.read().decode('utf-8'))
                
                if sess_info.get('status') == 'ready':
                    resp = json.dumps({"status": "ready", "phone": sess_info.get("phone", "+5804XXXXXXXXX")}).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Content-Length', str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return

                req = urllib.request.Request('http://127.0.0.1:2785/api/sessions/YOUR_OPENWA_SESSION_ID/qr', headers=headers)
                with urllib.request.urlopen(req, timeout=4) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                resp = json.dumps({"status": "error", "message": str(e)}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return

        if parsed.path in ['/tv', '/oficina', '/visor']:
            try:
                html_path = os.getenv('DASHBOARD_HTML_PATH', '/app/dashboard/oficina_noc.html')
                if not os.path.exists(html_path):
                    html_path = os.path.join(os.path.dirname(__file__), 'oficina_noc.html')
                with open(html_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
                return

        if parsed.path == '/alerts/recent':
            qs = parse_qs(parsed.query)
            try:
                minutes = int(qs.get('minutes', [6])[0])
            except:
                minutes = 6
            recent = alerts_memory.get_recent(minutes=minutes)
            resp = json.dumps({"ok": True, "count": len(recent), "minutes": minutes, "alerts": recent}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            init_msg = json.dumps({"tipo": "sistema", "mensaje": "Conectado al bus de eventos NOC", "timestamp": datetime.now().strftime('%H:%M:%S')})
            try:
                self.wfile.write(f"data: {init_msg}\n\n".encode('utf-8'))
                self.wfile.flush()
            except:
                return

            q = event_hub.subscribe()
            try:
                while True:
                    try:
                        ev = q.get(timeout=20)
                        payload = json.dumps(ev)
                        self.wfile.write(f"data: {payload}\n\n".encode('utf-8'))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                event_hub.unsubscribe(q)
            return

        if parsed.path == '/events-history':
            with event_hub.lock:
                hist = list(event_hub.history)
            resp_body = json.dumps(hist).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            return

        if parsed.path == '/traffic-summary':
            try:
                qs = parse_qs(parsed.query)
                q = qs.get('q', [''])[0].strip() or qs.get('target', [''])[0].strip()
                chat_id = qs.get('chat_id', [''])[0].strip()
                period = qs.get('period', ['24h'])[0].strip()
                iface = qs.get('interface', [''])[0].strip()
                res = format_traffic_summary_response(q, chat_id=chat_id, period=period, iface=iface)
                resp = json.dumps(res).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == '/device-status-formatted':
            try:
                qs = parse_qs(parsed.query)
                q = qs.get('q', [''])[0].strip() or qs.get('target', [''])[0].strip()
                chat_id = qs.get('chat_id', [''])[0].strip()
                res = format_device_status_response(q, chat_id=chat_id)
                resp = json.dumps(res).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == '/interfaces-formatted':
            try:
                qs = parse_qs(parsed.query)
                q = qs.get('q', [''])[0].strip() or qs.get('target', [''])[0].strip()
                chat_id = qs.get('chat_id', [''])[0].strip()
                res = format_interfaces_response(q, chat_id=chat_id)
                resp = json.dumps(res).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == '/ping':
            try:
                qs = parse_qs(parsed.query)
                target = qs.get('target', ['192.168.1.1'])[0].strip()
                count = int(qs.get('count', [4])[0])
                res = run_linux_ping(target, count)
                event_hub.publish({
                    "tipo": "ping",
                    "herramienta": "ping",
                    "usuario": "Admin",
                    "target": target,
                    "pregunta": f"Ping a {target}",
                    "detalle": f"Ping nativo Linux (count={count})",
                    "respuesta": f"{'ONLINE' if res.get('alive') else 'OFFLINE'} - RTT: {res.get('rtt_avg_ms', 'N/D')}ms - Pérdida: {res.get('packet_loss_pct')}%"
                })
                resp_body = json.dumps(res).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
            except Exception as e:
                err = json.dumps({"error": str(e), "alive": False}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return
        if parsed.path == '/zabbix/problems':
            qs = parse_qs(parsed.query)
            lim = int(qs.get('limit', [15])[0])
            resp = json.dumps(handle_unified_zabbix_problems(lim)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == '/zabbix/history':
            qs = parse_qs(parsed.query)
            lim = int(qs.get('limit', [15])[0])
            resp = json.dumps(handle_unified_zabbix_history(lim)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == '/zabbix/search':
            qs = parse_qs(parsed.query)
            q = qs.get('query', [''])[0].strip()
            resp = json.dumps(handle_unified_zabbix_search(q)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == '/zabbix/topology':
            resp = json.dumps(handle_unified_zabbix_topology()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == '/pdm/status':
            try:
                res = get_pdm_summary()
                resp = json.dumps(res).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == '/health-check':
            try:
                qs = parse_qs(parsed.query)
                target = qs.get('target', [''])[0].strip()
                health_data = check_device_health(target)
                event_hub.publish({
                    "tipo": "consulta",
                    "herramienta": "diagnostico",
                    "usuario": "Admin",
                    "target": target,
                    "pregunta": f"Diagnóstico {target}",
                    "detalle": f"Diagnóstico de salud: {target}",
                    "respuesta": f"Estado: {health_data.get('estado') or health_data.get('conectividad') or 'OK'}"
                })
                resp_body = json.dumps(health_data).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == '/subscribers':
            subs = get_subscribers()
            resp_body = json.dumps(subs).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            return

        if parsed.path == '/interfaces':
            try:
                qs = parse_qs(parsed.query)
                target = qs.get('target', ['CoreRouter-01'])[0].strip()
                ifaces_data = get_host_interfaces(target)
                resp_body = json.dumps(ifaces_data).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                return
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

        if parsed.path == '/generate-report':
            try:
                qs = parse_qs(parsed.query)
                period = qs.get('period', ['24'])[0]
                host_target = qs.get('host', [''])[0].strip()
                iface_target = qs.get('interface', [''])[0].strip()
                event_hub.publish({
                    "tipo": "consulta",
                    "herramienta": "historial",
                    "usuario": "Admin",
                    "pregunta": f"/reporte ({period}) {host_target} {iface_target}".strip(),
                    "detalle": f"Generando Reporte PDF {period}",
                    "respuesta": f"PDF generado y enviado"
                })
                pdf_bytes = build_pdf_report(period, host_target, iface_target)
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.send_header('Content-Disposition', f'attachment; filename="reporte_noc_{period}.pdf"')
                self.send_header('Content-Length', str(len(pdf_bytes)))
                self.end_headers()
                self.wfile.write(pdf_bytes)
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
                return

        if parsed.path == '/generate-traffic-chart':
            try:
                qs = parse_qs(parsed.query)
                target = qs.get('target', ['CoreRouter-01'])[0].strip()
                period = qs.get('period', ['3h'])[0].strip()
                iface = qs.get('interface', [''])[0].strip()
                event_hub.publish({
                    "tipo": "consulta",
                    "herramienta": "trafico",
                    "usuario": "Admin",
                    "target": target,
                    "pregunta": f"/trafico {target} {iface} {period}".strip(),
                    "detalle": f"Gráfica de tráfico: {target} ({iface or 'General'}) {period}",
                    "respuesta": f"Gráfica PNG generada"
                })
                img_bytes = get_traffic_graph_bytes(target, period, iface)
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Disposition', f'inline; filename="trafico_{target}.png"')
                self.send_header('Content-Length', str(len(img_bytes)))
                self.end_headers()
                self.wfile.write(img_bytes)
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
                return

        if parsed.path == '/generate-dashboard-chart':
            try:
                event_hub.publish({
                    "tipo": "consulta",
                    "herramienta": "dashboard",
                    "usuario": "Admin",
                    "target": "general",
                    "pregunta": "Ver Dashboard General",
                    "detalle": "Generando snapshot PNG del Dashboard Ejecutivo NOC",
                    "respuesta": "Imagen PNG del Dashboard generada y enviada"
                })
                img_bytes = get_dashboard_snapshot_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Disposition', 'inline; filename="dashboard_noc.png"')
                self.send_header('Content-Length', str(len(img_bytes)))
                self.end_headers()
                self.wfile.write(img_bytes)
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
                return

        if parsed.path in ['/generate-pdm-chart', '/pdm/chart', '/generate-datacenter-chart']:
            try:
                event_hub.publish({
                    "tipo": "consulta",
                    "herramienta": "pdm_chart",
                    "usuario": "Admin",
                    "target": "pdm",
                    "pregunta": "Ver Gráfica PDM Datacenter",
                    "detalle": "Generando snapshot PNG del Datacenter Proxmox (PDM/PVE/PBS)",
                    "respuesta": "Imagen PNG de PDM generada y enviada"
                })
                img_bytes = get_pdm_chart_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Disposition', 'inline; filename="pdm_datacenter.png"')
                self.send_header('Content-Length', str(len(img_bytes)))
                self.end_headers()
                self.wfile.write(img_bytes)
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
                return

        # Reenvío de rutas /v1/* u otras a Facilitoya IA Upstream
        target_url = TARGET_BASE + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() != 'host'}
        req = urllib.request.Request(target_url, headers=headers, method='GET')
        try:
            with urllib.request.urlopen(req, context=ssl_context) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ['transfer-encoding', 'content-length']:
                        self.send_header(k, v)
                resp_body = resp.read()
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                return
        except urllib.error.HTTPError as e:
            print(f"[PROXY HTTP ERROR {e.code}]: {e}", flush=True)
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ['transfer-encoding', 'content-length']:
                    self.send_header(k, v)
            resp_body = e.read()
            print(f"[PROXY HTTP ERROR BODY]: {resp_body.decode('utf-8', errors='ignore')}", flush=True)
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            return
        except Exception as e:
            print(f"[PROXY EXCEPTION]: {e}", flush=True)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else None
        parsed = urlparse(self.path)

        if parsed.path == '/api/whatsapp-pairing-code':
            try:
                headers = {
                    'x-api-key': 'YOUR_OPENWA_API_KEY',
                    'Content-Type': 'application/json'
                }
                data = json.dumps({'phoneNumber': "584XXXXXXXXX"}).encode('utf-8')
                req = urllib.request.Request('http://127.0.0.1:2785/api/sessions/YOUR_OPENWA_SESSION_ID/pairing-code', data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as r:
                    res_body = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(res_body)))
                self.end_headers()
                self.wfile.write(res_body)
                return
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

        if parsed.path == '/api/whatsapp-webhook':
            try:
                raw_str = body.decode('utf-8', errors='replace') if body else '{}'
                data = json.loads(raw_str)
                threading.Thread(target=process_whatsapp_webhook, args=(data,), daemon=True).start()
                resp = json.dumps({"ok": True}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                print(f"[WhatsAppWebhook] Handler error: {e}", flush=True)
                self.send_response(200)
                self.end_headers()
            return

        if parsed.path == '/alerts/record':
            try:
                data = json.loads(body.decode('utf-8')) if body else {}
                alerts_memory.record(data)
                resp = json.dumps({"ok": True, "recorded": True}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
            return

        if parsed.path == '/alerts/voice':
            try:
                data = json.loads(body.decode('utf-8')) if body else {}
                alerts_memory.record(data)
                voice_coordinator.enqueue_alert(data)
                resp = json.dumps({"ok": True, "enqueued": True}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
            return

        if parsed.path == '/proxmox/vm-action':
            try:
                data = json.loads(body.decode('utf-8')) if body else {}
                res = handle_proxmox_vm_action(data)
                resp = json.dumps(res).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == '/publish-event':
            try:
                data = json.loads(body.decode('utf-8')) if body else {}
                event_hub.publish(data)
                resp = json.dumps({"status": "published", "event": data}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == '/subscribers':
            try:
                data = json.loads(body.decode('utf-8')) if body else {}
                chat_id = str(data.get('chat_id', ''))
                name = str(data.get('name', 'Usuario'))

                if not chat_id:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error": "chat_id es requerido"}')
                    return

                subs = get_subscribers()
                if not any(str(s.get('chat_id')) == chat_id for s in subs):
                    subs.append({"chat_id": chat_id, "name": name, "joined": datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                    save_subscribers(subs)
                    event_hub.publish({
                        "tipo": "alerta",
                        "herramienta": "seguridad",
                        "usuario": name,
                        "pregunta": "Autenticación de Usuario",
                        "detalle": f"Nuevo suscriptor autenticado con PIN: {name} ({chat_id})",
                        "respuesta": "Acceso Concedido"
                    })

                resp = json.dumps({"status": "ok", "subscribers": subs}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
            return

        # Reenvío de chat completions y rutas /v1/* a Facilitoya IA Upstream
        target_url = TARGET_BASE + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ['host', 'content-length']}
        import uuid
        headers['X-Idempotency-Key'] = str(uuid.uuid4())
        client_wants_stream = False
        if body and self.path.startswith('/v1/chat/completions'):
            try:
                req_obj = json.loads(body.decode('utf-8'))
                client_wants_stream = bool(req_obj.get('stream', False))
                if client_wants_stream:
                    req_obj['stream'] = False
                # Trim oversized historical tool messages to prevent >8k context overflow
                msgs_list = req_obj.get('messages', [])
                if len(msgs_list) > 3:
                    for m in msgs_list[:-2]:
                        if m.get('role') == 'tool' and isinstance(m.get('content'), str) and len(m['content']) > 600:
                            m['content'] = m['content'][:600] + '..."}'
                body = json.dumps(req_obj).encode('utf-8')
            except Exception as _e_req:
                print(f"[PROXY REQ PARSE WARN]: {_e_req}", flush=True)

        print(f'\n[PROXY POST] {self.path} | Body len: {len(body) if body else 0} | client_stream={client_wants_stream}', flush=True)
        req = urllib.request.Request(target_url, data=body if 'body' in locals() and body else None, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, context=ssl_context) as resp:
                resp_headers = [(k, v) for k, v in resp.getheaders() if k.lower() not in ['transfer-encoding', 'content-length', 'content-type']]
                resp_status = resp.status
                resp_body = resp.read()

                # INTERCEPCIÓN INTELIGENTE Y NORMALIZACIÓN DE TOOL CALLS
                if body and self.path.startswith('/v1/chat/completions'):
                    try:
                        resp_json = json.loads(resp_body.decode('utf-8'))
                        choice_Wrapper = resp_json.get('choices', [{}])[0]
                        choice = choice_Wrapper.get('message', {})
                        finish_reason = choice_Wrapper.get('finish_reason', '')
                        content = str(choice.get('content') or '')
                        req_json = json.loads(body.decode('utf-8'))
                        msgs = req_json.get('messages', [])
                        user_prompt = ""
                        for m in reversed(msgs):
                            if m.get('role') == 'user':
                                user_prompt = str(m.get('content', ''))
                                break

                        # 1. Normalizar tool_calls para que SIEMPRE tengan {"input": "..."} válido y nombre exacto
                        tcs = choice.get('tool_calls') or []
                        if tcs:
                            tool_name_map = {
                                'ver_problemas_zabbix': 'Ver_Problemas_Activos',
                                'ver_problemas_activos': 'Ver_Problemas_Activos',
                                'ver_historial_alertas_zabbix': 'Ver_Historial_Alertas',
                                'ver_historial_alertas': 'Ver_Historial_Alertas',
                                'buscar_equipo_zabbix': 'Buscar_Equipo',
                                'buscar_equipo': 'Buscar_Equipo',
                                'ver_topologia_zabbix': 'Ver_Topologia_Red',
                                'ver_topologia_red': 'Ver_Topologia_Red',
                                'hacer_ping_zabbix': 'Hacer_Ping',
                                'hacer_ping': 'Hacer_Ping',
                                'consultar_trafico_zabbix': 'Consultar_Trafico',
                                'consultar_trafico': 'Consultar_Trafico',
                                'diagnosticar_salud_equipo': 'Diagnosticar_Salud_Equipo',
                                'listar_interfaces_zabbix': 'Listar_Interfaces_Zabbix',
                                'consultar_accion_vm_proxmox': 'Consultar_Accion_VM_Proxmox',
                                'ejecutar_accion_vm_proxmox': 'Ejecutar_Accion_VM_Proxmox',
                                'consultar_datacenter_pdm': 'Consultar_Datacenter_PDM'
                            }
                            # Find registered tool names from request tools schema
                            req_tool_names = {}
                            for rt in req_json.get('tools', []):
                                fn_name = rt.get('function', {}).get('name', '')
                                if fn_name:
                                    req_tool_names[fn_name.lower()] = fn_name

                            for tc in tcs:
                                fn = tc.get('function', {})
                                raw_name = str(fn.get('name', ''))
                                mapped = tool_name_map.get(raw_name.lower(), raw_name)
                                if mapped.lower() in req_tool_names:
                                    fn['name'] = req_tool_names[mapped.lower()]
                                elif raw_name.lower() in req_tool_names:
                                    fn['name'] = req_tool_names[raw_name.lower()]

                                raw_args = fn.get('arguments', '{}')
                                try:
                                    args_obj = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args if isinstance(raw_args, dict) else {})
                                except Exception:
                                    args_obj = {"input": str(raw_args)}
                                if not isinstance(args_obj, dict):
                                    args_obj = {"input": str(args_obj)}
                                if not args_obj.get('input') or not str(args_obj.get('input')).strip():
                                    fallback_in = args_obj.get('query') or args_obj.get('target') or args_obj.get('host') or args_obj.get('vmid') or args_obj.get('limit') or user_prompt or 'all'
                                    args_obj['input'] = str(fallback_in).strip()
                                
                                # Auto-repair fragmented or typo tool arguments using smart inventory resolver
                                fn_l = fn.get('name', '').lower()
                                if any(k in fn_l for k in ['diagnosticar', 'salud', 'trafico', 'interfaces', 'buscar', 'ping']):
                                    inv_fix = network_inv.resolve(user_prompt)
                                    if inv_fix and inv_fix.get('score', 0) >= 450:
                                        curr_in = str(args_obj.get('input', '')).lower()
                                        if not curr_in or any(typo in curr_in for typo in ['trarico', 'tarfico', 'trafico', 'router', 'el router', 'switch', 'olt', 'como esta', 'estado']) or curr_in not in inv_fix['name'].lower():
                                            args_obj['input'] = inv_fix['name']
                                            print(f"[PROXY RESOLVER] Repaired tool argument to '{inv_fix['name']}' based on prompt: {user_prompt[:50]}", flush=True)
                                fn['arguments'] = json.dumps(args_obj, ensure_ascii=False)
                            resp_body = json.dumps(resp_json).encode('utf-8')

                        # 2. Si el modelo devolvió finish_reason=='tool_calls' pero sin tool_calls, o un error MCP, resolver directamente
                        is_empty_tc = (finish_reason == 'tool_calls' and not tcs and not content.strip())
                        is_mcp_err = ('Error invoking MCP tool' in content or 'error mcp tool' in content.lower()) and not tcs

                        if is_empty_tc or is_mcp_err:
                            print(f"[PROXY INTERCEPT] Caught empty_tc={is_empty_tc} / mcp_err={is_mcp_err} for prompt: {user_prompt[:80]}", flush=True)
                            up_low = user_prompt.lower()

                            # A. Alertas Activas / Problemas en Zabbix y The Dude
                            if any(w in up_low for w in ['alerta', 'problema', 'incidente', 'caido', 'caído', 'activas', 'activos', 'down', 'falla']):
                                probs_data = get_zabbix_problems(limit=12)
                                rec_list = probs_data.get('alertas_recientes_ultimos_6_min', [])
                                act_list = probs_data.get('problemas_activos_zabbix', [])
                                lines_out = ["🚨 <b>Alertas Recientes (Últimos 6 min)</b>\n"]
                                if rec_list:
                                    for a in rec_list[:6]:
                                        lines_out.append(
                                            f"🔴 <b>{a.get('equipo', 'Equipo')}</b>\n"
                                            f"• <b>Problema:</b> {a.get('problema', 'N/D')}\n"
                                            f"• <b>Severidad:</b> {a.get('severidad', 'Alta')}\n"
                                            f"• <b>Hora:</b> {a.get('hora_evento', 'Reciente')}\n"
                                            f"• <b>Origen:</b> <code>{a.get('origen', 'Zabbix')}</code>\n"
                                        )
                                else:
                                    lines_out.append("✅ No se han registrado alertas nuevas en los últimos 6 minutos.\n")

                                lines_out.append("\n⚠️ <b>Problemas Activos en Zabbix (3 Servidores Unificados)</b>\n")
                                if act_list:
                                    for p in act_list[:10]:
                                        sev = str(p.get('severidad', 'Media'))
                                        emo = "🔴" if sev in ['Desastre', 'Alta'] else ("🟠" if sev == 'Media' else "🟡")
                                        lines_out.append(
                                            f"{emo} <b>{p.get('equipo', 'Host')}</b>\n"
                                            f"• <b>Problema:</b> {p.get('problema', 'N/D')}\n"
                                            f"• <b>Severidad:</b> {sev}\n"
                                            f"• <b>Desde:</b> {p.get('hora', 'N/D')}\n"
                                            f"• <b>Origen:</b> <code>{p.get('origen', 'Zabbix')}</code>\n"
                                        )
                                else:
                                    lines_out.append("🟢 Sin problemas activos reportados actualmente.")

                                choice['content'] = "\n".join(lines_out).strip()
                                choice['tool_calls'] = []
                                choice_Wrapper['finish_reason'] = 'stop'
                                content = choice['content']
                                resp_body = json.dumps(resp_json).encode('utf-8')
                                print("[PROXY RESOLVED] Answered active alerts directly!", flush=True)

                            # B. Proxmox Datacenter Manager (PDM) / Backups resolution
                            elif any(w in up_low for w in ['pdm', 'datacenter', 'backup', 'respaldo', 'pbs', 'cuantas vms', 'cuantos nodos', 'nodos en proxmox']):
                                pdm_sum = get_pdm_summary()
                                if pdm_sum.get('ok'):
                                    pve = pdm_sum['pve_cluster']
                                    pbs = pdm_sum['pbs_backup']
                                    fixed_text = (
                                        f"🏢 <b>Proxmox Datacenter Manager (PDM) — Estado Global</b>\n\n"
                                        f"🖥️ <b>Clúster PVE:</b>\n"
                                        f"• <b>Nodos:</b> 🟢 {pve['nodos_online']} en línea ({pve.get('nodos_detalle', 'PMX10, pmx11, PMX12, Lumex01, lumex02, lumex04')})\n"
                                        f"• <b>Máquinas Virtuales:</b> 🟢 {pve['vms_running']} running | ⚪ {pve['vms_stopped']} stopped (Total: <b>{pve['vms_total']}</b>)\n"
                                        f"• <b>Contenedores LXC:</b> 🟢 {pve['lxc_running']} running | ⚪ {pve['lxc_stopped']} stopped (Total: <b>{pve['lxc_total']}</b>)\n\n"
                                        f"💾 <b>Servidor de Respaldo (PBS):</b>\n"
                                        f"• <b>Nodo:</b> 🟢 {pbs['nodo_pbs']} ({pbs['estado']})\n"
                                        f"• <b>Datastores:</b> {pbs['datastores_online']} Online\n"
                                        f"• <b>Almacenamiento:</b> {pbs['almacenamiento']}\n"
                                        f"• <b>Respaldos:</b> ✅ {pbs['respaldos_exitosos']} exitosos | ❌ {pbs['respaldos_fallidos']} fallidos\n"
                                        f"• <b>Prunes/Limpiezas:</b> {pbs['prunes_completados']} completados"
                                    )
                                    choice['content'] = fixed_text
                                    choice['tool_calls'] = []
                                    choice_Wrapper['finish_reason'] = 'stop'
                                    content = choice['content']
                                    resp_body = json.dumps(resp_json).encode('utf-8')
                                    print(f"[PROXY RESOLVED] Successfully answered with PDM datacenter data!", flush=True)

                            # C. Proxmox VM resolution
                            elif 'consultar_accion_vm_proxmox' in content or 'vm' in up_low or re.search(r'\bvm\s*\d+\b', user_prompt, re.I):
                                vm_match = re.search(r'\b(?:vm_?id|vm|id)?\s*[:=]?\s*(\d+)\b', user_prompt, re.I)
                                vm_target = vm_match.group(1) if vm_match else ""
                                if not vm_target:
                                    cleaned_prompt = user_prompt
                                    for stopw in ['dime', 'el', 'estado', 'de', 'la', 'del', 'contenedor', 'máquina', 'maquina', 'virtual', 'status', 'info', 'ver']:
                                        cleaned_prompt = re.sub(rf'\b{stopw}\b', '', cleaned_prompt, flags=re.I)
                                    cleaned_prompt = re.sub(r'\bvm\b', '', cleaned_prompt, flags=re.I).strip()
                                    vm_target = cleaned_prompt

                                action_found = 'status'
                                for act_name, kws in {
                                    'reboot': ['reboot', 'reiniciar', 'reinicie'],
                                    'shutdown': ['shutdown', 'apagar', 'apague'],
                                    'stop': ['stop', 'parar', 'pare', 'detener'],
                                    'start': ['start', 'encender', 'enciende', 'levantar', 'levanta', 'iniciar'],
                                    'reset': ['reset', 'hard reset', 'reinicio forzado'],
                                    'console': ['consola', 'console']
                                }.items():
                                    if any(kw in up_low for kw in kws):
                                        action_found = act_name
                                        break

                                if vm_target:
                                    pmx_res = handle_proxmox_vm_action({"target": vm_target, "action": action_found, "phase": "check"})
                                    if pmx_res.get('ok'):
                                        v_name = pmx_res.get('nombre', 'Desconocido')
                                        v_id = pmx_res.get('vmid', vm_target)
                                        v_node = pmx_res.get('nodo', 'PMX10')
                                        v_status = pmx_res.get('estado_actual', 'UNKNOWN').upper()
                                        st_emoji = "🟢" if "RUN" in v_status else "🔴"

                                        if action_found == 'status':
                                            v_cpu = pmx_res.get('cpu', 'N/D')
                                            v_mem = pmx_res.get('memoria', 'N/D')
                                            v_up = pmx_res.get('uptime', 'N/D')
                                            fixed_text = (
                                                f"🖥️ <b>Información de Máquina Virtual — Proxmox VE</b>\n\n"
                                                f"• <b>Nombre:</b> {v_name}\n"
                                                f"• <b>VMID:</b> <code>{v_id}</code>\n"
                                                f"• <b>Nodo físico:</b> <code>{v_node}</code>\n"
                                                f"• <b>Estado:</b> {st_emoji} <b>{v_status}</b>\n"
                                                f"• <b>Uso de CPU:</b> {v_cpu}\n"
                                                f"• <b>Memoria RAM:</b> {v_mem}\n"
                                                f"• <b>Tiempo activo (Uptime):</b> {v_up}"
                                            )
                                        else:
                                            act_label = pmx_res.get('accion_solicitada', action_found.upper())
                                            fixed_text = (
                                                f"⚠️ <b>Solicitud Operativa Proxmox VE</b>\n\n"
                                                f"• <b>Máquina:</b> {v_name} (VMID: <code>{v_id}</code>)\n"
                                                f"• <b>Nodo físico:</b> <code>{v_node}</code>\n"
                                                f"• <b>Estado actual:</b> {st_emoji} {v_status}\n"
                                                f"• <b>Acción solicitada:</b> <b>{act_label}</b>\n\n"
                                                f"¿Deseas proceder con la solicitud de esta acción sobre la máquina virtual? Responde <b>Sí</b> para continuar o <b>Cancelar</b> para anular."
                                            )
                                        choice['content'] = fixed_text
                                        choice['tool_calls'] = []
                                        choice_Wrapper['finish_reason'] = 'stop'
                                        resp_body = json.dumps(resp_json).encode('utf-8')
                                        print(f"[PROXY RESOLVED] Successfully answered with real Proxmox data for VM {vm_target} ({action_found})!", flush=True)
                                    else:
                                        err_msg = pmx_res.get('error', f"No se encontró la VM {vm_target} en Proxmox.")
                                        choice['content'] = f"ℹ️ {err_msg}"
                                        choice['tool_calls'] = []
                                        choice_Wrapper['finish_reason'] = 'stop'
                                        resp_body = json.dumps(resp_json).encode('utf-8')

                            elif 'consultar_trafico_zabbix' in content or any(w in up_low for w in ['trafico', 'trarico', 'tarfico', 'consumo', 'velocidad', 'ancho de banda', 'mbps']):
                                summary_res = format_traffic_summary_response(user_prompt)
                                if summary_res.get('ok'):
                                    choice['content'] = summary_res['caption']
                                else:
                                    choice['content'] = "ℹ️ No se encontraron interfaces de tráfico reportando datos actualmente para este equipo. Para ver las interfaces disponibles, escribe <code>/interfaces [equipo]</code> o haz un ping con <code>/ping [IP]</code>."
                                choice['tool_calls'] = []
                                choice_Wrapper['finish_reason'] = 'stop'
                                resp_body = json.dumps(resp_json).encode('utf-8')
                                print(f"[PROXY RESOLVED] Answered traffic query directly with real data for prompt: {user_prompt[:60]}", flush=True)

                            elif any(w in up_low for w in ['estado', 'estatus', 'status', 'salud', 'como esta', 'cómo está', 'diagnostico']) and not any(w in up_low for w in ['vm', 'proxmox', 'pdm', 'cluster']):
                                st_res = format_device_status_response(user_prompt)
                                if st_res.get('ok'):
                                    choice['content'] = st_res['text']
                                    choice['tool_calls'] = []
                                    choice_Wrapper['finish_reason'] = 'stop'
                                    resp_body = json.dumps(resp_json).encode('utf-8')
                                    print(f"[PROXY RESOLVED] Answered device status directly with real data for prompt: {user_prompt[:60]}", flush=True)

                            elif any(w in up_low for w in ['interfaces', 'puertos', 'paginado', 'pagina', 'página']):
                                if_res = format_interfaces_response(user_prompt)
                                if if_res.get('ok'):
                                    choice['content'] = if_res['text']
                                    choice['tool_calls'] = []
                                    choice_Wrapper['finish_reason'] = 'stop'
                                    resp_body = json.dumps(resp_json).encode('utf-8')
                                    print(f"[PROXY RESOLVED] Answered interfaces/pagination directly with real data for prompt: {user_prompt[:60]}", flush=True)

                        # 3. Auto-cerrar etiquetas HTML truncadas (<b>, <i>, <code>) para evitar error 400 en Telegram
                        if choice.get('content'):
                            c_txt = str(choice['content'])
                            # Remove any trailing incomplete tag like "<b" or "</b"
                            c_txt = re.sub(r'<[^>]*$', '', c_txt)
                            for tag in ['b', 'i', 'code']:
                                opens = len(re.findall(f'<{tag}>', c_txt, re.I))
                                closes = len(re.findall(f'</{tag}>', c_txt, re.I))
                                if opens > closes:
                                    c_txt += f'</{tag}>' * (opens - closes)
                            if c_txt != choice['content']:
                                choice['content'] = c_txt
                                resp_body = json.dumps(resp_json).encode('utf-8')

                        # 4. Si el cliente (LangChain) pidió stream=True, empaquetar resp_json como SSE válido
                        if client_wants_stream:
                            msg_obj = resp_json.get('choices', [{}])[0].get('message', {})
                            fin_r = resp_json.get('choices', [{}])[0].get('finish_reason', 'stop')
                            delta_obj = {"role": "assistant"}
                            if msg_obj.get('content'):
                                delta_obj["content"] = msg_obj['content']
                            if msg_obj.get('tool_calls'):
                                tc_deltas = []
                                for idx_tc, tc_item in enumerate(msg_obj['tool_calls']):
                                    tc_deltas.append({
                                        "index": idx_tc,
                                        "id": tc_item.get("id", f"call_{idx_tc}"),
                                        "type": "function",
                                        "function": {
                                            "name": tc_item.get("function", {}).get("name", ""),
                                            "arguments": tc_item.get("function", {}).get("arguments", "{}")
                                        }
                                    })
                                delta_obj["tool_calls"] = tc_deltas
                            chunk1 = {
                                "id": resp_json.get("id", "chatcmpl-proxy"),
                                "object": "chat.completion.chunk",
                                "created": resp_json.get("created", int(time.time())),
                                "model": resp_json.get("model", "agent.tools"),
                                "choices": [{"index": 0, "delta": delta_obj, "finish_reason": None}]
                            }
                            chunk2 = {
                                "id": resp_json.get("id", "chatcmpl-proxy"),
                                "object": "chat.completion.chunk",
                                "created": resp_json.get("created", int(time.time())),
                                "model": resp_json.get("model", "agent.tools"),
                                "choices": [{"index": 0, "delta": {}, "finish_reason": fin_r}],
                                "usage": resp_json.get("usage", {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
                            }
                            sse_payload = f"data: {json.dumps(chunk1, ensure_ascii=False)}\n\ndata: {json.dumps(chunk2, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
                            resp_body = sse_payload.encode('utf-8')
                            resp_headers.append(('Content-Type', 'text/event-stream; charset=utf-8'))
                        else:
                            resp_headers.append(('Content-Type', 'application/json'))
                    except Exception as e_res:
                        print(f"[PROXY INTERCEPT EXCEPTION]: {e_res}", flush=True)
                        resp_headers.append(('Content-Type', 'application/json'))

                self.send_response(resp_status)
                for k, v in resp_headers:
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)

                # EventHub auto-emit if chat completions
                try:
                    if body and self.path.startswith('/v1/chat/completions'):
                        resp_json = json.loads(resp_body.decode('utf-8'))
                        choice = resp_json.get('choices', [{}])[0].get('message', {})
                        tool_calls = choice.get('tool_calls', [])
                        req_json = json.loads(body.decode('utf-8'))
                        msgs = req_json.get('messages', [])
                        user_prompt = ""
                        for m in reversed(msgs):
                            if m.get('role') == 'user':
                                user_prompt = str(m.get('content', ''))
                                break
                        if tool_calls:
                            tc = tool_calls[0].get('function', {})
                            tname = tc.get('name', 'consulta')
                            targs = tc.get('arguments', '')
                            event_hub.publish({
                                "tipo": "consulta",
                                "herramienta": tname,
                                "usuario": "Admin",
                                "pregunta": user_prompt[:80],
                                "detalle": f"Ejecutando {tname}",
                                "target": targs[:40] if targs else ""
                            })
                        # Intercepción inteligente de alertas para Locución TV HUD y Memoria
                        if user_prompt and ('acaba de pasar a estado DOWN' in user_prompt or 'Alerta Zabbix' in user_prompt or 'caída' in user_prompt.lower()):
                            try:
                                h_match = re.search(r'(?:llamado|equipo|host)\s+([A-Za-z0-9_\-\.]+)', user_prompt, re.IGNORECASE)
                                host_found = h_match.group(1) if h_match else "Equipo de Red"
                                p_match = re.search(r'(?:problema|alerta|reporta|falla):\s*([^\n\.]+)', user_prompt, re.IGNORECASE)
                                prob_found = p_match.group(1) if p_match else "Caída o pérdida de conectividad"
                                alert_info = {
                                    "host": host_found,
                                    "problem": prob_found,
                                    "severity": "Alta",
                                    "source": "Dude" if "Dude" in user_prompt else "Zabbix"
                                }
                                alerts_memory.record(alert_info)
                                voice_coordinator.enqueue_alert(alert_info)
                            except Exception as _ex:
                                pass

                        elif user_prompt and choice.get('content'):
                            ans = str(choice.get('content', ''))[:100].replace('\n', ' ')
                            event_hub.publish({
                                "tipo": "consulta",
                                "herramienta": "sentry",
                                "usuario": "Admin",
                                "pregunta": user_prompt[:80],
                                "detalle": "Respuesta IA",
                                "respuesta": ans
                            })
                except Exception:
                    pass
                return
        except urllib.error.HTTPError as e:
            print(f"[PROXY HTTP ERROR {e.code}]: {e}", flush=True)
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ['transfer-encoding', 'content-length']:
                    self.send_header(k, v)
            resp_body = e.read()
            print(f"[PROXY HTTP ERROR BODY]: {resp_body.decode('utf-8', errors='ignore')}", flush=True)
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            return
        except Exception as e:
            print(f"[PROXY EXCEPTION]: {e}", flush=True)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()

if __name__ == '__main__':
    if os.environ.get('DISABLE_COLLECTORS') != '1':
        zabbix20_collector.start()
        zabbix74_collector.start()
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), ProxyHandler)
    print(f"Proxy NOC escuchando en puerto {PORT}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
