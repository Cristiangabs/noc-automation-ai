/**
 * sentinelPowerMonitor.js
 * Monitor centinela de energía eléctrica para FiberNet FTTH.
 * Vigila permanentemente la ONU centinela (IP 10.0.50.151) mediante sondeos ICMP de alta precisión.
 * Si la ONU se apaga (deja de responder por falla de energía), despacha alerta de corte eléctrico.
 * Al recuperarse, calcula la duración exacta de la interrupción y notifica la normalización.
 */

const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');
const config = require('./config');
const openwaClient = require('./openwaClient');

const STATE_FILE = path.join(__dirname, 'sentinel_state.json');

const SENTINEL_CONFIG = {
  TARGET_IP: process.env.SENTINEL_ONU_IP || '10.0.50.151',
  CHECK_INTERVAL_MS: parseInt(process.env.SENTINEL_INTERVAL_MS || '20000', 10), // 20s
  FAIL_THRESHOLD: 3,        // 3 fallos consecutivos (~60s) para confirmar corte eléctrico
  RECOVERY_THRESHOLD: 2,    // 2 respuestas consecutivas para confirmar retorno de energía
  PING_TIMEOUT_SEC: 2
};

class SentinelPowerMonitor {
  constructor() {
    this.targetIp = SENTINEL_CONFIG.TARGET_IP;
    this.timer = null;
    this.isChecking = false;

    this.state = {
      targetIp: this.targetIp,
      status: 'UNKNOWN',        // 'ONLINE' | 'OFFLINE' | 'UNKNOWN'
      lastCheck: null,
      lastRtt: null,
      lastStateChange: null,
      outageStartTime: null,
      consecutiveFails: 0,
      consecutiveSuccesses: 0,
      totalOutagesDetected: 0
    };

    this.loadState();
  }

  loadState() {
    try {
      if (fs.existsSync(STATE_FILE)) {
        const raw = fs.readFileSync(STATE_FILE, 'utf8');
        const parsed = JSON.parse(raw);
        this.state = Object.assign(this.state, parsed);
        console.log(`[Centinela] Estado cargado para ${this.targetIp}: ${this.state.status}`);
      }
    } catch (e) {
      console.error('[Centinela] Error cargando sentinel_state.json:', e.message);
    }
  }

  saveState() {
    try {
      fs.writeFileSync(STATE_FILE, JSON.stringify(this.state, null, 2), 'utf8');
    } catch (e) {
      console.error('[Centinela] Error guardando sentinel_state.json:', e.message);
    }
  }

