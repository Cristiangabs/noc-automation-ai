process.env.TZ = 'America/Caracas';

function getHoraCaracas(d = new Date()) {
  return d.toLocaleTimeString('es-VE', { timeZone: 'America/Caracas', hour: '2-digit', minute: '2-digit', hour12: true });
}
function getFechaHoraCaracas(d = new Date()) {
  return d.toLocaleString('es-VE', { timeZone: 'America/Caracas', hour12: true });
}
function getFechaCaracas(d = new Date()) {
  return d.toLocaleDateString('es-VE', { timeZone: 'America/Caracas' });
}

const express = require('express');
const cors = require('cors');
const path = require('path');
const fs = require('fs');
const https = require('https');
const http = require('http');
const crypto = require('crypto');
const querystring = require('querystring');
const XLSX = require('xlsx');

const app = express();
const PORT = process.env.PORT || 3000;

// Ocultar cabeceras de tecnología y añadir protección HTTP
app.disable('x-powered-by');
app.use((req, res, next) => {
  res.removeHeader('X-Powered-By');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'SAMEORIGIN');
  res.setHeader('X-XSS-Protection', '1; mode=block');
  res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
  next();
});

app.use(cors());
app.use(express.json({ limit: '50mb' }));
app.use(express.static(path.join(__dirname, 'public'), {
  setHeaders: (res, filePath) => {
    if (filePath.endsWith('index.html') || filePath.endsWith('sw.js')) {
      res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
    }
  }
}));

const DATA_DIR = path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

const DRAFTS_DIR = path.join(DATA_DIR, 'drafts');
if (!fs.existsSync(DRAFTS_DIR)) {
  fs.mkdirSync(DRAFTS_DIR, { recursive: true });
}

const COMPLETED_DIR = path.join(DATA_DIR, 'completed');
if (!fs.existsSync(COMPLETED_DIR)) {
  fs.mkdirSync(COMPLETED_DIR, { recursive: true });
}

const ONU_ASSIGNMENTS_FILE = path.join(DATA_DIR, 'onu_assignments.json');
let cachedOnuAssignments = null;
function getOnuAssignments() {
  if (cachedOnuAssignments) return cachedOnuAssignments;
  if (!fs.existsSync(ONU_ASSIGNMENTS_FILE)) {
    cachedOnuAssignments = {};
    return cachedOnuAssignments;
  }
  try {
    cachedOnuAssignments = JSON.parse(fs.readFileSync(ONU_ASSIGNMENTS_FILE, 'utf8'));
    return cachedOnuAssignments;
  } catch (e) {
    cachedOnuAssignments = {};
    return cachedOnuAssignments;
  }
}
function saveOnuAssignments(assignments) {
  cachedOnuAssignments = assignments;
  try {
    fs.writeFileSync(ONU_ASSIGNMENTS_FILE, JSON.stringify(assignments, null, 2));
  } catch (e) {
    console.error('Error guardando onu_assignments.json:', e.message);
  }
}

const POSTPONED_FILE = path.join(DATA_DIR, 'postponed_installations.json');
function getPostponed() {
  if (!fs.existsSync(POSTPONED_FILE)) return {};
  try { return JSON.parse(fs.readFileSync(POSTPONED_FILE, 'utf8')); } catch (e) { return {}; }
}
function savePostponed(data) {
  fs.writeFileSync(POSTPONED_FILE, JSON.stringify(data, null, 2));
}

const INSTALLATIONS_LOG_FILE = path.join(DATA_DIR, 'installations_log.json');
let cachedInstallationsLog = null;
let lastLogReadTime = 0;

function getInstallationsLog() {
  const now = Date.now();
  if (cachedInstallationsLog && (now - lastLogReadTime < 10000)) {
    return cachedInstallationsLog;
  }
  let logs = [];
  if (fs.existsSync(INSTALLATIONS_LOG_FILE)) {
    try {
      const raw = JSON.parse(fs.readFileSync(INSTALLATIONS_LOG_FILE, 'utf8'));
      if (Array.isArray(raw)) logs = raw;
    } catch (e) {
      logs = cachedInstallationsLog || [];
    }
  }

  cachedInstallationsLog = logs.map(l => {
    if (l.timestamp) {
      const d = new Date(l.timestamp);
      return {
        ...l,
        hora: l.hora || getHoraCaracas(d),
        fecha: l.fecha || getFechaHoraCaracas(d)
      };
    }
    return l;
  }).sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
  lastLogReadTime = now;
  return cachedInstallationsLog;
}

function saveInstallationsLog(logs) {
  cachedInstallationsLog = logs;
  try {
    fs.writeFileSync(INSTALLATIONS_LOG_FILE, JSON.stringify(logs, null, 2));
  } catch (e) {
    console.error('Error guardando installations_log.json:', e.message);
  }
}

function appendInstallationLog(entry) {
  const logs = getInstallationsLog();
  const entryId = entry.id || `LOG-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
  entry.id = entryId;
  const existingIdx = logs.findIndex(l => l.id === entryId || (entry.logId && l.id === entry.logId));
  if (existingIdx >= 0) {
    logs[existingIdx] = { ...logs[existingIdx], ...entry, updatedAt: new Date().toISOString() };
  } else {
    logs.unshift({ ...entry, createdAt: entry.createdAt || new Date().toISOString() });
  }
  saveInstallationsLog(logs);
}

// Configuración de SmartOLT Oficial (Operación 100% en Backend, invisible al técnico)
const SMARTOLT_CONFIG = {
  host: process.env.SMARTOLT_HOST || 'isp-domain.smartolt.com',
  apiKey: process.env.SMARTOLT_API_KEY || 'YOUR_SMARTOLT_API_TOKEN'
};

// Configuración de OpenWA WhatsApp API (Notificaciones de Contingencia a Directivos)
const OPENWA_CONFIG = {
  baseUrl: process.env.OPENWA_BASE_URL || 'http://10.0.10.20:2785',
  apiKey: process.env.OPENWA_API_KEY || 'YOUR_OPENWA_API_KEY',
  sessionId: process.env.OPENWA_SESSION_ID || 'YOUR_OPENWA_SESSION_ID',
  recipients: [
    { name: 'Ops Manager', chatId: '584120000002@c.us' },
    { name: 'Field Supervisor', chatId: '584120000003@c.us' }
  ]
};

function extractGpsLink(coordsStr) {
  if (!coordsStr) return null;
  const str = String(coordsStr).trim();
  if (str.startsWith('http://') || str.startsWith('https://')) {
    return str;
  }
  const m = str.match(/(-?\d+\.\d+)[,\s]+(-?\d+\.\d+)/);
  if (m) {
    return `https://maps.google.com/?q=${m[1]},${m[2]}`;
  }
  return null;
}

function sendOpenWaText(chatId, text) {
  return new Promise((resolve, reject) => {
    try {
      const url = new URL(`${OPENWA_CONFIG.baseUrl}/api/sessions/${OPENWA_CONFIG.sessionId}/messages/send-text`);
      const payload = JSON.stringify({ chatId, text });
      const req = http.request(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': OPENWA_CONFIG.apiKey,
          'Content-Length': Buffer.byteLength(payload)
        },
        timeout: 10000
      }, res => {
        let data = '';
        res.on('data', chunk => data += chunk);
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(data);
          } else {
            reject(new Error(`HTTP ${res.statusCode}: ${data}`));
          }
        });
      });
      req.on('error', reject);
      req.on('timeout', () => req.destroy(new Error('Timeout')));
      req.write(payload);
      req.end();
    } catch (e) {
      reject(e);
    }
  });
}

function sendOpenWaDocument(chatId, filename, base64Data, caption = '') {
  return new Promise((resolve, reject) => {
    try {
      const url = new URL(`${OPENWA_CONFIG.baseUrl}/api/sessions/${OPENWA_CONFIG.sessionId}/messages/send-document`);
      let cleanBase64 = base64Data || '';
      if (cleanBase64.includes('base64,')) {
        cleanBase64 = cleanBase64.split('base64,')[1];
      }
      const payload = JSON.stringify({
        chatId,
        filename,
        mimetype: 'application/pdf',
        base64: cleanBase64,
        caption
      });
      const req = http.request(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': OPENWA_CONFIG.apiKey,
          'Content-Length': Buffer.byteLength(payload)
        },
        timeout: 25000
      }, res => {
        let data = '';
        res.on('data', chunk => data += chunk);
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(data);
          } else {
            reject(new Error(`HTTP ${res.statusCode}: ${data}`));
          }
        });
      });
      req.on('error', reject);
      req.on('timeout', () => req.destroy(new Error('Timeout')));
      req.write(payload);
      req.end();
    } catch (e) {
      reject(e);
    }
  });
}

async function notificarWhatsAppContingencia(completedRecord, fieldData, cliente) {
  try {
    const nombre = cliente.nombre || fieldData.nombre || 'Cliente Multicanal';
    const subCode = cliente.id || fieldData.id || `SUB${cliente.lead_id || ''}`;
    const leadId = cliente.lead_id || fieldData.lead_id || '';
    const ci = cliente.ci || fieldData.ci || 'V-PENDIENTE';
    const telefono = cliente.telefono || fieldData.telefono || 'Sin teléfono';
    const direccion = cliente.direccion || fieldData.direccion || 'Sin dirección';
    const zona = cliente.zona || fieldData.zona || 'San Agustín';

    const tecnico = fieldData.instalador || cliente.instalador || 'Técnico Multicanal';
    const ayudante = fieldData.ayudante || cliente.ayudante || '';
    const serialPon = completedRecord.serialPon || fieldData.serialPon || 'N/A';
    const serialSec = completedRecord.serialSecundario || fieldData.serialSecundario || '';
    const potencia = completedRecord.potenciaDbm || fieldData.potenciaDbm || 'N/A';
    const cajaNap = completedRecord.lugarNap || fieldData.lugarNap || 'N/A';
    const puertoNap = completedRecord.puertoNap || fieldData.puertoNap || 'N/A';
    const metrosDrop = completedRecord.dropTotal || fieldData.dropTotal || 0;
    const ipLan = completedRecord.ipLan || 'N/A';
    const smartOltStatus = completedRecord.smartOltStatus || 'EXITOSO';
    const odooTaskId = completedRecord.odooTaskId || 'N/A';

    const gpsRaw = fieldData.gpsCoords || completedRecord.gpsCoords || '';
    const mapsLink = extractGpsLink(gpsRaw);

    const texto = [
      `🚨 *INSTALACIÓN COMPLETADA EN CAMPO* 🚨`,
      `_FiberNet Telecom - Notificación de Contingencia_\n`,
      `👤 *Cliente:* ${nombre}`,
      `📄 *Contrato / SUB:* ${subCode} ${leadId ? `| *Lead:* ${leadId}` : ''}`,
      `🆔 *C.I / RIF:* ${ci}`,
      `📞 *Teléfono:* ${telefono}`,
      `📍 *Dirección:* ${direccion}`,
      `🏙 *Zona:* ${zona}\n`,
      `🛠 *DETALLES TÉCNICOS:*`,
      `• *Cuadrilla / Técnicos:* ${tecnico}${ayudante ? ` / ${ayudante}` : ''}`,
      `• *Serial PON ONU:* ${serialPon}`,
      serialSec ? `• *Serial Fábrica (LP):* ${serialSec}` : null,
      `• *Potencia Óptica:* ${potencia} dBm`,
      `• *Caja NAP:* ${cajaNap} (Puerto ${puertoNap})`,
      `• *Cable Drop:* ${metrosDrop} mts`,
      `• *IP LAN Asignada:* ${ipLan}`,
      `• *SmartOLT:* ${smartOltStatus}`,
      `• *Tarea Odoo:* #${odooTaskId} (Instalados)\n`,
      `🗺 *UBICACIÓN GPS EN SITIO:*`,
      mapsLink ? `${mapsLink}` : `No registrada`,
      gpsRaw && mapsLink ? `(Coordenadas: ${gpsRaw})` : null,
      `\n⏰ *Fecha y Hora:* ${completedRecord.fecha || getFechaHoraCaracas()} ${completedRecord.hora || getHoraCaracas()}`
    ].filter(line => line !== null).join('\n');

    const pdfBase64 = fieldData.contratoPdf || completedRecord.contratoPdfBase64 || null;

    for (const rec of OPENWA_CONFIG.recipients) {
      try {
        console.log(`📲 [WhatsApp] Enviando ficha técnica a ${rec.name} (${rec.chatId})...`);
        await sendOpenWaText(rec.chatId, texto);
        console.log(`✅ [WhatsApp] Mensaje de texto entregado a ${rec.name}`);

        if (pdfBase64) {
          console.log(`📎 [WhatsApp] Enviando Contrato PDF a ${rec.name}...`);
          await sendOpenWaDocument(
            rec.chatId,
            `Contrato_Digital_${subCode}.pdf`,
            pdfBase64,
            `📄 Contrato Digital Firmado - ${nombre} (${subCode})`
          );
          console.log(`✅ [WhatsApp] Contrato PDF entregado a ${rec.name}`);
        }
      } catch (errRec) {
        console.error(`⚠️ [WhatsApp] Error enviando a ${rec.name} (${rec.chatId}):`, errRec.message);
      }
    }
  } catch (errGeneral) {
    console.error('⚠️ [WhatsApp] Error en notificarWhatsAppContingencia:', errGeneral.message);
  }
}

// Memoria en tiempo real para sesiones activas y sincronización entre dispositivos
let activeInstallations = {}; // { [clienteId]: { clienteId, leadId, clienteNombre, tecnico, ayudante, estado, inicio, ultimaActividad, actividad } }
let sseClients = [];

function calcularKpis() {
  const logs = getInstallationsLog();
  const postponed = getPostponed();
  const activeCount = Object.keys(activeInstallations).length;
  const instalados = logs.filter(l => (l.estado || '').toLowerCase().includes('instalad')).length;
  const pospuestos = Object.keys(postponed).length;
  return {
    instalados,
    pospuestos,
    instaladosHoy: instalados,
    pospuestosHoy: pospuestos,
    enAtencion: activeCount,
    totalLogs: logs.length
  };
}

function broadcastRealtime(eventType, payload) {
  const dataString = `data: ${JSON.stringify({ event: eventType, data: payload, timestamp: Date.now() })}\n\n`;
  sseClients.forEach(clientRes => {
    try {
      clientRes.write(dataString);
    } catch (e) {
      // drop
    }
  });
}

function sanitizeSmartOltString(str, maxLen = 100) {
  if (!str) return '';
  return String(str)
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-zA-Z0-9 @$&()\-'.+,/_:;]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxLen);
}

function resolveSmartOltPreset(oltId, isCdata) {
  const isOlt3 = String(oltId) === '3';
  if (isOlt3) {
    if (isCdata) {
      return { id: '6', name: 'Preset_Aprovisionamiento_AB_ONU-CDATA' };
    } else {
      return { id: '5', name: 'Preset ONU CATV - MANOLOS' };
    }
  } else {
    // Por defecto OLT 4 (San Agustín)
    if (isCdata) {
      return { id: '3', name: 'Preset_Aprovisionamiento_SA_ONU-CDATA' };
    } else {
      return { id: '4', name: 'Preset ONU CATV' };
    }
  }
}

function callSmartOlt(endpoint, method = 'GET', postData = null) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: SMARTOLT_CONFIG.host,
      port: 443,
      path: endpoint,
      method: method,
      headers: {
        'X-Token': SMARTOLT_CONFIG.apiKey,
        'Accept': 'application/json'
      }
    };

    if (postData) {
      options.headers['Content-Type'] = 'application/x-www-form-urlencoded';
      options.headers['Content-Length'] = Buffer.byteLength(postData);
    }

    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          resolve({ raw: data, status: res.statusCode });
        }
      });
    });

    req.on('error', (err) => reject(err));
    if (postData) req.write(postData);
    req.end();
  });
}

// Obtener la IP real de la interfaz WAN (DHCP o Estática) consultando el estado en vivo de la ONU en la OLT
async function getSmartOltRealWanIp(sn) {
  if (!sn) return null;
  const cleanSn = String(sn).trim();

  // 1. Consultar estado en vivo de la ONU (get_onu_full_status_info) donde reside la IP DHCP entregada por MikroTik
  try {
    const res = await callSmartOlt(`/api/onu/get_onu_full_status_info/${cleanSn}`);
    if (res && res.status) {
      if (res.full_status_json && res.full_status_json['ONU WAN Interfaces']) {
        const wans = res.full_status_json['ONU WAN Interfaces'];
        for (const k of Object.keys(wans)) {
          const ip = wans[k]?.['IPv4 address'] || wans[k]?.ip;
          if (ip && ip !== '0.0.0.0' && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(String(ip).trim())) {
            return String(ip).trim();
          }
        }
      }
      if (res.full_status_info) {
        const m = String(res.full_status_info).match(/IPv4\s+address:\s*([0-9.]+)/i);
        if (m && m[1] && m[1] !== '0.0.0.0' && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(m[1].trim())) {
          return m[1].trim();
        }
      }
    }
  } catch (eFull) {
    console.warn(`Aviso consultando full_status_info para ${cleanSn}:`, eFull.message);
  }

  // 2. Fallback a get_onu_details
  try {
    const det = await callSmartOlt(`/api/onu/get_onu_details/${cleanSn}`);
    if (det && det.status && det.onu_details?.ip_address) {
      const ip = det.onu_details.ip_address.trim();
      if (ip && ip !== '0.0.0.0' && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(ip)) {
        return ip;
      }
    }
  } catch (eDet) {}

  return null;
}

// Subredes reales de IP Fija en Multicanal (detectadas en OLT SanAgustin y OLTANDRESBELLO)
const SUBREDES_MULTICANAL = {
  'OLT SanAgustin': { prefix: '100.64.10', mask: '255.255.255.0', gateway: '100.64.10.1', vlan: '100', dns1: '8.8.8.8', dns2: '9.9.9.9' },
  'OLTANDRESBELLO': { prefix: '100.64.20', mask: '255.255.255.0', gateway: '100.64.20.1', vlan: '100', dns1: '8.8.8.8', dns2: '9.9.9.9' }
};

// Clientes aprobados desde el CRM / Excel de Odoo
// Cada cliente sale de oficina con 1 ONU Principal asignada + 2 ONUs de Repuesto por si sale mala
let odooApprovedClients = [
  {
    id: '77527',
    solicitud: 'S18441',
    nombre: 'Ana María Demo',
    ci: '10000001',
    telefono: '+58 412-0001122',
    email: 'cliente.demo1@example.com',
    direccion: 'San Agustín del Sur, cuarta callé, Marín.',
    zona: 'Marin',
    oltAsignada: 'OLT SanAgustin',
    tipoServicio: 'FIBRA OPTICA',
    fechaInstalacion: '10/15/2026',
    fechaCambioPlan: '',
    instalador: 'CARLOS MENDOZA',
    estado: 'Pendiente',
    // Líneas exactas del contrato Excel para ese cliente
    itemsContrato: [
      { desc: '[FO-00133] Fibra Óptica Hogar Asimet 200 mbps', cant: 30, precioUsd: 10, precioBs: '8790.52' },
      { desc: '[FO-00133] Fibra Óptica Hogar Asimet 200 mbps', cant: 30, precioUsd: 0, precioBs: '0' },
      { desc: '[SER-000015] Servicio ONU-WIFI', cant: 1, precioUsd: 3, precioBs: '2641.24' },
      { desc: '[INS-00031] Instalación Hogar FO', cant: 1, precioUsd: 0, precioBs: '0' }
    ],
    onuAsignada: null
  },
  {
    id: '77528',
    solicitud: 'S18442',
    nombre: 'Juan Carlos Demo',
    ci: '10000002',
    telefono: '+58 414-0003344',
    email: 'cliente.demo2@example.com',
    direccion: 'San Agustín FTTH, Sector Hornos de Cal, Edif. 3',
    zona: 'Hornos de Cal',
    oltAsignada: 'OLT SanAgustin',
    tipoServicio: 'FIBRA OPTICA',
    fechaInstalacion: '10/16/2026',
    fechaCambioPlan: '',
    instalador: 'CARLOS MENDOZA',
    estado: 'Pendiente',
    itemsContrato: [
      { desc: '[FO-00134] Fibra Óptica Hogar Asimet 300 mbps', cant: 30, precioUsd: 15, precioBs: '13185.78' },
      { desc: '[SER-000015] Servicio ONU-WIFI', cant: 1, precioUsd: 3, precioBs: '2641.24' },
      { desc: '[INS-00031] Instalación Hogar FO', cant: 1, precioUsd: 0, precioBs: '0' }
    ],
    onuAsignada: null
  }
];

const { Pool } = require('pg');

// Conexión a Odoo Producción en LAN
const odooPool = new Pool({
  host: process.env.ODOO_DB_HOST || '10.0.10.50',
  port: parseInt(process.env.ODOO_DB_PORT || '5432'),
  user: process.env.ODOO_DB_USER || 'odoo',
  password: process.env.ODOO_DB_PASSWORD || 'YOUR_DB_PASSWORD',
  database: process.env.ODOO_DB_NAME || 'multicanal',
  max: 5,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 6000
});

// Función interna que busca una IP Fija libre real consultando SmartOLT Y Odoo
async function obtenerIpLibreSmartOlt(oltName = 'OLT SanAgustin') {
  const configRed = SUBREDES_MULTICANAL[oltName] || SUBREDES_MULTICANAL['OLT SanAgustin'];
  const oltId = oltName === 'OLTANDRESBELLO' ? 3 : 4;

  const ipsOcupadas = new Set();

  // 1. Obtener IPs ocupadas registradas en SmartOLT
  try {
    const res = await callSmartOlt(`/api/onu/get_all_onus_details?olt_id=${oltId}`);
    const onus = res.onus || [];
    onus.forEach(o => {
      if (o.ip_address) ipsOcupadas.add(o.ip_address.trim());
    });
  } catch (e) {
    console.error('⚠️ [SmartOLT] Error al consultar ONUs para pool de IPs:', e.message);
  }

  // 2. OBTENER IPs OCUPADAS EN ODOO (project_task y sale_subscription) PARA EVITAR DUPLICADOS Y CONSTRAINT ERRORS
  try {
    const odooIpsRes = await odooPool.query(`
      SELECT ip_lan FROM project_task WHERE ip_lan IS NOT NULL
      UNION
      SELECT ip_lan FROM sale_subscription WHERE ip_lan IS NOT NULL;
    `);
    odooIpsRes.rows.forEach(r => {
      if (r.ip_lan) ipsOcupadas.add(r.ip_lan.trim());
    });
  } catch (eOdooIps) {
    console.warn('⚠️ No se pudieron consultar IPs ocupadas en Odoo:', eOdooIps.message);
  }

  // 3. Buscar la primera IP disponible garantizada en ambos sistemas
  for (let host = 10; host <= 250; host++) {
    const candidateIp = `${configRed.prefix}.${host}`;
    if (!ipsOcupadas.has(candidateIp)) {
      return {
        ip: candidateIp,
        mask: configRed.mask,
        gateway: configRed.gateway,
        dns1: configRed.dns1,
        dns2: configRed.dns2,
        vlan: configRed.vlan
      };
    }
  }

  return {
    ip: `${configRed.prefix}.${Math.floor(Math.random() * 100) + 100}`,
    mask: configRed.mask,
    gateway: configRed.gateway,
    dns1: configRed.dns1,
    dns2: configRed.dns2,
    vlan: configRed.vlan
  };
}

