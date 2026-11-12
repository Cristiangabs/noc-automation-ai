module.exports = {
  PORT: process.env.PORT || 11450,
  
  OPENWA: {
    BASE_URL: process.env.OPENWA_BASE_URL || 'http://10.0.10.20:2785',
    SESSION_ID: process.env.OPENWA_SESSION_ID || 'YOUR_OPENWA_SESSION_ID',
    API_KEY: process.env.OPENWA_API_KEY || 'YOUR_OPENWA_API_KEY',
    WEBHOOK_URL: process.env.WEBHOOK_URL || 'http://10.0.10.30:11450/api/sofia-webhook'
  },

  // Bequant BQN - Integración diagnóstica
  BEQUANT: {
    HOST: process.env.BQN_HOST || '10.0.20.10',
    PORT: parseInt(process.env.BQN_PORT || '443', 10),
    USER: process.env.BQN_USER || 'bqnop',
    PASS: process.env.BQN_PASS || 'YOUR_BQN_PASSWORD',
    TIMEOUT_MS: 15000
  },

  LLM: {
    BASE_URL: process.env.LLM_BASE_URL || 'https://10.0.10.40:11443/v1',
    API_KEY: process.env.LLM_API_KEY || 'Bearer YOUR_LLM_BEARER_TOKEN',
    MODEL: process.env.LLM_MODEL || 'agent.fast',
    MAX_TOKENS: 350,
    TEMPERATURE: 0.65
  },

  // Modo Pruebas: si es true, solo responderá a los números en WHITELIST.
  // Si TEST_MODE es false, responderá a todos los clientes individuales (@c.us).
  TEST_MODE: process.env.TEST_MODE === 'true' || false,
  WHITELIST: (process.env.WHITELIST ? process.env.WHITELIST.split(',') : []).map(s => s.trim()),

  // Destinatarios técnicos para alertas proactivas (NOC / Cuadrillas / Directivo)
  TECH_ALERT_RECIPIENTS: [
    '584120000001@c.us',
    '584120000002@c.us',
    '584120000003@c.us'
  ],

  // Grupos técnicos de WhatsApp para alertas proactivas (@g.us)
  TECH_GROUP_IDS: (process.env.TECH_GROUP_IDS ? process.env.TECH_GROUP_IDS.split(',') : []).map(s => s.trim()).filter(Boolean),

  // Parámetros del motor de monitoreo en tiempo real
  MONITOR: {
    ENABLED: true,
    CHECK_INTERVAL_MS: 60 * 1000,       // Revisión en SmartOLT cada 60s
    FOLLOW_UP_INTERVAL_MS: 2 * 3600 * 1000, // Seguimiento cada 2 horas si sigue en LOS
    POWER_FAIL_THRESHOLD: 5             // Alertar apagón si caen más de 5 ONUs en la misma zona
  }
};
