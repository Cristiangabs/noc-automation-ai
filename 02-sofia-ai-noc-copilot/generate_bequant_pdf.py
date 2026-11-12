#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_bequant_pdf.py
Generador automático de reporte profesional en PDF de Sobreconsumo de Conexiones
con telemetría y gráficos fieles a Bequant BQN (Maximum Subscriber Speed Over Time).
"""

import os
import sys
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import matplotlib.dates as mdates
from datetime import datetime

def parse_time_series(raw_text):
    if not raw_text or not isinstance(raw_text, str):
        return [], [], []
    lines = raw_text.strip().split('\n')
    times = []
    val1 = []
    val2 = []
    for l in lines:
        parts = l.strip().split()
        if len(parts) >= 3:
            try:
                t = datetime.fromisoformat(parts[0])
                v1 = float(parts[1]) if parts[1] != 'n/a' else 0.0
                v2 = float(parts[2]) if parts[2] != 'n/a' else 0.0
                times.append(t)
                val1.append(v1)
                val2.append(v2)
            except Exception:
                continue
    return times, val1, val2

def generate_pdf(data_json_path, output_pdf_path):
    with open(data_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    timestamp_str = data.get('timestamp', datetime.now().strftime('%d/%m/%Y %H:%M:%S'))
    is_monthly = data.get('isMonthly', True)
    month_title = data.get('monthTitle', 'Octubre 2026')
    total_monitored = data.get('totalMonitored', 0)
    subscribers = data.get('subscribers', [])
    over_count = len(subscribers)
    over_total_count = data.get('overTotalCount', over_count)
    single_chart_mode = data.get('singleChartMode', True)

    with PdfPages(output_pdf_path) as pdf:
        # =====================================================================
        # PÁGINA 1: RESUMEN Y TABLA CONSOLIDADA DE SOBRECONSUMO (NOC MULTICANAL)
        # =====================================================================
        ROWS_PER_PAGE = 26
        total_pages_table = max(1, (over_count + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)

        for page_idx in range(total_pages_table):
            fig = plt.figure(figsize=(11, 8.5), dpi=100)
            fig.patch.set_facecolor('#ffffff')

            # Encabezado institucional
            fig.text(0.08, 0.94, "MULTICANAL — CENTRO DE OPERACIONES DE RED (NOC)",
                     fontsize=12, fontweight='bold', color='#1e293b')
            
            report_heading = f"REPORTE MENSUAL DE SOBRECONSUMO DE CONEXIONES — BEQUANT BQN ({month_title.upper()})" if is_monthly else "REPORTE DE SOBRECONSUMO DE CONEXIONES (BEQUANT BQN)"
            fig.text(0.08, 0.91, report_heading,
                     fontsize=13.5, fontweight='bold', color='#0284c7')

            # Banner de métricas
            period_label = f"Mes Consultado: {month_title}" if is_monthly else f"Fecha/Hora: {timestamp_str}"
            if over_total_count > over_count:
                over_label = f"Sobreconsumo (≥ 500 flujos): {over_total_count} (Top {over_count} detallados)"
            else:
                over_label = f"Abonados con Sobreconsumo (≥ 500 flujos): {over_count}"

            meta_box = (
                f"{period_label}    |    Abonados Monitoreados: {total_monitored:,}    |    "
                f"{over_label}    |    Página {page_idx + 1}/{total_pages_table}"
            )
            fig.text(0.08, 0.855, meta_box, fontsize=10, color='#334155',
                     bbox=dict(boxstyle='round,pad=0.5', facecolor='#f1f5f9', edgecolor='#cbd5e1'))

            # Tabla
            start_i = page_idx * ROWS_PER_PAGE
            end_i = min(over_count, start_i + ROWS_PER_PAGE)
            slice_subs = subscribers[start_i:end_i]

            col_labels = ["#", "IP", "SUBSCRIBER-ID", "NODO", "Plan", "FLOWS"]
            table_rows = []
            for idx, s in enumerate(slice_subs, start=start_i + 1):
                clean_name = str(s.get('subscriberId', 'N/A')).replace('_', ' ')
                clean_name = clean_name.encode('utf-8', 'ignore').decode('utf-8')
                if len(clean_name) > 34:
                    clean_name = clean_name[:32] + '..'
                clean_plan = str(s.get('policyRate', 'N/A')).replace('_', ' ')
                table_rows.append([
                    str(idx),
                    str(s.get('ip', 'N/A')),
                    clean_name,
                    str(s.get('nodo', '')),
                    clean_plan,
                    f"{int(s.get('flows', 0)):,}"
                ])

            ax_tab = fig.add_axes([0.08, 0.08, 0.84, 0.74])
            ax_tab.axis('off')
            tab = ax_tab.table(cellText=table_rows, colLabels=col_labels, loc='center',
                               colWidths=[0.05, 0.16, 0.38, 0.10, 0.20, 0.11])
            tab.auto_set_font_size(False)
            tab.set_fontsize(8.5)
            tab.scale(1.0, 1.45)

            for (r, c), cell in tab.get_celld().items():
                cell.set_edgecolor('#cbd5e1')
                if r == 0:
                    cell.set_facecolor('#0f172a')
                    cell.set_text_props(color='white', fontweight='bold')
                else:
                    if r % 2 == 0:
                        cell.set_facecolor('#f8fafc')
                    else:
                        cell.set_facecolor('#ffffff')
                    if c == 5:
                        cell.set_text_props(fontweight='bold', color='#7c3aed')

            pdf.savefig(fig)
            plt.close(fig)

        # =====================================================================
        # PÁGINAS INDIVIDUALES: TELEMETRÍA EXACTA BEQUANT BQN
        # =====================================================================
        for idx, s in enumerate(subscribers, start=1):
            fig = plt.figure(figsize=(11, 8.5), dpi=100)
            fig.patch.set_facecolor('#0f172a')  # Dark theme NOC

            # Parsear series de max-speed (parts[1]=UP, parts[2]=DOWN)
            t_speed, up_speed, down_speed = parse_time_series(s.get('speedRaw', ''))
            t_flows, fl_created, fl_active = parse_time_series(s.get('flowsRaw', ''))

            ip = s.get('ip', 'N/A')
            raw_id = str(s.get('subscriberId', 'N/A')).replace('_', ' ')
            raw_id = raw_id.encode('utf-8', 'ignore').decode('utf-8')
            if len(raw_id) > 42:
                raw_id = raw_id[:40] + '..'
            nodo = s.get('nodo', '')
            plan = str(s.get('policyRate', 'N/A')).replace('_', ' ')
            flows = int(s.get('flows', 0))

            max_down = max(down_speed) if down_speed else 0.0
            max_up = max(up_speed) if up_speed else 0.0
            max_fl = max(fl_active) if fl_active else flows
            avg_fl = int(sum(fl_active) / len(fl_active)) if fl_active else flows

            # Encabezado superior
            fig.text(0.08, 0.94, f"MULTICANAL — CENTRO DE OPERACIONES DE RED (NOC) — BEQUANT BQN",
                     fontsize=10.5, fontweight='bold', color='#94a3b8')
            
            client_title = f"FICHA DE ABONADO #{idx}: {raw_id} ({ip})"
            fig.text(0.08, 0.905, client_title, fontsize=13, fontweight='bold', color='#38bdf8')

            # Banner de datos del abonado
            nodo_str = f"   |   NODO: {nodo}" if nodo else ""
            client_banner = (
                f"Plan: {plan}{nodo_str}   |   Pico Down: {max_down:.1f} Mbps   |   Pico Up: {max_up:.1f} Mbps\n"
                f"Pico Flujos: {int(max_fl):,}   |   Promedio Flujos: {avg_fl:,}   |   Período: {month_title}"
            )
            fig.text(0.08, 0.835, client_banner, fontsize=9.5, color='#e2e8f0',
                     bbox=dict(boxstyle='round,pad=0.6', facecolor='#1e293b', edgecolor='#334155'))

            if single_chart_mode:
                # -------------------------------------------------------------
                # MODO 1: GRÁFICA ÚNICA "Maximum Subscriber Speed Over Time"
                # Ocupa todo el espacio con máxima resolución y detalle Bequant
                # -------------------------------------------------------------
                fig.text(0.08, 0.80, "Maximum Subscriber Speed Over Time",
                         fontsize=11.5, fontweight='bold', color='#ffffff')

                ax1 = fig.add_axes([0.08, 0.10, 0.84, 0.64])
                ax1.set_facecolor('#14171d')
                ax1.grid(True, linestyle='-', color='#282d37', linewidth=0.8, alpha=0.9)

                if t_speed:
                    ax1.plot(t_speed, down_speed, label='Down-Mbps', color='#0090ff', linewidth=1.7)
                    ax1.plot(t_speed, up_speed, label='Up-Mbps', color='#f59e0b', linewidth=1.4)
                    ax1.set_ylabel("Mbps", fontsize=9.5, color='#8892b0', labelpad=8)
                    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
                    ax1.xaxis.set_major_locator(mdates.DayLocator(interval=4))
                    leg = ax1.legend(loc='lower left', bbox_to_anchor=(0.0, 1.02), ncol=2, frameon=False, fontsize=10, handlelength=1.2)
                    for text in leg.get_texts():
                        text.set_color('#cbd5e1')
                else:
                    ax1.text(0.5, 0.5, "Sin telemetría histórica de velocidad máxima en el período", ha='center', va='center', color='#64748b', fontsize=11)

                for spine in ax1.spines.values():
                    spine.set_color('#2e3440')
                    spine.set_linewidth(1.0)
                ax1.tick_params(colors='#8892b0', which='both', labelsize=8.5)

            else:
                # -------------------------------------------------------------
                # MODO 2: DOBLE GRÁFICA (Max Speed arriba + Flows abajo)
                # -------------------------------------------------------------
                # Gráfica 1: Maximum Subscriber Speed Over Time
                fig.text(0.08, 0.80, "Maximum Subscriber Speed Over Time",
                         fontsize=10.5, fontweight='bold', color='#ffffff')

                ax1 = fig.add_axes([0.08, 0.48, 0.84, 0.28])
                ax1.set_facecolor('#14171d')
                ax1.grid(True, linestyle='-', color='#282d37', linewidth=0.8, alpha=0.9)

                if t_speed:
                    ax1.plot(t_speed, down_speed, label='Down-Mbps', color='#0090ff', linewidth=1.6)
                    ax1.plot(t_speed, up_speed, label='Up-Mbps', color='#f59e0b', linewidth=1.3)
                    ax1.set_ylabel("Mbps", fontsize=8.5, color='#8892b0')
                    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
                    ax1.xaxis.set_major_locator(mdates.DayLocator(interval=5))
                    leg = ax1.legend(loc='lower left', bbox_to_anchor=(0.0, 1.02), ncol=2, frameon=False, fontsize=9, handlelength=1.2)
                    for text in leg.get_texts():
                        text.set_color('#cbd5e1')
                else:
                    ax1.text(0.5, 0.5, "Sin telemetría histórica en el período", ha='center', va='center', color='#64748b')

                for spine in ax1.spines.values():
                    spine.set_color('#2e3440')
                ax1.tick_params(colors='#8892b0', which='both', labelsize=8)

                # Gráfica 2: Flows Over Time
                fig.text(0.08, 0.42, "Flows Over Time (Conexiones Concurrentes)",
                         fontsize=10.5, fontweight='bold', color='#ffffff')

                ax2 = fig.add_axes([0.08, 0.09, 0.84, 0.28])
                ax2.set_facecolor('#14171d')
                ax2.grid(True, linestyle='-', color='#282d37', linewidth=0.8, alpha=0.9)

                if t_flows:
                    ax2.plot(t_flows, fl_active, label='Fl-Active (Conexiones Activas)', color='#a855f7', linewidth=1.6)
                    ax2.plot(t_flows, fl_created, label='Fl-Created-per-minute', color='#10b981', linewidth=1.1, linestyle=':')
                    ax2.set_ylabel("Flows", fontsize=8.5, color='#8892b0')
                    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
                    ax2.xaxis.set_major_locator(mdates.DayLocator(interval=5))
                    leg2 = ax2.legend(loc='lower left', bbox_to_anchor=(0.0, 1.02), ncol=2, frameon=False, fontsize=9, handlelength=1.2)
                    for text in leg2.get_texts():
                        text.set_color('#cbd5e1')
                else:
                    ax2.text(0.5, 0.5, "Sin telemetría de flujos en el período", ha='center', va='center', color='#64748b')

                for spine in ax2.spines.values():
                    spine.set_color('#2e3440')
                ax2.tick_params(colors='#8892b0', which='both', labelsize=8)

            pdf.savefig(fig)
            plt.close(fig)

    print(f"SUCCESS: PDF generated at {output_pdf_path} (Total pages: {total_pages_table + over_count})")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python3 generate_bequant_pdf.py <input_data.json> <output.pdf>")
        sys.exit(1)
    generate_pdf(sys.argv[1], sys.argv[2])
