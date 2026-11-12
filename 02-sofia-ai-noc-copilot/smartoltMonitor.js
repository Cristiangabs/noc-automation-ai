const fs = require('fs');
const path = require('path');
const https = require('https');
const { Pool } = require('pg');
const config = require('./config');
const openwaClient = require('./openwaClient');
const learningService = require('./learningService');

const STATE_FILE = path.join(__dirname, 'monitor_state.json');

const odooPool = new Pool({
  host: process.env.ODOO_DB_HOST || '10.0.10.50',
  port: parseInt(process.env.ODOO_DB_PORT || '5432'),
  user: process.env.ODOO_DB_USER || 'odoo',
  password: process.env.ODOO_DB_PASSWORD || 'YOUR_DB_PASSWORD',
  database: process.env.ODOO_DB_NAME || 'multicanal',
  max: 3,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000
});

class SmartOltMonitor {
  constructor() {
    this.zones = new Map([
      ['22', 'Calle Anacoco'],
      ['23', 'Los Manolos'],
      ['28', 'residencia las Danielas'],
      ['33', 'Centro Empresarial Don Bosco'],
      ['36', 'La Charneca'],
      ['38', 'Hornos de Cal'],
      ['39', 'La Televisora'],
      ['40', 'Barrio Ajuro'],
      ['41', 'San Agustin FTTH'],
      ['42', 'Marin']
    ]);
    this.olts = new Map([
      ['3', 'OLTANDRESBELLO'],
      ['4', 'OLT SanAgustin']
    ]);
    this.state = {
      initialized: false,
      onus: {},               // sn -> { sn, name, zoneId, oltId, board, port, onuNum, status, lastStatusChange, lastNotifiedAt, lastFollowUpAt, followUpCount }
      activeOutages: {},      // zoneId -> { zoneId, zoneName, count, firstAlertAt, lastAlertAt }
      techGroups: []          // Lista de grupos técnicos registrados (@g.us)
    };
    this.timer = null;
    this.loadState();
  }

  loadState() {
    try {
      if (fs.existsSync(STATE_FILE)) {
        const raw = fs.readFileSync(STATE_FILE, 'utf8');
        this.state = Object.assign(this.state, JSON.parse(raw));
        if (!Array.isArray(this.state.techGroups)) {
          this.state.techGroups = [];
        }
        console.log(`[Monitor] Estado cargado desde ${STATE_FILE} (${Object.keys(this.state.onus).length} ONUs, ${this.state.techGroups.length} grupos registrados).`);
      }
    } catch (e) {
      console.error('[Monitor] Error al cargar monitor_state.json:', e.message);
    }
  }

  saveState() {
    try {
      fs.writeFileSync(STATE_FILE, JSON.stringify(this.state, null, 2), 'utf8');
    } catch (e) {
      console.error('[Monitor] Error al guardar monitor_state.json:', e.message);
    }
  }

  registerTechGroup(groupId) {
    if (!groupId || typeof groupId !== 'string' || !groupId.endsWith('@g.us')) {
      return false;
    }
    if (!Array.isArray(this.state.techGroups)) {
      this.state.techGroups = [];
    }
    if (!this.state.techGroups.includes(groupId)) {
      this.state.techGroups.push(groupId);
      this.saveState();
      console.log(`[Monitor] Nuevo grupo técnico registrado: ${groupId}`);
      return true;
    }
    return false;
  }

  async fetchSmartOlt(endpoint) {
    return new Promise((resolve) => {
      const req = https.request({
        hostname: 'isp-domain.smartolt.com',
        port: 443,
        path: endpoint,
        method: 'GET',
        headers: {
          'X-Token': 'YOUR_SMARTOLT_API_TOKEN'
        },
        timeout: 25000 // 25s timeout para respuestas grandes de SmartOLT
      }, res => {
        let d = '';
        res.on('data', c => d += c);
        res.on('end', () => {
          try {
            resolve(JSON.parse(d));
          } catch(e) {
            resolve(null);
          }
        });
      });
      req.on('error', err => {
        console.error(`[Monitor] Error solicitando ${endpoint}:`, err.message);
        resolve(null);
      });
      req.on('timeout', () => {
        req.destroy();
        resolve(null);
      });
      req.end();
    });
  }

