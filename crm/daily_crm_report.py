#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reporte Diario Automatizado de CRM Odoo - Plantilla PLANIF INSTALACIONES
Asignación inteligente por cuadrillas y zonas geográficas.
Envío de Excel (.xlsx) + Imagen gráfica del cuadro (.png).
Destinatario principal: Coordinador de Despacho (+58 412-0000000)
Empresa: Telecom & ISP Solutions, C.A.
"""

import os
import sys
import json
import base64
import random
import urllib.request
from datetime import datetime
from collections import defaultdict
import psycopg2
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ==============================================================================
# CONFIGURACIÓN DE DESTINATARIOS
# ==============================================================================
DEST_CHAT_IDS = os.getenv("DEST_CHAT_IDS", "584XXXXXXXXX@c.us").split(",")

OPENWA_SESSION = os.getenv("OPENWA_SESSION_ID", "your_openwa_session_id")
OPENWA_BASE = f"http://127.0.0.1:2785/api/sessions/{OPENWA_SESSION}/messages"
API_KEY = os.getenv("OPENWA_API_KEY", "your_openwa_api_key")
LOG_FILE = "/var/log/daily_crm_dispatch.log"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# Saludos ejecutivos y profesionales rotativos
GREETING_TEMPLATES = [
    "Estimado Sr. un cordial saludo. Le hago entrega del reporte de planificación de instalaciones (Aprobados) correspondiente a la jornada de hoy.",
    "Buen día, Sr. Espero se encuentre muy bien. A continuación le comparto la distribución de instalaciones según plantilla y asignación por zonas.",
    "Muy buenos días, Sr. Por medio del presente le remito el cuadro y archivo detallado de planificación de instalaciones para su respectiva gestión.",
    "Estimado Sr. gusto en saludarle. Cumplo con adjuntarle el consolidado de prospectos aprobados con asignación de cuadrillas y ayudantes.",
    "Buen día, Sr. Le adjunto el cuadro gráfico y el archivo Excel de planificación de instalaciones (Fibra Óptica Comunitaria y Radio Frecuencia).",
    "Estimado Sr. reciba un cordial saludo. Le comparto el reporte diario de instalaciones aprobadas en sistema para las coordinaciones pertinentes.",
    "Muy buen día, Sr. Le hago llegar la planificación de instalaciones asignada a cuadrillas de la fecha. Quedamos atentos ante cualquier requerimiento.",
    "Estimado Sr. un placer saludarle. Le remito adjunto la planificación de instalaciones y su respectivo cuadro de seguimiento."
]

CLOSINGS = [
    "Quedo a su entera disposición ante cualquier duda o requerimiento.",
    "A su completa orden ante cualquier consulta operativa.",
    "Que tenga una excelente y productiva jornada laboral.",
    "Quedamos atentos a cualquier indicación de su parte.",
    "A su entera disposición. Saludos cordiales.",
    "Le deseamos un excelente día de trabajo."
]

def fetch_data():
    conn = psycopg2.connect(
        host=os.getenv("ODOO_PG_HOST", "192.168.1.70"),
        port=5432,
        user=os.getenv("ODOO_PG_USER", "odoo"),
        password=os.getenv("ODOO_PG_PASSWORD", "your_pg_password"),
        dbname=os.getenv("ODOO_PG_DB", "isp_database"),
        connect_timeout=10
    )
    cur = conn.cursor()
    
    sql = """
    SELECT 
        COALESCE(sub.code, '') AS "sub_code",
        l.id AS "lead_id",
        COALESCE(l.contact_name, p.name, '') AS "contact_name",
        CASE 
            WHEN l.id = 77567 OR l.medio_campaign = 'rf' THEN 'Radio Frecuencia'
            ELSE COALESCE(t.name, 'Fibra Óptica Comunitaria')
        END AS "team_name",
        COALESCE(l.x_studio_ubicacin, '') AS "ubicacion",
        COALESCE(z.name, '') AS "zona",
        COALESCE(l.phone, l.mobile, '') AS "phone",
        COALESCE(p.street, l.street, '') AS "address",
        s.name AS "stage_name",
        to_char(l.x_studio_fecha_y_hora_de_aprobacin, 'DD/MM/YYYY HH24:MI:SS') AS "aprob_date"
    FROM crm_lead l
    LEFT JOIN (
        SELECT DISTINCT ON (so.opportunity_id) so.opportunity_id, sub.code
        FROM sale_order so
        JOIN sale_order_line sol ON sol.order_id = so.id
        JOIN sale_subscription sub ON sol.subscription_id = sub.id
        WHERE sub.code IS NOT NULL
        ORDER BY so.opportunity_id, sub.id DESC
    ) sub ON sub.opportunity_id = l.id
    LEFT JOIN crm_team t ON l.team_id = t.id
    LEFT JOIN crm_stage s ON l.stage_id = s.id
    LEFT JOIN res_partner p ON l.partner_id = p.id
    LEFT JOIN zone z ON l.zone_id = z.id
    WHERE l.stage_id = 6 
      AND l.active = true 
      AND (l.company_id = 2 OR l.company_id IS NULL)
      AND (t.name = 'Fibra Óptica Comunitaria' OR l.id = 77567 OR l.medio_campaign = 'rf')
    ORDER BY "team_name", l.id DESC;
    """
    
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def assign_cuadrillas(rows):
    # Ayudantes rotativos basados en el día del año
    day_of_year = datetime.now().timetuple().tm_yday
    if day_of_year % 2 == 0:
        ayudante_mixta = "Luis"
        ayudante_fibra = "Torres"
    else:
        ayudante_mixta = "Torres"
        ayudante_fibra = "Luis"

    tecnicos_mixta = "Carlos, Roberto"
    tecnicos_fibra = "Alejandro, Daniel"

    # Agrupar clientes por ubicación principal
    zones = defaultdict(list)
    for r in rows:
        loc = r[4].strip() if r[4].strip() else (r[5].strip() if r[5].strip() else "Otras Zonas")
        zones[loc].append(r)

    assigned_rows = []
    
    # 1. Zonas con Radio Frecuencia van obligatoriamente a Cuadrilla Mixta
    remaining_zones = {}
    for loc, loc_rows in zones.items():
        has_rf = any(r[3] == 'Radio Frecuencia' for r in loc_rows)
        if has_rf:
            for r in loc_rows:
                assigned_rows.append(list(r) + ["Cuadrilla Mixta", tecnicos_mixta, ayudante_mixta])
        else:
            remaining_zones[loc] = loc_rows

    # Contar asignados hasta ahora
    count_mixta = sum(1 for r in assigned_rows if r[10] == "Cuadrilla Mixta")
    count_fibra = 0

    # 2. Asignar zonas restantes completas balanceando carga
    sorted_zones = sorted(remaining_zones.items(), key=lambda x: len(x[1]), reverse=True)
    for loc, loc_rows in sorted_zones:
        if count_mixta <= count_fibra:
            for r in loc_rows:
                assigned_rows.append(list(r) + ["Cuadrilla Mixta", tecnicos_mixta, ayudante_mixta])
            count_mixta += len(loc_rows)
        else:
            for r in loc_rows:
                assigned_rows.append(list(r) + ["Cuadrilla Solo Fibra", tecnicos_fibra, ayudante_fibra])
            count_fibra += len(loc_rows)

    # Ordenar por Cuadrilla y luego por ID
    assigned_rows.sort(key=lambda x: (x[10], x[1]), reverse=True)
    return assigned_rows, ayudante_mixta, ayudante_fibra, count_mixta, count_fibra

def generate_caption(total_records, count_mixta, count_fibra, ayudante_mixta, ayudante_fibra):
    greeting = random.choice(GREETING_TEMPLATES)
    closing = random.choice(CLOSINGS)
    now_str = datetime.now().strftime("%d/%m/%Y")
    
    caption = (
        f"{greeting}\n\n"
        f"📋 *Planificación de Instalaciones ({now_str}):*\n"
        f"• *Total Aprobados:* {total_records} instalaciones\n\n"
        f"👷‍♂️ *Distribución de Cuadrillas por Zona:*\n"
        f"🔴 *Cuadrilla Mixta ({count_mixta} clientes):*\n"
        f"  • Técnicos: Carlos Mendoza, Roberto Silva\n"
        f"  • Ayudante: {ayudante_mixta}\n\n"
        f"🔵 *Cuadrilla Solo Fibra ({count_fibra} clientes):*\n"
        f"  • Técnicos: Alejandro Gomez, Daniel Herrera\n"
        f"  • Ayudante: {ayudante_fibra}\n\n"
        f"📸 *Se adjunta imagen gráfica resumen y archivo Excel formal.*\n\n"
        f"{closing}"
    )
    return caption

def build_excel(rows, output_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PLANIF INSTALACIONES"

    date_str = datetime.now().strftime("%d/%m/%Y")
    
    # Title Banner
    ws.merge_cells("A1:M1")
    title_cell = ws["A1"]
    title_cell.value = f"INVERSIONES. - PLANIFICACIÓN DE INSTALACIONES ({date_str})"
    title_cell.font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    # Subtitle
    ws.merge_cells("A2:M2")
    sub_cell = ws["A2"]
    sub_cell.value = f"Plantilla Odoo: PLANIF INSTALACIONES | Cuadrillas Asignadas por Zonas | Generado: {datetime.now().strftime('%d/%m/%Y %I:%M %p')}"
    sub_cell.font = Font(name="Calibri", size=10, italic=True, color="555555")
    sub_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 20

    # 10 headers de Odoo + 3 de cuadrilla
    headers = [
        "Pedidos/Subscription/Referencia",
        "ID",
        "Nombre del contacto",
        "Equipo de ventas",
        "Ubicación",
        "Zona",
        "Teléfono",
        "Cliente/Dirección completa",
        "Etapa",
        "Fecha y Hora de Aprobación",
        "Cuadrilla Asignada",
        "Técnicos Asignados",
        "Ayudante Asignado"
    ]

    header_fill = PatternFill(start_color="2A52BE", end_color="2A52BE", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style='thin', color='DDDDDD'),
        right=Side(style='thin', color='DDDDDD'),
        top=Side(style='thin', color='DDDDDD'),
        bottom=Side(style='thin', color='DDDDDD')
    )

    ws.row_dimensions[4].height = 26
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_idx, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    zebra_fill = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    for r_idx, row in enumerate(rows, 5):
        fill = zebra_fill if r_idx % 2 == 0 else white_fill
        ws.row_dimensions[r_idx].height = 22
        cuadrilla = row[10]

        for c_idx, val in enumerate(row, 1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.fill = fill
            cell.border = thin_border
            
            if c_idx in (1, 2, 6, 7, 9, 10):
                cell.alignment = Alignment(horizontal="center")
            elif c_idx == 4:
                cell.alignment = Alignment(horizontal="center")
                if val == "Fibra Óptica Comunitaria":
                    cell.font = Font(name="Calibri", bold=True, color="0070C0")
                elif val == "Radio Frecuencia":
                    cell.font = Font(name="Calibri", bold=True, color="C00000")
            elif c_idx == 11:
                cell.alignment = Alignment(horizontal="center")
                if cuadrilla == "Cuadrilla Mixta":
                    cell.font = Font(name="Calibri", bold=True, color="B71C1C")
                else:
                    cell.font = Font(name="Calibri", bold=True, color="0D47A1")
            elif c_idx in (12, 13):
                cell.alignment = Alignment(horizontal="center")

    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.row > 2 and cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = max(max_len + 4, 13)

    wb.save(output_path)

def build_png_table(rows, output_path):
    table_data = []
    for r in rows:
        sub_code = r[0]
        lead_id = str(r[1])
        cliente = r[2][:22] # Truncate for clean table
        tech = "Radio Frec." if r[3] == "Radio Frecuencia" else "Fibra Óptica"
        
        # Format location
        loc = r[4]
        if r[5] and r[5] not in loc:
            loc += f" ({r[5]})"
        loc = loc[:32]
        
        cuad_short = "Mixta" if "Mixta" in r[10] else "Solo Fibra"
        tecs_short = "Carlos M. / Roberto S." if "Carlos" in r[11] else "Alejandro G. / Daniel H."
        ayudante = r[12]
        
        table_data.append([sub_code, lead_id, cliente, tech, loc, cuad_short, tecs_short, ayudante])

    headers = ["Suscripción", "ID", "Cliente", "Tecnología", "Ubicación / Sector", "Cuadrilla", "Técnicos", "Ayudante"]

    fig_height = max(len(table_data) * 0.45 + 2.5, 7.5)
    fig, ax = plt.subplots(figsize=(15, fig_height), dpi=220)
    ax.axis('off')

    date_str = datetime.now().strftime("%d/%m/%Y")
    fig.text(0.5, 0.96, f"TELECOM & ISP SOLUTIONS, C.A. - PLANIFICACIÓN DE INSTALACIONES ({date_str})", 
             ha='center', va='center', fontsize=14, weight='bold', color='#1B365D')
    fig.text(0.5, 0.93, "Distribución de Clientes por Zona Geográfica y Cuadrilla Operativa", 
             ha='center', va='center', fontsize=10, style='italic', color='#555555')

    col_widths = [0.10, 0.07, 0.16, 0.11, 0.22, 0.11, 0.13, 0.10]
    table = ax.table(cellText=table_data, colLabels=headers, colWidths=col_widths, loc='center', cellLoc='left')

    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    table.scale(1.0, 1.6)

    for j in range(len(headers)):
        cell = table[(0, j)]
        cell.set_facecolor('#1B365D')
        cell.set_text_props(color='white', weight='bold', ha='center')

    for i, row in enumerate(table_data):
        cuadrilla = row[5]
        row_idx = i + 1
        base_color = '#F8F9FA' if i % 2 == 0 else '#FFFFFF'
        
        for j in range(len(headers)):
            c = table[(row_idx, j)]
            c.set_facecolor(base_color)
            c.set_edgecolor('#E0E0E0')
            
            if j in (0, 1, 3, 5):
                c.set_text_props(ha='center')
            
            if j == 5:
                if cuadrilla == "Mixta":
                    c.set_text_props(color='#B71C1C', weight='bold')
                    c.set_facecolor('#FFEBEE')
                else:
                    c.set_text_props(color='#0D47A1', weight='bold')
                    c.set_facecolor('#E3F2FD')

    plt.tight_layout(rect=[0.01, 0.02, 0.99, 0.92])
    plt.savefig(output_path, dpi=220, bbox_inches='tight', facecolor='white')
    plt.close()

def send_image_to_whatsapp(image_path, caption, chat_id):
    with open(image_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "chatId": chat_id,
        "mimetype": "image/png",
        "base64": b64_data,
        "caption": caption
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": API_KEY
    }

    url = f"{OPENWA_BASE}/send-image"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def send_document_to_whatsapp(file_path, caption, chat_id):
    with open(file_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    filename = os.path.basename(file_path)
    payload = {
        "chatId": chat_id,
        "filename": filename,
        "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "base64": b64_data,
        "caption": caption
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": API_KEY
    }

    url = f"{OPENWA_BASE}/send-document"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    log("=== INICIO DE GENERACION REPORTE CRM (PLANIF INSTALACIONES) ===")
    try:
        raw_rows = fetch_data()
        log(f"Datos obtenidos de Odoo: {len(raw_rows)} registros aprobados.")
        
        assigned_rows, ayudante_mixta, ayudante_fibra, count_mixta, count_fibra = assign_cuadrillas(raw_rows)
        log(f"Asignación completada: Mixta={count_mixta} (Ayudante: {ayudante_mixta}), Solo Fibra={count_fibra} (Ayudante: {ayudante_fibra}).")

        date_file = datetime.now().strftime("%Y-%m-%d")
        xlsx_path = f"/tmp/Planif_Instalaciones_{date_file}.xlsx"
        png_path = f"/tmp/Planif_Instalaciones_{date_file}.png"

        # 1. Generar Excel
        build_excel(assigned_rows, xlsx_path)
        log(f"Excel generado en {xlsx_path}.")

        # 2. Generar Imagen Gráfica
        build_png_table(assigned_rows, png_path)
        log(f"Imagen gráfica generada en {png_path}.")

        # 3. Caption profesional
        caption = generate_caption(len(assigned_rows), count_mixta, count_fibra, ayudante_mixta, ayudante_fibra)

        for chat_id in DEST_CHAT_IDS:
            log(f"Enviando imagen gráfica por WhatsApp a {chat_id}...")
            resp_img = send_image_to_whatsapp(png_path, caption, chat_id)
            log(f"IMAGEN ENVIADA! Message ID: {resp_img.get('messageId')}")

            log(f"Enviando archivo Excel formal a {chat_id}...")
            resp_doc = send_document_to_whatsapp(xlsx_path, f"📎 Archivo Excel formal: Planif_Instalaciones_{date_file}.xlsx", chat_id)
            log(f"EXCEL ENVIADO! Message ID: {resp_doc.get('messageId')}")

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
