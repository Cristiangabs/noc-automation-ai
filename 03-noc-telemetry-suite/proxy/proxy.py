
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

PORT = int(os.getenv('PROXY_PORT', 11440))
CA_CERT = os.getenv('CA_CERT_PATH', './multicanal-ca.crt')
TARGET_BASE = os.getenv('AI_GATEWAY_URL', 'https://ai-gateway.local:11443')
if os.path.exists(CA_CERT):
    ssl_context = ssl.create_default_context(cafile=CA_CERT)
else:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
ZABBIX_URL = os.getenv('ZABBIX_URL', 'http://zabbix.local/zabbix/api_jsonrpc.php')
ZABBIX_TOKEN = os.getenv('ZABBIX_TOKEN', 'YOUR_ZABBIX_API_TOKEN')
SUBSCRIBERS_FILE = os.getenv('SUBSCRIBERS_FILE', './subscribers.json')

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

def zabbix_query(method, params):
    req_data = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode('utf-8')
    req = urllib.request.Request(ZABBIX_URL, data=req_data, headers={
        'Authorization': f'Bearer {ZABBIX_TOKEN}',
        'Content-Type': 'application/json'
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8')).get('result', [])

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

    items_rx = []
    items_tx = []
    resolved_host_id = None
    resolved_host_name = None

    search_terms = [target_iface] if target_iface else []
    if target_iface.lower().startswith('vlan176'):
        search_terms.append('vlan76' + target_iface[7:])
    elif target_iface.lower().startswith('vlan76'):
        search_terms.append('vlan176' + target_iface[6:])

    for term in search_terms:
        if not term: continue
        tag_items = zabbix_query('item.get', {
            'output': ['itemid', 'hostid', 'name', 'lastvalue'],
            'tags': [{'tag': 'interface', 'value': term, 'operator': '0'}]
        })
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
            })
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
                h = zabbix_query('host.get', {'hostids': [it['hostid']], 'output': ['name', 'host']})
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
            h = zabbix_query('host.get', {'hostids': [resolved_host_id], 'output': ['name', 'host']})
            resolved_host_name = h[0].get('name') or h[0].get('host') if h else "Dispositivo"

        for it in items_tx:
            if it['hostid'] == resolved_host_id:
                best_tx = it
                break

    return resolved_host_name, resolved_host_id, best_rx, best_tx

def get_host_interfaces(target):
    target = str(target or '').strip()
    es_ip = all(part.isdigit() for part in target.split('.')) and len(target.split('.')) == 4
    host_id = None
    host_name = target

    if es_ip:
        ifaces = zabbix_query('hostinterface.get', {'output': ['hostid'], 'filter': {'ip': target}})
        if ifaces:
            host_id = ifaces[0]['hostid']
            hosts = zabbix_query('host.get', {'output': ['hostid', 'name', 'host'], 'hostids': [host_id]})
            if hosts: host_name = hosts[0].get('name') or hosts[0].get('host')
    if not host_id:
        hosts = zabbix_query('host.get', {'output': ['hostid', 'name', 'host'], 'search': {'name': target}, 'searchByAny': True, 'limit': 1})
        if hosts:
            host_id = hosts[0]['hostid']
            host_name = hosts[0].get('name') or hosts[0].get('host')

    if not host_id:
        return {"error": f"Host '{target}' no encontrado en Zabbix"}

    items_in = zabbix_query('item.get', {
        'hostids': [host_id],
        'output': ['itemid', 'name', 'key_', 'lastvalue'],
        'search': {'name': 'Bits received'},
        'limit': 150
    })
    items_out = zabbix_query('item.get', {
        'hostids': [host_id],
        'output': ['itemid', 'name', 'key_', 'lastvalue'],
        'search': {'name': 'Bits sent'},
        'limit': 150
    })

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
            "out_mbps": last_out_mbps
        })

    return {
        "host": host_name,
        "total": len(ifaces_list),
        "interfaces": ifaces_list
    }

