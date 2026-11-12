/**
 * bequantReportService.js
 * Servicio para consultar Bequant BQN en tiempo real, filtrar abonados en sobreconsumo
 * (FLOWS >= 500), extraer telemetría histórica mensual (o 24h), generar PDF y enviarlo por WhatsApp.
 */

const WebSocket = require('ws');
const { exec } = require('child_process');
const fs = require('fs');
const path = require('path');
const config = require('./config');
const openwaClient = require('./openwaClient');

const BQN_CONFIG = config.BEQUANT || {
  HOST: process.env.BQN_HOST || '10.0.20.10',
  PORT: process.env.BQN_PORT || 443,
  USER: process.env.BQN_USER || 'bqnop',
  PASS: process.env.BQN_PASS || 'YOUR_BQN_PASSWORD',
  TIMEOUT_MS: 15000
};

function filterStatsByMonth(rawStats, yearMonth) {
  if (!rawStats || typeof rawStats !== 'string') return '';
  const lines = rawStats.trim().split('\n');
  if (lines.length < 2) return '';
  const headerLines = [];
  const dataLines = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith('TIME') || trimmed.startsWith('---') || trimmed.startsWith('%')) {
      headerLines.push(trimmed);
      continue;
    }
    const parts = trimmed.split(/\s+/);
    if (parts.length >= 2 && parts[0].startsWith(yearMonth)) {
      dataLines.push(trimmed);
    }
  }
  return [...headerLines, ...dataLines].join('\n');
}

class BequantReportService {
  constructor() {
    this.isGenerating = false;
  }

  /**
   * Ejecuta comandos en Bequant de forma secuencial sobre una conexión WebSocket activa
   */
  async runCmd(ws, reqId, cmd, timeoutMs = 8000) {
    return new Promise((resolve, reject) => {
      const curId = reqId.val++;
      let timer = null;

      function onMsg(data) {
        try {
          const m = JSON.parse(data.toString());
          if (m.reqId === curId && m.srv && m.srv.CmdResponse) {
            clearTimeout(timer);
            ws.removeListener('message', onMsg);
            resolve(m.srv.CmdResponse.response || '');
          }
        } catch (e) {
          clearTimeout(timer);
          ws.removeListener('message', onMsg);
          reject(e);
        }
      }

      timer = setTimeout(() => {
        ws.removeListener('message', onMsg);
        resolve(''); // Resuelve vacío en timeout para no tumbar el reporte
      }, timeoutMs);

      ws.on('message', onMsg);
      ws.send(JSON.stringify({
        reqId: curId,
        srv: { CmdRequest: { cmd } }
      }));
    });
  }

  /**
   * Conecta y autentica contra Bequant BQN
   */
  async connectBqn(reqId) {
    return new Promise((resolve, reject) => {
      const wsUrl = `wss://${BQN_CONFIG.HOST}:${BQN_CONFIG.PORT}/bqn`;
      const ws = new WebSocket(wsUrl, {
        rejectUnauthorized: false,
        handshakeTimeout: 5000
      });

      const connectTimer = setTimeout(() => {
        try { ws.terminate(); } catch (e) {}
        reject(new Error('Timeout de conexión inicial con Bequant'));
      }, 7000);

      ws.on('open', () => {
        ws.send(JSON.stringify({
          reqId: reqId.val++,
          srv: { ConnectRequest: { token: null } }
        }));
      });

      ws.on('message', function onLogin(data) {
        try {
          const m = JSON.parse(data.toString());
          if (m.srv && m.srv.ConnectResponse) {
            ws.send(JSON.stringify({
              reqId: reqId.val++,
              srv: {
                LoginRequest: {
                  user: BQN_CONFIG.USER,
                  passwd: BQN_CONFIG.PASS
                }
              }
            }));
          } else if (m.srv && m.srv.LoginResponse) {
            clearTimeout(connectTimer);
            ws.removeListener('message', onLogin);
            if (m.srv.LoginResponse.code && m.srv.LoginResponse.code !== 0) {
              try { ws.terminate(); } catch (e) {}
              reject(new Error('Credenciales incorrectas en Bequant'));
            } else {
              resolve(ws);
            }
          }
        } catch (err) {
          clearTimeout(connectTimer);
          ws.removeListener('message', onLogin);
          reject(err);
        }
      });

      ws.on('error', (err) => {
        clearTimeout(connectTimer);
        reject(err);
      });
    });
  }

