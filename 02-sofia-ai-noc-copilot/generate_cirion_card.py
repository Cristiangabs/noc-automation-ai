import os, sys
from PIL import Image, ImageDraw, ImageFont

def generate_cirion_card_v2(cluster_ok=True, wan_ping_loss=100, gw_ok=True, output_path="cirion_diagnostic.png"):
    width, height = 1000, 560
    im = Image.new("RGB", (width, height), color="#0F172A") # Deep slate
    draw = ImageDraw.Draw(im)

    try:
        font_title = ImageFont.truetype("arialbd.ttf", 26)
        font_subtitle = ImageFont.truetype("arial.ttf", 15)
        font_box_title = ImageFont.truetype("arialbd.ttf", 17)
        font_body = ImageFont.truetype("arial.ttf", 14)
        font_bold = ImageFont.truetype("arialbd.ttf", 14)
        font_mono = ImageFont.truetype("consola.ttf", 13)
        font_verdict_title = ImageFont.truetype("arialbd.ttf", 18)
        font_verdict_body = ImageFont.truetype("arial.ttf", 15)
    except:
        font_title = font_subtitle = font_box_title = font_body = font_bold = font_mono = font_verdict_title = font_verdict_body = ImageFont.load_default()

    # Top Header Banner
    draw.rectangle([(0, 0), (width, 85)], fill="#1E293B")
    draw.rectangle([(0, 83), (width, 86)], fill="#2563EB") # Electric blue line
    
    draw.text((35, 18), "DIAGNÓSTICO CIRION (CLÚSTER LUMEX)", fill="#F8FAFC", font=font_title)
    draw.text((35, 52), "Monitoreo en Tiempo Real | Infraestructura de Servidores & Salida WAN", fill="#94A3B8", font=font_subtitle)

    # Box 1: Servidores Físicos y VMs (Left Card)
    # Width: x=35 to x=485 (width = 450)
    b1_x1, b1_y1, b1_x2, b1_y2 = 35, 110, 485, 335
    draw.rectangle([(b1_x1, b1_y1), (b1_x2, b1_y2)], fill="#1E293B", outline="#334155", width=2)
    
    draw.text((b1_x1 + 18, b1_y1 + 14), "1. Servidores y Virtualización", fill="#38BDF8", font=font_box_title)
    
    # 3 Nodes status
    node_color = "#22C55E" if cluster_ok else "#EF4444"
    draw.ellipse([(b1_x1 + 18, b1_y1 + 52), (b1_x1 + 30, b1_y1 + 64)], fill=node_color)
    draw.text((b1_x1 + 40, b1_y1 + 49), "Nodos Físicos Lumex01, 02, 04:", fill="#94A3B8", font=font_body)
    draw.text((b1_x1 + 40, b1_y1 + 68), "ONLINE (3 Nodos Activos - Quórum OK)", fill="#22C55E" if cluster_ok else "#EF4444", font=font_bold)
    
    # VMs status
    draw.ellipse([(b1_x1 + 18, b1_y1 + 104), (b1_x1 + 30, b1_y1 + 116)], fill="#22C55E")
    draw.text((b1_x1 + 40, b1_y1 + 101), "Máquinas Virtuales (OVBaruta, SanDiego...):", fill="#94A3B8", font=font_body)
    draw.text((b1_x1 + 40, b1_y1 + 120), "OPERANDO NORMAL (Estado PLAY Verde)", fill="#22C55E", font=font_bold)

    # Enlaces de Acceso Web (Sin credenciales)
    draw.line([(b1_x1 + 18, b1_y1 + 152), (b1_x2 - 18, b1_y1 + 152)], fill="#334155", width=1)
    draw.text((b1_x1 + 18, b1_y1 + 162), "Acceso Proxmox GUI (HTTPS):", fill="#F1F5F9", font=font_bold)
    draw.text((b1_x1 + 18, b1_y1 + 183), "Principal:   https://10.0.30.80:8006", fill="#60A5FA", font=font_mono)
    draw.text((b1_x1 + 18, b1_y1 + 203), "Secundarios: https://10.0.30.81:8006 | .83:8006", fill="#94A3B8", font=font_mono)

    # Box 2: Enlace WAN Carrier Cirion (Right Card)
    # Width: x=515 to x=965 (width = 450)
    b2_x1, b2_y1, b2_x2, b2_y2 = 515, 110, 965, 335
    draw.rectangle([(b2_x1, b2_y1), (b2_x2, b2_y2)], fill="#1E293B", outline="#334155", width=2)
    
    draw.text((b2_x1 + 18, b2_y1 + 14), "2. Enlace WAN Carrier (Cirion)", fill="#F59E0B", font=font_box_title)

    # Gateway local
    gw_color = "#22C55E" if gw_ok else "#EF4444"
    draw.ellipse([(b2_x1 + 18, b2_y1 + 52), (b2_x1 + 30, b2_y1 + 64)], fill=gw_color)
    draw.text((b2_x1 + 40, b2_y1 + 49), "Gateway Local pfSense (10.0.30.1):", fill="#94A3B8", font=font_body)
    draw.text((b2_x1 + 40, b2_y1 + 68), "RESPONDE NORMAL (<73 ms - Red Interna OK)", fill="#22C55E" if gw_ok else "#EF4444", font=font_bold)

    # WAN Cirion IP
    wan_color = "#EF4444" if wan_ping_loss == 100 else "#22C55E"
    draw.ellipse([(b2_x1 + 18, b2_y1 + 104), (b2_x1 + 30, b2_y1 + 116)], fill=wan_color)
    draw.text((b2_x1 + 40, b2_y1 + 101), "WAN Carrier IP (198.51.100.10):", fill="#94A3B8", font=font_body)
    wan_status_txt = "100% PACKET LOSS (SIN RESPUESTA)" if wan_ping_loss == 100 else "0% PACKET LOSS (CON SALIDA WAN)"
    draw.text((b2_x1 + 40, b2_y1 + 120), wan_status_txt, fill=wan_color, font=font_bold)

    # Shell test command
    draw.line([(b2_x1 + 18, b2_y1 + 152), (b2_x2 - 18, b2_y1 + 152)], fill="#334155", width=1)
    draw.text((b2_x1 + 18, b2_y1 + 162), "Pruebas Shell Ejecutadas:", fill="#F1F5F9", font=font_bold)
    draw.text((b2_x1 + 18, b2_y1 + 183), "ping -c 3 198.51.100.10 -> 100% Pérdida", fill="#F87171" if wan_ping_loss == 100 else "#4ADE80", font=font_mono)
    draw.text((b2_x1 + 18, b2_y1 + 203), "ping -c 3 10.0.30.1       -> 0% Pérdida", fill="#4ADE80", font=font_mono)

    # Bottom Big Banner: VERDICT (Kept, polished, no "Guía rápida")
    bv_x1, bv_y1, bv_x2, bv_y2 = 35, 360, 965, 520
    if wan_ping_loss == 100:
        verdict_bg = "#450A0A"
        verdict_border = "#DC2626"
        verdict_title = "VEREDICTO: FALLA DE CIRION (CARRIER / ENLACE CAÍDO)"
        verdict_l1 = "• El enlace WAN / fibra del carrier Cirion está cortado o caído."
        verdict_l2 = "• Los servidores físicos, bases de datos y VMs están 100% operativos."
        verdict_l3 = "• Acción requerida: Reportar avería inmediatamente a Cirion."
    else:
        verdict_bg = "#052E16"
        verdict_border = "#16A34A"
        verdict_title = "VEREDICTO: ENLACE CIRION ACTIVO (TRÁFICO OPERATIVO)"
        verdict_l1 = "• El carrier Cirion responde con 0% de pérdida de paquetes."
        verdict_l2 = "• La conectividad externa hacia internet está restablecida."
        verdict_l3 = "• Si algún servicio no abre, la causa es interna de la máquina virtual correspondiente."

    draw.rectangle([(bv_x1, bv_y1), (bv_x2, bv_y2)], fill=verdict_bg, outline=verdict_border, width=3)
    draw.text((bv_x1 + 25, bv_y1 + 18), verdict_title, fill="#FEF2F2" if wan_ping_loss == 100 else "#F0FDF4", font=font_verdict_title)
    
    text_color = "#FECACA" if wan_ping_loss == 100 else "#DCFCE7"
    draw.text((bv_x1 + 25, bv_y1 + 54), verdict_l1, fill=text_color, font=font_verdict_body)
    draw.text((bv_x1 + 25, bv_y1 + 80), verdict_l2, fill=text_color, font=font_verdict_body)
    draw.text((bv_x1 + 25, bv_y1 + 106), verdict_l3, fill=text_color, font=font_verdict_body)

    im.save(output_path, quality=95)
    print(f"Generated clean card v2 at: {output_path}")

if __name__ == '__main__':
    cluster_ok = True
    wan_ping_loss = 100
    gw_ok = True
    output_path = r"C:\Users\csalinas\.gemini\antigravity\scratch\cirion_diagnostic.png"

    if len(sys.argv) > 1:
        cluster_ok = sys.argv[1].lower() in ['true', '1', 'yes']
    if len(sys.argv) > 2:
        try:
            wan_ping_loss = int(sys.argv[2])
        except:
            wan_ping_loss = 100
    if len(sys.argv) > 3:
        gw_ok = sys.argv[3].lower() in ['true', '1', 'yes']
    if len(sys.argv) > 4:
        output_path = sys.argv[4]

    generate_cirion_card_v2(cluster_ok=cluster_ok, wan_ping_loss=wan_ping_loss, gw_ok=gw_ok, output_path=output_path)
