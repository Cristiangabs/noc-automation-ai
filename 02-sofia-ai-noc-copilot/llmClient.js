const https = require('https');
const url = require('url');
const config = require('./config');
const systemPromptCustomer = require('./systemPrompt');
const systemPromptTechCopilot = require('./systemPrompt_tech_copilot');
const sentinelPowerMonitor = require('./sentinelPowerMonitor');
const learningService = require('./learningService');

class LLMClient {
  constructor() {
    this.parsedUrl = new url.URL(config.LLM.BASE_URL);
  }

  async generateReply(history, userMessage, customerContext = null, callerContext = null) {
    const isStaff = Boolean(callerContext?.isStaff || callerContext?.isGroup);
    let activeSystemPrompt = isStaff ? systemPromptTechCopilot : systemPromptCustomer;

    // 1. Inyectar estado en vivo de energía y ONU centinela
    try {
      const s = sentinelPowerMonitor.getStatus();
      const isOnline = s.isOnline;
      const rttStr = s.lastRtt ? `${s.lastRtt} ms` : '3.3 ms';
      const lastCheckStr = s.lastCheck ? new Date(s.lastCheck).toLocaleTimeString('es-VE') : 'reciente';

      activeSystemPrompt += `\n\n--- ESTADO EN TIEMPO REAL: SUMINISTRO ELECTRICO Y ONU CENTINELA ---
• Estado del suministro electrico: ${isOnline ? '🟢 NORMAL / CON ENERGIA (ONU Centinela 10.0.50.151 ONLINE con latencia ' + rttStr + ', ultimo sondeo ' + lastCheckStr + ')' : '🔴 CORTE ELECTRICO ACTIVO (ONU Centinela 10.0.50.151 APAGADA por corte de luz en el sector desde ' + (s.outageStartTime ? new Date(s.outageStartTime).toLocaleTimeString('es-VE') : 'hace unos momentos') + ')'}
• Total de cortes registrados hoy: ${s.totalOutagesDetected}

INSTRUCCIONES PARA PREGUNTAS SOBRE LUZ, ENERGIA O "CENTINELA":
- Si el colega pregunta si se fue la luz, si llego la luz, si hay luz, por cortes electricos o por "centinela":
  * Si el estado es NORMAL: Confirma que la central y sector tienen fluido electrico normal y los equipos estan en linea.
  * Si el estado es CORTE ELECTRICO ACTIVO: Informale que se detecto corte electrico en el sector segun el monitoreo centinela.
- "Centinela" es nuestra ONU de monitoreo electrico de la red (10.0.50.151).`;
    } catch (sentinelErr) {
      console.warn('[Sofía] Advertencia obteniendo estado centinela en LLM:', sentinelErr.message);
    }

    // 2. Inyectar aprendizajes y conocimiento consolidado de la red (Learning Loop)
    try {
      const learnedCtx = learningService.getPromptContext();
      if (learnedCtx) {
        activeSystemPrompt += learnedCtx;
      }
    } catch (learnErr) {
      console.warn('[Sofía] Advertencia obteniendo contexto de aprendizaje en LLM:', learnErr.message);
    }

    // 3. Inyectar datos en vivo del abonado consultado por el técnico
    if (customerContext && customerContext.found) {
      const c = customerContext.client;
      const b = customerContext.billing;
      const t = customerContext.technical;
      activeSystemPrompt += `\n\n--- DATOS TÉCNICOS DEL ABONADO CONSULTADO (ODOO + SMARTOLT) ---
• Abonado: ${c.fullName}
• Cedula: ${c.cedula}
• Contrato: ${c.contrato}
• Telefono: ${c.telefono}
• Zona: ${t.zone} - ${c.direccion}
• Estado ONT (SmartOLT): ${t.hasOnu ? (t.online ? `ONLINE (Potencia optica RX: ${t.rxPower}, Calidad: ${t.signalQuality})` : `OFFLINE (Causa de desconexion: ${t.lastDownReason})`) : 'No se encontro ONU registrada'}
• Facturacion (Odoo): ${b.hasDebt ? `Deuda pendiente: $${b.totalDebt} (${b.unpaidInvoicesCount} factura(s))` : 'AL DIA'}

INSTRUCCIÓN: Presenta el diagnóstico técnico de este abonado al colega de forma concisa, destacando estado óptico, potencia y datos clave.`;
    }

    // 4. Inyectar modo grupo técnico o interlocutor staff si aplica
    if (callerContext?.isGroup) {
      activeSystemPrompt += `\n\n--- MODO GRUPO TÉCNICO MULTICANAL ---
• Estás en un grupo técnico/operativo de la empresa.
• El mensaje proviene del colega autorizado: ${callerContext.authorName || 'Colega'}.
• Tu rol es de COPILOTO TÉCNICO: responde directo al grano, con tono profesional entre compañeros técnicos, sin rodeos ni saludos de atención al cliente.
• CERO ALUCINACIONES: básate exclusivamente en los datos verificados del sistema. Si no tienes datos suficientes o no tienes acceso a algo, dilo con honestidad técnica.`;
    } else if (isStaff) {
      const callerName = callerContext?.authorName || callerContext?.memberName || 'Cristian Salinas (Directivo)';
      activeSystemPrompt += `\n\n--- INTERLOCUTOR IDENTIFICADO (CHAT DIRECTO) ---
• Nombre: ${callerName}
• Rol: Directivo / Equipo Técnico de Multicanal
• Instrucción OBLIGATORIA: Estás hablando en chat privado con un DIRECTIVO o técnico de Multicanal.
• PROHIBICIONES ESTRICTAS:
  - NUNCA lo saludes con "Bienvenida a Multicanal", "Hola! Bienvenida...", ni discursos de atención al cliente residencial.
  - NUNCA le preguntes "¿Qué sucede con tu servicio?", "¿Tienes problemas con tu servicio?", "¿Quieres consultar tu factura?", ni des soporte para su hogar.
  - NUNCA asumas que quien te habla tiene problemas con su internet personal.
• ACTITUD Y TONO:
  - Responde como su copiloto técnica de inteligencia artificial para operaciones, NOC y redes.
  - Saluda de forma natural y ejecutiva (ej: "Hola Cristian! En qué te apoyo hoy?", "Dime Cristian, qué revisamos?", "A la orden Cristian").
  - Si pregunta por la red, cortes, abonados, sobreconsumo o cualquier tema técnico, responde con criterio de ingeniería, concisa y sin rodeos.`;
    }

    const messages = [
      { role: 'system', content: activeSystemPrompt },
      ...history,
      { role: 'user', content: userMessage }
    ];

    const payload = JSON.stringify({
      model: config.LLM.MODEL,
      messages: messages,
      temperature: config.LLM.TEMPERATURE,
      max_tokens: config.LLM.MAX_TOKENS
    });

    return new Promise((resolve, reject) => {
      const options = {
        hostname: this.parsedUrl.hostname,
        port: this.parsedUrl.port || 443,
        path: `${this.parsedUrl.pathname.replace(/\/$/, '')}/chat/completions`,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': config.LLM.API_KEY,
          'Content-Length': Buffer.byteLength(payload)
        },
        rejectUnauthorized: false,
        timeout: 20000
      };

      const req = https.request(options, (res) => {
        let data = '';
        res.on('data', (chunk) => { data += chunk; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              const parsed = JSON.parse(data);
              let reply = parsed.choices?.[0]?.message?.content?.trim();
              if (reply) {
                // 1. Eliminar signos de apertura ¡ y ¿
                reply = reply.replace(/[¡¿]/g, '');

                // 2. Corregir voseo extranjero a tuteo venezolano
                reply = reply.replace(/\b([Ss])os\b/g, '$1eres')
                             .replace(/\b([Pp])od[eé]s\b/g, '$1uedes')
                             .replace(/\b([Tt])en[eé]s\b/g, '$1ienes')
                             .replace(/\b([Ss])eg[uú][eéí]s\b/g, '$1igues')
                             .replace(/\b([Hh])ac[eé]s\b/g, '$1aces')
                             .replace(/\b([Ss])ab[eé]s\b/g, '$1abes')
                             .replace(/\b([Vv])en[eéí]s\b/g, '$1ienes');

                // 3. Sanitizar residuo de bienvenida residencial si el interlocutor es staff/técnico
                if (isStaff) {
                  reply = reply.replace(/^(Hola!?\s*)?Bienvenid[oa]\s+a\s+Multicanal[^.!?\n]*[.!?\n]*/gi, '')
                               .replace(/¿?En qué puedo ayudarte hoy con tu servicio\??/gi, '¿En qué te apoyo hoy?')
                               .trim();
                }

                // 4. Si no es el primer mensaje de la conversación (history.length > 0),
                // eliminar saludos redundantes iniciales tipo "Hola Noris! 👋" o "Hola!"
                if (history && history.length > 0) {
                  reply = reply.replace(/^(Hola|Buen día|Buenas tardes|Buenas noches)(\s+[A-ZÁÉÍÓÚÑa-z]+)?\s*(!|\.)?\s*(👋|😊)?\s*/i, '');
                }

                // Asegurar que comience con mayúscula
                if (reply.length > 0) {
                  reply = reply.charAt(0).toUpperCase() + reply.slice(1);
                }

                resolve(reply);
              } else {
                reject(new Error('Respuesta LLM vacía o estructura inesperada'));
              }
            } catch (err) {
              reject(new Error(`Error al parsear JSON de LLM: ${err.message}`));
            }
          } else {
            reject(new Error(`LLM Error HTTP ${res.statusCode}: ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout de 20s esperando respuesta del LLM local'));
      });

      req.on('error', (err) => {
        reject(err);
      });

      req.write(payload);
      req.end();
    });
  }

  /**
   * Generación de texto directo para tareas de fondo / análisis y reflexión
   */
  async generateText(promptText, customSystemPrompt = 'Eres un analista de ingeniería de telecomunicaciones y redes.') {
    const messages = [
      { role: 'system', content: customSystemPrompt },
      { role: 'user', content: promptText }
    ];

    const payload = JSON.stringify({
      model: config.LLM.MODEL,
      messages: messages,
      temperature: 0.2,
      max_tokens: 600
    });

    return new Promise((resolve, reject) => {
      const options = {
        hostname: this.parsedUrl.hostname,
        port: this.parsedUrl.port || 443,
        path: `${this.parsedUrl.pathname.replace(/\/$/, '')}/chat/completions`,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': config.LLM.API_KEY,
          'Content-Length': Buffer.byteLength(payload)
        },
        rejectUnauthorized: false,
        timeout: 25000
      };

      const req = https.request(options, (res) => {
        let data = '';
        res.on('data', (chunk) => { data += chunk; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              const parsed = JSON.parse(data);
              resolve(parsed.choices?.[0]?.message?.content?.trim() || '');
            } catch (err) {
              reject(new Error(`Error parseando JSON: ${err.message}`));
            }
          } else {
            reject(new Error(`LLM Error HTTP ${res.statusCode}: ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout en generateText'));
      });

      req.on('error', (err) => {
        reject(err);
      });

      req.write(payload);
      req.end();
    });
  }
}

module.exports = new LLMClient();