  async loadCatalogs() {
    try {
      const [zRes, oRes] = await Promise.all([
        this.fetchSmartOlt('/api/system/get_zones'),
        this.fetchSmartOlt('/api/system/get_olts')
      ]);

      if (zRes && Array.isArray(zRes.response)) {
        zRes.response.forEach(z => {
          this.zones.set(String(z.id), z.name);
        });
      }
      if (oRes && Array.isArray(oRes.response)) {
        oRes.response.forEach(o => {
          this.olts.set(String(o.id), o.name);
        });
      }
      console.log(`[Monitor] Catálogos sincronizados: ${this.zones.size} zonas, ${this.olts.size} OLTs.`);
    } catch (e) {
      console.error('[Monitor] Error cargando catálogos:', e.message);
    }
  }

  getZoneName(zoneId) {
    return this.zones.get(String(zoneId)) || `Zona ${zoneId}`;
  }

  getOltName(oltId) {
    return this.olts.get(String(oltId)) || `OLT ${oltId}`;
  }

  // Formato individual requerido para cada ONU en alertas y consultas
  formatOnuCard(onu, cust) {
    const board = onu.board || 1;
    const port = onu.port || 1;
    const onuNum = onu.onu || onu.onuNum || 1;
    const zoneName = this.getZoneName(onu.zone_id || onu.zoneId);
    const sn = (onu.sn || onu.unique_external_id || '').toUpperCase();
    const clientName = (cust && cust.clientName) ? cust.clientName : (onu.name || 'Cliente no identificado');
    const subCode = (cust && cust.subCode) ? cust.subCode : 'N/A';

    return `• *GPON:* ONU gpon-onu_1/${board}/${port}:${onuNum}\n` +
           `• *PORT:* Port ${port} (${zoneName})\n` +
           `• *ZONE:* ${zoneName}\n` +
           `• *CLIENTE:* ${clientName}\n` +
           `• *SUB:* ${subCode}\n` +
           `• *SN GPON:* ${sn}`;
  }

  // Consultar en lote Nombre de Cliente y Código SUB desde Odoo y SmartOLT
  async getCustomerDetailsBatch(onus) {
    const result = new Map();
    if (!Array.isArray(onus) || onus.length === 0) return result;

    const snsToQuery = [];

    for (const o of onus) {
      const sn = (o.sn || o.unique_external_id || '').toUpperCase();
      if (!sn) continue;

      let name = (o.name || '').trim();
      let subCode = null;

      // Extraer SUB si viene en el campo nombre de SmartOLT (ej. "SUB8461 - Noris Mendoza")
      const m = name.match(/SUB\s*(\d+)/i);
      if (m) {
        subCode = 'SUB' + m[1];
        name = name.replace(/SUB\s*\d+\s*[-:]*\s*/i, '').trim();
      }

      result.set(sn, {
        clientName: name || 'Cliente no identificado',
        subCode: subCode || 'N/A'
      });
      snsToQuery.push(sn);
    }

    if (snsToQuery.length === 0) return result;

    try {
      const query = `
        SELECT 
          UPPER(COALESCE(t.serial_onu, t.codigo_onu)) as sn,
          part.name as partner_name,
          s.code as contrato
        FROM project_task t
        JOIN res_partner part ON part.id = t.partner_id
        LEFT JOIN sale_subscription s ON s.partner_id = part.id
        WHERE (
          UPPER(t.serial_onu) = ANY($1) 
          OR UPPER(t.codigo_onu) = ANY($1)
        )
      `;

      const res = await odooPool.query(query, [snsToQuery]);
      for (const row of res.rows) {
        if (!row.sn) continue;
        const key = row.sn.toUpperCase();
        const existing = result.get(key) || {};

        const finalName = row.partner_name || existing.clientName;
        const finalSub = row.contrato || existing.subCode;

        result.set(key, {
          clientName: finalName || 'Cliente no identificado',
          subCode: finalSub || 'N/A'
        });
      }
    } catch (e) {
      console.error('[Monitor] Error consultando Odoo para detalles de clientes:', e.message);
    }

    return result;
  }

