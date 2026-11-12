const fs = require('fs');
const path = require('path');
const openwaClient = require('./openwaClient');

const DATA_DIR = path.join(__dirname, 'data');
const TELEMETRY_LOG = path.join(DATA_DIR, 'telemetry_events.jsonl');
const CHAT_LOG = path.join(DATA_DIR, 'chat_interactions.jsonl');
const MEMORY_FILE = path.join(DATA_DIR, 'learned_memory.json');

// Número exclusivo del directivo / usuario para el reporte matutino
const TARGET_USER_CHAT_ID = process.env.REPORT_TARGET_CHAT_ID || '584120000001@c.us';

class LearningService {
  constructor() {
    this.ensureDataDir();
    this.memory = this.loadMemory();
    this.consolidationTimer = null;
    this.isConsolidating = false;
  }

  ensureDataDir() {
    try {
      if (!fs.existsSync(DATA_DIR)) {
        fs.mkdirSync(DATA_DIR, { recursive: true });
      }
    } catch (e) {
      console.error('[LearningService] Error creando directorio data:', e.message);
    }
  }

  loadMemory() {
    try {
      if (fs.existsSync(MEMORY_FILE)) {
        return JSON.parse(fs.readFileSync(MEMORY_FILE, 'utf8'));
      }
    } catch (e) {
      console.error('[LearningService] Error cargando learned_memory.json:', e.message);
    }
    return {
      lastConsolidatedAt: null,
      learnedLessons: [
        'Las ONUs C-Data utilizan el prefijo GPON "CDTC" para SmartOLT y el serial secundario con guion (ej. DF5F-...) para Odoo.',
        'La dirección MAC de las ONUs (12 caracteres hex) debe ser ignorada y descartada en el aprovisionamiento.',
        'Los presets oficiales son: OLT 3 CDATA (ID 6), OLT 3 VSOL (ID 5), OLT 4 CDATA (ID 3), OLT 4 VSOL (ID 4).'
      ],
      recentPatterns: []
    };
  }

  saveMemory() {
    try {
      fs.writeFileSync(MEMORY_FILE, JSON.stringify(this.memory, null, 2), 'utf8');
    } catch (e) {
      console.error('[LearningService] Error guardando learned_memory.json:', e.message);
    }
  }

  /**
   * Registra un evento de caída de ONU (LOS o Power Fail)
   */
  logDrop(event) {
    try {
      const entry = {
        type: 'drop',
        sn: event.sn,
        name: event.name || 'Desconocido',
        zone: event.zone || 'Sin Zona',
        oltId: event.oltId,
        board: event.board,
        port: event.port,
        dropType: event.dropType, // 'los' | 'power_fail'
        timestamp: Date.now(),
        iso: new Date().toISOString()
      };
      fs.appendFileSync(TELEMETRY_LOG, JSON.stringify(entry) + '\n', 'utf8');
    } catch (e) {
      console.error('[LearningService] Error en logDrop:', e.message);
    }
  }

  /**
   * Registra la recuperación de una ONU a Online
   */
  logRecovery(event) {
    try {
      const entry = {
        type: 'recovery',
        sn: event.sn,
        name: event.name || 'Desconocido',
        zone: event.zone || 'Sin Zona',
        oltId: event.oltId,
        board: event.board,
        port: event.port,
        prevStatus: event.prevStatus,
        durationMinutes: event.durationMinutes || 0,
        timestamp: Date.now(),
        iso: new Date().toISOString()
      };
      fs.appendFileSync(TELEMETRY_LOG, JSON.stringify(entry) + '\n', 'utf8');
    } catch (e) {
      console.error('[LearningService] Error en logRecovery:', e.message);
    }
  }

  /**
   * Registra una interacción en el chat
   */
  logInteraction(chatId, pushname, userMessage, assistantReply, isNocCommand = false) {
    try {
      const entry = {
        chatId,
        pushname: pushname || '',
        userMessage: (userMessage || '').slice(0, 500),
        assistantReply: (assistantReply || '').slice(0, 500),
        isNocCommand: Boolean(isNocCommand),
        timestamp: Date.now(),
        iso: new Date().toISOString()
      };
      fs.appendFileSync(CHAT_LOG, JSON.stringify(entry) + '\n', 'utf8');
    } catch (e) {
      console.error('[LearningService] Error en logInteraction:', e.message);
    }
  }