def get_traffic_graph_with_stats(query_term="192.168.1.100", period_str="3h", iface_filter=""):
    query_term = str(query_term or '').strip()
    iface_filter = str(iface_filter or '').strip()

    # 1. Resolver interfaz y host automáticamente
    res_host_name, res_host_id, res_rx, res_tx = resolve_interface_and_host(query_term, iface_filter)

    in_item = res_rx
    out_item = res_tx
    host_id = res_host_id
    host_name = res_host_name or query_term

    # 2. Si no se resolvió por interfaz global, fallback a búsqueda tradicional por host
    if not host_id:
        es_ip = all(part.isdigit() for part in query_term.split('.')) and len(query_term.split('.')) == 4
        if es_ip:
            ifaces = zabbix_query('hostinterface.get', {'output': ['hostid'], 'filter': {'ip': query_term}})
            if ifaces: host_id = ifaces[0]['hostid']
        if not host_id:
            hosts = zabbix_query('host.get', {'output': ['hostid', 'name', 'host'], 'search': {'name': query_term}, 'searchByAny': True, 'limit': 1})
            if hosts:
                host_id = hosts[0]['hostid']
                host_name = hosts[0].get('name') or hosts[0].get('host')
        if not host_id:
            hosts = zabbix_query('host.get', {'output': ['hostid', 'name'], 'limit': 1})
            if hosts:
                host_id = hosts[0]['hostid']
                host_name = hosts[0]['name']

        in_candidates = zabbix_query('item.get', {
            'hostids': [host_id],
            'output': ['itemid', 'name', 'key_', 'units'],
            'search': {'name': 'Bits received'},
            'limit': 150
        })
        out_candidates = zabbix_query('item.get', {
            'hostids': [host_id],
            'output': ['itemid', 'name', 'key_', 'units'],
            'search': {'name': 'Bits sent'},
            'limit': 150
        })

        iface_clean = iface_filter.lower()
        if iface_clean:
            for it in in_candidates:
                if iface_clean in it['name'].lower() or iface_clean in it['key_'].lower():
                    in_item = it; break
            for it in out_candidates:
                if iface_clean in it['name'].lower() or iface_clean in it['key_'].lower():
                    out_item = it; break

        if not in_item and in_candidates: in_item = in_candidates[0]
        if not out_item and out_candidates: out_item = out_candidates[0]

    since_ts, now_ts, duration_sec, period_label, raw_period = parse_time_window(period_str)
    use_trends = duration_sec > (24 * 3600)

    hist_in = []
    hist_out = []

    # Ajustar límite de datos para periodos largos (hasta 2 años)
    limit_points = 2500 if duration_sec > (30 * 86400) else 1000

    if use_trends:
        if in_item:
            hist_in = zabbix_query('trend.get', {'itemids': [in_item['itemid']], 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points})
        if out_item:
            hist_out = zabbix_query('trend.get', {'itemids': [out_item['itemid']], 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points})
    else:
        if in_item:
            hist_in = zabbix_query('history.get', {'itemids': [in_item['itemid']], 'history': 3, 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points})
        if out_item:
            hist_out = zabbix_query('history.get', {'itemids': [out_item['itemid']], 'history': 3, 'time_from': since_ts, 'time_till': now_ts, 'sortfield': 'clock', 'sortorder': 'ASC', 'limit': limit_points})

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

    ax.set_title(f"Company NOC · {host_name} · {disp_iface} ({period_label})", fontsize=11, fontweight='bold', color='#F3F4F6', pad=12)
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

def get_traffic_graph_bytes(query_term="192.168.1.100", period_str="3h", iface_filter=""):
    img_bytes, _ = get_traffic_graph_with_stats(query_term, period_str, iface_filter)
    return img_bytes


