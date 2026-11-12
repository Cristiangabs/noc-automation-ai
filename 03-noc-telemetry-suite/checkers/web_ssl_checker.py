#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tecnored Web Services & SSL Certificate Health Checker
Monitors municipal portals, corporate services and internal web apps.
Performs DNS resolution, strict SSL validation (SAN match, days left, issuer),
and HTTP latency & status code checks.
"""

import os
import sys
import time
import json
import socket
import ssl
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
import concurrent.futures

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtensionOID

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.path.join(BASE_DIR, 'web_targets.json')
STATUS_FILE = os.path.join(BASE_DIR, 'web_ssl_status.json')

_cache_data = None
_cache_lock = threading.Lock()
_worker_started = False

def check_single_target(target):
    t_id = target.get("id")
    name = target.get("name")
    url = target.get("url")
    category = target.get("category", "General")
    
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    
    res = {
        "id": t_id,
        "name": name,
        "url": url,
        "category": category,
        "hostname": hostname,
        "ip": None,
        "dns_ok": False,
        "http_ok": False,
        "http_code": 0,
        "latency_ms": 0,
        "ssl_ok": False,
        "ssl_status": "NONE" if parsed.scheme != 'https' else "UNKNOWN",
        "ssl_days": 0,
        "ssl_expiry": None,
        "ssl_issuer": "N/A",
        "ssl_cn": "N/A",
        "ssl_match": False,
        "sans": [],
        "overall_status": "DOWN", # ONLINE, WARNING, CRITICAL, DOWN
        "error_msg": None,
        "last_checked": datetime.now().strftime("%H:%M:%S")
    }
    
    if not hostname:
        res["error_msg"] = "Invalid URL hostname"
        return res
        
    # 1. DNS Resolution
    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
        ip = addr_info[0][4][0]
        res["ip"] = ip
        res["dns_ok"] = True
    except Exception as e:
        res["error_msg"] = f"Error DNS: No resuelve ({str(e).split(']')[-1].strip()})"
        res["overall_status"] = "DOWN"
        return res
        
    # 2. SSL Verification (HTTPS)
    if parsed.scheme == 'https':
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            
            with socket.create_connection((ip, port), timeout=4) as sock:
                with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                    der = ssock.getpeercert(binary_form=True)
                    
            if der:
                cert = x509.load_der_x509_certificate(der)
                
                # Expiration
                if hasattr(cert, "not_valid_after_utc"):
                    exp_dt = cert.not_valid_after_utc
                else:
                    exp_dt = cert.not_valid_after.replace(tzinfo=timezone.utc)
                    
                now_dt = datetime.now(timezone.utc)
                days_left = int((exp_dt - now_dt).total_seconds() / 86400)
                res["ssl_days"] = days_left
                res["ssl_expiry"] = exp_dt.strftime("%Y-%m-%d")
                
                # Common Name
                cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                res["ssl_cn"] = cn_attrs[0].value if cn_attrs else "N/A"
                
                # Issuer
                try:
                    iss_attrs = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
                    if not iss_attrs:
                        iss_attrs = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
                    res["ssl_issuer"] = iss_attrs[0].value if iss_attrs else "Desconocido"
                except Exception:
                    res["ssl_issuer"] = "Desconocido"
                    
                # SANs
                try:
                    san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
                    sans = san_ext.value.get_values_for_type(x509.DNSName)
                    res["sans"] = sans
                except Exception:
                    res["sans"] = []
                    
                # Check domain match
                all_valid_names = set(res["sans"])
                if res["ssl_cn"] != "N/A":
                    all_valid_names.add(res["ssl_cn"])
                    
                match = False
                hn_l = hostname.lower()
                for pat in all_valid_names:
                    p_l = pat.lower()
                    if p_l == hn_l:
                        match = True
                        break
                    if p_l.startswith("*."):
                        suffix = p_l[2:]
                        if hn_l.endswith(suffix) and hn_l.count('.') == p_l.count('.'):
                            match = True
                            break
                            
                res["ssl_match"] = match
                
                if not match:
                    res["ssl_status"] = "MISMATCH"
                    res["error_msg"] = f"Certificado no coincide con dominio ({res['ssl_cn']})"
                elif res["ssl_days"] < 0:
                    res["ssl_status"] = "EXPIRED"
                    res["error_msg"] = f"Certificado vencido hace {abs(res['ssl_days'])} días"
                elif res["ssl_days"] <= 7:
                    res["ssl_status"] = "CRITICAL"
                    res["ssl_ok"] = True
                elif res["ssl_days"] <= 20:
                    res["ssl_status"] = "WARNING"
                    res["ssl_ok"] = True
                else:
                    res["ssl_status"] = "VALID"
                    res["ssl_ok"] = True
        except Exception as e_ssl:
            res["ssl_status"] = "SSL_ERROR"
            err_str = str(e_ssl)
            if "timed out" in err_str:
                res["error_msg"] = "SSL Timeout (Servicio no responde en 443)"
            elif "Connection refused" in err_str:
                res["error_msg"] = "Puerto 443 cerrado o rechazado"
            else:
                res["error_msg"] = f"Fallo SSL: {err_str[:60]}"

    # 3. HTTP Request
    t_http_start = time.time()
    try:
        ctx_http = ssl.create_default_context()
        ctx_http.check_hostname = False
        ctx_http.verify_mode = ssl.CERT_NONE
        
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Tecnored-NOC-Monitor/2.0',
                'Accept': '*/*'
            }
        )
        with urllib.request.urlopen(req, context=ctx_http if parsed.scheme == 'https' else None, timeout=5) as resp:
            elapsed = int((time.time() - t_http_start) * 1000)
            res["latency_ms"] = elapsed
            res["http_code"] = resp.status
            res["http_ok"] = (200 <= resp.status < 400)
    except urllib.error.HTTPError as he:
        elapsed = int((time.time() - t_http_start) * 1000)
        res["latency_ms"] = elapsed
        res["http_code"] = he.code
        # 401/403 often indicates an authenticated admin portal or login screen (service is active)
        res["http_ok"] = he.code in (401, 403)
        if not res["http_ok"]:
            res["error_msg"] = f"HTTP Error {he.code}"
    except Exception as e_http:
        elapsed = int((time.time() - t_http_start) * 1000)
        res["latency_ms"] = elapsed
        if not res["error_msg"]:
            err_str = str(e_http)
            if "timed out" in err_str:
                res["error_msg"] = "HTTP Timeout (> 5s)"
            else:
                res["error_msg"] = f"HTTP Error: {err_str[:60]}"

    # Overall State
    if not res["dns_ok"]:
        res["overall_status"] = "DOWN"
    elif res["ssl_status"] in ("EXPIRED", "MISMATCH", "SSL_ERROR"):
        res["overall_status"] = "CRITICAL"
    elif not res["http_ok"]:
        res["overall_status"] = "DOWN"
    elif res["ssl_status"] in ("CRITICAL", "WARNING"):
        res["overall_status"] = res["ssl_status"]
    else:
        res["overall_status"] = "ONLINE"
        
    return res

def run_full_check():
    global _cache_data
    if not os.path.exists(TARGETS_FILE):
        return {"ok": False, "error": f"Target file not found: {TARGETS_FILE}"}
        
    with open(TARGETS_FILE, 'r', encoding='utf-8') as f:
        targets = json.load(f)
        
    active_targets = [t for t in targets if t.get('enabled', True)]
    t0 = time.time()
    
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=14) as executor:
        futures = {executor.submit(check_single_target, t): t for t in active_targets}
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
            
    # Sort: Críticos / Down first, then Warnings, then Online; Alcaldías first
    results.sort(key=lambda x: (
        x["overall_status"] not in ("CRITICAL", "DOWN"),
        x["overall_status"] != "WARNING",
        x["category"] != "Alcaldías",
        x["name"]
    ))
    
    total_time = round(time.time() - t0, 2)
    online_cnt = sum(1 for r in results if r["overall_status"] == "ONLINE")
    warn_cnt = sum(1 for r in results if r["overall_status"] == "WARNING")
    crit_cnt = sum(1 for r in results if r["overall_status"] in ("CRITICAL", "DOWN"))
    avg_lat = int(sum(r["latency_ms"] for r in results if r["latency_ms"] > 0) / max(1, sum(1 for r in results if r["latency_ms"] > 0)))
    
    payload = {
        "ok": True,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_targets": len(results),
        "online_count": online_cnt,
        "warning_count": warn_cnt,
        "critical_count": crit_cnt,
        "avg_latency_ms": avg_lat,
        "execution_time_s": total_time,
        "services": results
    }
    
    with _cache_lock:
        _cache_data = payload
        
    # Save atomically
    tmp_path = STATUS_FILE + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, STATUS_FILE)
    except Exception as e:
        print(f"[WebSSLChecker] Save file error: {e}", file=sys.stderr)
        
    return payload

def _background_worker():
    print("[WebSSLChecker] Background worker thread started (interval 60s).", flush=True)
    while True:
        try:
            res = run_full_check()
            print(f"[WebSSLChecker] [{datetime.now().strftime('%H:%M:%S')}] Check complete in {res.get('execution_time_s')}s (Online: {res.get('online_count')}/{res.get('total_targets')})", flush=True)
        except Exception as e:
            print(f"[WebSSLChecker] Worker error: {e}", file=sys.stderr, flush=True)
        time.sleep(60)

def ensure_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        t = threading.Thread(target=_background_worker, daemon=True, name="WebSSLWorker")
        t.start()

def get_web_status():
    global _cache_data
    ensure_worker()
    with _cache_lock:
        if _cache_data:
            return _cache_data
            
    # Try reading from disk
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            with _cache_lock:
                _cache_data = data
            return data
        except Exception:
            pass
            
    # Otherwise run once synchronously
    return run_full_check()

# Start background immediately when module is imported
ensure_worker()

def main():
    if '--once' in sys.argv:
        print("[WebSSLChecker] Running single inspection...")
        res = run_full_check()
        print(f"[WebSSLChecker] Done in {res.get('execution_time_s')}s | Total: {res.get('total_targets')} | Online: {res.get('online_count')} | Warn: {res.get('warning_count')} | Crit: {res.get('critical_count')}")
        for s in res.get('services', []):
            if s['overall_status'] in ('CRITICAL', 'DOWN', 'WARNING'):
                print(f" -> [{s['overall_status']}] {s['name']} ({s['url']}): {s.get('error_msg')}")
        return

    # Loop mode
    _background_worker()

if __name__ == '__main__':
    main()