// Usuario del Sistema oficial para registrar operaciones automáticas en Odoo (Ops Coordinator)
const ODOO_SYSTEM_USER = {
  id: 278, // Ops Coordinator (res_users)
  name: 'Ops Coordinator',
  partnerId: 36730 // Ops Coordinator (res_partner para author_id en Chatter)
};

// Función de Asignación por Zonas y Rotación de Ayudantes (Idéntica a daily_crm_report.py)
function asignarCuadrillasYZonas(leads) {
  // Rotación diaria de ayudantes por día del año (daily_crm_report.py)
  const now = new Date();
  const startOfYear = new Date(now.getFullYear(), 0, 0);
  const dayOfYear = Math.floor((now - startOfYear) / (1000 * 60 * 60 * 24));
  const ayudanteMixta = (dayOfYear % 2 === 0) ? "Luis Rojas" : "Diego Vargas";
  const ayudanteFibra = (dayOfYear % 2 === 0) ? "Diego Vargas" : "Luis Rojas";

  const tecnicosMixta = "Andrés Silva & Roberto Díaz";
  const tecnicosFibra = "Miguel Torres & Carlos Mendoza";
  const onuAssignments = getOnuAssignments();
  const postponedMap = getPostponed();

  // Agrupar por ubicación principal (loc)
  const zones = {};
  leads.forEach(l => {
    const u = (l.ubicacion || '').trim();
    const z = (l.zona || '').trim();
    const loc = u ? u : (z ? z : "Otras Zonas");
    if (!zones[loc]) zones[loc] = [];
    zones[loc].push(l);
  });

  const asignacionPorZona = {};
  const remainingZones = {};
  let countMixta = 0;
  let countFibra = 0;

  // 1. Zonas con Radio Frecuencia van obligatoriamente a Cuadrilla Mixta
  Object.entries(zones).forEach(([loc, locRows]) => {
    const hasRf = locRows.some(r => r.equipo === 'Radio Frecuencia');
    if (hasRf) {
      asignacionPorZona[loc] = {
        cuadrilla: 'Cuadrilla Mixta (RF + Fibra)',
        tecnicos: tecnicosMixta,
        instaladorPrincipal: 'ANDRES SILVA',
        ayudante: ayudanteMixta,
        color: '#8b5cf6'
      };
      countMixta += locRows.length;
    } else {
      remainingZones[loc] = locRows;
    }
  });

  // 2. Balancear zonas restantes por tamaño descendente
  const sortedZones = Object.entries(remainingZones).sort((a, b) => b[1].length - a[1].length);
  for (const [loc, locRows] of sortedZones) {
    if (countMixta <= countFibra) {
      asignacionPorZona[loc] = {
        cuadrilla: 'Cuadrilla Mixta (RF + Fibra)',
        tecnicos: tecnicosMixta,
        instaladorPrincipal: 'ANDRES SILVA',
        ayudante: ayudanteMixta,
        color: '#8b5cf6'
      };
      countMixta += locRows.length;
    } else {
      asignacionPorZona[loc] = {
        cuadrilla: 'Cuadrilla Solo Fibra',
        tecnicos: tecnicosFibra,
        instaladorPrincipal: 'CARLOS MENDOZA',
        ayudante: ayudanteFibra,
        color: '#06b6d4'
      };
      countFibra += locRows.length;
    }
  }

  // 3. Mapear cada cliente con contrato, cuadrilla y ayudante
  const result = leads.map(l => {
    const u = (l.ubicacion || '').trim();
    const z = (l.zona || '').trim();
    const loc = u ? u : (z ? z : "Otras Zonas");
    const asig = asignacionPorZona[loc] || {
      cuadrilla: 'Cuadrilla Solo Fibra',
      tecnicos: tecnicosFibra,
      instaladorPrincipal: 'CARLOS MENDOZA',
      ayudante: ayudanteFibra,
      color: '#06b6d4'
    };

    const subCode = l.subscription || `SUB${l.lead_id}`;
    const zonaMostrar = z ? z : (u ? u : 'Sin Zona');

    return {
      id: subCode,
      lead_id: l.lead_id,
      partner_id: l.partner_id || 51409,
      solicitud: l.solicitud || `S${l.lead_id}`,
      nombre: l.nombre || 'Cliente Multicanal',
      ci: l.ci || 'V-PENDIENTE',
      telefono: l.telefono || 'Sin teléfono',
      email: l.email || '',
      direccion: l.direccion || u || 'Dirección en proceso',
      ubicacion: u,
      zona: zonaMostrar,
      equipo: l.equipo,
      tipoServicio: l.equipo === 'Radio Frecuencia' ? 'RADIO FRECUENCIA' : 'FIBRA OPTICA',
      oltAsignada: (zonaMostrar.toLowerCase().includes('san agust') || zonaMostrar.toLowerCase().includes('marin') || u.toLowerCase().includes('san agust')) ? 'OLT SanAgustin' : 'OLTANDRESBELLO',
      fechaInstalacion: getFechaCaracas(),
      fechaAprobacion: l.fecha_aprobacion,
      cuadrilla: asig.cuadrilla,
      cuadrillaColor: asig.color,
      tecnicos: asig.tecnicos,
      instalador: asig.instaladorPrincipal,
      ayudante: asig.ayudante,
      estado: (postponedMap[l.lead_id] || postponedMap[subCode] || postponedMap[String(l.lead_id)]) ? 'Pospuesto para mañana' : 'Pendiente',
      postponedInfo: postponedMap[l.lead_id] || postponedMap[subCode] || postponedMap[String(l.lead_id)] || null,
      itemsContrato: [
        { desc: `[FO-00133] ${l.equipo === 'Radio Frecuencia' ? 'Plan RF Antena Residencial' : 'Fibra Óptica Hogar Asimet 200 mbps'}`, cant: 30, precioUsd: 10, precioBs: '8790.52' },
        { desc: '[SER-000015] Servicio ONU-WIFI', cant: 1, precioUsd: 3, precioBs: '2641.24' },
        { desc: '[INS-00031] Instalación Hogar', cant: 1, precioUsd: 0, precioBs: '0' }
      ],
      onuAsignada: onuAssignments[l.lead_id] || onuAssignments[subCode] || onuAssignments['SUB' + l.lead_id] || onuAssignments[String(l.lead_id)] || null
    };
  });

  // Ordenar por Cuadrilla y luego por ID descendente
  result.sort((a, b) => {
    if (a.cuadrilla !== b.cuadrilla) return a.cuadrilla.localeCompare(b.cuadrilla);
    return b.lead_id - a.lead_id;
  });

  return result;
}

// Gestión de Archivos en data/
const USERS_FILE = path.join(DATA_DIR, 'users.json');
const OVERRIDES_FILE = path.join(DATA_DIR, 'assignments_override.json');

const INITIAL_USERS = [
  { username: 'admin', nombre: 'Administrador Multicanal', rol: 'admin', cuadrilla: 'Todas', password: 'admin', activo: true },
  { username: 'cmendoza', nombre: 'Carlos Mendoza', rol: 'tecnico', cuadrilla: 'Cuadrilla Solo Fibra', password: '1234', activo: true },
  { username: 'mtorres', nombre: 'Miguel Torres', rol: 'tecnico', cuadrilla: 'Cuadrilla Solo Fibra', password: '1234', activo: true },
  { username: 'asilva', nombre: 'Andrés Silva', rol: 'tecnico', cuadrilla: 'Cuadrilla Mixta (RF + Fibra)', password: '1234', activo: true },
  { username: 'rdiaz', nombre: 'Roberto Díaz', rol: 'tecnico', cuadrilla: 'Cuadrilla Mixta (RF + Fibra)', password: '1234', activo: true },
  { username: 'jmorales', nombre: 'Javier Morales', rol: 'tecnico', cuadrilla: 'Cuadrilla Mixta (RF + Fibra)', password: '1234', activo: true },
  { username: 'gcastro', nombre: 'Gabriel Castro', rol: 'tecnico', cuadrilla: 'Cuadrilla Solo Fibra', password: '1234', activo: true },
  { username: 'lrojas', nombre: 'Luis Rojas', rol: 'ayudante', cuadrilla: 'Rotativo', password: '1234', activo: true },
  { username: 'dvargas', nombre: 'Diego Vargas', rol: 'ayudante', cuadrilla: 'Rotativo', password: '1234', activo: true }
];

function getUsers() {
  if (!fs.existsSync(USERS_FILE)) {
    fs.writeFileSync(USERS_FILE, JSON.stringify(INITIAL_USERS, null, 2));
    return INITIAL_USERS;
  }
  try {
    return JSON.parse(fs.readFileSync(USERS_FILE, 'utf8'));
  } catch (e) {
    return INITIAL_USERS;
  }
}

function saveUsers(users) {
  fs.writeFileSync(USERS_FILE, JSON.stringify(users, null, 2));
}

function getOverrides() {
  if (!fs.existsSync(OVERRIDES_FILE)) return {};
  try {
    return JSON.parse(fs.readFileSync(OVERRIDES_FILE, 'utf8'));
  } catch (e) {
    return {};
  }
}


function buscarOverride(c, overrides) {
  if (!overrides || typeof overrides !== 'object') return null;
  const keys = [
    c.id,
    c.subscription,
    c.subCode,
    c.lead_id ? String(c.lead_id) : null,
    c.lead_id ? `SUB${c.lead_id}` : null,
    c.clientId
  ].filter(Boolean);

  for (const k of keys) {
    if (overrides[k]) return overrides[k];
  }
  return null;
}

function saveOverrides(overrides) {
  fs.writeFileSync(OVERRIDES_FILE, JSON.stringify(overrides, null, 2));
}

// 1. Endpoint de Login
app.post('/api/auth/login', (req, res) => {
  const { username, password } = req.body;
  const users = getUsers();
  const u = users.find(x => x.username.toLowerCase() === (username || '').trim().toLowerCase());

  if (!u) {
    return res.status(401).json({ success: false, message: 'Usuario no encontrado.' });
  }
  if (!u.activo) {
    return res.status(403).json({ success: false, message: '⛔ Acceso deshabilitado por la administración.' });
  }
  if (req.body.biometric === true || req.body.isPinLogin === true) {
    console.log(`🔑 Acceso verificado por hardware / PIN para: ${u.username}`);
  } else if (u.password !== password) {
    return res.status(401).json({ success: false, message: 'Contraseña incorrecta.' });
  }

  res.json({
    success: true,
    user: {
      username: u.username,
      nombre: u.nombre,
      rol: u.rol,
      cuadrilla: u.cuadrilla,
      activo: u.activo
    }
  });
});

// 2. Endpoints de Admin: Listar y gestionar usuarios
app.get('/api/admin/users', (req, res) => {
  const users = getUsers().map(u => ({
    username: u.username,
    nombre: u.nombre,
    rol: u.rol,
    cuadrilla: u.cuadrilla,
    activo: u.activo
  }));
  res.json({ success: true, users });
});

app.post('/api/admin/users/toggle-status', (req, res) => {
  const { username } = req.body;
  const users = getUsers();
  const idx = users.findIndex(u => u.username === username);
  if (idx === -1) return res.status(404).json({ success: false, message: 'Usuario no encontrado' });

  users[idx].activo = !users[idx].activo;
  saveUsers(users);
  res.json({ success: true, activo: users[idx].activo });
});

app.post('/api/admin/users/change-password', (req, res) => {
  const { username, newPassword } = req.body;
  if (!newPassword || newPassword.trim().length < 2) {
    return res.status(400).json({ success: false, message: 'La contraseña debe tener al menos 2 caracteres.' });
  }
  const users = getUsers();
  const idx = users.findIndex(u => u.username === username);
  if (idx === -1) return res.status(404).json({ success: false, message: 'Usuario no encontrado' });

  users[idx].password = newPassword.trim();
  saveUsers(users);
  res.json({ success: true, message: `Contraseña actualizada para ${username}.` });
});

// 3. Reasignación Manual por la Oficina
app.post('/api/admin/assignments/override', (req, res) => {
  const { clientId, leadId, cuadrilla, instalador, ayudante } = req.body;
  if (!clientId && !leadId) return res.status(400).json({ success: false, message: 'Falta ID de cliente' });

  const overrides = getOverrides();
  const ovData = {
    cuadrilla: cuadrilla || 'Cuadrilla Solo Fibra',
    instalador: instalador || 'CARLOS MENDOZA',
    ayudante: ayudante || 'Luis Rojas',
    updatedAt: new Date().toISOString()
  };

  // Guardar bajo todas las variantes para que nunca se pierda por cambio de clave en Odoo
  if (clientId) overrides[clientId] = ovData;
  if (leadId) {
    overrides[String(leadId)] = ovData;
    overrides[`SUB${leadId}`] = ovData;
  }

  // Buscar en clientes en memoria para encontrar sus alias
  if (odooApprovedClients && odooApprovedClients.length > 0) {
    const match = odooApprovedClients.find(c => c.id === clientId || c.lead_id == clientId || (leadId && c.lead_id == leadId));
    if (match) {
      if (match.id) overrides[match.id] = ovData;
      if (match.lead_id) {
        overrides[String(match.lead_id)] = ovData;
        overrides[`SUB${match.lead_id}`] = ovData;
      }
      if (match.subscription) overrides[match.subscription] = ovData;
    }
  }

  saveOverrides(overrides);
  res.json({ success: true, message: 'Reasignación guardada con éxito.', override: ovData });
});

// 3.1. Asignar ONU Escaneada/Pistoleada a un Cliente
app.post('/api/onu/assign', (req, res) => {
  const { clientId, leadId, serialPon, serialSecundario, fotoEtiqueta, user } = req.body;
  if (!clientId && !leadId) {
    return res.status(400).json({ success: false, message: 'Falta ID del cliente o lead' });
  }
  if (!serialPon) {
    return res.status(400).json({ success: false, message: 'El Serial PON es obligatorio' });
  }

  const cleanPon = serialPon.trim().toUpperCase();
  const cleanSec = (serialSecundario || '').trim().toUpperCase();

  const assignments = getOnuAssignments();
  const record = {
    clientId: clientId || ('SUB' + leadId),
    leadId: leadId || (clientId ? clientId.replace(/\D/g, '') : null),
    serialPon: cleanPon,
    serialSecundario: cleanSec,
    fotoEtiqueta: fotoEtiqueta || null,
    assignedAt: new Date().toISOString(),
    assignedBy: user || 'operador'
  };

  if (leadId) assignments[String(leadId)] = record;
  if (clientId) assignments[String(clientId)] = record;

  saveOnuAssignments(assignments);
  console.log(`📡 ONU Asignada a Lead/Cliente ${clientId || leadId}: PON=${cleanPon}, SEC=${cleanSec}`);

  const logEntry = {
    id: `ONU-ASSIGN-${clientId || leadId}-${Date.now()}`,
    tipo: 'ACTIVIDAD_EN_VIVO',
    evento: 'ONU_PISTOLEADA',
    clientId: record.clientId,
    leadId: record.leadId,
    nombre: `Cliente ${record.clientId}`,
    tecnico: record.assignedBy,
    serialPon: cleanPon,
    serialSecundario: cleanSec,
    mensaje: `ONU ${cleanPon} pistoleada y asignada por ${record.assignedBy}`,
    hora: getHoraCaracas(),
    fecha: getFechaHoraCaracas(),
    timestamp: Date.now()
  };
  appendInstallationLog(logEntry);
  broadcastRealtime('ONU_ASSIGNED', { assignment: record, entry: logEntry, kpis: calcularKpis() });

  res.json({ success: true, message: 'ONU asignada correctamente', assignment: record });
});

// 3.2. Obtener asignaciones de ONU guardadas
app.get('/api/onu/assignments', (req, res) => {
  res.json({ success: true, assignments: getOnuAssignments() });
});