  // Enviar alerta por WhatsApp a números técnicos y a grupos configurados
  async sendWithRetry(recipient, message, maxRetries = 2) {
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        await openwaClient.sendText(recipient, message);
        console.log(`[Monitor] ✅ Alerta entregada a ${recipient} (intento ${attempt})`);
        return true;
      } catch (err) {
        console.warn(`[Monitor] Intento ${attempt}/${maxRetries} falló para ${recipient}: ${err.message}`);
        if (attempt < maxRetries) {
          await new Promise(r => setTimeout(r, 2000));
        }
      }
    }
    console.error(`[Monitor] ❌ Error definitivo enviando alerta a ${recipient} tras ${maxRetries} intentos.`);
    return false;
  }

  async broadcastToTechs(message) {
    const recipients = new Set([...(config.TECH_ALERT_RECIPIENTS || [])]);
    (config.TECH_GROUP_IDS || []).forEach(g => recipients.add(g));
    (this.state.techGroups || []).forEach(g => recipients.add(g));

    if (recipients.size === 0) {
      console.warn('[Monitor] No hay destinatarios ni grupos técnicos configurados.');
      return;
    }

    console.log(`[Monitor] Enviando alerta por WhatsApp en paralelo a ${recipients.size} destinatario(s)... Titular: ${message.split('\n')[0]}`);
    const tasks = Array.from(recipients).map(r => this.sendWithRetry(r, message, 2));
    await Promise.allSettled(tasks);
  }


  // REGLA: Solo considerar averías recientes (máximo maxHours, por defecto 24h)
  isRecentOutage(lastStatusChange, now = Date.now(), maxHours = 24) {
    if (!lastStatusChange) return true;
    try {
      const cleaned = String(lastStatusChange).trim().replace(' ', 'T');
      const changeTs = new Date(cleaned).getTime();
      if (isNaN(changeTs) || changeTs <= 0) return true;
      const ageMs = now - changeTs;
      const maxAgeMs = maxHours * 3600 * 1000;
      return ageMs <= maxAgeMs;
    } catch (e) {
      return true;
    }
  }

  formatDuration(ms) {
    const totalMinutes = Math.floor(ms / (60 * 1000));
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours === 0) {
      if (minutes <= 0) return 'Menos de 1 min';
      return `${minutes} min`;
    }
    return `${hours}h ${minutes}m`;
  }

  // Filtrar clientes suspendidos o de baja en Odoo para no alertar falsos cortes de luz
  async filterSuspendedOnus(onusList) {
    if (!Array.isArray(onusList) || onusList.length === 0) {
      return { activeOnus: [], discardedCount: 0 };
    }

    const sns = [];
    const subs = [];

    onusList.forEach(o => {
      const sn = (o.sn || o.unique_external_id || '').toUpperCase();
      if (sn) sns.push(sn);
      const m = (o.name || '').match(/SUB\s*(\d+)/i);
      if (m) subs.push('SUB' + m[1]);
    });

    try {
      const query = `
        SELECT 
          t.codigo_onu,
          t.serial_onu,
          s.code as contrato,
          s.stage_id,
          st.name as stage_name
        FROM sale_subscription s
        JOIN sale_subscription_stage st ON s.stage_id = st.id
        LEFT JOIN project_task t ON t.partner_id = s.partner_id AND (t.codigo_onu IS NOT NULL OR t.serial_onu IS NOT NULL)
        WHERE (
          (t.codigo_onu = ANY($1) OR t.serial_onu = ANY($1))
          OR s.code = ANY($2)
        )
        AND (s.stage_id IN (5, 6, 11) OR st.name ILIKE '%suspendid%' OR st.name ILIKE '%baja%' OR st.name ILIKE '%closed%')
      `;

      const res = await odooPool.query(query, [sns, subs]);
      const suspendedSns = new Set();
      const suspendedSubs = new Set();

      res.rows.forEach(r => {
        if (r.codigo_onu) suspendedSns.add(r.codigo_onu.toUpperCase());
        if (r.serial_onu) suspendedSns.add(r.serial_onu.toUpperCase());
        if (r.contrato) suspendedSubs.add(r.contrato.toUpperCase());
      });

      let discardedCount = 0;
      const activeOnus = onusList.filter(o => {
        const sn = (o.sn || o.unique_external_id || '').toUpperCase();
        const m = (o.name || '').match(/SUB\s*(\d+)/i);
        const sub = m ? ('SUB' + m[1]).toUpperCase() : null;

        const isSuspended = suspendedSns.has(sn) || (sub && suspendedSubs.has(sub));
        if (isSuspended) {
          discardedCount++;
          return false;
        }
        return true;
      });

      return { activeOnus, discardedCount };
    } catch (e) {
      console.error('[Monitor] Error al consultar Odoo para filtrar suspendidos:', e.message);
      return { activeOnus: onusList, discardedCount: 0 };
    }
  }

  // Ciclo principal de monitoreo en tiempo real
  async check() {
    try {
      if (this.zones.size === 0) {
        await this.loadCatalogs();
      }

      const res = await this.fetchSmartOlt('/api/onu/get_onus_statuses');
      if (!res || !Array.isArray(res.response)) {
        console.warn('[Monitor] No se recibió lista de estados de SmartOLT.');
        return;
      }

      const onusList = res.response;
      const now = Date.now();
      const isFirstRun = !this.state.initialized;

      // Listas de eventos detectados en ESTA pasada
      const newLosList = [];          // ONUs que pasaron a LOS en este ciclo
      const newPfList = [];           // ONUs que pasaron a Power Fail en este ciclo
      const recoveredLosList = [];    // ONUs recuperadas (de LOS a Online)
      const followUpLosList = [];     // ONUs con avería persistente en LOS (> 2h)

      // Contadores globales
      let countOnline = 0;
      let countLos = 0;
      let countPf = 0;
      let countOff = 0;

      for (const onu of onusList) {
        const sn = (onu.sn || onu.unique_external_id || '').toUpperCase();
        if (!sn) continue;

        const status = onu.status || 'Offline';
        if (status === 'Online') countOnline++;
        else if (status === 'LOS') countLos++;
        else if (status === 'Power fail') countPf++;
        else countOff++;

        let tracked = this.state.onus[sn];

        if (!tracked) {
          // Primera vez que se detecta esta ONU
          tracked = {
            sn,
            name: onu.name,
            zoneId: onu.zone_id,
            oltId: onu.olt_id,
            board: onu.board || 1,
            port: onu.port || 1,
            onuNum: onu.onu || 1,
            status,
            lastStatusChange: onu.last_status_change,
            firstSeenAt: now,
            lastNotifiedAt: isFirstRun ? now : 0,
            lastFollowUpAt: isFirstRun ? now : 0,
            followUpCount: 0
          };
          this.state.onus[sn] = tracked;

          // Si es el arranque inicial, no alertamos histórico
          if (isFirstRun) {
            continue;
          }
        }

        const prevStatus = tracked.status;

        // 1. Detectar transición a LOS (Corte óptico)
        if (status === 'LOS' && prevStatus !== 'LOS' && !isFirstRun) {
          tracked.status = 'LOS';
          tracked.lastStatusChange = onu.last_status_change || new Date().toISOString();
          tracked.lastNotifiedAt = now;
          tracked.lastFollowUpAt = now;
          tracked.followUpCount = 0;
          try {
            learningService.logDrop({
              sn,
              name: onu.name,
              zone: this.zones.get(String(onu.zone_id)) || 'Desconocida',
              oltId: onu.olt_id,
              board: onu.board,
              port: onu.port,
              dropType: 'los'
            });
          } catch (e) {}
          // REGLA: Solo alertar caídas NUEVAS recientes (<= 24 horas)
          if (this.isRecentOutage(onu.last_status_change, now, 24)) {
            newLosList.push(onu);
          } else {
            console.log(`[Monitor] Omitida alerta LOS para ${sn} (${onu.name}): avería antigua de hace más de 24h (${onu.last_status_change}).`);
          }
        }

        // 2. Detectar transición a Power Fail (Corte de energía / Dying Gasp)
        else if (status === 'Power fail' && prevStatus !== 'Power fail' && !isFirstRun) {
          tracked.status = 'Power fail';
          tracked.lastStatusChange = onu.last_status_change || new Date().toISOString();
          tracked.lastNotifiedAt = now;
          try {
            learningService.logDrop({
              sn,
              name: onu.name,
              zone: this.zones.get(String(onu.zone_id)) || 'Desconocida',
              oltId: onu.olt_id,
              board: onu.board,
              port: onu.port,
              dropType: 'power_fail'
            });
          } catch (e) {}
          // REGLA: Solo alertar caídas NUEVAS recientes (<= 24 horas)
          if (this.isRecentOutage(onu.last_status_change, now, 24)) {
            newPfList.push(onu);
          } else {
            console.log(`[Monitor] Omitida alerta Power Fail para ${sn} (${onu.name}): avería antigua de hace más de 24h (${onu.last_status_change}).`);
          }
        }

        // 3. Seguimiento a avería persistente en LOS: DESACTIVADO (No spamear averías viejas)

        // 4. Recuperación: de LOS o Power Fail a Online
        else if (status === 'Online' && (prevStatus === 'LOS' || prevStatus === 'Power fail') && !isFirstRun) {
          const durationMin = tracked.lastStatusChange ? Math.round((now - new Date(tracked.lastStatusChange).getTime()) / 60000) : 0;
          const wasAlerted = tracked.notifiedAlert === true;
          const isRecent = this.isRecentOutage(tracked.lastStatusChange, now, 24);
          tracked.status = 'Online';
          tracked.lastStatusChange = onu.last_status_change || new Date().toISOString();
          tracked.notifiedAlert = false;
          if (wasAlerted && isRecent) {
            recoveredLosList.push({ onu, tracked, prevStatus });
          }
          try {
            learningService.logRecovery({
              sn,
              name: onu.name,
              zone: this.zones.get(String(onu.zone_id)) || 'Desconocida',
              oltId: onu.olt_id,
              board: onu.board,
              port: onu.port,
              prevStatus,
              durationMinutes: durationMin
            });
          } catch (e) {}
        }

        // Actualizar datos base
        tracked.status = status;
        tracked.name = onu.name;
        tracked.zoneId = onu.zone_id;
        tracked.board = onu.board || tracked.board || 1;
        tracked.port = onu.port || tracked.port || 1;
        tracked.onuNum = onu.onu || tracked.onuNum || 1;
      }

      // =========================================================================
      // DISPACHO DE ALERTAS (SOLO SI NO ES EL ARRANQUE INICIAL)
      // =========================================================================
      if (!isFirstRun) {

        // -----------------------------------------------------------------------
        // A) ALERTAS LOS: Individual o Simultáneas (Filtrando suspendidos en Odoo)
        // -----------------------------------------------------------------------
        const { activeOnus: activeNewLos } = await this.filterSuspendedOnus(newLosList);
        if (activeNewLos.length === 1) {
          const onu = activeNewLos[0];
          const snL = (onu.sn || onu.unique_external_id).toUpperCase();
          if (this.state.onus[snL]) this.state.onus[snL].notifiedAlert = true;
          const custDetails = await this.getCustomerDetailsBatch([onu]);
          const card = this.formatOnuCard(onu, custDetails.get(snL));

          const alertMsg = `🔴 *ALERTA NOC: Pérdida de Señal (LOS)*\n\n` +
            `${card}\n` +
            `• *HORA:* ${onu.last_status_change || 'Hace instantes'}\n\n` +
            `⚠️ *Diagnóstico:* Corte de fibra óptica en línea o conector desconectado.`;

          await this.broadcastToTechs(alertMsg);
        } else if (activeNewLos.length > 1) {
          // Caídas simultáneas en LOS: enviar consolidado con pie de mensaje colectivo
          const custDetails = await this.getCustomerDetailsBatch(activeNewLos);
          const cards = activeNewLos.map((onu, idx) => {
            const card = this.formatOnuCard(onu, custDetails.get((onu.sn || onu.unique_external_id).toUpperCase()));
            return `${idx + 1}️⃣\n${card}`;
          });

          const alertMsg = `🔴 *ALERTA NOC: Pérdidas de Señal Simultáneas (LOS: ${activeNewLos.length})*\n\n` +
            cards.join('\n\n') + `\n\n` +
            `💬 Estamos validando para tomar las acciones necesarias.`;

          activeNewLos.forEach(o => { const sn = (o.sn || o.unique_external_id || '').toUpperCase(); if (this.state.onus[sn]) this.state.onus[sn].notifiedAlert = true; });
          await this.broadcastToTechs(alertMsg);
        }

        // -----------------------------------------------------------------------
        // B) ALERTAS POWER FAIL: Múltiples clientes simultáneos (sin luz)
        //    Regla: Cualquier cantidad siempre que sean varias simultáneas (>= 2).
        //    Excluyendo clientes suspendidos o de baja en Odoo.
        // -----------------------------------------------------------------------
        if (newPfList.length > 0) {
          const { activeOnus: activeNewPf, discardedCount } = await this.filterSuspendedOnus(newPfList);
          console.log(`[Monitor] Nuevas caídas Power Fail en este ciclo: ${newPfList.length} detectadas -> ${activeNewPf.length} activas (${discardedCount} suspendidas en Odoo).`);

          if (activeNewPf.length >= 2) {
            const custDetails = await this.getCustomerDetailsBatch(activeNewPf);
            const cards = activeNewPf.map((onu, idx) => {
              const card = this.formatOnuCard(onu, custDetails.get((onu.sn || onu.unique_external_id).toUpperCase()));
              return `${idx + 1}️⃣\n${card}`;
            });

            const alertMsg = `⚡ *ALERTA NOC: Clientes en Power Fail (Sin Energía: ${activeNewPf.length})*\n\n` +
              cards.join('\n\n') + `\n\n` +
              `💬 Estamos validando para tomar las acciones necesarias.`;

            activeNewPf.forEach(o => { const sn = (o.sn || o.unique_external_id || '').toUpperCase(); if (this.state.onus[sn]) this.state.onus[sn].notifiedAlert = true; });
            await this.broadcastToTechs(alertMsg);
          }
        }

        // -----------------------------------------------------------------------
        // C) SERVICIOS RECUPERADOS (Por lote en mensaje consolidado si son varios)
        // -----------------------------------------------------------------------
        if (recoveredLosList.length === 1) {
          const { onu, tracked } = recoveredLosList[0];
          const custDetails = await this.getCustomerDetailsBatch([onu]);
          const cust = custDetails.get((onu.sn || onu.unique_external_id).toUpperCase());
          const subCode = (cust && cust.subCode) ? cust.subCode : 'N/A';
          const clientName = (cust && cust.clientName) ? cust.clientName : (onu.name || 'Cliente no identificado');
          const totalDownMs = now - (tracked.lastNotifiedAt || now);
          const durationText = this.formatDuration(totalDownMs);

          const recoveryMsg = `🟢 *RECUPERADO: Servicio Normalizado*\n\n` +
            `• *SUB:* ${subCode}\n` +
            `• *CLIENTE:* ${clientName}\n` +
            `• *TIEMPO FUERA:* ${durationText}\n\n` +
            `✅ Módem sincronizado en línea nuevamente con la OLT.`;

          await this.broadcastToTechs(recoveryMsg);
        } else if (recoveredLosList.length > 1) {
          // Lote consolidado en un solo mensaje general
          const custDetails = await this.getCustomerDetailsBatch(recoveredLosList.map(r => r.onu));
          const itemsText = recoveredLosList.map((item, idx) => {
            const { onu, tracked } = item;
            const cust = custDetails.get((onu.sn || onu.unique_external_id).toUpperCase());
            const subCode = (cust && cust.subCode) ? cust.subCode : 'N/A';
            const clientName = (cust && cust.clientName) ? cust.clientName : (onu.name || 'Cliente no identificado');
            const totalDownMs = now - (tracked.lastNotifiedAt || now);
            const durationText = this.formatDuration(totalDownMs);

            return `${idx + 1}️⃣\n` +
                   `• *SUB:* ${subCode}\n` +
                   `• *CLIENTE:* ${clientName}\n` +
                   `• *TIEMPO FUERA:* ${durationText}`;
          }).join('\n\n');

          const batchRecoveryMsg = `🟢 *RECUPERADO: Servicios Normalizados (${recoveredLosList.length})*\n\n` +
            `${itemsText}\n\n` +
            `✅ Módems sincronizados en línea nuevamente con la OLT.`;

          await this.broadcastToTechs(batchRecoveryMsg);
        }
        // -----------------------------------------------------------------------
        // D) SEGUIMIENTO AVERÍA PERSISTENTE EN LOS: DESACTIVADO
        //    (Solo se avisan caídas nuevas <= 24h, sin recordatorios repetitivos)
        // -----------------------------------------------------------------------
      }

      // Marcar inicialización completa
      if (isFirstRun) {
        this.state.initialized = true;
        console.log(`[Monitor] Inicialización de línea base completa: Online: ${countOnline}, LOS: ${countLos}, Power Fail: ${countPf}, Offline: ${countOff}.`);
      }

      this.saveState();

    } catch (err) {
      console.error('[Monitor] Error en ciclo de verificación:', err.message);
    }
  }

  // Generar resumen rápido para respuesta técnica por WhatsApp
  async getQuickSummary() {
    let list = Object.values(this.state.onus);
    if (list.length === 0) {
      const res = await this.fetchSmartOlt('/api/onu/get_onus_statuses');
      if (res && Array.isArray(res.response)) list = res.response;
    }

    if (list.length === 0) {
      return 'No se pudo obtener el estado de la red desde SmartOLT en este momento.';
    }

    let online = 0, los = 0, powerFail = 0, offline = 0;
    const pfList = [];

    list.forEach(o => {
      const s = o.status || 'Offline';
      if (s === 'Online') online++;
      else if (s === 'LOS') los++;
      else if (s === 'Power fail') {
        powerFail++;
        pfList.push(o);
      } else offline++;
    });

    const { activeOnus, discardedCount } = await this.filterSuspendedOnus(pfList);

    // Agrupar activas sin energía por zona
    const pfByZone = {};
    activeOnus.forEach(o => {
      const z = String(o.zoneId || o.zone_id || 'unknown');
      pfByZone[z] = (pfByZone[z] || 0) + 1;
    });

    const activeOutagesText = Object.entries(pfByZone)
      .filter(([_, count]) => count >= 2)
      .map(([zId, count]) => `• ${this.getZoneName(zId)}: ${count} ONUs activas sin luz`)
      .join('\n');

    return `📊 *ESTADO ACTUAL DE RED (SmartOLT)*\n\n` +
      `🟢 *Online:* ${online} ONUs (${((online / list.length) * 100).toFixed(1)}%)\n` +
      `🔴 *Corte Óptico (LOS):* ${los} ONUs\n` +
      `⚡ *Power Fail (Sin luz):* ${activeOnus.length} ONUs activas (${discardedCount} suspendidos en Odoo descartados)\n` +
      `⚪ *Offline:* ${offline} ONUs\n` +
      `📡 *Total Configurado:* ${list.length} ONUs\n\n` +
      (activeOutagesText ? `⚡ *Sectores con concentración de fallas eléctricas activas:*\n${activeOutagesText}\n\n` : '') +
      `_Escribe "los" para ver la lista de cortes de fibra o "luz" para ver sectores sin energía._`;
  }

  // Generar reporte detallado de cortes eléctricos (filtrando suspendidos en Odoo)
  async getActiveOutagesReport() {
    let list = Object.values(this.state.onus);
    if (list.length === 0) {
      const res = await this.fetchSmartOlt('/api/onu/get_onus_statuses');
      if (res && Array.isArray(res.response)) list = res.response;
    }

    const pfList = list.filter(o => o.status === 'Power fail');
    const { activeOnus, discardedCount } = await this.filterSuspendedOnus(pfList);

    if (activeOnus.length === 0) {
      return `🔌 *Estado Eléctrico:* No se detectan clientes activos en Power Fail en ninguna zona.\n\n` +
        `📊 *Total Power Fail en SmartOLT:* ${pfList.length} ONUs\n` +
        `• *Clientes suspendidos/de baja:* ${discardedCount} excluidos en Odoo`;
    }

    const custDetails = await this.getCustomerDetailsBatch(activeOnus.slice(0, 10));
    const cards = activeOnus.slice(0, 10).map((onu, idx) => {
      const card = this.formatOnuCard(onu, custDetails.get((onu.sn || onu.unique_external_id).toUpperCase()));
      return `${idx + 1}️⃣\n${card}`;
    });

    return `⚡ *CLIENTES ACTIVOS EN POWER FAIL (Sin Energía: ${activeOnus.length})*\n\n` +
      cards.join('\n\n') +
      (activeOnus.length > 10 ? `\n\n_... y ${activeOnus.length - 10} clientes más sin energía._` : '') +
      `\n\nℹ️ (${discardedCount} clientes suspendidos en Odoo fueron excluidos).`;
  }

  // Listar ONUs en corte óptico (LOS) con formato estandarizado
  async getLosList() {
    let list = Object.values(this.state.onus);
    if (list.length === 0) {
      const res = await this.fetchSmartOlt('/api/onu/get_onus_statuses');
      if (res && Array.isArray(res.response)) list = res.response;
    }

    const losList = list.filter(o => o.status === 'LOS');
    if (losList.length === 0) {
      return '🟢 *Todo despejado:* No hay ninguna ONU en estado LOS (corte óptico) en la red en este momento.';
    }

    const custDetails = await this.getCustomerDetailsBatch(losList.slice(0, 10));
    const cards = losList.slice(0, 10).map((onu, idx) => {
      const card = this.formatOnuCard(onu, custDetails.get((onu.sn || onu.unique_external_id).toUpperCase()));
      return `${idx + 1}️⃣\n${card}\n• *HORA:* ${onu.lastStatusChange || onu.last_status_change || 'N/A'}`;
    });

    return `🔴 *ONUs CON PÉRDIDA DE SEÑAL (LOS: ${losList.length})*\n\n` +
      cards.join('\n\n') +
      (losList.length > 10 ? `\n\n_... y ${losList.length - 10} más en SmartOLT._` : '');
  }

  // Iniciar el temporizador en segundo plano
  start() {
    if (this.timer) return;
    const interval = config.MONITOR.CHECK_INTERVAL_MS || 60000;
    console.log(`[Monitor] Iniciando motor de monitoreo SmartOLT (Intervalo: ${interval / 1000}s)...`);

    // Primera verificación tras 3 segundos de arranque
    setTimeout(() => this.check(), 3000);

    // Ciclo recurrente cada 60s
    this.timer = setInterval(() => this.check(), interval);
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
      console.log('[Monitor] Motor de monitoreo detenido.');
    }
  }
}

module.exports = new SmartOltMonitor();
