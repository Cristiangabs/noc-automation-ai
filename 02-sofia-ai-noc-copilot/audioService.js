const http = require('http');
const https = require('https');
const url = require('url');
const config = require('./config');

class AudioService {
  constructor() {
    this.ttsPort = 11452;
  }

  // Limpiar texto para que la síntesis no pronuncie códigos, viñetas ni emojis
  cleanTextForSpeech(text) {
    if (!text) return '';
    return text
      .replace(/https?:\/\/\S+/gi, '')
      .replace(/\*/g, '')
      .replace(/^\s*[-•]\s*/gm, '')
      .replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F900}-\u{1F9FF}\u{1F018}-\u{1F270}\u{2388}\u{200D}]/gu, '')
      .replace(/\n+/g, '. ')
      .trim();
  }

  // Generar nota de voz .ogg a partir de texto usando el motor de voz clonada XTTS-v2 en memoria
  async textToSpeech(text) {
    const clean = this.cleanTextForSpeech(text);
    if (!clean) {
      throw new Error('Texto vacío tras limpieza para síntesis de voz');
    }

    const payload = JSON.stringify({ text: clean });

    return new Promise((resolve, reject) => {
      const req = http.request({
        hostname: '127.0.0.1',
        port: this.ttsPort,
        path: '/synthesize',
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(payload)
        },
        timeout: 45000
      }, res => {
        let data = '';
        res.on('data', chunk => data += chunk);
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              const parsed = JSON.parse(data);
              if (parsed.success && parsed.base64) {
                resolve({
                  oggBase64: parsed.base64,
                  duration: parsed.duration,
                  bytes: parsed.bytes
                });
              } else {
                reject(new Error(parsed.error || 'Respuesta inválida del motor XTTS'));
              }
            } catch (err) {
              reject(new Error(`Error parseando respuesta de XTTS: ${err.message}`));
            }
          } else {
            reject(new Error(`TTS HTTP error: ${res.statusCode} - ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout de 45s en síntesis de voz XTTS-v2'));
      });

      req.on('error', (err) => {
        reject(new Error(`Fallo de conexión con motor TTS local: ${err.message}`));
      });

      req.write(payload);
      req.end();
    });
  }

  // Transcribir audio entrante a texto (STT) usando el modelo NVIDIA Parakeet en el servidor local de IA
  async speechToText(audioBuffer) {
    const boundary = '----WebKitFormBoundary' + Math.random().toString(36).substring(2);
    const parsedLlm = new url.URL(config.LLM.BASE_URL);

    const postDataHeader = Buffer.from(
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="model"\r\n\r\nauto-asr\r\n` +
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="file"; filename="voice.ogg"\r\n` +
      `Content-Type: audio/ogg\r\n\r\n`
    );
    const postDataFooter = Buffer.from(`\r\n--${boundary}--\r\n`);
    const fullPayload = Buffer.concat([postDataHeader, audioBuffer, postDataFooter]);

    return new Promise((resolve, reject) => {
      const options = {
        hostname: parsedLlm.hostname,
        port: parsedLlm.port || 443,
        path: '/v1/audio/transcriptions',
        method: 'POST',
        headers: {
          'Authorization': config.LLM.API_KEY,
          'Content-Type': `multipart/form-data; boundary=${boundary}`,
          'Content-Length': fullPayload.length
        },
        rejectUnauthorized: false,
        timeout: 20000
      };

      const req = https.request(options, (res) => {
        let data = '';
        res.on('data', chunk => data += chunk);
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              const parsed = JSON.parse(data);
              resolve((parsed.text || '').trim());
            } catch (e) {
              reject(new Error(`Error parseando transcripción: ${e.message}`));
            }
          } else {
            reject(new Error(`Error STT HTTP ${res.statusCode}: ${data}`));
          }
        });
      });

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Timeout de 20s en transcripción STT'));
      });

      req.on('error', reject);
      req.write(fullPayload);
      req.end();
    });
  }
}

module.exports = new AudioService();