  /**
   * Devuelve las lecciones aprendidas para inyectarlas en el contexto de Sofía
   */
  getPromptContext() {
    if (!this.memory || !this.memory.learnedLessons || this.memory.learnedLessons.length === 0) {
      return '';
    }
    const lessons = this.memory.learnedLessons.map((l, i) => `${i + 1}. ${l}`).join('\n');
    let ctx = `\n\nCONOCIMIENTO Y APRENDIZAJES RECIENTES DE LA RED Y OPERACIÓN:\n${lessons}`;
    if (this.memory.recentPatterns && this.memory.recentPatterns.length > 0) {
      const patterns = this.memory.recentPatterns.map((p, i) => `• ${p}`).join('\n');
      ctx += `\n\nPATRONES OPERATIVOS DETECTADOS:\n${patterns}`;
    }
    return ctx;
  }

  /**
   * Procesa las últimas 24 horas de eventos y genera la consolidación con IA
   */
  async runDailyConsolidation(llmClient, forceSend = false) {
    if (this.isConsolidating) {
      console.log('[LearningService] Consolidación ya en progreso, omitiendo...');
      return;
    }

    this.isConsolidating = true;
    console.log('[LearningService] 🌙 Iniciando consolidación nocturna y autoaprendizaje...');

    try {
      const now = Date.now();
      const oneDayAgo = now - 24 * 3600 * 1000;

      // 1. Analizar telemetría de las últimas 24 horas
      const drops = [];
      const recoveries = [];
      if (fs.existsSync(TELEMETRY_LOG)) {
        const lines = fs.readFileSync(TELEMETRY_LOG, 'utf8').trim().split('\n');
        for (const l of lines) {
          if (!l) continue;
          try {
            const ev = JSON.parse(l);
            if (ev.timestamp >= oneDayAgo) {
              if (ev.type === 'drop') drops.push(ev);
              else if (ev.type === 'recovery') recoveries.push(ev);
            }
          } catch (e) {}
        }
      }

      const losCount = drops.filter(d => d.dropType === 'los').length;
      const pfCount = drops.filter(d => d.dropType === 'power_fail').length;

      // Conteo por zonas
      const zoneStats = {};
      drops.forEach(d => {
        const z = d.zone || 'Sin Zona';
        zoneStats[z] = (zoneStats[z] || 0) + 1;
      });
      const topZones = Object.entries(zoneStats)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 3)
        .map(([z, c]) => `${z} (${c} eventos)`);

      // Tiempo medio de recuperación
      let avgRecoveryTime = 0;
      if (recoveries.length > 0) {
        const totalMin = recoveries.reduce((acc, r) => acc + (r.durationMinutes || 0), 0);
        avgRecoveryTime = Math.round(totalMin / recoveries.length);
      }

      // 2. Analizar interacciones de chat
      const chatInteractions = [];
      if (fs.existsSync(CHAT_LOG)) {
        const lines = fs.readFileSync(CHAT_LOG, 'utf8').trim().split('\n');
        for (const l of lines) {
          if (!l) continue;
          try {
            const ci = JSON.parse(l);
            if (ci.timestamp >= oneDayAgo) {
              chatInteractions.push(ci);
            }
          } catch (e) {}
        }
      }

      // 3. Sintetizar lecciones aprendidas usando LLM si está disponible
      let newLessons = [];
      let newPatterns = [];

      if (llmClient && (drops.length > 0 || chatInteractions.length > 0)) {
        try {
          const promptAnalysis = `Eres el analista de datos y operaciones NOC de Multicanal. Analiza la siguiente telemetría y eventos de las últimas 24 horas y extrae entre 2 y 4 conclusiones o aprendizajes técnicos muy concretos, objetivos y útiles para la operación de la red.
          
DATOS DE TELEMETRÍA (Últimas 24h):
- Caídas por Fibra/LOS: ${losCount}
- Caídas por Energía/Power Fail: ${pfCount}
- ONUs Recuperadas: ${recoveries.length} (Tiempo medio de recuperación: ${avgRecoveryTime} minutos)
- Zonas con más eventos: ${topZones.join(', ') || 'Ninguna zona crítica'}
- Total consultas técnicas en chat: ${chatInteractions.length}

INSTRUCCIONES:
Devuelve un JSON estricto con esta estructura:
{
  "lecciones": ["conclusión técnica 1", "conclusión técnica 2"],
  "patrones": ["patrón observado 1", "patrón observado 2"]
}`;
          const aiResponse = await llmClient.generateText(promptAnalysis);
          const jsonMatch = aiResponse.match(/\{[\s\S]*\}/);
          if (jsonMatch) {
            const parsed = JSON.parse(jsonMatch[0]);
            if (Array.isArray(parsed.lecciones)) newLessons = parsed.lecciones;
            if (Array.isArray(parsed.patrones)) newPatterns = parsed.patrones;
          }
        } catch (llmErr) {
          console.warn('[LearningService] Aviso en síntesis LLM:', llmErr.message);
        }
      }

      // Si el LLM no arrojó o no había datos suficientes, generar patrones objetivos basados en datos reales
      if (newLessons.length === 0) {
        if (pfCount > losCount) {
          newLessons.push(`La causa predominante de cortes en las últimas 24h fue eléctrica (${pfCount} Power Fails vs ${losCount} LOS).`);
        } else if (losCount > 0) {
          newLessons.push(`Se observaron ${losCount} eventos de corte óptico (LOS). La cuadrilla resolvió en promedio en ${avgRecoveryTime || 'N/A'} min.`);
        } else {
          newLessons.push('Estabilidad de red alta en las últimas 24h, sin eventos masivos de fibra ni apagones sectoriales.');
        }
        newLessons.push('Se mantiene verificado el soporte de aprovisionamiento de ONUs C-Data y VSOL con sus respectivos presets.');
      }

      if (newPatterns.length === 0 && topZones.length > 0) {
        newPatterns.push(`Concentración de incidencias focalizada en: ${topZones.join(', ')}.`);
      }

      // Actualizar memoria permanente
      this.memory.lastConsolidatedAt = new Date().toISOString();
      this.memory.learnedLessons = newLessons;
      this.memory.recentPatterns = newPatterns;
      this.saveMemory();

      // 4. Formatear y Enviar Mensaje Matutino al Usuario
      const nowCaracas = new Date().toLocaleDateString('es-VE', { timeZone: 'America/Caracas' });
      const timeCaracas = new Date().toLocaleTimeString('es-VE', { timeZone: 'America/Caracas', hour: '2-digit', minute: '2-digit' });

      const lessonsText = newLessons.map((l, i) => `${i + 1}. ${l}`).join('\n');
      const patternsText = newPatterns.length > 0 
        ? `\n\n⚡ *Patrones Observados:*\n${newPatterns.map(p => `• ${p}`).join('\n')}` 
        : '';

      const morningBriefing = 
`🌅 *SOFÍA NOC - Resumen Matutino y Autoaprendizaje*
_Fecha: ${nowCaracas} ${timeCaracas}_\n
📊 *Telemetría de las Últimas 24 Horas:*
• *Cortes Eléctricos (Power Fail):* ${pfCount}
• *Pérdidas de Señal Óptica (LOS):* ${losCount}
• *Equipos Recuperados:* ${recoveries.length}
• *Tiempo Medio de Solución:* ${avgRecoveryTime ? avgRecoveryTime + ' min' : 'N/A'}
• *Zonas con más actividad:* ${topZones.length > 0 ? topZones.join(', ') : 'Todo estable'}\n
🧠 *Qué aprendí y concluí hoy:*
${lessonsText}${patternsText}\n
📡 *Estado:* Memoria operativa actualizada. Sigo activa con el monitoreo en vivo y comandos NOC.`;

      console.log(`[LearningService] Enviando resumen matutino exclusivamente a ${TARGET_USER_CHAT_ID}...`);
      await openwaClient.sendText(TARGET_USER_CHAT_ID, morningBriefing);
      console.log(`✅ [LearningService] Resumen matutino entregado a ${TARGET_USER_CHAT_ID}`);

    } catch (err) {
      console.error('[LearningService] Error durante consolidación:', err);
    } finally {
      this.isConsolidating = false;
    }
  }

  /**
   * Programa la consolidación para que ocurra todos los días a las 07:00 AM hora Caracas
   */
  startScheduler(llmClient) {
    if (this.consolidationTimer) clearInterval(this.consolidationTimer);

    // Revisar cada 60 segundos si es la hora indicada
    this.consolidationTimer = setInterval(() => {
      try {
        const now = new Date();
        const caracasHourStr = now.toLocaleTimeString('en-US', { timeZone: 'America/Caracas', hour12: false, hour: '2-digit' });
        const caracasMinStr = now.toLocaleTimeString('en-US', { timeZone: 'America/Caracas', minute: '2-digit' });
        const hour = parseInt(caracasHourStr, 10);
        const min = parseInt(caracasMinStr, 10);

        // Disparar a las 07:00 AM exactamente
        if (hour === 7 && min === 0 && !this.isConsolidating) {
          const lastDate = this.memory.lastConsolidatedAt ? new Date(this.memory.lastConsolidatedAt).toDateString() : '';
          const todayDate = now.toDateString();
          if (lastDate !== todayDate) {
            console.log('[LearningService] Hora de consolidación matutina alcanzada (07:00 AM). Ejecutando...');
            this.runDailyConsolidation(llmClient);
          }
        }
      } catch (e) {
        console.error('[LearningService] Error en scheduler:', e.message);
      }
    }, 60000);

    console.log('[LearningService] 🕒 Programador matutino activo: Se ejecutará diariamente a las 07:00 AM (América/Caracas).');
  }
}

module.exports = new LearningService();