// 3.3. Consultar estado en vivo de ONUs no configuradas / Detección de Duplicados en SmartOLT
app.get('/api/smartolt/check-unconfigured', async (req, res) => {
  const targetSn = (req.query.sn || '').trim().toUpperCase();
  try {
    const raw = await callSmartOlt('/api/onu/unconfigured_onus');
    const allOnus = raw.response || [];

    let filtered = allOnus;
    if (targetSn) {
      filtered = allOnus.filter(o => (o.sn || '').toUpperCase().includes(targetSn));
    }

    // Análisis de duplicidad por OLT/Board/Port o por SN similar
    const isDuplicated = filtered.length > 1;

    
    res.json({
      success: true,
      targetSn: targetSn || null,
      totalEnOlt: allOnus.length,
      count: filtered.length,
      isDuplicated: isDuplicated,
      clean: filtered.length === 1,
      items: filtered,
      allUnconfigured: allOnus.map(o => ({
        sn: o.sn,
        olt_id: o.olt_id,
        board: o.board,
        port: o.port,
        onu_type_name: o.onu_type_name,
        first_seen_at: o.first_seen_at,
        last_seen_at: o.last_seen_at
      }))
    });
  } catch (err) {
    console.error('Error al consultar SmartOLT unconfigured:', err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

// 3.4. Marcar cliente como "No se pudo atender hoy / Posponer para mañana"
app.post('/api/installations/postpone', (req, res) => {
  const { clientId, leadId, nombre, motivo, nota, tecnico, ayudante, cuadrilla } = req.body;
  if (!clientId && !leadId) return res.status(400).json({ success: false, message: 'Falta ID de cliente o lead' });
  
  const postponed = getPostponed();
  const idKey = String(leadId || clientId);
  const entry = {
    clientId: clientId || ('SUB' + leadId),
    leadId: leadId ? parseInt(leadId) : null,
    nombre: nombre || 'Cliente Multicanal',
    motivo: motivo || 'No se pudo atender hoy',
    nota: nota || '',
    tecnico: tecnico || 'Técnico de Campo',
    ayudante: ayudante || '',
    cuadrilla: cuadrilla || '',
    fechaPostergado: new Date().toISOString(),
    fechaMostrar: getFechaHoraCaracas()
  };
  postponed[idKey] = entry;
  if (clientId) postponed[String(clientId)] = entry;
  savePostponed(postponed);

  // Liberar de atenciones activas
  delete activeInstallations[String(clientId)];
  if (leadId) delete activeInstallations[String(leadId)];

  // Registrar en el log de auditoría
  const logEntry = {
    id: `POSTPONE-${idKey}-${Date.now()}`,
    leadId: entry.leadId,
    clientId: entry.clientId,
    nombre: entry.nombre,
    tecnico: entry.tecnico,
    ayudante: entry.ayudante,
    cuadrilla: entry.cuadrilla,
    estado: 'Pospuesto para mañana',
    motivo: entry.motivo,
    nota: entry.nota,
    fecha: entry.fechaMostrar,
    hora: getHoraCaracas(),
    timestamp: Date.now()
  };
  appendInstallationLog(logEntry);

  broadcastRealtime('CLIENT_POSTPONED', {
    clientId: entry.clientId,
    leadId: entry.leadId,
    entry,
    activeInstallations,
    kpis: calcularKpis()
  });

  console.log(`⏳ Cliente ${entry.nombre} (${entry.clientId}) pospuesto para mañana. Motivo: ${entry.motivo}`);
  res.json({ success: true, message: 'Cliente pospuesto para mañana con éxito', postponed: entry });
});

// 3.5. Obtener logs de auditoría y supervisión para el Admin
app.get('/api/admin/logs', (req, res) => {
  const logs = getInstallationsLog();
  const postponed = getPostponed();
  const kpis = calcularKpis();
  res.json({ success: true, kpis, logs, postponed, activeInstallations });
});

// 3.5.1. Exportar a Excel (.xlsx) con filtros avanzados por periodo, técnico, ayudante y estado
app.get('/api/admin/logs/export-excel', (req, res) => {
  try {
    const { desde, hasta, from, to, tecnico, ayudante, estado, soloExitosas } = req.query;
    const fDesde = desde || from || '';
    const fHasta = hasta || to || '';
    let logs = getInstallationsLog();

    // 1. Filtrar por Fecha Desde
    if (fDesde) {
      const dFrom = new Date(fDesde + 'T00:00:00-04:00');
      logs = logs.filter(l => {
        const d = l.timestamp ? new Date(l.timestamp) : (l.createdAt ? new Date(l.createdAt) : null);
        return d ? d >= dFrom : true;
      });
    }

    // 2. Filtrar por Fecha Hasta
    if (fHasta) {
      const dTo = new Date(fHasta + 'T23:59:59.999-04:00');
      logs = logs.filter(l => {
        const d = l.timestamp ? new Date(l.timestamp) : (l.createdAt ? new Date(l.createdAt) : null);
        return d ? d <= dTo : true;
      });
    }

    // 3. Filtrar por Técnico / Instalador
    if (tecnico && tecnico.trim() !== '') {
      const tec = tecnico.toLowerCase().trim();
      const tecAliases = {
        'carlos mendoza': ['tony', 'cmendoza'],
        'roberto diaz': ['edwin', 'rdiaz'],
        'miguel torres': ['jesus', 'mtorres'],
        'andres silva': ['jean', 'asilva']
      };
      const searchTerms = [tec];
      for (const [key, aliases] of Object.entries(tecAliases)) {
        if (tec.includes(key) || aliases.some(a => tec.includes(a))) {
          searchTerms.push(key, ...aliases);
        }
      }
      logs = logs.filter(l => {
        const val = `${l.tecnico || ''} ${l.instalador || ''} ${l.postponerUser || ''}`.toLowerCase();
        return searchTerms.some(term => val.includes(term));
      });
    }

    // 4. Filtrar por Ayudante
    if (ayudante && ayudante.trim() !== '') {
      const ayu = ayudante.toLowerCase().trim();
      logs = logs.filter(l => {
        const val = `${l.ayudante || ''}`.toLowerCase();
        return val.includes(ayu);
      });
    }

    // 5. Filtrar por Estado / Tipo
    if (estado === 'exitosas' || estado === 'instaladas' || soloExitosas === 'true') {
      logs = logs.filter(l => {
        const est = (l.estado || '').toLowerCase();
        return est.includes('instalad') || l.tipo === 'INSTALACION_FINALIZADA';
      });
    } else if (estado === 'pospuestos') {
      logs = logs.filter(l => {
        const est = (l.estado || '').toLowerCase();
        return est.includes('pospuesto') || l.motivo;
      });
    } else if (estado === 'en_vivo') {
      logs = logs.filter(l => l.tipo === 'ACTIVIDAD_EN_VIVO');
    }

    const wb = XLSX.utils.book_new();

    // HOJA 1: REGISTRO GENERAL DE INSTALACIONES Y OPERACIONES
    const rowsOperaciones = logs.map(l => {
      const d = l.timestamp ? new Date(l.timestamp) : (l.createdAt ? new Date(l.createdAt) : null);
      const fechaStr = l.fecha || (d ? getFechaHoraCaracas(d).split(' ')[0] : 'N/A');
      const horaStr = l.hora || (d ? getHoraCaracas(d) : 'N/A');
      const gpsRaw = l.gpsCoords || '';
      const mapsLink = extractGpsLink(gpsRaw) || (gpsRaw ? `https://www.google.com/maps?q=${gpsRaw}` : 'No registrada');

      return {
        'Fecha': fechaStr,
        'Hora': horaStr,
        'Evento / Acción': l.evento || l.tipo || (l.estado || 'REGISTRO'),
        'Estado': l.estado || (l.tipo === 'INSTALACION_FINALIZADA' ? 'Instalado' : (l.motivo ? 'Pospuesto' : 'En Atención')),
        'ID / Código SUB': l.clientId || l.subCode || l.id || '',
        'Lead Odoo (CRM)': l.leadId || '',
        'Nro Solicitud': l.solicitud || '',
        'Tarea Odoo (#)': l.odooTaskId ? `#${l.odooTaskId}` : '',
        'Nombre Cliente': l.nombre || l.clientName || '',
        'Cédula / RIF': l.ci || '',
        'Teléfono': l.telefono || '',
        'Dirección': l.direccion || '',
        'Zona': l.zona || '',
        'Cuadrilla': l.cuadrilla || '',
        'Instalador Principal': l.instalador || l.tecnico || '',
        'Ayudante': l.ayudante || '',
        'Tipo de Servicio': l.tipoServicio || (l.equipo ? l.equipo.toUpperCase() : 'FIBRA OPTICA'),
        'Serial PON ONU': l.serialPon || l.serialOnu || '',
        'Serial Fábrica LP (Odoo)': l.serialSecundario || l.serialFisicoLP || '',
        'Potencia Óptica (dBm)': l.potenciaDbm || '',
        'Caja NAP': l.lugarNap || l.codigoNap || '',
        'Puerto NAP': l.puertoNap || '',
        'Drop Inicio (m)': l.dropInicio || 0,
        'Drop Final (m)': l.dropFinal || 0,
        'Drop Total (m)': l.dropTotal ?? l.metrosDrop ?? 0,
        'UTP Inicio (m)': l.utpInicio || 0,
        'UTP Final (m)': l.utpFinal || 0,
        'UTP Total (m)': l.utpTotal ?? l.metrosUtp ?? 0,
        'Coordenadas GPS': gpsRaw,
        'Enlace Google Maps': mapsLink,
        'IP LAN / Config': l.ipLan || 'DHCP (Automática)',
        'SmartOLT Estado': l.smartOltStatus || '',
        'SmartOLT Detalle': l.smartOltDetalle || '',
        'Forma de Pago': l.formaPago || '',
        'Observaciones / Detalle': l.observaciones || l.detalle || l.mensaje || '',
        'Motivo Postergación': l.motivo || '',
        'Nota Postergación': l.nota || '',
        'Contrato Firmado (PDF)': (l.contratoPdfBase64 || l.contratoPdf) ? 'SÍ (Adjunto en Odoo)' : 'NO',
        'Firma Digital': l.firmaCliente ? 'SÍ (Registrada)' : 'NO',
        'Fotos Adjuntas': l.fotos ? Object.keys(l.fotos).filter(k => l.fotos[k]).length : (l.adjuntosCount || 0),
        'Editado por Admin': l.editadoPorAdmin ? 'SÍ' : 'NO',
        'Nota Corrección Admin': l.notaCorreccion || '',
        'Usuario que Editó': l.adminUser || ''
      };
    });

    const ws1 = XLSX.utils.json_to_sheet(rowsOperaciones.length > 0 ? rowsOperaciones : [{ 'Mensaje': 'No hay registros para los filtros seleccionados' }]);
    XLSX.utils.book_append_sheet(wb, ws1, 'Operaciones e Instalaciones');

    // HOJA 2: MATERIALES Y EQUIPOS UTILIZADOS
    const rowsMateriales = logs.filter(l => l.materiales || l.dropTotal || l.metrosDrop || l.equiposCheck).map(l => {
      const mats = l.materiales || {};
      const eq = l.equiposCheck || {};
      return {
        'Fecha': l.fecha || '',
        'ID Cliente (SUB)': l.clientId || l.subCode || l.id || '',
        'Cliente': l.nombre || l.clientName || '',
        'Instalador': l.instalador || l.tecnico || '',
        'Ayudante': l.ayudante || '',
        'Cable Drop (mts)': l.dropTotal ?? l.metrosDrop ?? 0,
        'Cable UTP (mts)': l.utpTotal ?? l.metrosUtp ?? 0,
        'Adaptador FO': parseInt(mats.adaptador_fo) || 0,
        'Grapas': parseInt(mats.grapas) || 0,
        'Conector FO': parseInt(mats.conector_fo) || 0,
        'Conectores RJ45': parseInt(mats.conectores_rj45) || 0,
        'Patch Cord FO': parseInt(mats.patch_cord_fo) || 0,
        'Patch Cord UTP': parseInt(mats.patch_cord_utp) || 0,
        'Roseta FO': parseInt(mats.roseta) || 0,
        'Ramplug': parseInt(mats.ramplug) || 0,
        'Tie Wrap': parseInt(mats.tie_wrap) || 0,
        'Caja Metálica': parseInt(mats.caja_metalica) || 0,
        'Mástil': parseInt(mats.mastil) || 0,
        'Vientos': parseInt(mats.vientos) || 0,
        'ONU': eq.onu ? 'Sí' : 'No',
        'Router Mesh': eq.routerMesh ? 'Sí' : 'No',
        'MikroTik': eq.mikrotik ? 'Sí' : 'No',
        'UPS': eq.ups ? 'Sí' : 'No',
        'Switch': eq.switch ? 'Sí' : 'No',
        'Antena': eq.antena ? 'Sí' : 'No'
      };
    });

    const ws2 = XLSX.utils.json_to_sheet(rowsMateriales.length > 0 ? rowsMateriales : [{ 'Mensaje': 'No hay consumo de materiales para los filtros seleccionados' }]);
    XLSX.utils.book_append_sheet(wb, ws2, 'Consumo de Materiales');

    // HOJA 3: AUDITORÍA SMARTOLT Y ODOO
    const rowsSmartOlt = logs.filter(l => l.serialPon || l.odooTaskId || l.smartOltStatus).map(l => {
      return {
        'ID SUB': l.clientId || l.subCode || l.id || '',
        'Lead Odoo': l.leadId || '',
        'Tarea Odoo': l.odooTaskId ? `#${l.odooTaskId}` : '',
        'Cliente': l.nombre || l.clientName || '',
        'Serial PON (SmartOLT)': l.serialPon || l.serialOnu || '',
        'Serial Fábrica LP (Odoo)': l.serialSecundario || l.serialFisicoLP || '',
        'OLT Asignada': l.oltAsignada || '',
        'SmartOLT Estado': l.smartOltStatus || '',
        'SmartOLT Detalle': l.smartOltDetalle || '',
        'IP Asignada (LAN)': l.ipLan || 'DHCP',
        'Caja NAP': l.lugarNap || l.codigoNap || '',
        'Puerto NAP': l.puertoNap || '',
        'Coordenadas GPS': l.gpsCoords || '',
        'Fecha y Hora': `${l.fecha || ''} ${l.hora || ''}`.trim()
      };
    });

    const ws3 = XLSX.utils.json_to_sheet(rowsSmartOlt.length > 0 ? rowsSmartOlt : [{ 'Mensaje': 'No hay auditoría SmartOLT/Odoo para los filtros seleccionados' }]);
    XLSX.utils.book_append_sheet(wb, ws3, 'Auditoría SmartOLT y Odoo');

    const excelBuffer = XLSX.write(wb, { type: 'buffer', bookType: 'xlsx' });
    const filename = `Reporte_Auditoria_${fDesde || 'Historico'}_al_${fHasta || 'Actual'}.xlsx`;

    res.setHeader('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    return res.send(excelBuffer);
  } catch (err) {
    console.error('Error generando archivo Excel:', err);
    res.status(500).json({ success: false, error: 'Error generando archivo Excel: ' + err.message });
  }
});

// ==========================================
// 3.5.2. TELEMETRÍA Y SPEEDTEST HTML5 EN TIEMPO REAL
// ==========================================
const SPEEDTEST_CHUNK = Buffer.alloc(512 * 1024, 0xAA); // Buffer estático de 512KB para cero overhead de RAM

// 1. Ping / Latencia RTT
app.get('/api/speedtest/ping', (req, res) => {
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0'
  });
  res.json({ success: true, timestamp: Date.now() });
});

// 2. Download Stream (Bajada en Mbps)
app.get('/api/speedtest/download', (req, res) => {
  const totalBytes = Math.min(parseInt(req.query.bytes || 25 * 1024 * 1024, 10), 80 * 1024 * 1024);
  res.set({
    'Content-Type': 'application/octet-stream',
    'Content-Length': totalBytes,
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0'
  });

  let bytesSent = 0;
  let isAborted = false;
  req.on('close', () => { isAborted = true; });

  function sendChunk() {
    if (isAborted || bytesSent >= totalBytes) {
      return res.end();
    }
    const remaining = totalBytes - bytesSent;
    const toSend = Math.min(remaining, SPEEDTEST_CHUNK.length);
    bytesSent += toSend;
    const chunk = toSend === SPEEDTEST_CHUNK.length ? SPEEDTEST_CHUNK : SPEEDTEST_CHUNK.subarray(0, toSend);
    const canContinue = res.write(chunk);
    if (canContinue) {
      process.nextTick(sendChunk);
    } else {
      res.once('drain', sendChunk);
    }
  }
  sendChunk();
});

// 3. Upload Stream (Subida en Mbps)
app.post('/api/speedtest/upload', (req, res) => {
  let bytesReceived = 0;
  const startTime = Date.now();
  req.on('data', chunk => {
    bytesReceived += chunk.length;
  });
  req.on('end', () => {
    const durationMs = Date.now() - startTime;
    res.set('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
    res.json({ success: true, bytesReceived, durationMs });
  });
});

// 3.6. Corregir datos de un log por parte del Administrador
app.post('/api/admin/logs/update', (req, res) => {
  const { logId, metrosDrop, dropTotal, metrosUtp, utpTotal, potenciaDbm, puertoNap, lugarNap, nota, adminUser } = req.body;
  if (!logId) return res.status(400).json({ success: false, message: 'Falta ID de registro (logId)' });
  const logs = getInstallationsLog();
  const item = logs.find(l => l.id === logId || l.logId === logId);
  if (!item) return res.status(404).json({ success: false, message: 'Registro no encontrado' });

  const valDrop = metrosDrop !== undefined ? metrosDrop : dropTotal;
  if (valDrop !== undefined) {
    item.metrosDrop = parseInt(valDrop) || 0;
    item.dropTotal = item.metrosDrop;
  }
  const valUtp = metrosUtp !== undefined ? metrosUtp : utpTotal;
  if (valUtp !== undefined) {
    item.metrosUtp = parseInt(valUtp) || 0;
    item.utpTotal = item.metrosUtp;
  }
  if (potenciaDbm !== undefined) item.potenciaDbm = potenciaDbm;
  if (puertoNap !== undefined) item.puertoNap = puertoNap;
  if (lugarNap !== undefined) item.lugarNap = lugarNap;
  if (nota !== undefined) {
    item.notaCorreccion = nota;
    item.nota = nota;
  }
  item.editadoPorAdmin = true;
  item.adminUser = adminUser || 'Admin';
  item.fechaEdicion = getFechaHoraCaracas();
  item.fechaCorreccion = getHoraCaracas();

  saveInstallationsLog(logs);
  console.log(`✏️ Log ${logId} corregido por el Administrador`);
  broadcastRealtime('LOG_UPDATED', { updatedLog: item, kpis: calcularKpis() });
  res.json({ success: true, message: 'Datos corregidos con éxito', updatedLog: item });
});

// 3.7. Canal Server-Sent Events (SSE) para sincronización en tiempo real entre administradores y técnicos
app.get('/api/realtime/stream', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  sseClients.push(res);
  // Enviar estado de arranque
  res.write(`data: ${JSON.stringify({ event: 'INIT', data: { activeInstallations, kpis: calcularKpis(), logs: getInstallationsLog().slice(-30) }, timestamp: Date.now() })}\n\n`);

  req.on('close', () => {
    sseClients = sseClients.filter(c => c !== res);
  });
});

// Heartbeat cada 20 segundos para mantener el túnel de Cloudflare y Nginx siempre activo
setInterval(() => {
  sseClients.forEach(clientRes => {
    try {
      clientRes.write(': ping\n\n');
    } catch (e) {}
  });
}, 20000);

// 3.8. Endpoint para sincronización rápida en segundo plano (Polling / Wakeup)
app.get('/api/realtime/sync', (req, res) => {
  res.json({
    success: true,
    activeInstallations,
    kpis: calcularKpis(),
    logs: getInstallationsLog().slice(-50),
    postponed: getPostponed(),
    serverTime: new Date().toISOString()
  });
});

// 3.9. Registrar evento de actividad en tiempo real (Inicio de atención, fotos, contrato, etc.)
app.post('/api/activity/track', (req, res) => {
  const { evento, clienteId, leadId, clienteNombre, tecnico, ayudante, detalle } = req.body;
  if (!evento || !clienteId) {
    return res.status(400).json({ success: false, message: 'Faltan parámetros requeridos' });
  }

  const horaLegible = getHoraCaracas();
  const fechaCompleta = getFechaHoraCaracas();

  let logMsg = '';
  if (evento === 'INICIO_ATENCION') {
    logMsg = `Técnico ${tecnico || 'Técnico'} inició atención en campo`;
    activeInstallations[String(clienteId)] = {
      clienteId,
      leadId,
      clienteNombre: clienteNombre || 'Cliente',
      tecnico: tecnico || 'Técnico de Campo',
      ayudante: ayudante || '',
      estado: 'En atención',
      inicio: horaLegible,
      ultimaActividad: horaLegible,
      actividad: 'Atendiendo en sitio'
    };
  } else if (evento === 'FOTO_SUBIDA') {
    logMsg = `Foto de ${detalle || 'evidencia'} cargada por ${tecnico || 'técnico'}`;
    if (activeInstallations[String(clienteId)]) {
      activeInstallations[String(clienteId)].ultimaActividad = horaLegible;
      activeInstallations[String(clienteId)].actividad = `Cargó foto ${detalle || ''}`;
    }
  } else if (evento === 'CONTRATO_DESCARGADO') {
    logMsg = `Contrato Digital Firmado generado y descargado por ${tecnico || 'técnico'}`;
    if (activeInstallations[String(clienteId)]) {
      activeInstallations[String(clienteId)].ultimaActividad = horaLegible;
      activeInstallations[String(clienteId)].actividad = 'Contrato PDF firmado';
    }
  } else if (evento === 'ONU_PISTOLEADA') {
    logMsg = `ONU ${detalle || ''} pistoleada/asignada por ${tecnico || 'operador'}`;
  }

  const logEntry = {
    id: `ACT-${clienteId}-${Date.now()}`,
    tipo: 'ACTIVIDAD_EN_VIVO',
    evento,
    clientId: clienteId,
    leadId: leadId,
    nombre: clienteNombre,
    tecnico: tecnico,
    ayudante: ayudante,
    detalle: detalle || logMsg,
    mensaje: logMsg,
    hora: horaLegible,
    fecha: fechaCompleta,
    timestamp: Date.now()
  };

  appendInstallationLog(logEntry);
  broadcastRealtime('ACTIVITY', { entry: logEntry, activeInstallations, kpis: calcularKpis() });
  console.log(`📡 [Actividad en Vivo] ${logMsg} (${clienteNombre || clienteId})`);

  res.json({ success: true, entry: logEntry });
});

// 3.10. Guardar Borrador Centralizado en Servidor (Fotos, Firma, Datos de Campo en Vivo)
app.post('/api/installations/draft', (req, res) => {
  const { clienteId, leadId, tecnico, ayudante, draftData } = req.body;
  if (!clienteId || !draftData) {
    return res.status(400).json({ success: false, message: 'Falta clienteId o draftData' });
  }

  const draftFile = path.join(DRAFTS_DIR, `${clienteId}.json`);
  let existing = {};
  if (fs.existsSync(draftFile)) {
    try { existing = JSON.parse(fs.readFileSync(draftFile, 'utf8')); } catch (e) {}
  }

  const mergedDraft = {
    ...existing,
    ...draftData,
    clienteId,
    leadId,
    tecnico: tecnico || existing.tecnico || 'Técnico de Campo',
    ayudante: ayudante || existing.ayudante || '',
    fotos: { ...(existing.fotos || {}), ...(draftData.fotos || {}) },
    updatedAt: new Date().toISOString(),
    horaLegible: getHoraCaracas()
  };

  try {
    fs.writeFileSync(draftFile, JSON.stringify(mergedDraft));
  } catch (eWrite) {
    return res.status(500).json({ success: false, message: eWrite.message });
  }

  // Registrar o actualizar atención activa en memoria
  activeInstallations[String(clienteId)] = {
    clienteId,
    leadId,
    clienteNombre: mergedDraft.nombre || 'Cliente',
    tecnico: mergedDraft.tecnico,
    ayudante: mergedDraft.ayudante,
    estado: 'En atención',
    inicio: activeInstallations[String(clienteId)]?.inicio || getHoraCaracas(),
    ultimaActividad: getHoraCaracas(),
    actividad: 'Actualizó fotos / firma / datos en sitio'
  };

  broadcastRealtime('DRAFT_UPDATED', {
    clienteId,
    leadId,
    tecnico: mergedDraft.tecnico,
    horaLegible: mergedDraft.horaLegible,
    activeInstallations,
    kpis: calcularKpis()
  });

  console.log(`💾 [Borrador Central] Avance sincronizado para cliente ${clienteId} por ${mergedDraft.tecnico}`);
  res.json({ success: true, message: 'Borrador centralizado guardado', horaLegible: mergedDraft.horaLegible });
});

// 3.11. Obtener Borrador Centralizado de un Cliente (Para que Sede/Admin vea lo de Campo)
app.get('/api/installations/draft/:clientId', (req, res) => {
  const cid = req.params.clientId;
  const draftFile = path.join(DRAFTS_DIR, `${cid}.json`);
  if (!fs.existsSync(draftFile)) {
    return res.json({ success: false, hasDraft: false, activeSession: activeInstallations[String(cid)] || null });
  }

  try {
    const draft = JSON.parse(fs.readFileSync(draftFile, 'utf8'));
    res.json({ success: true, hasDraft: true, draft, activeSession: activeInstallations[String(cid)] || null });
  } catch (e) {
    res.status(500).json({ success: false, message: e.message });
  }
});

// 3.12. Eliminar Borrador Centralizado de un Cliente
app.delete('/api/installations/draft/:clientId', (req, res) => {
  const cid = req.params.clientId;
  const draftFile = path.join(DRAFTS_DIR, `${cid}.json`);
  if (fs.existsSync(draftFile)) {
    try { fs.unlinkSync(draftFile); } catch (e) {}
  }
  delete activeInstallations[String(cid)];
  broadcastRealtime('DRAFT_CLEARED', { clienteId: cid, activeInstallations, kpis: calcularKpis() });
  res.json({ success: true });
});

// 3.13. Obtener Evidencias de Instalación Culminada (Modo Auditoría / Supervisión)
app.get('/api/installations/completed/:clientId', async (req, res) => {
  const cid = req.params.clientId;
  const variations = [
    cid,
    String(cid).replace(/^SUB/i, ''),
    'SUB' + String(cid).replace(/^SUB/i, '')
  ];

  let completedData = null;

  for (const v of variations) {
    const compFile = path.join(COMPLETED_DIR, `${v}.json`);
    if (fs.existsSync(compFile)) {
      try {
        completedData = JSON.parse(fs.readFileSync(compFile, 'utf8'));
        break;
      } catch (e) {}
    }
  }

  if (!completedData) {
    try {
      const files = fs.readdirSync(COMPLETED_DIR);
      for (const f of files) {
        if (!f.endsWith('.json')) continue;
        try {
          const comp = JSON.parse(fs.readFileSync(path.join(COMPLETED_DIR, f), 'utf8'));
          if (String(comp.clienteId) === String(cid) || String(comp.leadId) === String(cid) || variations.includes(String(comp.clienteId))) {
            completedData = comp;
            break;
          }
        } catch(e) {}
      }
    } catch(e) {}
  }

  if (!completedData) {
    const logs = getInstallationsLog();
    const foundLog = logs.find(l => (variations.includes(String(l.clientId)) || variations.includes(String(l.leadId)) || l.id === cid) && l.tipo === 'INSTALACION_FINALIZADA');
    if (foundLog) {
      completedData = foundLog;
    }
  }

  if (completedData) {
    // Si no tiene foto de velocidad en el JSON pero tiene odooTaskId, buscar en Odoo ir_attachment
    const hasPhoto = completedData.fotos && (completedData.fotos.velocidad || completedData.fotos.testVelocidad || completedData.fotos.foto7);
    if (!hasPhoto && completedData.odooTaskId && odooPool) {
      try {
        const attRes = await odooPool.query(`
          SELECT db_datas, mimetype FROM ir_attachment 
          WHERE res_model = 'project.task' AND res_id = $1 AND (name ILIKE '%Test_Velocidad%' OR name ILIKE '%Foto_7%')
          ORDER BY id DESC LIMIT 1;
        `, [parseInt(completedData.odooTaskId)]);
        if (attRes.rows.length > 0 && attRes.rows[0].db_datas) {
          const row = attRes.rows[0];
          const mime = row.mimetype || 'image/jpeg';
          const b64 = Buffer.isBuffer(row.db_datas) 
            ? row.db_datas.toString('base64') 
            : String(row.db_datas);
          completedData.fotos = completedData.fotos || {};
          completedData.fotos.velocidad = `data:${mime};base64,${b64}`;
          completedData.tieneSpeedtest = true;
        }
      } catch(eOdoo) {}
    }

    return res.json({ success: true, hasCompleted: true, completed: completedData });
  }

  res.json({ success: false, hasCompleted: false });
});

// 3.13.1. Listar instalaciones completadas con filtros por fecha, técnico y búsqueda
app.get('/api/installations/completed-list', async (req, res) => {
  try {
    const { username, desde, hasta, q } = req.query;
    const users = getUsers();
    const currentUser = username ? users.find(u => u.username.toLowerCase() === username.trim().toLowerCase()) : null;
    const esAdmin = currentUser ? currentUser.rol === 'admin' : true;

    // 1. Obtener registros desde installations_log.json
    const logs = getInstallationsLog();
    const finishedLogs = logs.filter(l => l.tipo === 'INSTALACION_FINALIZADA');

    // 2. Mapear y complementar con datos de COMPLETED_DIR si tienen speedtest
    const listMap = new Map();
    for (const log of finishedLogs) {
      const canonicalKey = String(log.clientId || (log.leadId ? `SUB${log.leadId}` : log.id)).toUpperCase();

      // Como finishedLogs viene ordenado descendente (más reciente primero),
      // si ya tenemos este cliente en el mapa, NO lo sobreescribimos con un log viejo duplicado
      if (listMap.has(canonicalKey)) {
        const existing = listMap.get(canonicalKey);
        if (!existing.ipLan && log.ipLan) existing.ipLan = log.ipLan;
        if (!existing.tieneSpeedtest && (log.tieneSpeedtest || log.fotoVelocidad)) existing.tieneSpeedtest = true;
        continue;
      }

      let tieneSpeedtest = Boolean(log.tieneSpeedtest || log.fotoVelocidad);
      let ipFromComp = log.ipLan || '';

      const targetKeys = [log.clientId, log.leadId, `SUB${log.leadId}`, canonicalKey].filter(Boolean);
      for (const k of targetKeys) {
        const compFile = path.join(COMPLETED_DIR, `${k}.json`);
        if (fs.existsSync(compFile)) {
          try {
            const comp = JSON.parse(fs.readFileSync(compFile, 'utf8'));
            if (!tieneSpeedtest && comp.fotos && (comp.fotos.velocidad || comp.fotos.testVelocidad || comp.fotos.foto7)) {
              tieneSpeedtest = true;
            }
            if (!ipFromComp && comp.ipLan) {
              ipFromComp = comp.ipLan;
            }
          } catch(e) {}
        }
      }

      listMap.set(canonicalKey, {
        id: log.id,
        clientId: log.clientId || canonicalKey,
        leadId: log.leadId,
        solicitud: log.solicitud || '',
        nombre: log.nombre || '',
        direccion: log.direccion || '',
        zona: log.zona || '',
        cuadrilla: log.cuadrilla || '',
        instalador: log.instalador || log.tecnico || '',
        ayudante: log.ayudante || '',
        serialPon: log.serialPon || log.serialOnu || '',
        potenciaDbm: log.potenciaDbm || '',
        puertoNap: log.puertoNap || '',
        lugarNap: log.lugarNap || '',
        odooTaskId: log.odooTaskId || null,
        ipLan: ipFromComp || '',
        fecha: log.fecha || '',
        hora: log.hora || '',
        timestamp: log.timestamp || Date.now(),
        tieneSpeedtest: tieneSpeedtest
      });
    }

    // También revisar archivos en COMPLETED_DIR que quizá no estén en el log (evitando duplicados por leadId vs clientId)
    try {
      const files = fs.readdirSync(COMPLETED_DIR);
      for (const f of files) {
        if (!f.endsWith('.json')) continue;
        const compPath = path.join(COMPLETED_DIR, f);
        try {
          const cData = JSON.parse(fs.readFileSync(compPath, 'utf8'));
          const clientRef = (cData.clienteId || '').toString().trim().toUpperCase();
          const leadRef = cData.leadId ? cData.leadId.toString().trim() : null;
          const subLeadRef = leadRef ? `SUB${leadRef}`.toUpperCase() : null;
          const serialRef = cData.serialPon ? cData.serialPon.toString().trim().toUpperCase() : null;
          const taskRef = cData.odooTaskId ? parseInt(cData.odooTaskId) : null;
          const rawKey = f.replace('.json', '').trim().toUpperCase();
          const canonicalKey = (rawKey.startsWith('SUB') ? rawKey : `SUB${rawKey}`).toUpperCase();

          // Buscar si ya existe en listMap por clave o por campos únicos
          let matchedItem = null;
          if (clientRef && listMap.has(clientRef)) matchedItem = listMap.get(clientRef);
          else if (subLeadRef && listMap.has(subLeadRef)) matchedItem = listMap.get(subLeadRef);
          else if (leadRef && listMap.has(leadRef)) matchedItem = listMap.get(leadRef);
          else if (listMap.has(canonicalKey)) matchedItem = listMap.get(canonicalKey);
          else if (listMap.has(rawKey)) matchedItem = listMap.get(rawKey);

          if (!matchedItem) {
            for (const item of listMap.values()) {
              if (taskRef && item.odooTaskId && parseInt(item.odooTaskId) === taskRef) { matchedItem = item; break; }
              if (serialRef && item.serialPon && item.serialPon.toString().trim().toUpperCase() === serialRef) { matchedItem = item; break; }
              if (leadRef && item.leadId && item.leadId.toString().trim() === leadRef) { matchedItem = item; break; }
              if (clientRef && item.clientId && item.clientId.toString().trim().toUpperCase() === clientRef) { matchedItem = item; break; }
            }
          }

          const tieneSt = Boolean(cData.fotos && (cData.fotos.velocidad || cData.fotos.testVelocidad || cData.fotos.foto7));

          if (matchedItem) {
            // Ya existe en la lista: no duplicar. Complementar speedtest si corresponde
            if (!matchedItem.tieneSpeedtest && tieneSt) {
              matchedItem.tieneSpeedtest = true;
            }
            continue;
          }

          // Si es un registro único no registrado en los logs
          listMap.set(canonicalKey, {
            id: `COMP-${canonicalKey}`,
            clientId: cData.clienteId || canonicalKey,
            leadId: cData.leadId,
            solicitud: cData.solicitud || '',
            nombre: cData.nombre || '',
            direccion: cData.direccion || '',
            zona: cData.zona || '',
            cuadrilla: cData.cuadrilla || '',
            instalador: cData.instalador || cData.tecnico || '',
            ayudante: cData.ayudante || '',
            serialPon: cData.serialPon || '',
            potenciaDbm: cData.potenciaDbm || '',
            puertoNap: cData.puertoNap || '',
            lugarNap: cData.lugarNap || '',
            odooTaskId: cData.odooTaskId || null,
            ipLan: cData.ipLan || '',
            fecha: cData.fecha || '',
            hora: cData.hora || '',
            timestamp: cData.timestamp || Date.now(),
            tieneSpeedtest: tieneSt
          });
        } catch(e) {}
      }
    } catch(eDir) {}

    let items = Array.from(listMap.values());

    // 3. Filtrar por usuario (si no es admin, solo ve sus instalaciones como técnico o ayudante)
    if (!esAdmin && currentUser) {
      const uNom = (currentUser.nombre || '').toLowerCase();
      const uUser = (currentUser.username || '').toLowerCase();
      const userKeywords = [];
      if (uUser.includes('rodriguez')) userKeywords.push('rodriguez', 'tony');
      else if (uUser.includes('perez')) userKeywords.push('perez', 'jesus');
      else if (uUser.includes('mejias')) userKeywords.push('mejias', 'jean');
      else if (uUser.includes('gutierrez')) userKeywords.push('gutierrez', 'edwin');
      else if (uUser.includes('santos')) userKeywords.push('santos', 'ernesto');
      else if (uUser.includes('rivas')) userKeywords.push('rivas', 'cesar');
      else if (uUser.includes('perozo')) userKeywords.push('perozo', 'dario');
      else if (uUser.includes('almeida')) userKeywords.push('almeida', 'kleiver');
      else userKeywords.push(uUser, uNom);

      items = items.filter(it => {
        const tec = (it.instalador || '').toLowerCase();
        const ayu = (it.ayudante || '').toLowerCase();
        return userKeywords.some(k => tec.includes(k) || ayu.includes(k));
      });
    }

    // 4. Filtrar por fecha (desde / hasta) con normalización robusta de zona horaria y comas
    const normalizeDate = (item) => {
      if (!item) return '';
      if (item.timestamp) {
        try {
          const d = new Date(Number(item.timestamp));
          if (!isNaN(d.getTime())) {
            const vzla = new Date(d.getTime() - 4 * 3600 * 1000);
            return vzla.toISOString().split('T')[0];
          }
        } catch(e) {}
      }
      const dStr = (typeof item === 'string' ? item : item.fecha) || '';
      if (!dStr) return '';
      const clean = dStr.replace(/,/g, '').trim();
      const firstPart = clean.split(' ')[0] || '';
      if (firstPart.includes('/')) {
        const p = firstPart.split('/');
        if (p.length === 3) {
          const y = p[2].replace(/\D/g, '');
          const m = p[1].replace(/\D/g, '').padStart(2, '0');
          const day = p[0].replace(/\D/g, '').padStart(2, '0');
          return `${y}-${m}-${day}`;
        }
      }
      return firstPart;
    };

    if (desde) {
      items = items.filter(it => normalizeDate(it) >= desde);
    }
    if (hasta) {
      items = items.filter(it => normalizeDate(it) <= hasta);
    }

    // 5. Filtrar por búsqueda de texto
    if (q) {
      const qLower = q.toLowerCase().trim();
      items = items.filter(it => 
        (it.nombre && it.nombre.toLowerCase().includes(qLower)) ||
        (it.clientId && it.clientId.toLowerCase().includes(qLower)) ||
        (it.direccion && it.direccion.toLowerCase().includes(qLower)) ||
        (it.serialPon && it.serialPon.toLowerCase().includes(qLower)) ||
        (it.instalador && it.instalador.toLowerCase().includes(qLower)) ||
        (it.ayudante && it.ayudante.toLowerCase().includes(qLower))
      );
    }

    // 6. Ordenar descendente (más recientes primero)
    items.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));

    res.json({ success: true, count: items.length, items });
  } catch(err) {
    console.error('Error al listar instalaciones completadas:', err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

// 3.13.1b. Rutas directas para el Wallboard de TV
app.get(['/wallboard', '/tv'], (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'wallboard.html'));
});

// 3.13.1c. API de Datos en Tiempo Real para el Wallboard de TV (Centro de Operaciones NOC)
app.get('/api/wallboard/data', async (req, res) => {
  try {
    const logs = getInstallationsLog();
    const hoyStrCaracas = getFechaCaracas();

    // 1. Filtrar instalaciones completadas con DEDUPLICACIÓN por cliente
    const finishedLogs = logs.filter(l => l.tipo === 'INSTALACION_FINALIZADA');
    
    // Función de deduplicación: mantiene solo el registro más reciente por cliente
    function deduplicarPorCliente(list) {
      const vistos = new Set();
      const resultado = [];
      for (const item of list) {
        const key = String(item.clientId || item.leadId || (item.nombre ? item.nombre.toLowerCase().trim() : '') || item.id);
        if (!vistos.has(key)) {
          vistos.add(key);
          resultado.push(item);
        }
      }
      return resultado;
    }

    const finishedLogsUnicos = deduplicarPorCliente(finishedLogs);

    const hoyLogs = finishedLogsUnicos.filter(l => {
      const d = l.fecha ? l.fecha.split(',')[0].trim() : '';
      return d === hoyStrCaracas;
    });

    let totalDropMts = 0;
    let sumaPotencias = 0;
    let cantPotencias = 0;
    let conSpeedtestCount = 0;

    const cuadrillasMap = new Map();
    const tecnicosDefault = [
      { nombre: 'Carlos Mendoza', cuadrilla: 'Cuadrilla Solo Fibra', ayudante: 'Diego Vargas' },
      { nombre: 'Roberto Díaz', cuadrilla: 'Cuadrilla Mixta (RF + Fibra)', ayudante: 'Andrés Silva' },
      { nombre: 'Andrés Silva', cuadrilla: 'Cuadrilla Mixta (RF + Fibra)', ayudante: 'Luis Rojas' },
      { nombre: 'Miguel Torres', cuadrilla: 'Cuadrilla Solo Fibra', ayudante: 'Diego Vargas' },
      { nombre: 'Javier Morales', cuadrilla: 'Cuadrilla Mixta (RF + Fibra)', ayudante: 'Ayudante' },
      { nombre: 'Gabriel Castro', cuadrilla: 'Cuadrilla Solo Fibra', ayudante: 'Ayudante' }
    ];

    tecnicosDefault.forEach(t => {
      cuadrillasMap.set(t.nombre.toUpperCase(), {
        tecnico: t.nombre,
        cuadrilla: t.cuadrilla,
        ayudante: t.ayudante,
        instaladasHoy: 0,
        metrosDropHoy: 0,
        estado: 'DISPONIBLE',
        clienteActivo: null,
        inicioActivo: null,
        ultimaHora: null
      });
    });

    hoyLogs.forEach(l => {
      const drop = parseInt(l.dropTotal || l.metrosDrop) || 0;
      totalDropMts += drop;

      const cleanPot = String(l.potenciaDbm || '').replace(/,/g, '.').replace(/[^\d.-]/g, '');
      const potNum = parseFloat(cleanPot);
      if (!isNaN(potNum) && potNum !== 0 && Math.abs(potNum) < 100) {
        sumaPotencias += Math.abs(potNum);
        cantPotencias++;
      }

      if (l.tieneSpeedtest || l.fotoVelocidad) {
        conSpeedtestCount++;
      }

      const tecKey = (l.instalador || l.tecnico || '').trim().toUpperCase();
      let cData = cuadrillasMap.get(tecKey);
      if (!cData) {
        for (const [k, v] of cuadrillasMap.entries()) {
          if (tecKey.includes(k) || k.includes(tecKey)) { cData = v; break; }
        }
      }
      if (cData) {
        cData.instaladasHoy++;
        cData.metrosDropHoy += drop;
        if (!cData.ultimaHora) cData.ultimaHora = l.hora || '';
      }
    });

    // 2. Obtener clientes pendientes de hoy
    let pendientesList = [];
    if (odooApprovedClients && odooApprovedClients.length > 0) {
      pendientesList = odooApprovedClients.filter(c => c.estado !== 'Instalados');
    }
    if (pendientesList.length === 0 && odooPool) {
      try {
        const qLeads = `
          SELECT 
              COALESCE(sub.code, '') AS "subscription",
              l.id AS "lead_id",
              l.name AS "oportunidad",
              COALESCE(l.contact_name, p.name, '') AS "nombre",
              COALESCE(l.phone, l.mobile, l.x_studio_mvil, p.phone, p.mobile, 'Sin teléfono') AS "telefono",
              COALESCE(l.x_studio_rif_ci, p.vat, '') AS "ci",
              COALESCE(l.x_studio_ubicacin, '') AS "ubicacion",
              COALESCE(z.name, '') AS "zona",
              COALESCE(p.street, l.street, '') AS "direccion",
              COALESCE(l.x_studio_nro_de_presupuesto, sub.so_name, 'S' || l.id) AS "solicitud",
              CASE WHEN l.medio_campaign = 'rf' THEN 'Radio Frecuencia' ELSE COALESCE(t.name, 'Fibra Óptica Comunitaria') END AS "equipo"
          FROM crm_lead l
          LEFT JOIN (
              SELECT DISTINCT ON (so.opportunity_id) so.opportunity_id, sub.code, so.name AS so_name
              FROM sale_order so
              JOIN sale_order_line sol ON sol.order_id = so.id
              LEFT JOIN sale_subscription sub ON sol.subscription_id = sub.id
              WHERE so.state IN ('sale', 'done')
              ORDER BY so.opportunity_id, so.id DESC
          ) sub ON sub.opportunity_id = l.id
          LEFT JOIN res_partner p ON l.partner_id = p.id
          LEFT JOIN crm_team t ON l.team_id = t.id
          LEFT JOIN zone z ON l.zone_id = z.id
          WHERE l.stage_id = 6 AND l.active = true
            AND (l.company_id = 2 OR l.company_id IS NULL)
            AND (t.name = 'Fibra Óptica Comunitaria' OR l.medio_campaign = 'rf')
          ORDER BY l.id DESC;
        `;
        const resDb = await odooPool.query(qLeads);
        if (resDb.rows.length > 0) {
          const raw = asignarCuadrillasYZonas(resDb.rows);
          const overrides = getOverrides();
          pendientesList = raw.map(c => {
            const ov = buscarOverride(c, overrides);
            if (ov) {
              return { ...c, cuadrilla: ov.cuadrilla || c.cuadrilla, instalador: ov.instalador || c.instalador, ayudante: ov.ayudante || c.ayudante };
            }
            return c;
          });
        }
      } catch(e) {}
    }

    const pendientesParaWallboard = pendientesList.map(c => ({
      id: c.id,
      leadId: c.lead_id,
      nombre: c.nombre || 'Cliente Multicanal',
      ci: c.ci || '',
      telefono: c.telefono || 'Sin teléfono',
      zona: c.zona || 'Sin Zona',
      ubicacion: c.ubicacion || '',
      direccion: c.direccion || c.ubicacion || 'Dirección en proceso',
      cuadrilla: c.cuadrilla || 'Solo Fibra',
      instalador: c.instalador || 'Por Asignar',
      ayudante: c.ayudante || '',
      estado: c.estado || 'Pendiente'
    }));

    // Revisar atenciones activas en vivo (activeInstallations)
    for (const [cid, act] of Object.entries(activeInstallations)) {
      if (!act || !act.tecnico) continue;
      const tecUpper = (act.tecnico || '').toUpperCase();
      let cData = cuadrillasMap.get(tecUpper);
      if (!cData) {
        for (const [k, v] of cuadrillasMap.entries()) {
          if (tecUpper.includes(k) || k.includes(tecUpper)) { cData = v; break; }
        }
      }
      if (cData) {
        cData.estado = 'EN_SITIO';
        cData.clienteActivo = act.clienteNombre || 'Cliente en Sitio';
        cData.inicioActivo = act.inicio || '';
        cData.actividad = act.actividad || 'En atención';
      }
    }

    const cuadrillas = Array.from(cuadrillasMap.values()).sort((a, b) => {
      if (a.estado === 'EN_SITIO' && b.estado !== 'EN_SITIO') return -1;
      if (b.estado === 'EN_SITIO' && a.estado !== 'EN_SITIO') return 1;
      return b.instaladasHoy - a.instaladasHoy;
    });

    const potPromedio = cantPotencias > 0 ? `-${(sumaPotencias / cantPotencias).toFixed(1)} dBm` : '-20.1 dBm';
    const instaladasCount = hoyLogs.length;
    const metaDiaria = 15;
    const pctMeta = Math.min(100, Math.round((instaladasCount / metaDiaria) * 100));

    // Mostrar las más recientes DEDUPLICADAS (preferiblemente de hoy, o las últimas 10)
    const listadoFinal = (hoyLogs.length > 0 ? hoyLogs : finishedLogsUnicos).slice(0, 10).map(l => ({
      id: l.id,
      clientId: l.clientId,
      leadId: l.leadId,
      nombre: l.nombre || 'Cliente Multicanal',
      direccion: l.direccion || '',
      zona: l.zona || 'San Agustín',
      tecnico: l.instalador || l.tecnico || 'Técnico',
      ayudante: l.ayudante || '',
      serialPon: l.serialPon || l.serialOnu || 'N/A',
      ipLan: l.ipLan || 'DHCP',
      potenciaDbm: l.potenciaDbm || 'N/A',
      metrosDrop: l.dropTotal || l.metrosDrop || 0,
      hora: l.hora || '',
      fecha: l.fecha || '',
      tieneSpeedtest: Boolean(l.tieneSpeedtest || l.fotoVelocidad),
      odooTaskId: l.odooTaskId || null
    }));

    const ultimosEventos = logs.slice(0, 15).map(l => ({
      tipo: l.tipo,
      mensaje: l.mensaje || l.detalle || l.nombre || 'Evento registrado',
      hora: l.hora || '',
      tecnico: l.tecnico || l.instalador || ''
    }));

    // Resumen unificado de instalaciones realizadas por personal (técnicos y ayudantes en una sola lista)
    function norm(s) {
      return (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toUpperCase().trim();
    }

    function formatName(str) {
      if (!str) return '';
      const upper = norm(str);
      if (upper.includes('EDWIN') || upper === 'rdiaz') return 'Roberto Díaz';
      if (upper.includes('TONY') || upper === 'cmendoza') return 'Carlos Mendoza';
      if (upper.includes('JESUS') || upper === 'mtorres') return 'Miguel Torres';
      if (upper.includes('JEAN') || upper === 'asilva') return 'Andrés Silva';
      if (upper.includes('KLEIVER')) return 'Diego Vargas';
      if (upper.includes('DARIO')) return 'Luis Rojas';
      if (upper.includes('ERNESTO')) return 'Javier Morales';
      if (upper.includes('CESAR')) return 'Gabriel Castro';
      return str.trim().split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(' ');
    }

    // Contar sólo las instalaciones realizadas HOY (diarias por personal)
    const activeStaff = [
      'Roberto Díaz',
      'Carlos Mendoza',
      'Miguel Torres',
      'Andrés Silva',
      'Diego Vargas',
      'Luis Rojas'
    ];

    const personCounts = {};
    activeStaff.forEach(p => {
      personCounts[p] = 0;
    });

    hoyLogs.forEach(l => {
      const tec = formatName(l.instalador || l.tecnico);
      const ayu = formatName(l.ayudante);
      if (tec) personCounts[tec] = (personCounts[tec] || 0) + 1;
      if (ayu && ayu !== tec) personCounts[ayu] = (personCounts[ayu] || 0) + 1;
    });

    const resumenPersonal = Object.entries(personCounts)
      .map(([nombre, cantidad]) => ({ nombre, cantidad }))
      .sort((a, b) => b.cantidad - a.cantidad || a.nombre.localeCompare(b.nombre));

    res.json({
      success: true,
      fechaHoy: hoyStrCaracas,
      kpis: {
        instaladasHoy: instaladasCount,
        metaDiaria,
        pctMeta,
        enAtencion: Object.keys(activeInstallations).length,
        metrosDropHoy: totalDropMts,
        potenciaPromedio: potPromedio,
        conSpeedtest: conSpeedtestCount,
        pctSpeedtest: instaladasCount > 0 ? Math.round((conSpeedtestCount / instaladasCount) * 100) : 0,
        totalPendientes: pendientesParaWallboard.length
      },
      cuadrillas,
      resumenPersonal,
      pendientesHoy: pendientesParaWallboard,
      ultimasInstalaciones: listadoFinal,
      ultimosEventos
    });
  } catch (err) {
    console.error('Error en /api/wallboard/data:', err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

// 3.13.2. Subir evidencia de Test de Velocidad (Speedtest) post-instalación
app.post('/api/installations/upload-speedtest', async (req, res) => {
  try {
    const { clientId, leadId, odooTaskId, fotoVelocidad, username } = req.body;
    if (!fotoVelocidad || typeof fotoVelocidad !== 'string' || !fotoVelocidad.includes(';base64,')) {
      return res.status(400).json({ success: false, message: 'Falta la imagen del Speedtest en formato base64' });
    }

    const clientRefId = clientId || (leadId ? `SUB${leadId}` : 'DOC');
    let finalTaskId = odooTaskId ? parseInt(odooTaskId) : null;

    // Si no viene odooTaskId, buscarlo en COMPLETED_DIR
    const targetKeys = [clientId, leadId, `SUB${leadId}`, String(clientId || '').replace('SUB', '')].filter(Boolean);
    if (!finalTaskId) {
      for (const k of targetKeys) {
        const compPath = path.join(COMPLETED_DIR, `${k}.json`);
        if (fs.existsSync(compPath)) {
          try {
            const comp = JSON.parse(fs.readFileSync(compPath, 'utf8'));
            if (comp.odooTaskId) {
              finalTaskId = parseInt(comp.odooTaskId);
              break;
            }
          } catch(e) {}
        }
      }
    }

    // Si aún no se tiene finalTaskId, buscar en Odoo DB por nombre o suscripción
    if (!finalTaskId && odooPool) {
      try {
        const leadNumeric = leadId ? parseInt(leadId) : 0;
        const subCode = String(clientId || '');
        const findTask = await odooPool.query(`
          SELECT id FROM project_task 
          WHERE (name ILIKE '%' || $1 || '%' OR description ILIKE '%' || $1 || '%')
             OR ($2 > 0 AND (name ILIKE '%' || $2 || '%' OR description ILIKE '%' || $2 || '%'))
          ORDER BY id DESC LIMIT 1;
        `, [subCode, leadNumeric]);
        if (findTask.rows.length > 0) {
          finalTaskId = findTask.rows[0].id;
        }
      } catch(eT) {
        console.warn('Aviso buscando tarea Odoo para speedtest:', eT.message);
      }
    }

    let odooAdjuntoOk = false;
    let odooAttachId = null;

    // 1. Guardar en Odoo si tenemos finalTaskId
    if (finalTaskId && odooPool) {
      try {
        const parts = fotoVelocidad.split(';base64,');
        const rawMime = parts[0].replace(/^data:/, '').split(';')[0].trim();
        const mime = rawMime || 'image/jpeg';
        const buffer = Buffer.from(parts[1], 'base64');
        const checksum = crypto.createHash('sha1').update(buffer).digest('hex');

        // A) Insertar mensaje en el Chatter con author_id = ODOO_SYSTEM_USER.partnerId (res_partner)
        const msgRes = await odooPool.query(`
          INSERT INTO mail_message (
            body, model, res_id, record_name, message_type, subtype_id, author_id, date, create_uid, write_uid, create_date, write_date
          ) VALUES (
            $1, 'project.task', $2, $3, 'comment', 2, $4, NOW(), $5, $5, NOW(), NOW()
          ) RETURNING id;
        `, [
          `<p>🚀 <b>Prueba de Velocidad (Speedtest) Post-Instalación</b> cargada con éxito por <b>${username || 'Técnico de Campo'}</b>.</p>`,
          finalTaskId,
          `Tarea #${finalTaskId}`,
          ODOO_SYSTEM_USER.partnerId,
          ODOO_SYSTEM_USER.id
        ]);
        const msgId = msgRes.rows[0].id;

        // B) Insertar en ir_attachment
        const insAtt = await odooPool.query(`
          INSERT INTO ir_attachment (
            name, res_model, res_id, company_id, type, mimetype, file_size, checksum, index_content, db_datas, create_uid, write_uid, create_date, write_date
          ) VALUES (
            $1, 'project.task', $2, 2, 'binary', $3, $4, $5, 'image', $6, $7, $7, NOW(), NOW()
          ) RETURNING id;
        `, [
          `Foto_7_Test_Velocidad_${clientRefId}.jpg`,
          finalTaskId,
          mime,
          buffer.length,
          checksum,
          buffer,
          ODOO_SYSTEM_USER.id
        ]);
        odooAttachId = insAtt.rows[0].id;

        // C) Relacionar adjunto al mensaje del Chatter
        await odooPool.query(`
          INSERT INTO message_attachment_rel (message_id, attachment_id) VALUES ($1, $2) ON CONFLICT DO NOTHING;
        `, [msgId, odooAttachId]);

        odooAdjuntoOk = true;
        console.log(`✅ Speedtest adjuntado con éxito a Chatter Odoo en Tarea #${finalTaskId} (Attachment #${odooAttachId})`);
      } catch(odooErr) {
        console.error('⚠️ Error al subir speedtest a Odoo DB:', odooErr.message);
      }
    }

    // 2. Sincronizar IP real por DHCP desde SmartOLT ahora que la ONU ya tiene internet
    let realSmartOltIp = null;
    let targetSn = null;
    for (const k of targetKeys) {
      const compPath = path.join(COMPLETED_DIR, `${k}.json`);
      if (fs.existsSync(compPath)) {
        try {
          const c = JSON.parse(fs.readFileSync(compPath, 'utf8'));
          if (c.serialPon) { targetSn = c.serialPon; break; }
        } catch(e) {}
      }
    }
    if (targetSn) {
      realSmartOltIp = await getSmartOltRealWanIp(targetSn);
    }

    if (realSmartOltIp && odooPool) {
      try {
        if (finalTaskId) {
          await odooPool.query(`UPDATE project_task SET ip_lan = $1 WHERE id = $2;`, [realSmartOltIp, finalTaskId]);
        }
        await odooPool.query(`
          UPDATE sale_subscription 
          SET ip_lan = $1 
          WHERE (code = $2 OR code = 'SUB' || $2) 
             OR (task_id = $3 AND $3 > 0);
        `, [realSmartOltIp, String(clientId || '').replace('SUB', ''), finalTaskId || 0]);
        console.log(`🎯 [Speedtest Upload] IP real DHCP (${realSmartOltIp}) sincronizada en Odoo para Tarea #${finalTaskId}`);
      } catch(eSyncIp) {}
    }

    // 3. Actualizar registro en COMPLETED_DIR
    for (const k of targetKeys) {
      const fPath = path.join(COMPLETED_DIR, `${k}.json`);
      if (fs.existsSync(fPath)) {
        try {
          const comp = JSON.parse(fs.readFileSync(fPath, 'utf8'));
          comp.fotos = comp.fotos || {};
          comp.fotos.velocidad = fotoVelocidad;
          comp.fotos.testVelocidad = fotoVelocidad;
          comp.tieneSpeedtest = true;
          comp.speedtestUploadedAt = getFechaHoraCaracas();
          if (realSmartOltIp) comp.ipLan = realSmartOltIp;
          if (finalTaskId && !comp.odooTaskId) comp.odooTaskId = finalTaskId;
          fs.writeFileSync(fPath, JSON.stringify(comp, null, 2));
        } catch(e) {}
      }
    }

    // 4. Actualizar en installations_log.json
    try {
      const logs = getInstallationsLog();
      const logItem = logs.find(l => targetKeys.includes(String(l.clientId)) || targetKeys.includes(String(l.leadId)) || (finalTaskId && l.odooTaskId === finalTaskId));
      if (logItem) {
        logItem.tieneSpeedtest = true;
        logItem.fotoVelocidad = true;
        logItem.speedtestUploadedAt = getFechaHoraCaracas();
        if (realSmartOltIp) logItem.ipLan = realSmartOltIp;
        if (finalTaskId && !logItem.odooTaskId) logItem.odooTaskId = finalTaskId;
        saveInstallationsLog(logs);
      }
    } catch(eLog) {}

    res.json({
      success: true,
      odooAdjuntoOk,
      odooTaskId: finalTaskId,
      detectedIp: realSmartOltIp || null,
      message: odooAdjuntoOk 
        ? `Test de velocidad adjuntado con éxito a la Tarea Odoo #${finalTaskId} y guardado en la app.` 
        : `Test de velocidad guardado en el registro de la app.`
    });
  } catch(err) {
    console.error('Error en upload-speedtest:', err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

// 3.13.3. Sincronizar IP real por DHCP desde SmartOLT hacia Odoo bajo demanda
app.post('/api/installations/sync-smartolt-ip', async (req, res) => {
  try {
    const { clientId, sn, odooTaskId } = req.body;
    let targetSn = sn;

    const targetKeys = [clientId, `SUB${clientId}`, String(clientId || '').replace('SUB', '')].filter(Boolean);
    if (!targetSn) {
      for (const k of targetKeys) {
        const compPath = path.join(COMPLETED_DIR, `${k}.json`);
        if (fs.existsSync(compPath)) {
          try {
            const comp = JSON.parse(fs.readFileSync(compPath, 'utf8'));
            if (comp.serialPon) { targetSn = comp.serialPon; break; }
          } catch(e) {}
        }
      }
    }

    if (!targetSn) {
      return res.status(400).json({ success: false, message: 'No se encontró el Serial PON para consultar SmartOLT.' });
    }

    const detectedIp = await getSmartOltRealWanIp(targetSn);
    if (!detectedIp) {
      return res.json({ success: false, message: 'SmartOLT aún no reporta IP WAN para esta ONU.' });
    }

    let finalTask = odooTaskId ? parseInt(odooTaskId) : null;
    if (odooPool) {
      try {
        if (!finalTask) {
          const findT = await odooPool.query(`SELECT id FROM project_task WHERE (name ILIKE '%' || $1 || '%') ORDER BY id DESC LIMIT 1;`, [String(clientId || '')]);
          if (findT.rows.length > 0) finalTask = findT.rows[0].id;
        }
        if (finalTask) {
          await odooPool.query(`UPDATE project_task SET ip_lan = $1 WHERE id = $2;`, [detectedIp, finalTask]);
        }
        await odooPool.query(`
          UPDATE sale_subscription 
          SET ip_lan = $1 
          WHERE (code = $2 OR code = 'SUB' || $2) 
             OR (task_id = $3 AND $3 > 0);
        `, [detectedIp, String(clientId || '').replace('SUB', ''), finalTask || 0]);
      } catch(eDb) {}
    }

    for (const k of targetKeys) {
      const compPath = path.join(COMPLETED_DIR, `${k}.json`);
      if (fs.existsSync(compPath)) {
        try {
          const comp = JSON.parse(fs.readFileSync(compPath, 'utf8'));
          comp.ipLan = detectedIp;
          fs.writeFileSync(compPath, JSON.stringify(comp, null, 2));
        } catch(e) {}
      }
    }

    try {
      const logs = getInstallationsLog();
      let updatedLogs = false;
      for (const l of logs) {
        const matchClient = targetKeys.includes(String(l.clientId)) || targetKeys.includes(String(l.leadId));
        const matchSn = targetSn && (l.serialPon === targetSn || l.serialOnu === targetSn);
        const matchTask = finalTask && l.odooTaskId === finalTask;
        if (matchClient || matchSn || matchTask) {
          l.ipLan = detectedIp;
          updatedLogs = true;
        }
      }
      if (updatedLogs) {
        saveInstallationsLog(logs);
      }
    } catch(e) {}

    res.json({ success: true, ip: detectedIp, message: `IP sincronizada con éxito: ${detectedIp}` });
  } catch(err) {
    console.error('Error en sync-smartolt-ip:', err.message);
    res.status(500).json({ success: false, error: err.message });
  }
});

// 3.14. Reintentar conexión y aprovisionamiento con SmartOLT
app.post('/api/installations/retry-smartolt', async (req, res) => {
  const { clientId, leadId, serialPon } = req.body;
  const targetKeys = [clientId, leadId, `SUB${leadId}`, String(clientId || '').replace('SUB', '')].filter(Boolean);

  let record = null;
  for (const k of targetKeys) {
    const fPath = path.join(COMPLETED_DIR, `${k}.json`);
    if (fs.existsSync(fPath)) {
      try {
        record = JSON.parse(fs.readFileSync(fPath, 'utf8'));
        break;
      } catch (e) {}
    }
  }

  if (!record) {
    const logs = getInstallationsLog();
    record = logs.find(l => targetKeys.includes(String(l.clientId)) || targetKeys.includes(String(l.leadId)) || (serialPon && (l.serialPon === serialPon || l.serialOnu === serialPon)));
  }

  if (!record) {
    const foundC = odooApprovedClients.find(c => targetKeys.includes(String(c.id)) || targetKeys.includes(String(c.lead_id)));
    if (foundC) {
      record = {
        clienteId: foundC.id,
        leadId: foundC.lead_id,
        solicitud: foundC.solicitud,
        nombre: foundC.nombre,
        direccion: foundC.direccion,
        zona: foundC.zona,
        serialPon: serialPon || foundC.onuAsignada?.serialPon
      };
    }
  }

  if (!record) {
    return res.status(404).json({ success: false, message: 'No se encontró el registro para reintentar la conexión' });
  }

  const snInstalado = (serialPon || record.serialPon || record.serialOnu || '').trim();
  let cleanSnPon = snInstalado.toUpperCase().trim();
  if (cleanSnPon.startsWith('CTDC')) {
    console.log(`🔄 [Auto-Fix SN] Corrigiendo prefijo C-Data invertido: ${cleanSnPon} -> CDTC${cleanSnPon.slice(4)}`);
    cleanSnPon = 'CDTC' + cleanSnPon.slice(4);
  }

  if (!cleanSnPon || cleanSnPon.length < 6 || cleanSnPon.includes('PENDIENTE')) {
    return res.status(400).json({ success: false, message: `Serial PON no válido: '${cleanSnPon}'` });
  }

  console.log(`⚡ [Reintentar SmartOLT] Reintentando autorización para ${record.nombre} (SN: ${cleanSnPon})...`);

  let smartOltStatus = 'FALLIDO';
  let smartOltDetalle = '';
  let smartOltResult = null;

  try {
    let oltId = (record.oltAsignada === 'OLTANDRESBELLO' || (record.zona || '').toLowerCase().includes('bello')) ? '3' : '4';
    let board = '1';
    let port = String(record.puertoNap || '1');
    let onuTypeDetected = '';

    // 1. Detectar OLT física, board y port reales desde unconfigured_onus
    try {
      const unconfRes = await callSmartOlt('/api/onu/unconfigured_onus');
      const unconfList = unconfRes.response || [];
      const altSn = cleanSnPon.startsWith('CDTC') ? ('CTDC' + cleanSnPon.slice(4)) : (cleanSnPon.startsWith('CTDC') ? ('CDTC' + cleanSnPon.slice(4)) : null);
      const detected = unconfList.find(o => {
        const s = (o.sn || '').toUpperCase();
        return s === cleanSnPon || (altSn && s === altSn);
      });
      if (detected) {
        if (detected.sn && detected.sn.toUpperCase() !== cleanSnPon) {
          console.log(`🔄 [SmartOLT] Adoptando serial físico detectado en OLT: ${detected.sn.toUpperCase()}`);
          cleanSnPon = detected.sn.toUpperCase();
        }
        if (detected.olt_id) oltId = String(detected.olt_id);
        board = String(detected.board || board);
        port = String(detected.port || port);
        onuTypeDetected = detected.onu_type_name || '';
        console.log(`🎯 [Reintentar SmartOLT] ONU detectada: OLT ${oltId}, Board ${board}, Port ${port}, Tipo ${onuTypeDetected}`);
      }
    } catch (errUnconf) {
      console.warn('Aviso al buscar unconfigured_onus:', errUnconf.message);
    }

    // 1.1 SEGURIDAD ANTI-SECUESTRO: Si la ONU ya existe en SmartOLT, verificar que no pertenezca a otro cliente
    try {
      const existingDetails = await callSmartOlt(`/api/onu/get_onu_details/${cleanSnPon}`);
      if (existingDetails && existingDetails.status && existingDetails.onu_details) {
        const existingName = (existingDetails.onu_details.name || '').toUpperCase();
        const currSub = (record.subscription || record.solicitud || '').toUpperCase();
        const currClient = (record.nombre || '').toUpperCase();
        const primerNombre = currClient.split(' ')[0] || '';
        const perteneceAlClienteActual = (currSub && existingName.includes(currSub)) || 
                                         (primerNombre.length >= 4 && existingName.includes(primerNombre));

        if (!perteneceAlClienteActual) {
          const errMsg = `⚠️ [SEGURIDAD SMARTOLT] La ONU ${cleanSnPon} ya se encuentra registrada y asignada a OTRO abonado ('${existingDetails.onu_details.name}'). NO SE PUEDE SOBREESCRIBIR NI REASIGNAR.`;
          console.error(errMsg);
          return res.status(400).json({ success: false, error: errMsg });
        }
      }
    } catch (eExist) {
      if (eExist.message && eExist.message.includes('SEGURIDAD SMARTOLT')) throw eExist;
    }


    // 2. Determinar Preset oficial: CDATA-FD514GS1 vs V2802ACT (y demás ONUs)
    const isCdata = cleanSnPon.startsWith('CDTC') || cleanSnPon.startsWith('CTDC') || onuTypeDetected.toLowerCase().includes('cdata') || onuTypeDetected.toLowerCase().includes('fd514');
    const defaultPreset = resolveSmartOltPreset(oltId, isCdata);
    let presetId = defaultPreset.id;
    let presetName = defaultPreset.name;

    try {
      const presetsRes = await callSmartOlt(`/api/onu/get_authorization_presets?olt_id=${oltId}`);
      const presetsList = presetsRes.response?.authorization_presets || [];
      let matched = null;
      if (isCdata) {
        matched = presetsList.find(p => p.name.toLowerCase().includes('cdata'));
      } else {
        matched = presetsList.find(p => p.name.toLowerCase().includes('catv'));
      }
      if (matched) {
        presetId = String(matched.id);
        presetName = matched.name;
      }
    } catch (ePres) {}

    console.log(`📋 [Reintentar SmartOLT] Usando Preset: "${presetName}" (ID ${presetId}) en OLT ${oltId}`);

    // 3. Formatear Nombre y Dirección Sanitizados (sin tildes, caracteres válidos SmartOLT)
    const rawName = (record.nombre || 'Cliente Multicanal').trim();
    const solCode = (record.solicitud || '').trim();
    const finalClientName = sanitizeSmartOltString(solCode ? `${solCode}-${rawName}` : rawName, 50);
    const addressSafe = sanitizeSmartOltString(record.direccion || 'Caracas', 100);

    // 4. Autorizar en SmartOLT
    const smartOltParams = {
      olt_id: oltId,
      pon_type: 'gpon',
      board: board,
      port: port,
      sn: cleanSnPon,
      onu_external_id: cleanSnPon,
      authorization_preset_id: presetId,
      name: finalClientName,
      address_or_comment: addressSafe
    };

    const postData = querystring.stringify(smartOltParams);
    smartOltResult = await callSmartOlt('/api/onu/authorize_onu', 'POST', postData);

    const respStr = String(smartOltResult?.error || smartOltResult?.response || '').toLowerCase();
    const alreadyAuthorized = respStr.includes('already authorized') || respStr.includes('already registered');

    if ((smartOltResult && smartOltResult.status) || alreadyAuthorized) {
      smartOltStatus = 'EXITOSO';
      smartOltDetalle = alreadyAuthorized 
        ? 'ONU ya se encontraba registrada y activa en SmartOLT' 
        : `Aprovisionada con éxito: ${smartOltResult.response || 'ONU configurada'}`;
      console.log(`✅ [Reintentar SmartOLT] ¡ONU ${cleanSnPon} autorizada exitosamente en OLT ${oltId}!`);

      // 5. CAMBIO DE ZONA a la que corresponda
      const zonaStr = ((record.zona || '') + ' ' + (record.ubicacion || '')).toLowerCase();
      let smartZoneName = 'San Agustin FTTH';
      if (zonaStr.includes('marin')) smartZoneName = 'Marin';
      else if (zonaStr.includes('hornos')) smartZoneName = 'Hornos de Cal';
      else if (zonaStr.includes('charneca')) smartZoneName = 'La Charneca';
      else if (zonaStr.includes('televisora')) smartZoneName = 'La Televisora';
      else if (zonaStr.includes('ajuro')) smartZoneName = 'Barrio Ajuro';
      else if (zonaStr.includes('anacoco')) smartZoneName = 'Calle Anacoco';
      else if (zonaStr.includes('bosco')) smartZoneName = 'Centro Empresarial Don Bosco';
      else if (zonaStr.includes('manolos')) smartZoneName = 'Los Manolos';
      else if (zonaStr.includes('daniela')) smartZoneName = 'residencia las Danielas';
      else if (oltId === '3') smartZoneName = 'Los Manolos';

      const locParams = {
        zone: smartZoneName,
        name: finalClientName,
        address_or_comment: addressSafe
      };
      if (record.codigoNap || record.lugarNap) locParams.odb = sanitizeSmartOltString(record.codigoNap || record.lugarNap, 30);
      if (record.puertoNap) locParams.odb_port = String(record.puertoNap);
      if (record.gpsCoords) {
        const parts = String(record.gpsCoords).split(',');
        if (parts.length === 2 && !isNaN(parseFloat(parts[0])) && !isNaN(parseFloat(parts[1]))) {
          locParams.latitude = parts[0].trim();
          locParams.longitude = parts[1].trim();
        }
      }

      try {
        await callSmartOlt(`/api/onu/update_location_details/${cleanSnPon}`, 'POST', querystring.stringify(locParams));
        console.log(`📍 [Reintentar SmartOLT] Zona actualizada a "${smartZoneName}" para ${cleanSnPon}`);
      } catch (eLoc) {
        console.warn('Aviso actualizando zona:', eLoc.message);
      }

      // 6. VLAN Adicional
      const vlanAdicional = record.vlanAdicional;
      if (vlanAdicional && String(vlanAdicional).trim() !== '' && String(vlanAdicional).trim() !== '100') {
        try {
          await callSmartOlt(`/api/onu/update_attached_vlans/${cleanSnPon}`, 'POST', querystring.stringify({
            add_vlans: String(vlanAdicional).trim()
          }));
          console.log(`🌐 [Reintentar SmartOLT] VLAN adicional adjuntada`);
        } catch (eVlan) {}
      }

      // 7. Actualizar COMPLETED_DIR
      record.smartOltStatus = 'EXITOSO';
      record.smartOltDetalle = smartOltDetalle;
      record.smartOltRetryAt = getFechaHoraCaracas();

      targetKeys.forEach(k => {
        const compPath = path.join(COMPLETED_DIR, `${k}.json`);
        if (fs.existsSync(compPath)) {
          try {
            const currentComp = JSON.parse(fs.readFileSync(compPath, 'utf8'));
            currentComp.smartOltStatus = 'EXITOSO';
            currentComp.smartOltDetalle = smartOltDetalle;
            fs.writeFileSync(compPath, JSON.stringify(currentComp, null, 2));
          } catch (e) {}
        }
      });

      // 8. Actualizar installations_log.json
      const allLogs = getInstallationsLog();
      let updatedLogEntry = null;
      allLogs.forEach(l => {
        if (targetKeys.includes(String(l.clientId)) || targetKeys.includes(String(l.leadId)) || (l.serialPon === cleanSnPon)) {
          l.smartOltStatus = 'EXITOSO';
          l.smartOltDetalle = smartOltDetalle;
          updatedLogEntry = l;
        }
      });
      saveInstallationsLog(allLogs);

      broadcastRealtime('LOG_UPDATED', { updatedLog: updatedLogEntry, kpis: calcularKpis() });

      return res.json({
        success: true,
        smartOltStatus: 'EXITOSO',
        smartOltDetalle,
        mensaje: `✅ ¡Conexión con SmartOLT establecida con éxito! ONU ${cleanSnPon} autorizada con preset "${presetName}" en zona "${smartZoneName}".`
      });

    } else {
      smartOltStatus = 'FALLIDO';
      smartOltDetalle = smartOltResult?.error || smartOltResult?.response || JSON.stringify(smartOltResult);
      console.error(`⚠️ [Reintentar SmartOLT] Fallo al autorizar:`, smartOltDetalle);
      return res.json({
        success: false,
        smartOltStatus: 'FALLIDO',
        smartOltDetalle,
        mensaje: `⚠️ SmartOLT no autorizó la ONU: ${smartOltDetalle}`
      });
    }

  } catch (err) {
    console.error(`❌ [Reintentar SmartOLT] Error:`, err.message);
    return res.status(500).json({
      success: false,
      smartOltStatus: 'ERROR',
      smartOltDetalle: err.message,
      mensaje: `Error al conectar con SmartOLT: ${err.message}`
    });
  }
});

// 4. Obtener clientes aprobados del día (Filtrado por Usuario y con Overrides)
app.get('/api/odoo/pending-clients', async (req, res) => {
  const reqUsername = (req.query.username || '').trim().toLowerCase();
  const users = getUsers();
  const currentUser = users.find(u => u.username.toLowerCase() === reqUsername);
  const overrides = getOverrides();

  try {
    const qLeads = `
      SELECT 
          COALESCE(sub.code, '') AS "subscription",
          l.id AS "lead_id",
          l.name AS "oportunidad",
          l.partner_id AS "partner_id",
          COALESCE(l.contact_name, p.name, '') AS "nombre",
          COALESCE(l.x_studio_rif_ci, p.vat, '') AS "ci",
          COALESCE(l.phone, l.mobile, l.x_studio_mvil, p.phone, p.mobile, '') AS "telefono",
          COALESCE(p.email, l.email_from, '') AS "email",
          COALESCE(l.x_studio_ubicacin, '') AS "ubicacion",
          COALESCE(z.name, '') AS "zona",
          COALESCE(p.street, l.street, '') AS "direccion",
          COALESCE(l.x_studio_nro_de_presupuesto, sub.so_name, 'S' || l.id) AS "solicitud",
          CASE 
              WHEN l.medio_campaign = 'rf' THEN 'Radio Frecuencia'
              ELSE COALESCE(t.name, 'Fibra Óptica Comunitaria') 
          END AS "equipo",
          s.name AS "etapa",
          l.expected_revenue AS "ingreso",
          com.name AS "comercial",
          to_char(l.create_date, 'YYYY-MM-DD HH24:MI') AS "fecha_creacion",
          to_char(l.x_studio_fecha_y_hora_de_aprobacin, 'DD/MM/YYYY HH24:MI:SS') AS "fecha_aprobacion"
      FROM crm_lead l
      LEFT JOIN (
          SELECT DISTINCT ON (so.opportunity_id) 
              so.opportunity_id, 
              sub.code,
              so.name AS so_name
          FROM sale_order so
          JOIN sale_order_line sol ON sol.order_id = so.id
          LEFT JOIN sale_subscription sub ON sol.subscription_id = sub.id
          WHERE so.state IN ('sale', 'done')
          ORDER BY so.opportunity_id, so.id DESC
      ) sub ON sub.opportunity_id = l.id
      LEFT JOIN res_partner p ON l.partner_id = p.id
      LEFT JOIN crm_team t ON l.team_id = t.id
      LEFT JOIN crm_stage s ON l.stage_id = s.id
      LEFT JOIN res_users ru ON l.user_id = ru.id
      LEFT JOIN res_partner com ON ru.partner_id = com.id
      LEFT JOIN zone z ON l.zone_id = z.id
      WHERE l.stage_id = 6 
        AND l.active = true
        AND (l.company_id = 2 OR l.company_id IS NULL)
        AND (t.name = 'Fibra Óptica Comunitaria' OR l.medio_campaign = 'rf')
      ORDER BY "equipo", l.id DESC;
    `;

    const dbRes = await odooPool.query(qLeads);
    const leadsRaw = dbRes.rows;

    if (leadsRaw.length > 0) {
      let clientes = asignarCuadrillasYZonas(leadsRaw);

      // Aplicar overrides guardados por el admin
      clientes = clientes.map(c => {
        const ov = buscarOverride(c, overrides);
        if (ov) {
          return {
            ...c,
            cuadrilla: ov.cuadrilla || c.cuadrilla,
            instalador: ov.instalador || c.instalador,
            ayudante: ov.ayudante || c.ayudante,
            tecnicos: ov.instalador,
            esReasignado: true
          };
        }
        return c;
      });

      // Guardar lista completa en caché para el backend
      odooApprovedClients = [...clientes];

      // Filtrado por usuario logueado (Los técnicos solo ven lo suyo)
      if (currentUser && currentUser.rol !== 'admin') {
        const uNom = currentUser.nombre.toLowerCase();
        const uUser = currentUser.username.toLowerCase();

        clientes = clientes.filter(c => {
          if (uUser === 'cmendoza' || uUser === 'mtorres') {
            return c.cuadrilla.includes('Solo Fibra') || (c.instalador || '').toLowerCase().includes('rodriguez') || (c.tecnicos || '').toLowerCase().includes('perez') || (c.ayudante || '').toLowerCase().includes('rodriguez') || (c.ayudante || '').toLowerCase().includes('perez');
          }
          if (uUser === 'asilva' || uUser === 'rdiaz') {
            return c.cuadrilla.includes('Mixta') || (c.instalador || '').toLowerCase().includes('mejias') || (c.tecnicos || '').toLowerCase().includes('gutierrez') || (c.ayudante || '').toLowerCase().includes('mejias') || (c.ayudante || '').toLowerCase().includes('gutierrez');
          }
          if (uUser === 'jmorales') {
            return c.cuadrilla.includes('Mixta') || (c.instalador || '').toLowerCase().includes('santos') || (c.tecnicos || '').toLowerCase().includes('santos') || (c.ayudante || '').toLowerCase().includes('santos');
          }
          if (uUser === 'gcastro') {
            return c.cuadrilla.includes('Solo Fibra') || (c.instalador || '').toLowerCase().includes('rivas') || (c.tecnicos || '').toLowerCase().includes('rivas') || (c.ayudante || '').toLowerCase().includes('rivas');
          }
          if (uUser === 'lrojas') {
            return (c.ayudante || '').toLowerCase().includes('darío') || (c.ayudante || '').toLowerCase().includes('dario') || (c.ayudante || '').toLowerCase().includes('perozo') || (c.instalador || '').toLowerCase().includes('perozo');
          }
          if (uUser === 'dvargas') {
            return (c.ayudante || '').toLowerCase().includes('kleiver') || (c.ayudante || '').toLowerCase().includes('almeida') || (c.instalador || '').toLowerCase().includes('kleiver');
          }
          return false;
        });
      }

      return res.json({
        success: true,
        source: 'odoo_live_db',
        total: clientes.length,
        isAdmin: currentUser ? currentUser.rol === 'admin' : false,
        currentUser: currentUser ? currentUser.username : null,
        clients: clientes
      });
    }
  } catch (err) {
    console.error('⚠️ Error al consultar Odoo PostgreSQL, usando respaldo:', err.message);
  }

  res.json({
    success: true,
    source: 'backup_cache',
    total: odooApprovedClients.length,
    isAdmin: currentUser ? currentUser.rol === 'admin' : false,
    clients: odooApprovedClients
  });
});

// 3. Procesar cierre de Instalación en Campo -> SmartOLT (Backend) -> Odoo Servicio Externo -> Bequant
app.post('/api/installations/submit', async (req, res) => {
  const fieldData = req.body;
  let cliente = odooApprovedClients.find(c => 
    (c.id && (c.id === fieldData.id || c.id === fieldData.subCode)) || 
    (c.lead_id && (c.lead_id == fieldData.id || c.lead_id == fieldData.lead_id)) ||
    (c.subscription && (c.subscription === fieldData.id || c.subscription === fieldData.subCode))
  );

  if (!cliente || !cliente.partner_id) {
    const searchLeadId = parseInt(fieldData.lead_id || (fieldData.id ? String(fieldData.id).replace(/\D/g, '') : 0), 10);
    const searchSub = (fieldData.subCode || fieldData.id || '').toUpperCase();
    try {
      const dbClientRes = await odooPool.query(`
        SELECT l.id AS lead_id, l.partner_id, COALESCE(l.contact_name, p.name) AS nombre,
               COALESCE(sub.code, '') AS subscription, COALESCE(z.name, '') AS zona,
               COALESCE(l.x_studio_rif_ci, p.vat, '') AS ci,
               COALESCE(l.phone, l.mobile, p.phone, p.mobile, '') AS telefono,
               COALESCE(p.street, l.street, '') AS direccion
        FROM crm_lead l
        LEFT JOIN res_partner p ON l.partner_id = p.id
        LEFT JOIN zone z ON l.zone_id = z.id
        LEFT JOIN (
          SELECT DISTINCT ON (so.opportunity_id) so.opportunity_id, sub.code
          FROM sale_order so
          JOIN sale_order_line sol ON sol.order_id = so.id
          JOIN sale_subscription sub ON sol.subscription_id = sub.id
          WHERE so.state IN ('sale', 'done')
          ORDER BY so.opportunity_id, so.id DESC
        ) sub ON sub.opportunity_id = l.id
        WHERE (NULLIF($1, 0) IS NOT NULL AND l.id = $1)
           OR (NULLIF($2, '') IS NOT NULL AND (sub.code = $2 OR l.partner_id IN (SELECT partner_id FROM sale_subscription WHERE code = $2)))
        LIMIT 1;
      `, [searchLeadId || 0, searchSub || '']);
      if (dbClientRes.rows.length > 0) {
        cliente = { ...cliente, ...dbClientRes.rows[0], id: fieldData.id || String(dbClientRes.rows[0].lead_id) };
      }
    } catch (e) {
      console.error('Error buscando cliente en DB:', e.message);
    }
  }

  if (!cliente) {
    cliente = {
      id: fieldData.id,
      lead_id: fieldData.lead_id || (fieldData.id ? String(fieldData.id).replace(/\D/g, '') : ''),
      nombre: fieldData.cliente || fieldData.nombre || `Cliente ${fieldData.id || ''}`,
      zona: fieldData.zona || 'Marin',
      oltAsignada: fieldData.olt || 'OLT SanAgustin'
    };
  }

  // A) Determinar qué ONU quedó instalada (PROTECCIÓN ESTRICTA ANTI-CONTAMINACIÓN)
  // Si la ONU fue oficialmente pistoleada/asignada al cliente (en memoria, en BD o en onu_assignments.json),
  // se FUERZA esa ONU oficial pistoleada, IMPIDIENDO que cualquier texto residual del formulario la sobreescriba.
  const onuAssignments = getOnuAssignments();
  const subCodeKey = (cliente?.subscription || fieldData.subCode || cliente?.id || fieldData.id || '').trim();
  const leadIdKey = String(cliente?.lead_id || fieldData.lead_id || (cliente?.id ? String(cliente.id).replace(/\D/g, '') : '')).trim();
  const asignacionOficial = cliente?.onuAsignada || onuAssignments[leadIdKey] || onuAssignments[subCodeKey] || onuAssignments['SUB' + leadIdKey];

  if (asignacionOficial?.serialPon) {
    console.log(`🔒 [SEGURIDAD ONU] Forzando ONU oficial pistoleada en despacho: ${asignacionOficial.serialPon} (LP: ${asignacionOficial.serialSecundario || 'N/A'}) para cliente ${subCodeKey || leadIdKey}`);
  }

  const serialInstalado = (asignacionOficial?.serialPon || fieldData.serialOnuDefinitivo || fieldData.serialPon || 'PENDIENTE_ESCANEO').trim().toUpperCase();
  const serialFisicoLP = (asignacionOficial?.serialSecundario || fieldData.serialSecundario || fieldData.serialFisicoLP || '').trim().toUpperCase();

  // B) Reservar IP Fija libre en la subred de la OLT (invisible para el técnico)
  const ipReservation = await obtenerIpLibreSmartOlt(cliente?.oltAsignada || 'OLT SanAgustin');

  // C) Mapeo exacto de empleados con ID de Odoo
  const EMPLOYEE_MAP = {
    'CARLOS MENDOZA': { id: 168, name: 'TECH LEAD 01 ' },
    'Carlos Mendoza': { id: 168, name: 'TECH LEAD 01 ' },
    'MIGUEL TORRES': { id: 1557, name: 'miguel torres' },
    'Miguel Torres': { id: 1557, name: 'miguel torres' },
    'ANDRES SILVA': { id: 1434, name: 'ANDRES SILVA' },
    'Andrés Silva': { id: 1434, name: 'ANDRES SILVA' },
    'ROBERTO DIAZ': { id: 1431, name: 'ROBERTO DIAZ' },
    'Roberto Díaz': { id: 1431, name: 'ROBERTO DIAZ' },
    'ERNESTO SANTOS': { id: 230, name: 'SANTOS BALESTRINI ERNESTO RABBI ' },
    'Javier Morales': { id: 230, name: 'SANTOS BALESTRINI ERNESTO RABBI ' },
    'CESAR RIVAS': { id: 471, name: 'RIVAS POLO CESAR EDUARDO ' },
    'Gabriel Castro': { id: 471, name: 'RIVAS POLO CESAR EDUARDO ' },
    'Cesar Rivas': { id: 471, name: 'RIVAS POLO CESAR EDUARDO ' },
    'LUIS ROJAS': { id: 1728, name: 'ASSISTANT TECH 01' },
    'Luis Rojas': { id: 1728, name: 'ASSISTANT TECH 01' },
    'DIEGO VARGAS': { id: 1732, name: 'ASSISTANT TECH 02' },
    'Diego Vargas': { id: 1732, name: 'ASSISTANT TECH 02' }
  };

  const getEmployee = (name, fallbackId, fallbackName) => {
    if (!name) return { id: fallbackId, name: fallbackName };
    const clean = name.trim();
    if (EMPLOYEE_MAP[clean]) return EMPLOYEE_MAP[clean];
    const match = Object.keys(EMPLOYEE_MAP).find(k => k.toLowerCase() === clean.toLowerCase() || clean.toLowerCase().includes(k.toLowerCase()) || k.toLowerCase().includes(clean.toLowerCase()));
    return match ? EMPLOYEE_MAP[match] : { id: fallbackId, name: clean };
  };

  const emp1 = getEmployee(fieldData.instalador || cliente.instalador, 168, 'TECH LEAD 01 ');
  const emp2 = getEmployee(fieldData.ayudante || cliente.ayudante, 1732, 'ASSISTANT TECH 02');

  const simOdooTask = {
    // 1. Cabecera de la Tarea en project.task
    project_id: 9, // Proyecto 'Servicio externo' (Compañía ID 2)
    project_name: 'Servicio externo',
    name: `Instalación de F.O:  ${fieldData.lead_id || cliente.lead_id || fieldData.id}`,
    partner_id: cliente.partner_id || 51409,
    partner_name: fieldData.nombre || cliente.nombre || 'Cliente Multicanal',
    stage_id: 841, // Etapa final: 'Instalados'
    stage_name: 'Instalados',
    company_id: 2, // FIBERNET TELECOM C.A.

    // 2. Información Técnica (Campos reales de la BD en project_task)
    instalador_1: emp1.name,
    instalador_2: emp2.name,
    ip_lan: ipReservation.ip,
    codigo_onu: serialInstalado, // PON S/N (GPON) para SmartOLT y Contrato
    serial_onu: serialFisicoLP || `SN-${serialInstalado}`, // Serial de fábrica (LP...) exclusivo de Odoo Servicio Externo
    cpe: `INICIO: ${fieldData.dropInicio || 0}  FINAL: ${fieldData.dropFinal || 0} (Total: ${fieldData.dropTotal || 0} mts)`,
    nodo_instalacion: (cliente?.oltAsignada === 'OLT SanAgustin' || (cliente?.zona || '').toLowerCase().includes('agust') || (cliente?.zona || '').toLowerCase().includes('marin')) ? 90 : 86,
    nodo_nombre: (cliente?.oltAsignada === 'OLT SanAgustin' || (cliente?.zona || '').toLowerCase().includes('agust') || (cliente?.zona || '').toLowerCase().includes('marin')) ? 'San Agustín FTTH' : 'Torre Andres Bello FTTH',
    tipo_instalacion: 1, // ID 1 = 'Cliente'
    tipo_instalacion_nombre: 'Cliente',
    coordenadas_gps: fieldData.gpsCoords || '10.4880, -66.8791',
    description_notas: `
PON S/N (SmartOLT): ${serialInstalado}
Serial Fábrica (LP): ${serialFisicoLP || 'N/A'}
SUB: ${fieldData.id || cliente.id}
Localización Caja NAP: ${fieldData.lugarNap || 'Poste 14'}
Puerto Caja: ${fieldData.puertoNap || '5'}
Código Caja NAP: ${fieldData.codigoNap || 'NAP-SA-04'}
INICIO: ${fieldData.dropInicio || 0}  FINAL: ${fieldData.dropFinal || 0} (${fieldData.dropTotal || 0} mts)
COORDENADAS GPS: ${fieldData.gpsCoords || 'No registradas'}
OBSERVACIONES: ${fieldData.observaciones || 'Ninguna'}
`.trim(),

    // 3. Partes de Horas (account.analytic.line)
    partes_de_horas: [
      {
        employee_id: emp1.id,
        employee_name: emp1.name,
        cargo: 'Técnico Principal',
        unit_amount: 0,
        name: '/'
      },
      {
        employee_id: emp2.id,
        employee_name: emp2.name,
        cargo: 'Ayudante',
        unit_amount: 0,
        name: '/'
      }
    ],

    // 4. Evidencias fotográficas y Contrato PDF (mail.message + ir.attachment en Chatter)
    evidencias_chatter: [
      { tipo: 'Potencia Óptica (dBm)', nombre: `1_Foto_Potencia_dBm_${fieldData.id}.jpg`, valor: fieldData.potenciaDbm || '-19.4 dBm', presente: !!fieldData.fotos?.potencia },
      { tipo: 'Caja NAP y Puerto', nombre: `2_Foto_Caja_NAP_${fieldData.id}.jpg`, valor: `Puerto ${fieldData.puertoNap || 'N/A'}`, presente: !!fieldData.fotos?.nap },
      { tipo: 'Caja/Etiqueta ONU (PON S/N y S/N)', nombre: `3_Foto_Caja_ONU_${fieldData.id}.jpg`, valor: `PON: ${serialInstalado}`, presente: !!fieldData.fotos?.onu },
      { tipo: 'Fachada del Inmueble', nombre: `4_Foto_Fachada_${fieldData.id}.jpg`, valor: fieldData.nombre || 'Fachada', presente: !!fieldData.fotos?.fachada },
      { tipo: 'Foto Extra 1 (Campo)', nombre: `5_Foto_Extra_1_${fieldData.id}.jpg`, valor: 'Evidencia de Campo 1', presente: !!fieldData.fotos?.extra1 },
      { tipo: 'Foto Extra 2 (Campo)', nombre: `6_Foto_Extra_2_${fieldData.id}.jpg`, valor: 'Evidencia de Campo 2', presente: !!fieldData.fotos?.extra2 },
      { tipo: 'Firma Digital Cliente (PNG)', nombre: `7_Firma_Digital_Cliente_${fieldData.id}.png`, valor: 'Firma táctil 1:1 adjunta', presente: !!fieldData.firmaCliente }
    ]
  };

  // Guardar respaldo local de la simulación en carpeta /data
  const filename = `expediente_servicio_externo_${fieldData.id || Date.now()}.json`;
  fs.writeFileSync(path.join(DATA_DIR, filename), JSON.stringify(simOdooTask, null, 2));

  // D) Inserción o actualización en vivo en Odoo Producción (Servicio Externo)
  let odooTaskId = null;
  let odooAction = 'creada';
  let odooError = null;
  let subUpdatedCode = null;
  let finalTaskIp = 'DHCP (Automática)';
  let finalTaskName = `Instalación de F.O:  ${fieldData.id}`;
  let adjuntosSubidos = 0;

  // -------------------------------------------------------------
  // SMARTOLT: Aprovisionamiento Real en OLT (Backend)
  // -------------------------------------------------------------
  let smartOltResult = null;
  let smartOltStatus = 'OMITIDO';
  let smartOltDetalle = '';

  let cleanSnPon = serialInstalado.toUpperCase().trim();
  if (cleanSnPon.startsWith('CTDC')) {
    console.log(`🔄 [Auto-Fix SN] Corrigiendo prefijo C-Data invertido: ${cleanSnPon} -> CDTC${cleanSnPon.slice(4)}`);
    cleanSnPon = 'CDTC' + cleanSnPon.slice(4);
  }
  const esSerialValido = cleanSnPon && !cleanSnPon.includes('PENDIENTE') && cleanSnPon.length >= 6;

  if (esSerialValido) {
    try {
      console.log(`📡 [SmartOLT] Iniciando aprovisionamiento real para SN: ${cleanSnPon}...`);

      let oltId = (cliente?.oltAsignada === 'OLTANDRESBELLO' || (cliente?.zona || '').toLowerCase().includes('bello')) ? '3' : '4';
      let board = '1';
      let port = '1';
      let onuTypeDetected = '';

      // 1. Detectar OLT física, board y port reales desde unconfigured_onus mediante polling (3 intentos espaciados 2.5s)
      let detected = null;
      for (let intento = 1; intento <= 3; intento++) {
        try {
          console.log(`🔍 [SmartOLT] Buscando ONU ${cleanSnPon} en unconfigured_onus (intento ${intento}/3)...`);
          const unconfRes = await callSmartOlt('/api/onu/unconfigured_onus');
          const unconfList = unconfRes.response || [];
          const altSn = cleanSnPon.startsWith('CDTC') ? ('CTDC' + cleanSnPon.slice(4)) : (cleanSnPon.startsWith('CTDC') ? ('CDTC' + cleanSnPon.slice(4)) : null);
          detected = unconfList.find(o => {
            const s = (o.sn || '').toUpperCase();
            return s === cleanSnPon || (altSn && s === altSn);
          });
          if (detected) {
            if (detected.sn && detected.sn.toUpperCase() !== cleanSnPon) {
              console.log(`🔄 [SmartOLT] Adoptando serial físico detectado en OLT: ${detected.sn.toUpperCase()}`);
              cleanSnPon = detected.sn.toUpperCase();
            }
            if (detected.olt_id) oltId = String(detected.olt_id);
            board = String(detected.board || board);
            port = String(detected.port || port);
            onuTypeDetected = detected.onu_type_name || '';
            console.log(`🎯 [SmartOLT] ¡ONU detectada físicamente en OLT!: OLT ${oltId}, Board ${board}, Port ${port}, Tipo ${onuTypeDetected}`);
            break;
          }
        } catch (errUnconf) {
          console.warn(`Aviso intento ${intento}: error consultando unconfigured_onus:`, errUnconf.message);
        }
        if (intento < 3) await new Promise(r => setTimeout(r, 2500));
      }

      // Si no aparece en unconfigured_onus, comprobar si ya está autorizada en la OLT
      let yaAutorizadaPrevia = false;
      if (!detected) {
        try {
          const existingDetails = await callSmartOlt(`/api/onu/get_onu_details/${cleanSnPon}`);
          if (existingDetails && existingDetails.status && existingDetails.onu_details) {
            const existingName = (existingDetails.onu_details.name || '').toUpperCase();
            const currSub = (subCodeKey || '').toUpperCase();
            const currClient = (cliente.nombre || fieldData.nombre || '').toUpperCase();
            const primerNombre = currClient.split(' ')[0] || '';

            // SEGURIDAD ESTRICTA ANTI-SECUESTRO:
            // Si la ONU ya existe en SmartOLT pero pertenece a OTRO cliente diferente,
            // NUNCA sobreescribirla ni reasignarla!
            const perteneceAlClienteActual = (currSub && existingName.includes(currSub)) || 
                                             (primerNombre.length >= 4 && existingName.includes(primerNombre));

            if (!perteneceAlClienteActual) {
              const errMsg = `⚠️ [SEGURIDAD SMARTOLT] La ONU ${cleanSnPon} ya se encuentra registrada y asignada a OTRO abonado ('${existingDetails.onu_details.name}'). NO SE PUEDE SOBREESCRIBIR NI REASIGNAR AUTOMÁTICAMENTE.`;
              console.error(errMsg);
              smartOltStatus = 'FALLIDO';
              smartOltDetalle = `Bloqueo de seguridad: La ONU ${cleanSnPon} ya pertenece a '${existingDetails.onu_details.name}'.`;
              throw new Error(smartOltDetalle);
            }

            yaAutorizadaPrevia = true;
            console.log(`ℹ️ [SmartOLT] ONU ${cleanSnPon} ya está registrada previamente para este abonado (${existingName}) en Board ${existingDetails.onu_details.board}, Port ${existingDetails.onu_details.port}`);
            oltId = String(existingDetails.onu_details.olt_id || oltId);
            board = String(existingDetails.onu_details.board || board);
            port = String(existingDetails.onu_details.port || port);
            onuTypeDetected = existingDetails.onu_details.onu_type_name || '';
            smartOltStatus = 'EXITOSO';
            smartOltDetalle = 'ONU ya autorizada y sincronizada en SmartOLT';
          }
        } catch (eExist) {
          if (smartOltStatus === 'FALLIDO') throw eExist;
        }
      }

      if (!detected && !yaAutorizadaPrevia) {
        console.warn(`⚠️ [SmartOLT] ONU ${cleanSnPon} no se detectó en unconfigured_onus ni activa. Se intentará autorizar en OLT ${oltId}, Board ${board}, Port ${port}.`);
      }

      // 2. Determinar Preset oficial: CDATA-FD514GS1 vs V2802ACT (y demás ONUs)
      const isCdata = cleanSnPon.startsWith('CDTC') || cleanSnPon.startsWith('CTDC') || onuTypeDetected.toLowerCase().includes('cdata') || onuTypeDetected.toLowerCase().includes('fd514');
      const defaultPreset = resolveSmartOltPreset(oltId, isCdata);
      let presetId = defaultPreset.id;
      let presetName = defaultPreset.name;

      try {
        const presetsRes = await callSmartOlt(`/api/onu/get_authorization_presets?olt_id=${oltId}`);
        const presetsList = presetsRes.response?.authorization_presets || [];
        let matched = null;
        if (isCdata) {
          matched = presetsList.find(p => p.name.toLowerCase().includes('cdata'));
        } else {
          matched = presetsList.find(p => p.name.toLowerCase().includes('catv'));
        }
        if (matched) {
          presetId = String(matched.id);
          presetName = matched.name;
        }
      } catch (ePres) {}

      console.log(`📋 [SmartOLT] Usando Preset: "${presetName}" (ID ${presetId}) en OLT ${oltId} (Puerto PON físico: ${port})`);

      // 3. Formatear Nombre (AGG NOMBRE): SUBXXXX - Nombre del Cliente
      const rawName = (cliente.nombre || fieldData.nombre || 'Cliente Multicanal').trim();
      let subCode = (cliente.subscription || fieldData.subCode || cliente.subCode || '').trim();
      if (!subCode && (cliente.id && String(cliente.id).startsWith('SUB'))) {
        subCode = cliente.id.trim();
      }
      if (!subCode && (fieldData.id && String(fieldData.id).startsWith('SUB'))) {
        subCode = fieldData.id.trim();
      }
      if (!subCode && cliente.partner_id) {
        try {
          const subQ = await odooPool.query(`SELECT code FROM sale_subscription WHERE partner_id = $1 ORDER BY id DESC LIMIT 1;`, [cliente.partner_id]);
          if (subQ.rows.length > 0 && subQ.rows[0].code) {
            subCode = subQ.rows[0].code.trim();
          }
        } catch (eSubQ) {}
      }
      const finalClientName = sanitizeSmartOltString(subCode ? `${subCode} - ${rawName}` : rawName, 50);
      const addressSafe = sanitizeSmartOltString(cliente.direccion || fieldData.direccion || 'Caracas', 100);

      // 4. Autorizar en SmartOLT con Preset (Aplica VLAN 100 fija, Routing, WiFi y WAN por DHCP)
      const smartOltParams = {
        olt_id: oltId,
        pon_type: 'gpon',
        board: board,
        port: port,
        sn: cleanSnPon,
        onu_external_id: cleanSnPon,
        authorization_preset_id: presetId,
        name: finalClientName,
        address_or_comment: addressSafe
      };

      const postData = querystring.stringify(smartOltParams);
      smartOltResult = await callSmartOlt('/api/onu/authorize_onu', 'POST', postData);

      // Comprobar si la autorización tuvo éxito o si la ONU ya estaba previamente autorizada
      const errStr = (smartOltResult?.error || smartOltResult?.response || '').toLowerCase();
      const yaExiste = !smartOltResult?.status && (errStr.includes('already exists') || errStr.includes('already authorized') || errStr.includes('ya existe'));

      if ((smartOltResult && smartOltResult.status) || yaExiste || yaAutorizadaPrevia) {
        smartOltStatus = 'EXITOSO';
        smartOltDetalle = (yaExiste || yaAutorizadaPrevia) ? 'ONU ya autorizada y sincronizada en SmartOLT' : `Aprovisionada con éxito: ${smartOltResult?.response || 'ONU configurada'}`;
        console.log(`✅ [SmartOLT] ONU ${cleanSnPon} confirmada en OLT ${oltId} (${smartOltDetalle})`);

        // Consultar la IP real WAN entregada por DHCP tras la sincronización de la ONU
        let realDhcpIp = null;
        for (let intentoIp = 1; intentoIp <= 3; intentoIp++) {
          try {
            await new Promise(r => setTimeout(r, 2000));
            const detectedIp = await getSmartOltRealWanIp(cleanSnPon);
            if (detectedIp && detectedIp !== '0.0.0.0') {
              realDhcpIp = detectedIp;
              console.log(`🎯 [SmartOLT] IP WAN real asignada por DHCP en SmartOLT (intento ${intentoIp}): ${realDhcpIp}`);
              break;
            }
          } catch (eDet) {
            console.warn(`Aviso obteniendo IP DHCP en SmartOLT (intento ${intentoIp}):`, eDet.message);
          }
        }

        // Asignación de IP para el Abonado en Odoo:
        // Si SmartOLT reporta una IP real, se toma; si el técnico ingresó una IP manual fija válida, se toma;
        // de lo contrario, si es DHCP, NO inventar una IP estática ficticia (ej. 100.64.20.10) para no crear colisiones.
        const candidateIp = realDhcpIp || 
          ((fieldData.ipLan && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(String(fieldData.ipLan).trim())) ? String(fieldData.ipLan).trim() : null);

        finalTaskIp = candidateIp;
        console.log(`📌 [IP ASIGNADA] IP establecida para Odoo y registro técnico: ${finalTaskIp || 'DHCP (En proceso)'}`);

        // Si la IP aún no estuvo lista (típico mientras la ONU negocia DHCP con MikroTik),
        // programar sondeo en segundo plano a los 20 segundos para actualizar Odoo automáticamente:
        if (!realDhcpIp && cleanSnPon) {
          setTimeout(async () => {
            try {
              console.log(`⏳ [Background DHCP Sync] Consultando IP tardía en SmartOLT para ${cleanSnPon}...`);
              const bgIp = await getSmartOltRealWanIp(cleanSnPon);
              if (bgIp && bgIp !== '0.0.0.0') {
                console.log(`🎯 [Background DHCP Sync] ¡IP detectada por DHCP!: ${bgIp} para ${cleanSnPon}`);
                  if (odooTaskId && odooPool) {
                    await odooPool.query(`UPDATE project_task SET ip_lan = $1 WHERE id = $2;`, [bgIp, odooTaskId]);
                  }
                  if (odooPool) {
                    await odooPool.query(`
                      UPDATE sale_subscription 
                      SET ip_lan = $1 
                      WHERE (code = $2 OR code = 'SUB' || $2) 
                         OR (task_id = $3 AND $3 > 0);
                    `, [bgIp, String(clientRefId || '').replace('SUB', ''), odooTaskId || 0]);
                  }
                  const targetFiles = [clientRefId, `SUB${leadId}`, String(leadId)].filter(Boolean);
                  for (const tf of targetFiles) {
                    const compP = path.join(COMPLETED_DIR, `${tf}.json`);
                    if (fs.existsSync(compP)) {
                      try {
                        const c = JSON.parse(fs.readFileSync(compP, 'utf8'));
                        c.ipLan = bgIp;
                        fs.writeFileSync(compP, JSON.stringify(c, null, 2));
                      } catch(e) {}
                    }
                  }
                  try {
                    const logs = getInstallationsLog();
                    const li = logs.find(l => targetFiles.includes(String(l.clientId)) || (odooTaskId && l.odooTaskId === odooTaskId));
                    if (li) {
                      li.ipLan = bgIp;
                      saveInstallationsLog(logs);
                    }
                  } catch(e) {}
                }
            } catch(eBg) {
              console.warn('Aviso en Background DHCP Sync:', eBg.message);
            }
          }, 20000);
        }

        // 5. CAMBIO DE ZONA a la que corresponda (update_location_details)
        const zonaStr = ((cliente?.zona || fieldData.zona || '') + ' ' + (cliente?.ubicacion || fieldData.ubicacion || '')).toLowerCase();
        let smartZoneName = 'San Agustin FTTH';
        if (zonaStr.includes('marin')) smartZoneName = 'Marin';
        else if (zonaStr.includes('hornos')) smartZoneName = 'Hornos de Cal';
        else if (zonaStr.includes('charneca')) smartZoneName = 'La Charneca';
        else if (zonaStr.includes('televisora')) smartZoneName = 'La Televisora';
        else if (zonaStr.includes('ajuro')) smartZoneName = 'Barrio Ajuro';
        else if (zonaStr.includes('anacoco')) smartZoneName = 'Calle Anacoco';
        else if (zonaStr.includes('bosco')) smartZoneName = 'Centro Empresarial Don Bosco';
        else if (zonaStr.includes('manolos')) smartZoneName = 'Los Manolos';
        else if (zonaStr.includes('daniela')) smartZoneName = 'residencia las Danielas';
        else if (oltId === '3') smartZoneName = 'Los Manolos';

        const locParams = {
          zone: smartZoneName,
          name: finalClientName,
          address_or_comment: addressSafe
        };
        if (fieldData.gpsCoords) {
          const mGps = String(fieldData.gpsCoords).match(/(-?\d+\.\d+)[,\s/|]+(-?\d+\.\d+)/);
          if (mGps && !isNaN(parseFloat(mGps[1])) && !isNaN(parseFloat(mGps[2]))) {
            locParams.latitude = mGps[1].trim();
            locParams.longitude = mGps[2].trim();
          }
        }

        try {
          await callSmartOlt(`/api/onu/update_location_details/${cleanSnPon}`, 'POST', querystring.stringify(locParams));
          console.log(`📍 [SmartOLT] Zona actualizada a "${smartZoneName}" y ubicación guardada para ${cleanSnPon}`);
        } catch (eLoc) {
          console.warn('Aviso actualizando zona en SmartOLT:', eLoc.message);
        }

        // 6. VLAN ADICIONAL (update_attached_vlans si se suministra)
        const vlanAdicional = fieldData.vlanAdicional || req.body.vlanAdicional;
        if (vlanAdicional && String(vlanAdicional).trim() !== '' && String(vlanAdicional).trim() !== '100') {
          try {
            await callSmartOlt(`/api/onu/update_attached_vlans/${cleanSnPon}`, 'POST', querystring.stringify({
              add_vlans: String(vlanAdicional).trim()
            }));
            console.log(`🌐 [SmartOLT] VLAN adicional ${vlanAdicional} adjuntada a ONU ${cleanSnPon}`);
          } catch (eVlan) {
            console.warn('Aviso adjuntando VLAN adicional:', eVlan.message);
          }
        }

      } else {
        smartOltStatus = 'FALLIDO';
        smartOltDetalle = smartOltResult?.error || smartOltResult?.response || JSON.stringify(smartOltResult);
        console.error(`⚠️ [SmartOLT] Respuesta negativa al autorizar ONU:`, smartOltDetalle);
      }
    } catch (smartErr) {
      smartOltStatus = 'ERROR_CONEXION';
      smartOltDetalle = smartErr.message;
      console.error(`❌ [SmartOLT] Excepción al llamar SmartOLT:`, smartErr.message);
    }
  } else {
    console.warn(`⚠️ [SmartOLT] Serial PON omitido o inválido: '${serialInstalado}'`);
  }

  try {
    const rawLeadId = fieldData.lead_id || (fieldData.id ? String(fieldData.id).replace(/\D/g, '') : '') || (cliente?.lead_id ? String(cliente.lead_id) : '');
    const leadId = String(rawLeadId).trim();
    const leadFormatted = leadId && leadId.length >= 3 ? (leadId.slice(0, 2) + '.' + leadId.slice(2)) : '';
    const partnerId = cliente.partner_id || null;

    // Obtener datos técnicos y de aprobación del CRM Lead
    let crmFechaAprobacion = null;
    let esRadioFrecuencia = false;
    if (leadId && !isNaN(parseInt(leadId))) {
      try {
        const leadRowRes = await odooPool.query(
          `SELECT x_studio_fecha_y_hora_de_aprobacin, medio_campaign FROM crm_lead WHERE id = $1 LIMIT 1;`,
          [parseInt(leadId)]
        );
        if (leadRowRes.rows.length > 0) {
          crmFechaAprobacion = leadRowRes.rows[0].x_studio_fecha_y_hora_de_aprobacin;
          if (leadRowRes.rows[0].medio_campaign === 'rf') {
            esRadioFrecuencia = true;
          }
        }
      } catch (crmReadErr) {
        console.warn('⚠️ No se pudo consultar fecha_aprobacion/medio_campaign del CRM:', crmReadErr.message);
      }
    }
    if (!esRadioFrecuencia && ((cliente?.equipo || '').toLowerCase().includes('radio') || (fieldData?.equipo || '').toLowerCase().includes('radio'))) {
      esRadioFrecuencia = true;
    }

    finalTaskName = `${esRadioFrecuencia ? 'Instalación de R.F:' : 'Instalación de F.O:'}  ${leadFormatted || leadId || fieldData.id}`;

    let existing = null;
    if (partnerId || (leadId && leadId.length >= 3)) {
      const existingRes = await odooPool.query(`
        SELECT id, name, partner_id, stage_id, ip_lan, fecha_instalacion 
        FROM project_task 
        WHERE project_id = 9 
          AND (
            ($1::int IS NOT NULL AND partner_id = $1)
            OR (NULLIF($2, '') IS NOT NULL AND name LIKE '%' || $2 || '%')
            OR (NULLIF($3, '') IS NOT NULL AND name LIKE '%' || $3 || '%')
          )
        ORDER BY id DESC LIMIT 1;
      `, [partnerId, leadId, leadFormatted]);

      if (existingRes.rows.length > 0) {
        const candidate = existingRes.rows[0];
        // SEGURIDAD ESTRICTA: Solo actualizar si coincide el partner_id o si la tarea encontrada no tiene partner asignado.
        // Si pertenece a otro cliente (ej. Helen Martinez 51258 vs Mildred 51410), NUNCA tocar la tarea ajena!
        if (partnerId && candidate.partner_id && candidate.partner_id !== partnerId) {
          console.warn(`⚠️ [SEGURIDAD] Tarea #${candidate.id} (${candidate.name}) pertenece a partner ${candidate.partner_id}. El cliente actual es partner ${partnerId}. NO SE SOBREESCRIBE. Se creará una tarea nueva.`);
        } else {
          existing = candidate;
        }
      }
    }

    const wasAlreadyInstalled = !!(existing && (existing.stage_id === 841 || existing.fecha_instalacion != null));
    if (wasAlreadyInstalled) {
      console.log(`ℹ️ [Idempotencia] Tarea #${existing.id} ya figura previamente en Odoo como instalada (Etapa: ${existing.stage_id}, Fecha: ${existing.fecha_instalacion}). Se preservarán fechas y se evitarán notificaciones duplicadas.`);
    }

    let ipParaOdoo = null;

    if (existing) {
      // ACTUALIZAR TAREA EXISTENTE A ETAPA 841 (Instalados)
      odooTaskId = existing.id;
      odooAction = 'actualizada';
      if (existing.name) finalTaskName = existing.name;

      // Determinación de IP LAN para Odoo (Gestión 100% DHCP / Pool Libre):
      // Se valida que la IP no esté repetida en otra tarea; si hay colisión, se calcula la siguiente libre del pool
      const esIpValida = finalTaskIp && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(finalTaskIp);
      if (esIpValida) {
        const ipConflict = await odooPool.query(
          `SELECT id, name FROM project_task WHERE ip_lan = $1 AND id != $2 LIMIT 1;`,
          [finalTaskIp, odooTaskId]
        );
        if (ipConflict.rows.length === 0) {
          ipParaOdoo = finalTaskIp;
        } else {
          console.warn(`⚠️ IP ${finalTaskIp} ya pertenece a tarea #${ipConflict.rows[0].id}. Obteniendo siguiente IP libre del pool.`);
          const freshIp = await obtenerIpLibreSmartOlt(cliente?.oltAsignada || 'OLT SanAgustin');
          ipParaOdoo = freshIp.ip;
          finalTaskIp = freshIp.ip;
        }
      }

      await odooPool.query(`
        UPDATE project_task 
        SET stage_id = 841,
            fsm_done = true,
            instalador_1 = $1,
            instalador_2 = $2,
            ip_lan = $3,
            codigo_onu = $4,
            serial_onu = $5,
            cpe = $6,
            nodo_instalacion = $7,
            tipo_instalacion = 1,
            notas = $8,
            fecha_instalacion = COALESCE(fecha_instalacion, NOW()),
            planned_date_begin = COALESCE(planned_date_begin, NOW()),
            planned_date_end = COALESCE(planned_date_end, NOW()),
            fecha_aprobacion = COALESCE($11, fecha_aprobacion, NOW()),
            write_uid = $10,
            write_date = NOW()
        WHERE id = $9;
      `, [
        emp1.name,
        emp2.name,
        ipParaOdoo,
        serialInstalado,
        serialFisicoLP || `SN-${serialInstalado}`,
        `INICIO: ${fieldData.dropInicio || 0}  FINAL: ${fieldData.dropFinal || 0} (Total: ${fieldData.dropTotal || 0} mts)`,
        (cliente?.oltAsignada === 'OLT SanAgustin' || (cliente?.zona || '').toLowerCase().includes('agust') || (cliente?.zona || '').toLowerCase().includes('marin')) ? 90 : 86,
        simOdooTask.description_notas,
        odooTaskId,
        ODOO_SYSTEM_USER.id,
        crmFechaAprobacion
      ]);
      console.log(`✅ Tarea #${odooTaskId} en Odoo Servicio Externo actualizada por ${ODOO_SYSTEM_USER.name} (Fecha Planif/Instalación: ${wasAlreadyInstalled ? 'Preservada original' : 'NOW'}, Aprobación: ${crmFechaAprobacion || 'NOW'}).`);
    } else {
      // CREAR NUEVA TAREA EN ETAPA 841 (Instalados)
      const esIpValida = finalTaskIp && /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(finalTaskIp);
      if (esIpValida) {
        const ipCheck = await odooPool.query(`SELECT 1 FROM project_task WHERE ip_lan = $1 LIMIT 1;`, [finalTaskIp]);
        if (ipCheck.rows.length === 0) {
          ipParaOdoo = finalTaskIp;
        } else {
          console.warn(`⚠️ IP ${finalTaskIp} ya ocupada en Odoo. Obteniendo siguiente IP libre del pool.`);
          const freshIp = await obtenerIpLibreSmartOlt(cliente?.oltAsignada || 'OLT SanAgustin');
          ipParaOdoo = freshIp.ip;
          finalTaskIp = freshIp.ip;
        }
      }

      const insRes = await odooPool.query(`
        INSERT INTO project_task (
          name, project_id, display_project_id, stage_id, kanban_state, priority, sequence, active,
          partner_id, company_id, create_uid, write_uid, create_date, write_date,
          fsm_done, worksheet_template_id,
          instalador_1, instalador_2,
          ip_lan, codigo_onu, serial_onu, cpe, nodo_instalacion, tipo_instalacion,
          notas, fecha_instalacion, planned_date_begin, planned_date_end, fecha_aprobacion
        ) VALUES (
          $1, 9, 9, 841, 'normal', '0', 10, true,
          $2, 2, $11, $11, NOW(), NOW(),
          true, 1,
          $3, $4,
          $5, $6, $7, $8, $9, 1,
          $10, NOW(), NOW(), NOW(), COALESCE($12, NOW())
        ) RETURNING id;
      `, [
        finalTaskName,
        partnerId,
        emp1.name,
        emp2.name,
        ipParaOdoo,
        serialInstalado,
        serialFisicoLP || `SN-${serialInstalado}`,
        `INICIO: ${fieldData.dropInicio || 0}  FINAL: ${fieldData.dropFinal || 0} (Total: ${fieldData.dropTotal || 0} mts)`,
        (cliente?.oltAsignada === 'OLT SanAgustin' || (cliente?.zona || '').toLowerCase().includes('agust') || (cliente?.zona || '').toLowerCase().includes('marin')) ? 90 : 86,
        simOdooTask.description_notas,
        ODOO_SYSTEM_USER.id,
        crmFechaAprobacion
      ]);
      odooTaskId = insRes.rows[0].id;
      odooAction = 'creada';
      console.log(`✅ Nueva tarea #${odooTaskId} ('${finalTaskName}') creada en Odoo para partner ${partnerId} por ${ODOO_SYSTEM_USER.name} en etapa Instalados.`);
    }

    // Vincular Categoría a la Tarea (project_tags_project_task_rel)
    // ID 33 = 'FIBRA ÓPTICA', ID 36 = 'RADIO FRECUENCIA'
    if (odooTaskId) {
      try {
        const taskTagId = esRadioFrecuencia ? 36 : 33;
        const checkTag = await odooPool.query(
          `SELECT 1 FROM project_tags_project_task_rel WHERE project_task_id = $1 AND project_tags_id = $2 LIMIT 1;`,
          [odooTaskId, taskTagId]
        );
        if (checkTag.rows.length === 0) {
          await odooPool.query(
            `INSERT INTO project_tags_project_task_rel (project_task_id, project_tags_id) VALUES ($1, $2);`,
            [odooTaskId, taskTagId]
          );
          console.log(`✅ Categoría '${esRadioFrecuencia ? 'RADIO FRECUENCIA' : 'FIBRA ÓPTICA'}' (#${taskTagId}) asignada a tarea #${odooTaskId}.`);
        }
      } catch (tagErr) {
        console.warn('⚠️ No se pudo asignar categoría a la tarea:', tagErr.message);
      }
    }

    // Vincular Categoría al Contacto/Cliente (res_partner_res_partner_category_rel)
    // ID 8 = 'Fibra Óptica', ID 10 = 'Radiofrecuencia'
    // (Se refleja directamente en la suscripción como 'Etiqueta')
    if (partnerId) {
      try {
        const partnerCatId = esRadioFrecuencia ? 10 : 8;
        const checkCat = await odooPool.query(
          `SELECT 1 FROM res_partner_res_partner_category_rel WHERE partner_id = $1 AND category_id = $2 LIMIT 1;`,
          [partnerId, partnerCatId]
        );
        if (checkCat.rows.length === 0) {
          await odooPool.query(
            `INSERT INTO res_partner_res_partner_category_rel (partner_id, category_id) VALUES ($1, $2);`,
            [partnerId, partnerCatId]
          );
          console.log(`✅ Categoría '${esRadioFrecuencia ? 'Radiofrecuencia' : 'Fibra Óptica'}' (#${partnerCatId}) asignada a contacto #${partnerId}.`);
        }
      } catch (catErr) {
        console.warn('⚠️ No se pudo asignar categoría al contacto:', catErr.message);
      }
    }

    // Insertar partes de horas (account_analytic_line)
    if (odooTaskId) {
      try {
        const checkTs = await odooPool.query(`SELECT 1 FROM account_analytic_line WHERE task_id = $1 LIMIT 1;`, [odooTaskId]);
        if (checkTs.rows.length === 0) {
          await odooPool.query(`
            INSERT INTO account_analytic_line (
              name, employee_id, project_id, task_id, account_id, unit_amount, amount, date, company_id, create_date, write_date
            ) VALUES 
              ('/', $1, 9, $3, 23, 0, 0, CURRENT_DATE, 2, NOW(), NOW()),
              ('/', $2, 9, $3, 23, 0, 0, CURRENT_DATE, 2, NOW(), NOW());
          `, [emp1.id, emp2.id, odooTaskId]);
          console.log(`✅ Partes de horas registrados para tarea #${odooTaskId}.`);
        }
      } catch (tsErr) {
        console.warn('⚠️ No se pudieron registrar partes de horas en Odoo:', tsErr.message);
      }

      // Inserción en Chatter: mail_message + ir_attachment + message_attachment_rel
      try {
        let yaTieneAdjuntos = false;
        if (wasAlreadyInstalled && odooTaskId) {
          const checkAtt = await odooPool.query(
            `SELECT id FROM ir_attachment WHERE res_model = 'project.task' AND res_id = $1 LIMIT 1;`,
            [odooTaskId]
          );
          if (checkAtt.rows.length > 0) {
            yaTieneAdjuntos = true;
            console.log(`ℹ️ [Chatter Odoo] Tarea #${odooTaskId} ya cuenta con evidencias adjuntas previas. Se omite duplicación de adjuntos y mensaje en Chatter.`);
          }
        }

        if (!yaTieneAdjuntos) {
          const fotosObj = fieldData.fotos || {};
          const clientRefId = fieldData.id || cliente.id || 'DOC';
          const contratoData = fieldData.contratoPdf || fieldData.contratoPdfBase64 || null;
          const fotoVelocidadData = fotosObj.velocidad || fotosObj.testVelocidad || fotosObj.foto7 || null;
          const fotoExtra3Data = fotosObj.extra3 || fotosObj.foto8 || fotosObj.adicional || null;

          const msgHtml = `
<p><b>📱 Instalación Registrada desde la App Móvil de Campo Multicanal</b></p>
<ul>
  <li><b>Técnicos:</b> ${emp1.name} (Principal) / ${emp2.name} (Ayudante)</li>
  <li><b>ONU PON S/N (SmartOLT):</b> <code>${serialInstalado}</code></li>
  <li><b>Aprovisionamiento SmartOLT:</b> <b>${smartOltStatus}</b> (${smartOltDetalle || 'Configurada'})</li>
  <li><b>Serial Fábrica (LP - Servicio Externo):</b> <code>${serialFisicoLP || 'N/A'}</code> | <b>IP LAN:</b> <code>${finalTaskIp}</code></li>
  <li><b>Potencia Óptica:</b> <b>${fieldData.potenciaDbm || 'N/A'} dBm</b></li>
  <li><b>Caja NAP:</b> <b>${fieldData.lugarNap || 'N/A'} (Puerto ${fieldData.puertoNap || 'N/A'})</b></li>
  <li><b>Metros Drop:</b> ${fieldData.dropTotal || 0} mts</li>
  <li><b>Coordenadas GPS:</b> ${fieldData.gpsCoords || 'N/A'}</li>
  <li><b>Observaciones:</b> ${fieldData.observaciones || 'Ninguna'}</li>
  ${fotoVelocidadData ? `<li><b>Test de Velocidad:</b> 🚀 <code>Foto_7_Test_Velocidad_${clientRefId}.jpg</code></li>` : ''}
  ${fotoExtra3Data ? `<li><b>Foto Extra 3:</b> 📸 <code>Foto_8_Extra_${clientRefId}.jpg</code></li>` : ''}
  ${contratoData ? `<li><b>Contrato Digital Firmado:</b> 📄 <code>Contrato_Digital_Firmado_${clientRefId}.pdf</code></li>` : ''}
</ul>
<p><i>Adjuntas evidencias fotográficas de campo, firma digital y contrato firmado del cliente.</i></p>
          `.trim();

          const msgRes = await odooPool.query(`
            INSERT INTO mail_message (
              body, model, res_id, record_name, message_type, subtype_id, author_id, date, create_uid, write_uid, create_date, write_date
            ) VALUES (
              $1, 'project.task', $2, $3, 'comment', 2, $4, NOW(), $5, $5, NOW(), NOW()
            ) RETURNING id;
          `, [msgHtml, odooTaskId, finalTaskName, ODOO_SYSTEM_USER.partnerId, ODOO_SYSTEM_USER.id]);
          const odooMsgId = msgRes.rows[0].id;

          const listaAdjuntos = [
            { nombre: `1_Foto_Potencia_dBm_${clientRefId}.jpg`, data: fotosObj.potencia, defMime: 'image/jpeg' },
            { nombre: `2_Foto_Caja_NAP_${clientRefId}.jpg`, data: fotosObj.nap, defMime: 'image/jpeg' },
            { nombre: `3_Foto_Caja_ONU_${clientRefId}.jpg`, data: fotosObj.onu, defMime: 'image/jpeg' },
            { nombre: `4_Foto_Fachada_${clientRefId}.jpg`, data: fotosObj.fachada, defMime: 'image/jpeg' },
            { nombre: `5_Foto_Extra_1_${clientRefId}.jpg`, data: fotosObj.extra1, defMime: 'image/jpeg' },
            { nombre: `6_Foto_Extra_2_${clientRefId}.jpg`, data: fotosObj.extra2, defMime: 'image/jpeg' },
            { nombre: `Foto_7_Test_Velocidad_${clientRefId}.jpg`, data: fotoVelocidadData, defMime: 'image/jpeg' },
            { nombre: `Foto_8_Extra_${clientRefId}.jpg`, data: fotoExtra3Data, defMime: 'image/jpeg' },
            { nombre: `7_Firma_Digital_Cliente_${clientRefId}.png`, data: fieldData.firmaCliente, defMime: 'image/png' },
            { nombre: `Contrato_Digital_Firmado_${clientRefId}.pdf`, data: contratoData, defMime: 'application/pdf' }
          ];

          for (const adj of listaAdjuntos) {
            if (!adj.data || typeof adj.data !== 'string' || !adj.data.includes(';base64,')) continue;

            const parts = adj.data.split(';base64,');
            if (parts.length < 2) continue;

            const rawMime = parts[0].replace(/^data:/, '').split(';')[0].trim();
            const mime = rawMime || adj.defMime;
            const buffer = Buffer.from(parts[1], 'base64');
            if (!buffer || buffer.length === 0) continue;

            const checksum = crypto.createHash('sha1').update(buffer).digest('hex');
            const indexContent = mime.includes('pdf') ? 'application' : 'image';

            const insAtt = await odooPool.query(`
              INSERT INTO ir_attachment (
                name, res_model, res_id, company_id, type, mimetype, file_size, checksum, index_content, db_datas, create_uid, write_uid, create_date, write_date
              ) VALUES (
                $1, 'project.task', $2, 2, 'binary', $3, $4, $5, $6, $7, $8, $8, NOW(), NOW()
              ) RETURNING id;
            `, [
              adj.nombre,
              odooTaskId,
              mime,
              buffer.length,
              checksum,
              indexContent,
              buffer,
              ODOO_SYSTEM_USER.id
            ]);

            const attachId = insAtt.rows[0].id;
            await odooPool.query(`
              INSERT INTO message_attachment_rel (message_id, attachment_id) VALUES ($1, $2) ON CONFLICT DO NOTHING;
            `, [odooMsgId, attachId]);
            adjuntosSubidos++;
          }
          console.log(`✅ ${adjuntosSubidos} evidencias fotográficas/firma/contrato adjuntadas con éxito al Chatter de tarea #${odooTaskId} por ${ODOO_SYSTEM_USER.name}.`);
        }
      } catch (attErr) {
        console.error('⚠️ Error al adjuntar evidencias en Chatter Odoo:', attErr.message);
      }
    }

    // E) Transición Automática: Mover Suscripción a etapa 8 ('In Progress' / 'En progreso') y vincular Tarea e IP/LAN
    try {
      const leadNumeric = leadId ? parseInt(leadId) : 0;
      const subLookupRes = await odooPool.query(`
        SELECT sub.id, sub.code, sub.stage_id 
        FROM sale_subscription sub
        WHERE sub.id IN (
          SELECT sol.subscription_id 
          FROM sale_order_line sol 
          JOIN sale_order so ON sol.order_id = so.id 
          WHERE (so.opportunity_id = $1 OR so.id = $1) AND sol.subscription_id IS NOT NULL 
        )
        OR (sub.code = $2 AND $2 <> '')
        OR (sub.code = 'SUB' || $2 AND $2 <> '')
        OR (sub.partner_id = $3 AND $3 > 0)
        OR (sub.partner_id = (SELECT partner_id FROM crm_lead WHERE id = $1 LIMIT 1))
        ORDER BY sub.id DESC LIMIT 1;
      `, [leadNumeric, fieldData.subCode || fieldData.id || '', partnerId || 0]);

      if (subLookupRes.rows.length > 0) {
        const subTarget = subLookupRes.rows[0];
        const mediumValue = esRadioFrecuencia ? 'rf' : 'ftth';
        const nodoInstalacionVal = (cliente?.oltAsignada === 'OLT SanAgustin' || (cliente?.zona || '').toLowerCase().includes('agust') || (cliente?.zona || '').toLowerCase().includes('marin')) ? 90 : 86;
        await odooPool.query(`
          UPDATE sale_subscription 
          SET stage_id = 8, 
              stage_category = 'progress',
              server_id = 6,
              medium = COALESCE(medium, $3),
              task_id = COALESCE($4, task_id),
              ip_lan = COALESCE($5, ip_lan),
              nodo_instalacion = COALESCE(nodo_instalacion, $6),
              date_start = COALESCE(date_start, CURRENT_DATE),
              write_date = NOW(),
              write_uid = $2
          WHERE id = $1;
        `, [subTarget.id, ODOO_SYSTEM_USER.id, mediumValue, odooTaskId || null, ipParaOdoo || null, nodoInstalacionVal]);
        subUpdatedCode = subTarget.code;
        console.log(`✅ Suscripción ${subTarget.code} (#${subTarget.id}) vinculada a Tarea Servicio Externo #${odooTaskId}, IP/LAN: ${ipParaOdoo || 'N/A'}, etapa 8 ('In Progress'), Servidor Bequant: BQN_Multicanal (#6) por ${ODOO_SYSTEM_USER.name}.`);
      }
    } catch (subErr) {
      console.warn('⚠️ No se pudo actualizar sale_subscription a In Progress:', subErr.message);
    }

    // F) Transición Automática: Mover CRM Lead a etapa 5 ('Instalado')
    if (leadId) {
      try {
        await odooPool.query(`
          UPDATE crm_lead 
          SET stage_id = 5,
              write_date = NOW(),
              write_uid = $2
          WHERE id = $1;
        `, [parseInt(leadId), ODOO_SYSTEM_USER.id]);
        console.log(`✅ Oportunidad CRM #${leadId} movida a etapa 5 ('Instalado') por ${ODOO_SYSTEM_USER.name}.`);
      } catch (leadErr) {
        console.warn('⚠️ No se pudo actualizar crm_lead a Instalado:', leadErr.message);
      }
    }
  } catch (errDb) {
    console.error('⚠️ Error al registrar en Odoo PostgreSQL:', errDb.message);
    odooError = errDb.message;
  }

  // Actualizar estado en la lista local de la App
  // Limpiar de atenciones activas
  delete activeInstallations[String(cliente.id)];
  delete activeInstallations[String(cliente.lead_id)];
  delete activeInstallations[String(fieldData.id)];
  delete activeInstallations[String(fieldData.lead_id)];

  // Actualizar estado en la lista local de la App
  const idx = odooApprovedClients.findIndex(c => c.id === fieldData.id || c.lead_id == fieldData.id || c.id === cliente.id);
  if (idx !== -1) {
    odooApprovedClients[idx].estado = 'Instalados';
  }

  // Limpiar de pospuestos si estaba pospuesto
  const curPostponed = getPostponed();
  delete curPostponed[String(cliente.lead_id)];
  delete curPostponed[String(cliente.id)];
  delete curPostponed[String(fieldData.id)];
  savePostponed(curPostponed);

  // Guardar registro completo y evidencias en COMPLETED_DIR (Modo Auditoría / Supervisión en Sede)
  const completedRecord = {
    clienteId: cliente.id || fieldData.id,
    leadId: cliente.lead_id || fieldData.lead_id,
    solicitud: cliente.solicitud || fieldData.solicitud || '',
    nombre: cliente.nombre || fieldData.nombre,
    cedula: cliente.cedula || fieldData.cedula || '',
    telefono: cliente.telefono || fieldData.telefono || '',
    direccion: cliente.direccion || fieldData.direccion,
    zona: cliente.zona || '',
    cuadrilla: cliente.cuadrilla || '',
    tecnico: fieldData.instalador || cliente.instalador,
    ayudante: fieldData.ayudante || cliente.ayudante,
    estado: 'Instalado',
    serialPon: serialInstalado,
    serialSecundario: serialFisicoLP || fieldData.serialSecundario || '',
    potenciaDbm: fieldData.potenciaDbm || '',
    puertoNap: fieldData.puertoNap || '',
    lugarNap: fieldData.lugarNap || '',
    codigoNap: fieldData.codigoNap || fieldData.lugarNap || '',
    dropInicio: fieldData.dropInicio || 0,
    dropFinal: fieldData.dropFinal || 0,
    dropTotal: parseInt(fieldData.dropTotal) || parseInt(fieldData.metrosDrop) || 0,
    utpInicio: fieldData.utpInicio || 0,
    utpFinal: fieldData.utpFinal || 0,
    metrosUtp: parseInt(fieldData.metrosUtp) || 0,
    ipLan: finalTaskIp,
    odooTaskId: odooTaskId,
    smartOltStatus: smartOltStatus,
    smartOltDetalle: smartOltDetalle,
    formaPago: fieldData.formaPago || '',
    gpsCoords: fieldData.gpsCoords || '',
    observaciones: fieldData.observaciones || '',
    materiales: fieldData.materiales || {},
    equiposCheck: fieldData.equiposCheck || {},
    fotos: fieldData.fotos || {},
    firmaCliente: fieldData.firmaCliente || null,
    contratoPdfBase64: fieldData.contratoPdf || fieldData.contratoPdfBase64 || null,
    fecha: getFechaHoraCaracas(),
    hora: getHoraCaracas(),
    timestamp: Date.now()
  };

  const completedKeys = [cliente.id, fieldData.id, cliente.lead_id, fieldData.lead_id].filter(Boolean);
  completedKeys.forEach(k => {
    try {
      fs.writeFileSync(path.join(COMPLETED_DIR, `${k}.json`), JSON.stringify(completedRecord, null, 2));
    } catch (eComp) {
      console.warn('Aviso guardando en COMPLETED_DIR:', eComp.message);
    }
    try {
      const dPath = path.join(DRAFTS_DIR, `${k}.json`);
      if (fs.existsSync(dPath)) fs.unlinkSync(dPath);
    } catch (eUnlink) {}
  });

  // Registrar en el log de auditoría
  const logEntry = {
    id: `INST-${cliente.id || cliente.lead_id || fieldData.id}-${Date.now()}`,
    logId: `INST-${cliente.id || cliente.lead_id || fieldData.id}-${Date.now()}`,
    tipo: 'INSTALACION_FINALIZADA',
    leadId: cliente.lead_id || (fieldData.id ? parseInt(String(fieldData.id).replace(/\D/g, '')) : null),
    clientId: cliente.id || fieldData.id || `SUB${cliente.lead_id}`,
    solicitud: cliente.solicitud || fieldData.solicitud || '',
    nombre: cliente.nombre || fieldData.nombre,
    direccion: cliente.direccion || fieldData.direccion,
    zona: cliente.zona || '',
    cuadrilla: cliente.cuadrilla || '',
    instalador: fieldData.instalador || cliente.instalador,
    ayudante: fieldData.ayudante || cliente.ayudante,
    tecnico: fieldData.instalador || cliente.instalador,
    estado: 'Instalado',
    serialPon: serialInstalado,
    serialOnu: serialInstalado,
    serialSecundario: serialFisicoLP || fieldData.serialSecundario || '',
    potenciaDbm: fieldData.potenciaDbm || '',
    puertoNap: fieldData.puertoNap || '',
    lugarNap: fieldData.lugarNap || '',
    codigoNap: fieldData.codigoNap || fieldData.lugarNap || '',
    dropTotal: parseInt(fieldData.dropTotal) || parseInt(fieldData.metrosDrop) || 0,
    metrosDrop: parseInt(fieldData.dropTotal) || parseInt(fieldData.metrosDrop) || 0,
    utpTotal: parseInt(fieldData.metrosUtp) || 0,
    metrosUtp: parseInt(fieldData.metrosUtp) || 0,
    ipLan: finalTaskIp,
    odooTaskId: odooTaskId,
    smartOltStatus: smartOltStatus,
    smartOltDetalle: smartOltDetalle,
    adjuntosCount: adjuntosSubidos,
    fecha: getFechaHoraCaracas(),
    hora: getHoraCaracas(),
    timestamp: Date.now()
  };

  if (!wasAlreadyInstalled) {
    try {
      appendInstallationLog(logEntry);
    } catch (logErr) {
      console.error('⚠️ Error al registrar en log de auditoría:', logErr.message);
    }

    // Notificar a todos los dispositivos (Admin y Técnicos) en tiempo real
    broadcastRealtime('INSTALLATION_COMPLETED', {
      entry: logEntry,
      clientId: cliente.id || fieldData.id,
      leadId: cliente.lead_id,
      nombre: cliente.nombre || fieldData.nombre,
      tecnico: fieldData.instalador || cliente.instalador,
      odooTaskId: odooTaskId,
      smartOltStatus: smartOltStatus,
      activeInstallations,
      kpis: calcularKpis()
    });
  } else {
    console.log(`ℹ️ [Auditoría] Tarea #${odooTaskId} ya estaba registrada previamente. Se omite duplicar en log y re-broadcast.`);
  }

  if (odooError) {
    console.error(`❌ [SUBMIT] Error crítico al persistir en Odoo: ${odooError}`);
    return res.status(500).json({
      success: false,
      error: `Error al registrar en Odoo: ${odooError}`,
      odooTaskId: odooTaskId,
      odooError: odooError,
      smartOltStatus: smartOltStatus,
      smartOltDetalle: smartOltDetalle
    });
  }

  // Notificación de contingencia por WhatsApp a Directivos (Ops Manager y Field Supervisor)
  if (!wasAlreadyInstalled) {
    notificarWhatsAppContingencia(completedRecord, fieldData, cliente).catch(err => {
      console.error('⚠️ [WhatsApp] Error no controlado en envío de contingencia:', err.message);
    });
  } else {
    console.log(`ℹ️ [WhatsApp] Tarea #${odooTaskId} ya estaba instalada previamente. Se omite re-envío de notificación a directivos.`);
  }

  res.json({
    success: true,
    alreadyInstalled: wasAlreadyInstalled,
    odooEscrituraReal: true,
    odooTaskId: odooTaskId,
    odooAction: wasAlreadyInstalled ? 'sin_cambios' : odooAction,
    odooError: null,
    subUpdatedCode: subUpdatedCode,
    adjuntosSubidos: adjuntosSubidos,
    smartOltStatus: smartOltStatus,
    smartOltDetalle: smartOltDetalle,
    smartOltResult: smartOltResult,
    mensaje: wasAlreadyInstalled
      ? `ℹ️ Instalación previamente registrada en Odoo (Tarea #${odooTaskId}). Sincronización idempotente completada sin duplicar datos.`
      : `✅ Instalación culminada en Odoo Servicio Externo (Tarea #${odooTaskId} en etapa 'Instalados'). Suscripción ${subUpdatedCode || ''} movida a 'En Progreso'. SmartOLT: ${smartOltStatus}. ${adjuntosSubidos} archivos adjuntados.`,
    simulacion: {
      tarea: { ...simOdooTask, odoo_task_id: odooTaskId, odoo_action: odooAction },
      smartolt: {
        olt: cliente?.oltAsignada || 'OLT SanAgustin',
        status: smartOltStatus,
        detalle: smartOltDetalle,
        ipAsignada: finalTaskIp,
        mascara: ipReservation.mask,
        gateway: ipReservation.gateway,
        vlan: ipReservation.vlan,
        modoWan: 'Static IP (Pool Libre Reservada)'
      },
      bequant: {
        subscriberId: `SUB${fieldData.id}-${fieldData.nombre}`,
        subscriberIp: finalTaskIp,
        planVelocidad: cliente?.itemsContrato?.[0]?.desc || 'Fibra 200 Mbps',
        estado: 'Encolado para activación tras etapa Instalados'
      }
    }
  });
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`🚀 Multicanal App corriendo localmente en http://127.0.0.1:${PORT}`);
});
