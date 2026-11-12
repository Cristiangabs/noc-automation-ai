#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WAN Carrier & Datacenter Cluster Health Checker
Performs concurrent ICMP, TCP (Proxmox VE 8006), and HTTPS probes across
primary/backup carrier links, VPN tunnels, and hypervisor nodes to determine
automated root-cause attribution (Carrier vs. Internal vs. Datacenter).
"""

import os
import subprocess
import socket
import urllib.request
import ssl
import time
import json
from concurrent.futures import ThreadPoolExecutor

WAN_PRIMARY_IP = os.getenv("WAN_PRIMARY_IP", "198.51.100.10")
WAN_BACKUP_IP = os.getenv("WAN_BACKUP_IP", "203.0.113.10")
ROUTER_LOCAL_IP = os.getenv("ROUTER_LOCAL_IP", "10.0.10.1")
TUNNEL_VPN_IP = os.getenv("TUNNEL_VPN_IP", "10.99.0.1")
NODE_01_IP = os.getenv("PROXMOX_NODE_01", "10.0.30.80")
NODE_02_IP = os.getenv("PROXMOX_NODE_02", "10.0.30.81")
NODE_03_IP = os.getenv("PROXMOX_NODE_03", "10.0.30.83")

def check_ping(ip):
    try:
        t0 = time.time()
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2
        )
        rtt = round((time.time() - t0) * 1000, 1)
        if r.returncode == 0:
            for l in r.stdout.splitlines():
                if "time=" in l:
                    rtt = float(l.split("time=")[1].split(" ")[0].replace("ms", ""))
            return {"up": True, "rtt": rtt}
        return {"up": False, "rtt": 0}
    except Exception:
        return {"up": False, "rtt": 0}

def check_tcp(ip, port):
    t0 = time.time()
    try:
        s = socket.socket()
        s.settimeout(1.0)
        res = s.connect_ex((ip, port))
        s.close()
        rtt = round((time.time() - t0) * 1000, 1)
        return {"up": (res == 0), "rtt": rtt}
    except Exception:
        return {"up": False, "rtt": 0}

def check_web(url):
    t0 = time.time()
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers={"User-Agent": "NOC-Carrier-Monitor/2.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=2.5) as r:
            rtt = round((time.time() - t0) * 1000, 1)
            return {"up": True, "code": r.getcode(), "rtt": rtt, "msg": "HTTP 200 OK"}
    except urllib.error.HTTPError as e:
        rtt = round((time.time() - t0) * 1000, 1)
        return {"up": False, "code": e.code, "rtt": rtt, "msg": f"HTTP {e.code}"}
    except Exception:
        rtt = round((time.time() - t0) * 1000, 1)
        return {"up": False, "code": 0, "rtt": rtt, "msg": "Timeout / No Route"}

def get_carrier_telemetry():
    targets = {
        "wan_primary": lambda: check_ping(WAN_PRIMARY_IP),
        "wan_backup": lambda: check_ping(WAN_BACKUP_IP),
        "router_local": lambda: check_ping(ROUTER_LOCAL_IP),
        "tunnel_vpn": lambda: check_ping(TUNNEL_VPN_IP),
        "node_pve01": lambda: check_tcp(NODE_01_IP, 8006),
        "node_pve02": lambda: check_tcp(NODE_02_IP, 8006),
        "node_pve03": lambda: check_tcp(NODE_03_IP, 8006),
        "web_portal_alpha": lambda: check_web("https://portal-alpha.example.gov"),
        "web_portal_beta": lambda: check_web("https://portal-beta.example.gov"),
        "web_portal_gamma": lambda: check_web("https://portal-gamma.example.gov"),
        "web_portal_delta": lambda: check_web("https://portal-delta.example.gov"),
    }

    res = {}
    with ThreadPoolExecutor(max_workers=11) as ex:
        futures = {k: ex.submit(v) for k, v in targets.items()}
        for k, f in futures.items():
            res[k] = f.result()

    # Automated Root-Cause Attribution
    c_up = res["wan_primary"]["up"]
    r_up = res["wan_backup"]["up"]
    t_up = res["tunnel_vpn"]["up"]
    n_up = res["node_pve01"]["up"] or res["node_pve02"]["up"]

    if not c_up and not r_up:
        diag = {
            "status": "critical",
            "title": "CAIDA TOTAL DE DATACENTER EXTERNO",
            "blame": "DATACENTER / ENERGIA",
            "badge": "black",
            "desc": "Ninguno de los enlaces WAN (Primario ni Respaldo) responde. Posible falla electrica general o corte mayor en el rack."
        }
    elif not c_up and r_up:
        diag = {
            "status": "warning",
            "title": "AVERIA EXTERNA: ENLACE FIBRA CARRIER PRINCIPAL CAIDO",
            "blame": "CARRIER PRINCIPAL",
            "badge": "red",
            "desc": "El enlace principal de fibra esta caido. El rack continua operativo a traves del enlace de respaldo."
        }
    elif c_up and not t_up:
        diag = {
            "status": "warning",
            "title": "FALLA DE INTERCONEXION: TUNEL VPN CAIDO",
            "blame": "RED INTERNA / ROUTER BORDE",
            "badge": "orange",
            "desc": "El datacenter tiene salida WAN normal, pero el tunel VPN de gestion no establece sesion."
        }
    elif t_up and not n_up:
        diag = {
            "status": "warning",
            "title": "FALLA DE HIPERVISOR: NODOS PROXMOX INACCESIBLES",
            "blame": "HARDWARE / SERVIDORES FISICOS",
            "badge": "yellow",
            "desc": "Conectividad VPN activa, pero el puerto 8006 del cluster Proxmox no responde."
        }
    else:
        diag = {
            "status": "ok",
            "title": "SISTEMA OPERATIVO Y DISPONIBLE",
            "blame": "NINGUNO (TODO OK)",
            "badge": "green",
            "desc": "Enlaces WAN, tunel VPN de interconexion y cluster Proxmox operando con normalidad."
        }

    res["diagnosis"] = diag
    res["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return res

if __name__ == "__main__":
    data = get_carrier_telemetry()
    print(json.dumps(data, indent=2))
