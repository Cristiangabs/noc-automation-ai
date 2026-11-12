const http = require('http');
const url = require('url');
const config = require('./config');

class OpenWAClient {
  constructor() {
    this.baseUrl = config.OPENWA.BASE_URL;
    this.sessionId = config.OPENWA.SESSION_ID;
    this.apiKey = config.OPENWA.API_KEY;
  }

  async sendText(chatId, text) {
    const payload = JSON.stringify({
      chatId,
      text
    });

    const parsedUrl = new url.URL(this.baseUrl);
    const options = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || 80,
      path: `/api/sessions/${this.sessionId}/messages/send-text`,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': this.apiKey,
        'Content-Length': Buffer.byteLength(payload)
      },
      timeout: 15000
    };

    return new Promise((resolve, reject) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(data));
            } catch (e) {
              resolve({ raw: data, status: res.statusCode });
            }
          } else {
            reject(new Error(`OpenWA error ${res.statusCode}: ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout enviando mensaje a OpenWA'));
      });

      req.on('error', reject);
      req.write(payload);
      req.end();
    });
  }

  async sendImage(chatId, base64Png, caption = '') {
    const payload = JSON.stringify({
      chatId,
      mimetype: 'image/png',
      base64: base64Png,
      caption
    });

    const parsedUrl = new url.URL(this.baseUrl);
    const options = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || 80,
      path: `/api/sessions/${this.sessionId}/messages/send-image`,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': this.apiKey,
        'Content-Length': Buffer.byteLength(payload)
      },
      timeout: 30000
    };

    return new Promise((resolve, reject) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(data));
            } catch (e) {
              resolve({ raw: data, status: res.statusCode });
            }
          } else {
            reject(new Error(`OpenWA send-image error ${res.statusCode}: ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout enviando imagen a OpenWA'));
      });

      req.on('error', reject);
      req.write(payload);
      req.end();
    });
  }

  async sendVoice(chatId, oggBase64) {
    const payload = JSON.stringify({
      chatId,
      base64: oggBase64,
      mimetype: 'audio/ogg; codecs=opus'
    });

    const parsedUrl = new url.URL(this.baseUrl);
    const options = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || 80,
      path: `/api/sessions/${this.sessionId}/messages/send-audio`,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': this.apiKey,
        'Content-Length': Buffer.byteLength(payload)
      },
      timeout: 20000
    };

    return new Promise((resolve, reject) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(data));
            } catch (e) {
              resolve({ raw: data, status: res.statusCode });
            }
          } else {
            reject(new Error(`OpenWA send-audio error ${res.statusCode}: ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout enviando audio a OpenWA'));
      });

      req.on('error', reject);
      req.write(payload);
      req.end();
    });
  }

  async sendDocument(chatId, base64Pdf, filename = 'Reporte.pdf', caption = '') {
    const payload = JSON.stringify({
      chatId,
      base64: base64Pdf,
      filename,
      mimetype: 'application/pdf',
      caption
    });

    const parsedUrl = new url.URL(this.baseUrl);
    const options = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || 80,
      path: `/api/sessions/${this.sessionId}/messages/send-document`,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': this.apiKey,
        'Content-Length': Buffer.byteLength(payload)
      },
      timeout: 30000
    };

    return new Promise((resolve, reject) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(data));
            } catch (e) {
              resolve({ raw: data, status: res.statusCode });
            }
          } else {
            reject(new Error(`OpenWA send-document error ${res.statusCode}: ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout enviando documento a OpenWA'));
      });

      req.on('error', reject);
      req.write(payload);
      req.end();
    });
  }

  async downloadMedia(chatId, messageId) {
    const parsedUrl = new url.URL(this.baseUrl);
    const options = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || 80,
      path: `/api/sessions/${this.sessionId}/messages/${encodeURIComponent(chatId)}/${encodeURIComponent(messageId)}/media`,
      method: 'GET',
      headers: {
        'x-api-key': this.apiKey
      },
      timeout: 25000
    };

    return new Promise((resolve, reject) => {
      const req = http.request(options, (res) => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(Buffer.concat(chunks));
          } else {
            reject(new Error(`Error descargando media ${res.statusCode}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout descargando media de OpenWA'));
      });

      req.on('error', reject);
      req.end();
    });
  }

  async getWebhooks() {
    const parsedUrl = new url.URL(this.baseUrl);
    const options = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || 80,
      path: `/api/sessions/${this.sessionId}/webhooks`,
      method: 'GET',
      headers: {
        'x-api-key': this.apiKey
      },
      timeout: 10000
    };

    return new Promise((resolve, reject) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve(JSON.parse(data));
            } catch (e) {
              resolve([]);
            }
          } else {
            reject(new Error(`Error obteniendo webhooks (${res.statusCode}): ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout consultando webhooks en OpenWA'));
      });

      req.on('error', reject);
      req.end();
    });
  }

  async ensureWebhookRegistered() {
    try {
      const targetUrl = config.OPENWA.WEBHOOK_URL;
      const webhooks = await this.getWebhooks();
      
      const existing = Array.isArray(webhooks) && webhooks.find(w => w.url === targetUrl);
      if (existing) {
        console.log(`[OpenWA] Webhook ya registrado correctamente: ${targetUrl} (ID: ${existing.id})`);
        return existing;
      }

      console.log(`[OpenWA] Registrando webhook secundario para Sofía: ${targetUrl}...`);
      const payload = JSON.stringify({
        url: targetUrl,
        events: ['message.received']
      });

      const parsedUrl = new url.URL(this.baseUrl);
      const options = {
        hostname: parsedUrl.hostname,
        port: parsedUrl.port || 80,
        path: `/api/sessions/${this.sessionId}/webhooks`,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': this.apiKey,
          'Content-Length': Buffer.byteLength(payload)
        },
        timeout: 10000
      };

      return new Promise((resolve, reject) => {
        const req = http.request(options, (res) => {
          let data = '';
          res.on('data', chunk => { data += chunk; });
          res.on('end', () => {
            if (res.statusCode >= 200 && res.statusCode < 300) {
              console.log(`[OpenWA] Webhook secundario registrado exitosamente: ${data}`);
              try { resolve(JSON.parse(data)); } catch (e) { resolve({ raw: data }); }
            } else {
              reject(new Error(`Error al registrar webhook en OpenWA (${res.statusCode}): ${data}`));
            }
          });
        });

        req.on('timeout', () => {
          req.destroy();
          reject(new Error('Timeout registrando webhook en OpenWA'));
        });

        req.on('error', reject);
        req.write(payload);
        req.end();
      });
    } catch (err) {
      console.error('[OpenWA] Advertencia al verificar/registrar webhook:', err.message);
    }
  }
}

module.exports = new OpenWAClient();