  /**
   * Genera el reporte mensual de sobreconsumo en PDF y lo envía al chat de WhatsApp solicitado
   */
  async generateAndSendMonthlyPdf(chatId, targetMonth, minFlows = 500) {
    if (this.isGenerating) {
      if (openwaClient && chatId) {
        await openwaClient.sendText(chatId, '⏳ Ya se está generando un reporte de sobreconsumo en este momento. Te lo enviaré en cuanto termine.');
      }
      return;
    }

    this.isGenerating = true;
    console.log(`[BequantReport] Iniciando generación de reporte MENSUAL (${targetMonth.title}) para ${chatId} (Umbral >= ${minFlows} flujos)...`);

    const reqId = { val: 1 };
    let ws = null;

    try {
      ws = await this.connectBqn(reqId);
      console.log('[BequantReport] Conectado y autenticado en Bequant');

      // 1. Obtener listado de todos los abonados
      const subsRaw = await this.runCmd(ws, reqId, 'show subscriber all downlink lines 2500', 12000);
      const lines = subsRaw.trim().split('\n');
      console.log(`[BequantReport] Total líneas recibidas de Bequant: ${lines.length}`);

      const subscribers = [];
      for (let i = 1; i < lines.length; i++) {
        const l = lines[i].trim();
        if (!l || l.startsWith('%')) continue;
        const tokens = l.split(/\s+/);
        if (tokens.length >= 19) {
          const ip = tokens[1];
          const flows = parseInt(tokens[tokens.length - 16], 10) || 0;
          const policyRate = tokens[tokens.length - 4];
          const currMbps = tokens[tokens.length - 12];
          const subscriberId = tokens.slice(2, tokens.length - 17).join(' ');
          subscribers.push({
            ip,
            subscriberId,
            nodo: '',
            policyRate,
            flows,
            currMbps
          });
        }
      }

      // 2. Filtrar abonados en sobreconsumo (flows >= minFlows)
      subscribers.sort((a, b) => b.flows - a.flows);
      const overSubs = subscribers.filter(s => s.flows >= minFlows);
      console.log(`[BequantReport] Abonados con sobreconsumo (>= ${minFlows} flujos): ${overSubs.length} de ${subscribers.length}`);

      if (overSubs.length === 0) {
        if (ws) { try { ws.close(); } catch (e) {} }
        this.isGenerating = false;
        await openwaClient.sendText(chatId, `✅ Actualmente no hay abonados con sobreconsumo (ninguno supera ${minFlows} flujos concurrentes en Bequant BQN).`);
        return;
      }

      // Limitar a los Top 25 más críticos para compilar un reporte detallado y ágil
      const MAX_REPORT_SUBS = 25;
      const targetSubs = overSubs.slice(0, MAX_REPORT_SUBS);

      // 3. Extraer telemetría mensual para cada abonado (horas 2160 = 90 días, intervalo 60 min)
      console.log(`[BequantReport] Extrayendo estadísticas mensuales (${targetMonth.title}) para los Top ${targetSubs.length} abonados...`);
      for (let i = 0; i < targetSubs.length; i++) {
        const s = targetSubs[i];
        try {
          const rawSpeed = await this.runCmd(ws, reqId, `show statistics subscribers max-speed ${s.ip} hours 2160 interval 60`, 4500);
          const rawFlows = await this.runCmd(ws, reqId, `show statistics subscribers flows ${s.ip} hours 2160 interval 60`, 4500);
          s.speedRaw = filterStatsByMonth(rawSpeed, targetMonth.yearMonth);
          s.flowsRaw = filterStatsByMonth(rawFlows, targetMonth.yearMonth);
        } catch (subErr) {
          console.error(`[BequantReport] Error consultando stats de ${s.ip}:`, subErr.message);
          s.speedRaw = '';
          s.flowsRaw = '';
        }
      }

      // Cerrar WebSocket de Bequant ordenadamente
      if (ws) {
        try { ws.close(); } catch (e) {}
        ws = null;
      }

      // 4. Preparar payload JSON
      const now = new Date();
      const dateStr = now.toLocaleDateString('es-VE', { timeZone: 'America/Caracas' });
      const timeStr = now.toLocaleTimeString('es-VE', { timeZone: 'America/Caracas' });
      const timestampFormatted = `${dateStr} ${timeStr}`;

      const reportPayload = {
        timestamp: timestampFormatted,
        isMonthly: true,
        monthTitle: targetMonth.title,
        totalMonitored: subscribers.length,
        overTotalCount: overSubs.length,
        subscribers: targetSubs
      };

      const jsonPath = `/tmp/bequant_monthly_${targetMonth.yearMonth}.json`;
      const pdfFileName = `Reporte_Sobreconsumo_Bequant_${targetMonth.monthName}_${targetMonth.year}.pdf`;
      const pdfPath = path.join('/tmp', pdfFileName);

      fs.writeFileSync(jsonPath, JSON.stringify(reportPayload), 'utf8');
      console.log(`[BequantReport] Datos exportados a ${jsonPath}. Compilando PDF...`);

      // 5. Compilar PDF con Python Matplotlib
      const pyScript = '/opt/multicanal-sofia/generate_bequant_pdf.py';
      await new Promise((resolve, reject) => {
        exec(`python3 ${pyScript} ${jsonPath} ${pdfPath}`, (err, stdout, stderr) => {
          if (err) {
            console.error('[BequantReport] Error python:', stderr);
            return reject(new Error(`Fallo generando PDF: ${stderr || err.message}`));
          }
          console.log('[BequantReport] Python stdout:', stdout.trim());
          resolve();
        });
      });

      // 6. Leer PDF y enviar por WhatsApp
      if (!fs.existsSync(pdfPath)) {
        throw new Error('El archivo PDF no fue generado');
      }

      const pdfBuffer = fs.readFileSync(pdfPath);
      const pdfBase64 = pdfBuffer.toString('base64');
      console.log(`[BequantReport] PDF mensual generado exitosamente (${(pdfBuffer.length / 1024).toFixed(1)} KB). Enviando a ${chatId}...`);

      const captionText = `📊 *Reporte Mensual de Sobreconsumo de Conexiones (Bequant BQN)*\n📅 Mes: *${targetMonth.title}*\nTotal analizados: ${subscribers.length.toLocaleString()} | Con sobreconsumo (≥ ${minFlows} flujos): ${overSubs.length}\n📄 Mostrando los ${targetSubs.length} casos con mayor tráfico y conexiones concurrentes.`;

      await openwaClient.sendDocument(chatId, pdfBase64, pdfFileName, captionText);
      console.log(`[BequantReport] Documento mensual enviado exitosamente a ${chatId}`);

      // Limpieza temporal
      try {
        fs.unlinkSync(jsonPath);
        fs.unlinkSync(pdfPath);
      } catch (e) {}

    } catch (error) {
      console.error('[BequantReport] Excepción general en reporte mensual:', error);
      if (chatId) {
        await openwaClient.sendText(chatId, `⚠️ Error generando el reporte de sobreconsumo: ${error.message}`);
      }
    } finally {
      if (ws) {
        try { ws.terminate(); } catch (e) {}
      }
      this.isGenerating = false;
    }
  }

  /**
   * Genera el reporte general (por defecto el mes en curso)
   */
  async generateAndSendPdf(chatId, minFlows = 500) {
    const now = new Date();
    const months = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'];
    const monthName = months[now.getMonth()];
    const year = now.getFullYear();
    const yearMonth = `${year}-${String(now.getMonth() + 1).padStart(2, '0')}`;
    return this.generateAndSendMonthlyPdf(chatId, {
      monthNum: now.getMonth() + 1,
      monthName,
      year,
      yearMonth,
      title: `${monthName} ${year}`
    }, minFlows);
  }
}

module.exports = new BequantReportService();