def build_pdf_report(period_hours=24, host_target="", iface_filter=""):
    if host_target and not iface_filter:
        r_name, r_id, r_rx, r_tx = resolve_interface_and_host(host_target, "")
        if r_name and (r_rx or r_tx):
            iface_filter = host_target
            host_target = r_name

    since_ts, now_ts, duration_sec, period_label, raw_period = parse_time_window(period_hours)
    
    target_to_graph = host_target or ("MKMulticanal" if iface_filter else "")
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

        report_title = "Company NOC · REPORTE EJECUTIVO GENERAL"
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
    story.append(Paragraph("Company Telecomunicaciones · Centro de Control de Operaciones (NOC) · Documento Confidencial", subtitle_style))

    doc.build(story)
    buf.seek(0)
    return buf.read()

def get_dashboard_snapshot_bytes():
    hosts = zabbix_query('host.get', {'output': ['hostid', 'status'], 'selectInterfaces': ['available']})
    online = 0
    down = 0
    monitored = 0
    disabled = 0
    
    for h in hosts:
        if h.get('status') == '1':
            disabled += 1
        else:
            avail = h.get('interfaces', [{}])[0].get('available', '0') if h.get('interfaces') else '0'
            if avail == '1': online += 1
            elif avail == '2': down += 1
            else: monitored += 1
            
    problems = zabbix_query('problem.get', {'output': ['severity']})
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
    fig.suptitle(f"Company NOC - DASHBOARD EJECUTIVO EN TIEMPO REAL\n{now_str}", 
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
PROXMOX_USER = os.getenv('PROXMOX_USER', 'root@pam')
PROXMOX_PASS = os.getenv('PROXMOX_PASS', 'YOUR_PROXMOX_PASSWORD')

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

        nodes = [item for item in all_res if item.get('type') == 'node']
        vms = [item for item in all_res if item.get('type') in ['qemu', 'lxc']]

        target_clean = str(target or '').strip().lower()
        target_num = re.sub(r'[^0-9]', '', target_clean)
        if target_clean and target_clean not in ['all', 'cluster', 'proxmox', 'general']:
            matched_vms = []
            for v in vms:
                vmid = str(v.get('vmid', ''))
                vmname = str(v.get('name', '')).lower()
                if (target_clean == vmid) or (target_num and target_num == vmid) or (target_clean == vmname) or (len(target_clean) >= 3 and target_clean in vmname):
                    matched_vms.append(v)
            if matched_vms:
                v = matched_vms[0]
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
        target_num = re.sub(r'[^0-9]', '', target)
        matched_vm = None
        for v in vms:
            vmid_str = str(v.get('vmid', ''))
            vmname_str = str(v.get('name', '')).lower()
            if target == vmid_str or (target_num and target_num == vmid_str) or target == vmname_str or (len(target) >= 3 and target in vmname_str):
                matched_vm = v
                break

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

    ping_res = run_linux_ping(target, count=3)
    return {
        "tipo": "Dispositivo de Red",
        "target": target,
        "conectividad": "ONLINE (Responde)" if ping_res.get('alive') else "OFFLINE (Sin respuesta)",
        "latencia": f"{ping_res.get('rtt_avg_ms', 'N/D')} ms" if ping_res.get('rtt_avg_ms') else "N/D",
        "paquetes_perdidos": f"{ping_res.get('packet_loss_pct', 100)}%",
        "monitoreo_zabbix": "Verificable en plataforma Zabbix"
    }


# --- ZABBIX 20 REALTIME COLLECTOR ---
ZABBIX20_URL = 'http://192.168.1.100/zabbix/api_jsonrpc.php'
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

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ['/tv', '/oficina', '/visor']:
            try:
                html_path = '/home/Company/n8n/oficina_noc.html'
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

        if parsed.path == '/ping':
            try:
                qs = parse_qs(parsed.query)
                target = qs.get('target', ['192.168.1.100'])[0].strip()
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
                target = qs.get('target', ['MKMulticanal'])[0].strip()
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
                target = qs.get('target', ['MKMulticanal'])[0].strip()
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
        print(f'\n[PROXY POST] {self.path} | Body len: {len(body) if body else 0}', flush=True)
        req = urllib.request.Request(target_url, data=body if 'body' in locals() and body else None, headers=headers, method='POST')
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
    zabbix20_collector.start()
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), ProxyHandler)
    print(f"Proxy NOC escuchando en puerto {PORT}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