  /**
   * Ejecuta un sondeo ICMP rápido a la IP objetivo
   */
  probe() {
    return new Promise((resolve) => {
      const cmd = `ping -c 2 -W ${SENTINEL_CONFIG.PING_TIMEOUT_SEC} ${this.targetIp}`;
      exec(cmd, (err, stdout) => {
        const isAlive = !err && stdout && stdout.includes('bytes from');
        let rtt = null;
        if (stdout) {
          const match = stdout.match(/rtt min\/avg\/max\/mdev = [\d.]+\/([\d.]+)\//);
          if (match) {
            rtt = parseFloat(match[1]);
          }
        }
        resolve({ isAlive, rtt });
      });
    });
  }

  formatDuration(ms) {
    if (!ms || ms < 0) return 'menos de 1 minuto';
    const totalMinutes = Math.floor(ms / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours === 0) {
      return `${minutes} minuto(s)`;
    }
    return `${hours} hora(s) y ${minutes} minuto(s)`;
  }

  async check() {
    if (this.isChecking) return;
    this.isChecking = true;

    try {
      const { isAlive, rtt } = await this.probe();
      const now = new Date();
      this.state.lastCheck = now.toISOString();
      if (rtt !== null) {
        this.state.lastRtt = rtt;
      }

      if (isAlive) {
        this.state.consecutiveFails = 0;
        this.state.consecutiveSuccesses += 1;

        // Si estaba OFFLINE y confirma recuperación
        if (this.state.status === 'OFFLINE' && this.state.consecutiveSuccesses >= SENTINEL_CONFIG.RECOVERY_THRESHOLD) {
          const outageMs = this.state.outageStartTime ? (now.getTime() - new Date(this.state.outageStartTime).getTime()) : 0;
          const durationStr = this.formatDuration(outageMs);

          console.log(`[Centinela] 🟢 ENERGÍA RESTABLECIDA en ONU ${this.targetIp}. Duración: ${durationStr}`);
          this.state.status = 'ONLINE';
          this.state.lastStateChange = now.toISOString();

          await this.notifyRecovery(durationStr, rtt || this.state.lastRtt);
          this.state.outageStartTime = null;
          this.saveState();
        } else if (this.state.status !== 'ONLINE' && this.state.consecutiveSuccesses >= SENTINEL_CONFIG.RECOVERY_THRESHOLD) {
          // Primera inicialización online
          this.state.status = 'ONLINE';
          this.state.lastStateChange = now.toISOString();
          this.saveState();
        }
      } else {
        // Falló ping
        this.state.consecutiveSuccesses = 0;
        this.state.consecutiveFails += 1;

        // Si estaba ONLINE y supera el umbral de fallas consecutivas
        if (this.state.status !== 'OFFLINE' && this.state.consecutiveFails >= SENTINEL_CONFIG.FAIL_THRESHOLD) {
          console.log(`[Centinela] 🔴 CORTE ELÉCTRICO DETECTADO: ONU ${this.targetIp} sin respuesta tras ${this.state.consecutiveFails} intentos.`);
          this.state.status = 'OFFLINE';
          this.state.outageStartTime = now.toISOString();
          this.state.lastStateChange = now.toISOString();
          this.state.totalOutagesDetected += 1;

          await this.notifyOutage();
          this.saveState();
        }
      }
    } catch (err) {
      console.error('[Centinela] Error durante chequeo:', err.message);
    } finally {
      this.isChecking = false;
    }
  }

  async notifyOutage() {
    const timeStr = new Date().toLocaleTimeString('es-VE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const msg = [
      '⚡🔴 *ALERTA: CORTE DE ENERGÍA DETECTADO*',
      '------------------------------------------------',
      `📍 *Dispositivo Centinela:* ONU ${this.targetIp}`,
      `⚠️ *Estado:* 🔴 APAGADA`,
      `⏰ *Hora de caída:* ${timeStr}`,
      '------------------------------------------------'
    ].join('\n');

    await this.dispatchWhatsApp(msg);
  }

  async notifyRecovery(durationStr, rtt) {
    const timeStr = new Date().toLocaleTimeString('es-VE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const msg = [
      '⚡🟢 *ENERGÍA RESTABLECIDA: SERVICIO NORMALIZADO*',
      '------------------------------------------------',
      `📍 *Dispositivo Centinela:* ONU ${this.targetIp}`,
      `📶 *Estado:* 🟢 EN LÍNEA`,
      `⏱️ *Duración del corte:* ${durationStr}`,
      `⏰ *Hora de normalización:* ${timeStr}`,
      '------------------------------------------------'
    ].join('\n');

    await this.dispatchWhatsApp(msg);
  }

  async sendWithRetry(recipient, message, maxRetries = 2) {
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        await openwaClient.sendText(recipient, message);
        console.log(`[Centinela] ✅ Alerta entregada a ${recipient} (intento ${attempt})`);
        return true;
      } catch (err) {
        console.warn(`[Centinela] Intento ${attempt}/${maxRetries} falló para ${recipient}: ${err.message}`);
        if (attempt < maxRetries) {
          await new Promise(r => setTimeout(r, 2000));
        }
      }
    }
    console.error(`[Centinela] ❌ Error definitivo enviando alerta a ${recipient} tras ${maxRetries} intentos.`);
    return false;
  }

  async dispatchWhatsApp(message) {
    const recipients = new Set(config.TECH_ALERT_RECIPIENTS || []);
    if (Array.isArray(config.TECH_GROUP_IDS)) {
      config.TECH_GROUP_IDS.forEach(g => recipients.add(g));
    }

    console.log(`[Centinela] Despachando notificación en paralelo a ${recipients.size} destinatario(s)...`);
    const tasks = Array.from(recipients).map(r => this.sendWithRetry(r, message, 2));
    await Promise.allSettled(tasks);
  }

  getStatus() {
    return {
      targetIp: this.targetIp,
      status: this.state.status,
      isOnline: this.state.status === 'ONLINE',
      lastCheck: this.state.lastCheck,
      lastRtt: this.state.lastRtt,
      lastStateChange: this.state.lastStateChange,
      consecutiveFails: this.state.consecutiveFails,
      outageStartTime: this.state.outageStartTime,
      totalOutagesDetected: this.state.totalOutagesDetected
    };
  }

  start() {
    if (this.timer) return;
    console.log(`[Centinela] Iniciando monitoreo de energía sobre ONU ${this.targetIp} cada ${SENTINEL_CONFIG.CHECK_INTERVAL_MS / 1000}s...`);
    // Ejecutar chequeo inicial inmediato
    this.check();
    this.timer = setInterval(() => this.check(), SENTINEL_CONFIG.CHECK_INTERVAL_MS);
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
      console.log('[Centinela] Monitoreo detenido.');
    }
  }
}

// Instancia singleton
const sentinelInstance = new SentinelPowerMonitor();

module.exports = sentinelInstance;
