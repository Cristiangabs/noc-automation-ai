# Telecom & NOC Intelligent Operations Suite (`noc-automation-ai`)

[![Node.js](https://img.shields.io/badge/Node.js-20.x-339933?logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Odoo%20ERP-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Zabbix](https://img.shields.io/badge/Zabbix-API%207.0-D40000?logo=zabbix&logoColor=white)](https://www.zabbix.com/)
[![Proxmox VE](https://img.shields.io/badge/Proxmox-Cluster%20API-E57000?logo=proxmox&logoColor=white)](https://www.proxmox.com/)
[![n8n](https://img.shields.io/badge/n8n-Workflow%20Automation-EA4B71?logo=n8n&logoColor=white)](https://n8n.io/)
[![vLLM](https://img.shields.io/badge/vLLM-Local%20AI%20Inference-8A2BE2)](https://github.com/vllm-project/vllm)

Plataforma integral **End-to-End de Automatizacion para Proveedores de Servicios de Internet (ISP) y Centros de Operaciones de Red (NOC)**.

Este monorepo consolida en una sola arquitectura de produccion tres capas criticas de ingenieria de telecomunicaciones:
1. **Operaciones de Campo FTTH/RF (`01-field-operations-pwa`)**: Aplicacion movil PWA *Offline-First* para cuadrillas tecnicas con aprovisionamiento GPON cero-errores en **SmartOLT**, asignacion libre de colisiones IP y cierre transaccional en **Odoo ERP** con contratos PDF firmados digitalmente.
2. **Sofia AI — Copiloto NOC & Centinela de Red (`02-sofia-ai-noc-copilot`)**: Asistente de Inteligencia Artificial local (**vLLM** + **STT/TTS**) integrado en WhatsApp para atencion de nivel 1 y copiloto de ingenieria en grupos tecnicos, con correlacion automatica de cortes de fibra (**LOS**), apagones electricos (**ICMP Sentinel**) y auditoria de trafico DPI (**Bequant BQN**).
3. **Suite de Telemetria NOC, Wallboards & Anti-Flapping (`03-noc-telemetry-suite`)**: Backend asincrono en Python + pipelines **n8n** con amortiguacion de microcortes, **Wallboard TV** con sintesis de voz en vivo, diagnostico automatico de enlaces **Carrier WAN / Proxmox** y centinela concurrente de **Certificados SSL/SAN**.

---

## Vista Previa de Interfaces y Paneles Operativos

### 1. NOC Real-Time Command Center & Voice Synthesis HUD
![NOC Wallboard HUD](./assets/screenshots/01_noc_wallboard_hud.svg)

### 2. Centinela Concurrente de Portales Web y Certificados X.509 SSL/SAN
![SSL Portals Sentinel](./assets/screenshots/02_ssl_portals_sentinel.svg)

### 3. PWA de Operaciones de Campo FTTH & Wallboard de Despacho en Vivo
![Field Operations PWA](./assets/screenshots/03_field_operations_pwa.svg)

### 4. Copiloto IA Sofia — Diagnostico Carrier WAN & Auditoria DPI Bequant BQN
![Sofia AI Diagnostics](./assets/screenshots/04_sofia_ai_diagnostics.svg)

---

## Arquitectura Global del Ecosistema

```mermaid
flowchart TB
    subgraph Field["1. Operaciones de Campo (01-field-operations-pwa)"]
        PWA["PWA Movil Offline-First\n(IndexedDB + Service Worker)"]
        FAPP["Backend Express.js\nZero-Touch Provisioning"]
        PDF["Generador Contrato PDF\n+ Firma Digital Canvas"]
    end

    subgraph CoreSystems["Infraestructura & Sistemas Core ISP"]
        OLT["SmartOLT GPON API\n(Autorizacion ONU & Potencia RX/TX)"]
        ODOO[("Odoo ERP PostgreSQL\nCRM · Tareas · Contratos · Chatter")]
        BQN["Bequant BQN Shaper\n(WebSocket DPI & Rate-Limiting)"]
        ZBX["Multi-Zabbix API 7.0\n+ The Dude SNMP/ICMP"]
        PMX["Clusteres Proxmox VE\n(Local + Carrier Datacenter)"]
    end

    subgraph AI["2. Copiloto IA & Centinelas (02-sofia-ai-noc-copilot)"]
        SOFIA["Sofia AI Core (Node.js)\nDebouncing & Context Router"]
        VLLM["Motor Local vLLM\n+ NVIDIA Parakeet STT / XTTS"]
        SENTINEL["Centinela Electrico ICMP\n& Correlacionador Masivo LOS"]
        OWA["Gateway WhatsApp REST\n(Alertas Directivas & Grupos NOC)"]
    end

    subgraph Telemetry["3. Telemetria NOC & Wallboards (03-noc-telemetry-suite)"]
        PROXY["Proxy Core & EventHub\n(Python SSE + ReportLab + Matplotlib)"]
        N8N["Pipelines n8n\n(Anti-Flapping 5m + Telegram AI Bot)"]
        WALL["Wallboards TV Interactivos\n(/tv · /portales · Sintesis de Voz)"]
        SSLCHK["Centinela Concurrente\nCertificados SSL/SAN & Carrier WAN"]
    end

    PWA <-->|"Sync Idempotente"| FAPP
    FAPP -->|"Preset CATV/C-Data\n+ Pool IP Libre"| OLT
    FAPP -->|"Cierre Tarea + Adjuntos"| ODOO
    FAPP --> PDF
    FAPP -->|"Notificacion Contingencia"| OWA

    OWA <-->|"Webhooks"| SOFIA
    SOFIA <-->|"OpenAI v1 API"| VLLM
    SOFIA -->|"Triaje Abonado"| ODOO
    SOFIA -->|"Estado Optico"| OLT
    SOFIA -->|"Auditoria Flujos >= 500"| BQN
    SENTINEL -->|"Alertas Corte/Retorno"| OWA

    ZBX --> PROXY
    PMX --> PROXY
    PROXY <--> N8N
    PROXY -->|"Server-Sent Events"| WALL
    SSLCHK --> WALL
```

---

## Modulos del Monorepo

### 1. [`01-field-operations-pwa`](./01-field-operations-pwa) — Field Service & Zero-Touch FTTH Provisioning
Aplicacion web progresiva (PWA) disenada para operar en zonas sin cobertura movil (sotanos, cuartos de telecomunicaciones) y automatizar el 100% del alta tecnica del abonado:
* **Algoritmo de Asignacion Diaria y Balanceo de Cuadrillas**: Consulta oportunidades aprobadas en Odoo CRM (`crm_lead`), clasifica instalaciones por tecnologia (`FTTH` vs `Radio Frecuencia`) y balancea automaticamente la carga entre cuadrillas con rotacion diaria de ayudantes.
* **Aprovisionamiento GPON Cero-Colisiones**:
  * Deteccion automatica del fabricante de la ONU (`C-Data` vs `VSOL/CATV`) y seleccion dinamica del *Preset* en **SmartOLT**.
  * Cruce bidireccional en tiempo real entre **SmartOLT** y **Odoo** (`project_task` + `sale_subscription`) para asignar la primera IP fija disponible del pool correspondiente a cada OLT, garantizando **0% de colisiones de IP**.
* **Sincronizacion Offline Idempotente (`IndexedDB` + `Service Worker`)**:
  * Almacena localmente evidencias fotograficas, coordenadas GPS, test de velocidad HTML5 y firma del cliente en `IndexedDB` cuando no hay senal.
  * **Blindaje de Idempotencia en Backend**: Previene que reenvios tardios sobrescriban fechas historicas (`COALESCE(fecha_instalacion, NOW())`), evita duplicar adjuntos en `ir_attachment` y bloquea re-notificaciones por WhatsApp, respondiendo `{ alreadyInstalled: true }` para purgar limpiamente la cola del dispositivo.
* **Wallboard de Despacho en Vivo (`/wallboard.html`)**: Panel de seguimiento en tiempo real mediante *Server-Sent Events (SSE)* con KPIs diarios de instalaciones completadas, en sitio y pospuestas.

---

### 2. [`02-sofia-ai-noc-copilot`](./02-sofia-ai-noc-copilot) — AI NOC Assistant, DPI Auditor & Network Sentinel
Copiloto operativo dual (Atencion al Cliente Nivel 1 + Copiloto de Ingenieria en Grupos Tecnicos) conectado a un servidor de inferencia local **vLLM**:
* **Diagnostico Unificado de Abonados (`customerLookupService.js`)**: Extrae cedula, codigo de contrato (`SUB`) o nombre desde lenguaje natural y cruza en milisegundos el estado administrativo/facturacion en **Odoo** con la telemetria optica en vivo en **SmartOLT** (potencia RX/TX en dBm, estado `Online`, `Dying Gasp` o `LOS`).
* **Correlacionador de Cortes Masivos y Centinela Electrico**:
  * `smartoltMonitor.js`: Sondea las OLTs cada 60 segundos; si detecta mas de 5 ONUs caidas simultaneamente en una misma zona, declara automaticamente una averia masiva o apagon zonal y alerta a los grupos tecnicos.
  * `sentinelPowerMonitor.js`: Sondeo ICMP de alta frecuencia (cada 20s) hacia la ONU centinela de referencia. Detecta cortes electricos al instante y, al restablecerse el fluido, calcula y reporta la duracion exacta de la interrupcion.
* **Auditoria de Sobreconsumo con Bequant BQN (`bequantReportService.js` + `generate_bequant_pdf.py`)**:
  * Conexion via WebSocket seguro (`WSS`) al shaper DPI **Bequant BQN** para filtrar suscriptores con exceso de sesiones concurrentes (`FLOWS >= 500`), extraer series temporales de ancho de banda y generar automaticamente un dossier PDF ejecutivo enviado por WhatsApp.
* **Diagnostico Automatizado de Carrier WAN (`cirionDiagnosticService.js` + `generate_cirion_card.py`)**:
  * Verifica por SSH el quorum del cluster **Proxmox VE** en el datacenter externo y ejecuta pruebas ICMP cruzadas hacia el gateway interno y la IP publica del carrier para emitir una tarjeta grafica con veredicto automatico (*Falla de Carrier Externo* vs. *Red Interna Operativa*).
* **Procesamiento de Voz Local (`audioService.js`)**: Transcripcion de notas de voz de WhatsApp mediante **NVIDIA Parakeet (STT)** y sintesis de voz con **XTTS-v2**.
* **Autoaprendizaje Nocturno (`learningService.js`)**: Consolida cada manana a las 07:00 AM las metricas de caidas opticas, cortes electricos y tiempos medios de recuperacion (MTTR), inyectando las lecciones aprendidas en el prompt del sistema.

---

### 3. [`03-noc-telemetry-suite`](./03-noc-telemetry-suite) — Real-Time NOC Wallboards, SSL Sentinel & Anti-Flapping
Suite de visualizacion para pantallas de sala de control y orquestacion de alertas:
* **Proxy Asincrono, EventHub & Generador de Reportes (`proxy/proxy.py`)**:
  * Conector JSON-RPC con **Zabbix 7.0** para consulta inteligente de interfaces, calculo de percentiles y generacion bajo demanda de graficas PNG (`Matplotlib`) y reportes ejecutivos PDF (`ReportLab`).
  * Integracion con **Proxmox VE API** para telemetria de nodos y control seguro de VMs/LXCs.
* **NOC TV HUD con Sintesis de Voz (`dashboard/oficina_noc.html`)**:
  * Interfaz visual animada conectada por *Server-Sent Events (SSE)* donde el avatar del operador se desplaza en tiempo real hacia la estacion de trabajo que genera un evento (Zabbix, Proxmox, Ping, Trafico).
  * **Coordinador de Voz Inteligente**: Agrupa rafagas de incidentes en ventanas de 18 segundos y pronuncia locuciones claras por los parlantes del NOC sin leer simbolos ni codigos crudos.
* **Centinela Concurrente de Portales Web y Certificados SSL/SAN (`checkers/web_ssl_checker.py` + `dashboard/web_portales_dashboard.html`)**:
  * Inspeccion multihilo (`ThreadPoolExecutor`) de portales gubernamentales y corporativos.
  * Validacion profunda de certificados X.509 (`cryptography`): dias restantes de vigencia, emisor, Common Name (CN) y verificacion estricta de **Subject Alternative Names (SAN)** para detectar errores de *Hostname Mismatch* (`ERR_CERT_COMMON_NAME_INVALID`) incluso cuando el certificado no ha expirado.
* **Pipelines Inteligentes en n8n (`workflows/`)**:
  * `workflow-1-smart-alerts.json`: Ventana de amortiguacion de 5 minutos (*Anti-Flapping*) que silencia microcortes fugaces en equipos de acceso y prioriza alertas de infraestructura troncal.
  * `workflow-2-ai-bot.json`: Bot conversacional de operaciones en Telegram con herramientas conectadas a Zabbix y Proxmox (con protocolo de doble confirmacion para reinicio/apagado de contenedores).

---

## Stack Tecnologico

| Capa | Tecnologias e Integraciones |
| :--- | :--- |
| **Backend & Servicios** | Node.js (Express), Python 3.11 (`http.server`, `concurrent.futures`, `cryptography`), WebSockets (`ws`), SSH2 |
| **Frontend & Movil** | HTML5, CSS3 Moderno, Vanilla JavaScript ES6+, Progressive Web App (PWA), Service Workers, `IndexedDB`, `HTML5 Canvas`, `Web Speech API` |
| **Redes & Telecom (ISP)** | **SmartOLT REST API** (GPON/FTTH), **Bequant BQN** (WSS DPI/TCP Optimization), **Zabbix 7.0 JSON-RPC**, **The Dude**, **MikroTik / pfSense / Fortinet** |
| **Virtualizacion & ERP** | **Proxmox VE API & Cluster (`pvecm`/`qm`/`pct`)**, **Odoo ERP** (`PostgreSQL 14` — `crm_lead`, `project_task`, `sale_subscription`, `ir_attachment`) |
| **Inteligencia Artificial** | **vLLM** (OpenAI-compatible API), **NVIDIA Parakeet** (ASR/STT), **Coqui XTTS-v2** (TTS), **n8n** + LangChain |
| **Reporteria & Graficos** | `Matplotlib`, `ReportLab`, `Pillow (PIL)`, `SheetJS (xlsx)`, `HTML5-QRCode` |

---

## Guia de Inicio Rapido

### 1. Clonar el repositorio y configurar variables de entorno
```bash
git clone https://github.com/Cristiangabs/noc-automation-ai.git
cd noc-automation-ai
cp .env.example .env
```
> **Nota de Seguridad:** Todos los modulos leen sus credenciales, direcciones IP y tokens desde variables de entorno. Ningun archivo del repositorio contiene contrasenas, IPs privadas reales ni datos sensibles de clientes.

### 2. Ejecutar la PWA de Operaciones de Campo (`01-field-operations-pwa`)
```bash
cd 01-field-operations-pwa
npm install
npm start
# App Movil disponible en: http://localhost:3000
# Wallboard de Despacho en: http://localhost:3000/wallboard.html
```

### 3. Ejecutar el Copiloto IA Sofia (`02-sofia-ai-noc-copilot`)
```bash
cd 02-sofia-ai-noc-copilot
npm install
npm start
# Servicio escuchando webhooks y simulaciones en: http://localhost:11450/health
```

### 4. Desplegar la Suite de Telemetria NOC y Dashboards (`03-noc-telemetry-suite`)
```bash
cd 03-noc-telemetry-suite
docker-compose up -d
# NOC TV Wallboard: http://localhost:11440/tv
# Centinela SSL/SAN: python3 checkers/web_ssl_checker.py --once
```

---

## Autor

**Cristian Gabriel Salinas Velasquez**  
*Administrador de Infraestructura TI, Redes & Automatizacion de Telecomunicaciones*  
Caracas, Venezuela  
[Cristiangsalinasv@gmail.com](mailto:Cristiangsalinasv@gmail.com) · [GitHub `@Cristiangabs`](https://github.com/Cristiangabs)
