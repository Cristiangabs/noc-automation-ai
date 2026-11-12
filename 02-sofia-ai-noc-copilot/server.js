const express = require('express');
const path = require('path');
const config = require('./config');
const conversationManager = require('./conversationManager');
const llmClient = require('./llmClient');
const openwaClient = require('./openwaClient');
const audioService = require('./audioService');
const customerLookupService = require('./customerLookupService');
const smartoltMonitor = require('./smartoltMonitor');
const bequantReportService = require('./bequantReportService');
const learningService = require('./learningService');
const authorizedMembersManager = require('./authorizedMembersManager');
const cirionDiagnosticService = require('./cirionDiagnosticService');

const app = express();
app.use(express.json({ limit: '25mb' }));
app.use(express.urlencoded({ extended: true, limit: '25mb' }));

// Servir archivos estáticos de audio generados
app.use('/media', express.static(path.join(__dirname, 'media')));

// Buffer para debouncing de mensajes en ráfaga por chatId
// { [chatId]: { texts: string[], timer: NodeJS.Timeout, pushname: string, hadAudio: boolean } }
const messageBuffers = new Map();
const DEBOUNCE_DELAY_MS = 2000;

// ==========================================
// Health check & status
// ==========================================
app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    service: 'multicanal-sofia',
    bot: 'Sofía',
    voiceModule: {
      tts: 'disabled (Respuestas solo texto)',
      stt: 'active (NVIDIA Parakeet - speech-to-text)'
    },
    activeConversations: conversationManager.conversations.size,
    testMode: config.TEST_MODE,
    uptime: Math.floor(process.uptime()) + 's'
  });
});

app.get('/api/conversations', (req, res) => {
  const result = [];
  for (const [chatId, conv] of conversationManager.conversations.entries()) {
    result.push({
      chatId,
      messageCount: conv.messages.length,
      escalatedToHuman: conv.escalatedToHuman,
      lastActive: new Date(conv.lastActive).toISOString()
    });
  }
  res.json(result);
});

// ==========================================
// Helper para reconocimiento de meses en comandos de sobreconsumo
// ==========================================
function parseTargetMonth(text) {
  if (!text) return null;
  const clean = text.toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[#!\/]/g, ' ')
    .trim();

  const currentYear = new Date().getFullYear();
  let year = currentYear;
  const yearMatch = clean.match(/\b(202[4-9])\b/);
  if (yearMatch) {
    year = parseInt(yearMatch[1], 10);
  }

  const months = [
    { key: 'octubre', aliases: ['octubre', 'oct', 'october'], num: 10, name: 'Octubre' },
    { key: 'noviembre', aliases: ['noviembre', 'nov', 'november'], num: 11, name: 'Noviembre' },
    { key: 'septiembre', aliases: ['septiembre', 'setiembre', 'sep', 'sept', 'september'], num: 9, name: 'Septiembre' },
    { key: 'diciembre', aliases: ['diciembre', 'dic', 'december'], num: 12, name: 'Diciembre' },
    { key: 'agosto', aliases: ['agosto', 'ago', 'august'], num: 8, name: 'Agosto' },
    { key: 'julio', aliases: ['julio', 'jul', 'july'], num: 7, name: 'Julio' },
    { key: 'junio', aliases: ['junio', 'jun', 'june'], num: 6, name: 'Junio' },
    { key: 'mayo', aliases: ['mayo', 'may'], num: 5, name: 'Mayo' },
    { key: 'abril', aliases: ['abril', 'abr', 'april'], num: 4, name: 'Abril' },
    { key: 'marzo', aliases: ['marzo', 'mar'], num: 3, name: 'Marzo' },
    { key: 'febrero', aliases: ['febrero', 'feb'], num: 2, name: 'Febrero' },
    { key: 'enero', aliases: ['enero', 'ene', 'january', 'jan'], num: 1, name: 'Enero' },
  ];

  for (const m of months) {
    for (const alias of m.aliases) {
      const regex = new RegExp(`(^|\\s)${alias}(\\s|$)`, 'i');
      if (regex.test(clean)) {
        const monthNumStr = String(m.num).padStart(2, '0');
        return {
          monthNum: m.num,
          monthName: m.name,
          year,
          yearMonth: `${year}-${monthNumStr}`,
          title: `${m.name} ${year}`
        };
      }
    }
  }

  const numMatch = clean.match(/\b(0?[1-9]|1[0-2])\b/);
  if (numMatch && (clean.length <= 4 || clean.includes('mes'))) {
    const num = parseInt(numMatch[1], 10);
    const m = months.find(item => item.num === num);
    if (m) {
      const monthNumStr = String(m.num).padStart(2, '0');
      return {
        monthNum: m.num,
        monthName: m.name,
        year,
        yearMonth: `${year}-${monthNumStr}`,
        title: `${m.name} ${year}`
      };
    }
  }

  return null;
}

// Endpoint de prueba interna (permite simular un mensaje sin enviar por WhatsApp)
app.post('/api/simulate', async (req, res) => {
  try {
    const { chatId = '584120000000@c.us', text, author = '584120000001@c.us' } = req.body;
    if (!text) return res.status(400).json({ error: 'Falta campo text' });

    const isGroup = Boolean(chatId.endsWith('@g.us') || req.body.isGroup === true);
    if (isGroup) {
      const SOFIA_MENTION_REGEX = /(?:^|\s|@)(?:hola\s+|oye\s+|mira\s+|buenos\s+d[ií]as\s+)?sof[ií]a\b/i;
      const hasMention = SOFIA_MENTION_REGEX.test(text);
      if (!hasMention) {
        return res.json({ chatId, user: text, reply: null, ignored: true, reason: 'No menciona a Sofía' });
      }
      if (!authorizedMembersManager.isAuthorized(author)) {
        return res.json({ chatId, user: text, reply: null, ignored: true, reason: 'Emisor no autorizado' });
      }
      const member = authorizedMembersManager.getMember(author);
      const authorName = member?.name || 'Colega';
      let cleanText = text
        .replace(/(?:^|\s|@)(?:hola\s+|oye\s+|mira\s+|buenos\s+d[ií]as\s+)?sof[ií]a\b[:,]?/gi, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      if (!cleanText) cleanText = 'hola';

      const normClean = cleanText.toLowerCase();
      if (normClean === 'resumen' || normClean === 'status' || normClean === 'red') {
        const summaryMsg = await smartoltMonitor.getQuickSummary();
        return res.json({ chatId, user: text, author: authorName, reply: summaryMsg, isNocCommand: true });
      }
      if (normClean === 'los' || normClean === 'cortes') {
        const losMsg = await smartoltMonitor.getLosList();
        return res.json({ chatId, user: text, author: authorName, reply: losMsg, isNocCommand: true });
      }
      if (normClean === 'luz' || normClean === 'power' || normClean === 'apagon' || normClean === 'apagones' || normClean === 'centinela') {
        const powerMsg = await smartoltMonitor.getActiveOutagesReport();
        return res.json({ chatId, user: text, author: authorName, reply: powerMsg, isNocCommand: true });
      }

      let customerData = null;
      if (customerLookupService.hasIdentifier(cleanText)) {
        const lookupRes = await customerLookupService.lookup(cleanText);
        if (lookupRes && lookupRes.found) customerData = lookupRes;
      }

      // Presentación única si es la primera vez que interactúa en el grupo
      let introSent = null;
      if (!authorizedMembersManager.hasGreeted(chatId)) {
        authorizedMembersManager.markGreeted(chatId);
        introSent = authorizedMembersManager.getPresentationMessage();
      }

      const history = conversationManager.getHistory(chatId);
      const reply = await llmClient.generateReply(history, `[${authorName}]: ${cleanText}`, customerData, { isGroup: true, authorName });
      return res.json({ chatId, user: text, author: authorName, introSent, reply, isGroup: true });
    }

    const normText = text.trim().toLowerCase();

    // 0. Comprobar si hay una acción pendiente para este chat (ej. selección de mes de sobreconsumo)
    const pendingAction = conversationManager.getPendingAction(chatId);
    if (pendingAction && pendingAction.action === 'awaiting_sobreconsumo_month') {
      if (normText === 'cancelar' || normText === 'cancel' || normText === 'salir' || normText === 'no') {
        conversationManager.clearPendingAction(chatId);
        return res.json({ chatId, user: text, reply: 'Operación cancelada. Si necesitas otra consulta o reporte, avísame.', isNocCommand: true });
      }
      const parsedMonth = parseTargetMonth(text);
      if (parsedMonth) {
        conversationManager.clearPendingAction(chatId);
        bequantReportService.generateAndSendMonthlyPdf(chatId, parsedMonth).catch(err => {
          console.error('[Sofía] Error en generateAndSendMonthlyPdf:', err);
        });
        return res.json({
          chatId,
          user: text,
          reply: `📊 Generando reporte mensual de sobreconsumo de Bequant BQN para *${parsedMonth.title}* en formato PDF... En unos segundos te lo envío adjunto.`,
          isNocCommand: true,
          month: parsedMonth
        });
      } else {
        return res.json({
          chatId,
          user: text,
          reply: '❓ No logré identificar el mes. Por favor indícame el nombre del mes que deseas consultar (por ejemplo: *octubre* o *noviembre*), o escribe *cancelar* para salir.',
          isNocCommand: true,
          pendingAction: 'awaiting_sobreconsumo_month'
        });
      }
    }

    if (normText === 'resumen' || normText === 'status' || normText === 'red') {
      const summaryMsg = await smartoltMonitor.getQuickSummary();
      return res.json({ chatId, user: text, reply: summaryMsg, isNocCommand: true });
    }
    if (normText === 'los' || normText === 'cortes') {
      const losMsg = await smartoltMonitor.getLosList();
      return res.json({ chatId, user: text, reply: losMsg, isNocCommand: true });
    }
    if (normText === 'luz' || normText === 'power' || normText === 'apagon' || normText === 'apagones') {
      const powerMsg = await smartoltMonitor.getActiveOutagesReport();
      return res.json({ chatId, user: text, reply: powerMsg, isNocCommand: true });
    }

    const isCirionQuerySim =
      normText.includes('cirion') ||
      normText.includes('lumex') ||
      normText === '!cirion' ||
      normText === '/cirion' ||
      normText === '#cirion';

    if (isCirionQuerySim && (chatId.includes('584120000001') || chatId === config.REPORT_TARGET_CHAT_ID)) {
      const diag = await cirionDiagnosticService.runDiagnosis();
      return res.json({
        chatId,
        user: text,
        reply: diag.reportText,
        hasImage: Boolean(diag.imageBase64),
        verdict: diag.isCarrierDown ? 'FALLA_CIRION' : 'CIRION_ACTIVO',
        isNocCommand: true
      });
    }
    if (
      normText.startsWith('!sobreconsumo') ||
      normText.startsWith('#sobreconsumo') ||
      normText.startsWith('/sobreconsumo') ||
      normText === 'sobreconsumo' ||
      normText.includes('reporte sobreconsumo') ||
      normText.includes('reporte de sobreconsumo') ||
      normText.includes('top flujos') ||
      normText.includes('abuso de conexiones') ||
      normText.includes('quienes tienen sobreconsumo')
    ) {
      const targetMonth = parseTargetMonth(text);
      if (targetMonth) {
        bequantReportService.generateAndSendMonthlyPdf(chatId, targetMonth).catch(err => {
          console.error('[Sofía] Error en generateAndSendMonthlyPdf:', err);
        });
        return res.json({
          chatId,
          user: text,
          reply: `📊 Generando reporte mensual de sobreconsumo de Bequant BQN para *${targetMonth.title}* en formato PDF... En unos segundos te lo envío adjunto.`,
          isNocCommand: true,
          month: targetMonth
        });
      } else {
        conversationManager.setPendingAction(chatId, { action: 'awaiting_sobreconsumo_month', createdAt: Date.now() });
        return res.json({
          chatId,
          user: text,
          reply: '📊 Para generar el reporte mensual de sobreconsumo de Bequant BQN, por favor indícame qué mes deseas consultar (por ejemplo: *octubre* o *noviembre*).\n\n_Tip: También puedes escribir directo: #sobreconsumo octubre_',
          isNocCommand: true,
          pendingAction: 'awaiting_sobreconsumo_month'
        });
      }
    }

    const isStaff = authorizedMembersManager.isAuthorized(chatId);
    const member = authorizedMembersManager.getMember(chatId);
    const callerName = member?.name || (chatId.includes('584120000001') ? 'Cristian Salinas (Directivo)' : 'Colega');

    let customerData = isStaff ? null : conversationManager.getCustomerData(chatId);
    const hasIdentifier = customerLookupService.hasIdentifier(text);

    if (hasIdentifier) {
      const lookupRes = await customerLookupService.lookup(text);
      if (lookupRes && lookupRes.found) {
        customerData = lookupRes;
        conversationManager.setCustomerData(chatId, customerData);
      }
    }

    let history = conversationManager.getHistory(chatId);
    if (isStaff) {
      history = history.filter(m => !m.content.includes('Bienvenida a Multicanal') && !m.content.includes('con tu servicio'));
    }

    const reply = await llmClient.generateReply(
      history,
      text,
      customerData,
      { isStaff, memberName: callerName, isGroup: false }
    );

    conversationManager.addMessage(chatId, 'user', text);
    conversationManager.addMessage(chatId, 'assistant', reply);

    res.json({
      chatId,
      user: text,
      customerFound: Boolean(customerData && customerData.found),
      customerData: customerData || null,
      reply,
      historyLength: conversationManager.getHistory(chatId).length
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ==========================================
// Webhook principal de WhatsApp (OpenWA)
// ==========================================
app.post('/api/sofia-webhook', async (req, res) => {
  // Responder inmediatamente 200 OK a OpenWA
  res.status(200).json({ received: true });

  try {
    const body = req.body;
    const data = body.data || body.payload || body;
    const event = body.event || body.type;

    // Detección de incorporación a grupo para presentación automática inicial
    if (event && (event.includes('group') || event.includes('join') || event.includes('participant'))) {
      const targetGroupId = data.chatId || data.id || body.chatId || body.id;
      if (targetGroupId && typeof targetGroupId === 'string' && targetGroupId.endsWith('@g.us')) {
        smartoltMonitor.registerTechGroup(targetGroupId);
        if (!authorizedMembersManager.hasGreeted(targetGroupId)) {
          authorizedMembersManager.markGreeted(targetGroupId);
          const introMsg = authorizedMembersManager.getPresentationMessage();
          console.log(`[Sofía Grupo] Evento de incorporación a grupo detectado. Enviando presentación inicial a ${targetGroupId}...`);
          await openwaClient.sendText(targetGroupId, introMsg);
        }
        return;
      }
    }

    if (event && !event.includes('message')) {
      return;
    }

    // 1. REGLA CRÍTICA DE SEGURIDAD: Ignorar mensajes propios (fromMe == true)
    const isFromMe = Boolean(
      data.fromMe === true ||
      (typeof data.id === 'string' && data.id.startsWith('true_'))
    );
    if (isFromMe) {
      return;
    }

    // Extraer chatId
    const chatId = data.chatId || data.from || (data.chat && data.chat.id);
    if (!chatId || typeof chatId !== 'string') {
      return;
    }

    const messageType = data.type || 'chat';
    let rawText = (data.body || data.text || data.caption || '').trim();
    const pushname = data.sender?.pushname || data.pushname || data.notifyName || '';
    const messageId = data.id;
    let isVoiceMessage = (messageType === 'ptt' || messageType === 'audio' || messageType === 'voice');

    // 2. Manejo de chats de grupo (@g.us)
    const isGroup = Boolean(
      data.isGroupMsg === true ||
      chatId.endsWith('@g.us') ||
      (data.chat && data.chat.isGroup === true)
    );

    if (isGroup) {
      // Regla 1: Detección del emisor real dentro del grupo
      const author = data.author || data.from || data.sender?.id || data.participant || '';
      if (!authorizedMembersManager.isAuthorized(author)) {
        // Silencio absoluto: no responder nada ni transcribir audios si el número no está en la lista autorizada
        return;
      }

      // Si el miembro autorizado envió una nota de voz, transcribirla primero con NVIDIA Parakeet
      if (isVoiceMessage) {
        console.log(`[Sofía Grupo] Nota de voz de emisor autorizado (${author}) en grupo ${chatId}. Transcribiendo...`);
        try {
          let audioBuffer = null;
          const targetMsgId = data.waMessageId || data.id || '';
          try {
            audioBuffer = await openwaClient.downloadMedia(chatId, targetMsgId);
          } catch (dlErr) {
            if (data.metadata?.media?.data) {
              audioBuffer = Buffer.from(data.metadata.media.data, 'base64');
            } else if (data.media?.data) {
              audioBuffer = Buffer.from(data.media.data, 'base64');
            }
          }
          if (audioBuffer && audioBuffer.length > 0) {
            const transcribed = await audioService.speechToText(audioBuffer);
            console.log(`[Sofía Grupo] Audio transcrito: "${transcribed}"`);
            if (transcribed && transcribed.trim().length > 0) {
              rawText = transcribed.trim();
            }
          }
        } catch (sttErr) {
          console.error('[Sofía Grupo] Error transcribiendo audio en grupo:', sttErr.message);
          return;
        }
      }

      // Regla 2: Detección estricta de mención explícita a Sofía ("Sofia", "Hola Sofia", etc.)
      const SOFIA_MENTION_REGEX = /(?:^|\s|@)(?:hola\s+|oye\s+|mira\s+|buenos\s+d[ií]as\s+)?sof[ií]a\b/i;
      const hasSofiaMention = SOFIA_MENTION_REGEX.test(rawText) ||
        (Array.isArray(data.mentionedJidList) && data.mentionedJidList.length > 0 &&
         data.mentionedJidList.some(jid => String(jid).includes('151') || String(jid).toLowerCase().includes('sofia')));

      if (!hasSofiaMention) {
        // En grupos, Sofía se mantiene en silencio absoluto si no la nombran explícitamente en el texto o audio
        return;
      }

      // Emisor autorizado detectado y mención confirmada
      const member = authorizedMembersManager.getMember(author);
      const authorName = member?.name || pushname || 'Colega';
      console.log(`[Sofía Grupo] ✅ Mención autorizada en grupo ${chatId} de ${authorName} (${author}) [Voz: ${isVoiceMessage}]: "${rawText}"`);

      // Asegurar que el grupo quede registrado para recibir alertas de red (cortes y luz)
      smartoltMonitor.registerTechGroup(chatId);

      // Limpiar la invocación de Sofia para procesar la instrucción limpia
      let cleanText = rawText
        .replace(/(?:^|\s|@)(?:hola\s+|oye\s+|mira\s+|buenos\s+d[ií]as\s+)?sof[ií]a\b[:,]?/gi, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      if (!cleanText) cleanText = 'hola';

      await processAndReplyGroup(chatId, cleanText, authorName, author);
      return;
    }

    // 3. REGLA CRÍTICA: Solo responder a chats individuales de clientes (@c.us)
    if (!chatId.endsWith('@c.us')) {
      return;
    }

    // 4. MODO PRUEBAS: Si TEST_MODE está activo, filtrar por WHITELIST
    if (config.TEST_MODE && Array.isArray(config.WHITELIST) && config.WHITELIST.length > 0) {
      const cleanPhone = chatId.replace('@c.us', '');
      const isAllowed = config.WHITELIST.some(allowed => {
        const cleanAllowed = allowed.replace('@c.us', '');
        return cleanPhone.includes(cleanAllowed) || cleanAllowed.includes(cleanPhone);
      });
      if (!isAllowed) {
        return;
      }
    }

    // 5. Verificar si el chat ya fue escalado a operador humano
    if (conversationManager.isEscalated(chatId)) {
      console.log(`[Sofía] Chat ${chatId} está escalado a operador humano. Sofía se mantiene en silencio.`);
      return;
    }

    // ==========================================
    // RECONOCIMIENTO DE VOZ (STT)
    // ==========================================
    if (isVoiceMessage) {
      console.log(`[Sofía] Audio / Nota de voz recibida de ${chatId} (${pushname}) - Tipo: ${messageType} - ID: ${messageId}`);
      try {
        let audioBuffer = null;
        const targetMsgId = data.waMessageId || data.id || '';
        try {
          audioBuffer = await openwaClient.downloadMedia(chatId, targetMsgId);
          console.log(`[Sofía] Audio descargado vía API (${audioBuffer ? audioBuffer.length : 0} bytes).`);
        } catch (dlErr) {
          console.warn('[Sofía] Descarga vía API falló, probando base64 inline:', dlErr.message);
          if (data.metadata?.media?.data) {
            audioBuffer = Buffer.from(data.metadata.media.data, 'base64');
          } else if (data.media?.data) {
            audioBuffer = Buffer.from(data.media.data, 'base64');
          }
        }

        if (audioBuffer && audioBuffer.length > 0) {
          console.log(`[Sofía] Transcribiendo nota de voz con NVIDIA Parakeet...`);
          const transcribed = await audioService.speechToText(audioBuffer);
          console.log(`[Sofía] Transcripción completada: "${transcribed}"`);
          if (transcribed && transcribed.trim().length > 0) {
            rawText = transcribed.trim();
          }
        }
      } catch (sttErr) {
        // Si no se pudo transcribir, avisar amablemente
        const isStaffAudio = authorizedMembersManager.isAuthorized(chatId);
        const fallbackAudioNotice = isStaffAudio
          ? 'No logré procesar el audio por una intermitencia de red. Por favor escríbeme la consulta por texto.'
          : `Hola${pushname ? ' ' + pushname : ''}! No logré escuchar bien tu nota de voz por una intermitencia de red. Podrías escribirme tu consulta por texto para atenderte de una? 🙂`;
        await openwaClient.sendText(chatId, fallbackAudioNotice);
        return;
      }
    }

    // Si tras el proceso no hay texto interpretable, ignorar
    if (!rawText) {
      return;
    }

    console.log(`[Sofía] Mensaje a procesar de ${chatId} (${pushname}): "${rawText}" [Voz: ${isVoiceMessage}]`);

    // ==========================================
    // DEBOUNCE: Manejo de ráfagas de mensajes
    // ==========================================
    let buffer = messageBuffers.get(chatId);
    if (!buffer) {
      buffer = { texts: [], timer: null, pushname, hadAudio: false };
      messageBuffers.set(chatId, buffer);
    }

    buffer.texts.push(rawText);
    if (pushname) buffer.pushname = pushname;
    if (isVoiceMessage) buffer.hadAudio = true;

    if (buffer.timer) {
      clearTimeout(buffer.timer);
    }

    buffer.timer = setTimeout(async () => {
      messageBuffers.delete(chatId);
      const combinedUserMessage = buffer.texts.join(' ');
      await processAndReply(chatId, combinedUserMessage, buffer.pushname);
    }, DEBOUNCE_DELAY_MS);

  } catch (err) {
    console.error('[Sofía] Error procesando webhook:', err);
  }
});

// ==========================================
// Procesamiento y respuesta para Grupos Técnicos Autorizados
// ==========================================
async function processAndReplyGroup(groupId, cleanText, authorName, authorPhone) {
  try {
    const normText = cleanText.toLowerCase().trim();

    // 0. Si Sofía aún no se ha presentado en este grupo, enviar presentación única
    if (!authorizedMembersManager.hasGreeted(groupId)) {
      authorizedMembersManager.markGreeted(groupId);
      const introMsg = authorizedMembersManager.getPresentationMessage();
      console.log(`[Sofía Grupo] Primera interacción en grupo ${groupId}. Enviando presentación inicial única...`);
      await openwaClient.sendText(groupId, introMsg);
    }

    // 1. Comandos técnicos rápidos de monitoreo NOC
    if (normText === 'resumen' || normText === 'status' || normText === 'red' || normText === 'estado') {
      const summaryMsg = await smartoltMonitor.getQuickSummary();
      await openwaClient.sendText(groupId, summaryMsg);
      learningService.logInteraction(groupId, authorName, cleanText, summaryMsg, true);
      return;
    }

    if (normText === 'los' || normText === 'cortes') {
      const losMsg = await smartoltMonitor.getLosList();
      await openwaClient.sendText(groupId, losMsg);
      learningService.logInteraction(groupId, authorName, cleanText, losMsg, true);
      return;
    }

    if (normText === 'luz' || normText === 'power' || normText === 'apagon' || normText === 'apagones' || normText === 'centinela') {
      const powerMsg = await smartoltMonitor.getActiveOutagesReport();
      await openwaClient.sendText(groupId, powerMsg);
      learningService.logInteraction(groupId, authorName, cleanText, powerMsg, true);
      return;
    }

    if (normText.includes('#registrar') || normText.includes('#grupo') || normText === 'registrar' || normText === 'grupo') {
      smartoltMonitor.registerTechGroup(groupId);
      await openwaClient.sendText(groupId, '✅ *Grupo técnico registrado exitosamente.* Sofía enviará las alertas de cortes de fibra (LOS) y fallas eléctricas en este chat.');
      return;
    }

    // Sobreconsumo mensual
    const isOverconsumptionQuery =
      normText.startsWith('!sobreconsumo') ||
      normText.startsWith('#sobreconsumo') ||
      normText.startsWith('/sobreconsumo') ||
      normText === 'sobreconsumo' ||
      normText.includes('reporte sobreconsumo') ||
      normText.includes('reporte de sobreconsumo') ||
      normText.includes('top flujos') ||
      normText.includes('abuso de conexiones');

    if (isOverconsumptionQuery) {
      const targetMonth = parseTargetMonth(cleanText);
      if (targetMonth) {
        await openwaClient.sendText(groupId, `📊 Generando reporte mensual de sobreconsumo de Bequant BQN para *${targetMonth.title}*... En unos segundos lo envío adjunto a este grupo.`);
        bequantReportService.generateAndSendMonthlyPdf(groupId, targetMonth).catch(err => {
          console.error('[Sofía Grupo] Error en generateAndSendMonthlyPdf:', err);
        });
        return;
      } else {
        await openwaClient.sendText(groupId, '📊 Por favor indícame qué mes deseas consultar para el reporte de sobreconsumo (por ejemplo: *Sofia #sobreconsumo octubre*).');
        return;
      }
    }

    // 2. Consulta técnica de abonado (cédula, contrato o identificador)
    let customerData = null;
    const hasIdentifier = customerLookupService.hasIdentifier(cleanText);
    if (hasIdentifier) {
      console.log(`[Sofía Grupo] Identificador detectado en consulta grupal: "${cleanText}". Consultando Odoo + SmartOLT...`);
      const lookupRes = await customerLookupService.lookup(cleanText);
      if (lookupRes && lookupRes.found) {
        customerData = lookupRes;
        console.log(`[Sofía Grupo] Abonado encontrado: ${customerData.client.fullName} (${customerData.client.contrato}) - Potencia: ${customerData.technical.rxPower}`);
      }
    }

    // 3. Inferencia con LLM en modo Copiloto Técnico
    const history = conversationManager.getHistory(groupId);
    const reply = await llmClient.generateReply(
      history,
      `[${authorName}]: ${cleanText}`,
      customerData,
      { isGroup: true, authorName }
    );

    // Guardar en el historial del grupo
    conversationManager.addMessage(groupId, 'user', `[${authorName}]: ${cleanText}`);
    conversationManager.addMessage(groupId, 'assistant', reply);

    // Enviar respuesta al grupo
    await openwaClient.sendText(groupId, reply);
    console.log(`[Sofía Grupo] Respuesta enviada al grupo ${groupId} para ${authorName}.`);

    // Registrar interacción en el motor de aprendizaje
    try {
      learningService.logInteraction(groupId, authorName, cleanText, reply, false);
    } catch (eLog) {}

  } catch (err) {
    console.error(`[Sofía Grupo] Error respondiendo en grupo ${groupId}:`, err.message);
  }
}

// Procesamiento con IA y envío de respuesta (Exclusivamente por Texto)
async function processAndReply(chatId, userMessage, pushname) {
  try {
    const normText = (userMessage || '').trim().toLowerCase();

    // 0. Comprobar si hay una acción pendiente para este chat (ej. esperando selección de mes para sobreconsumo)
    const pendingAction = conversationManager.getPendingAction(chatId);
    if (pendingAction && pendingAction.action === 'awaiting_sobreconsumo_month') {
      if (pendingAction.createdAt && (Date.now() - pendingAction.createdAt > 5 * 60 * 1000)) {
        conversationManager.clearPendingAction(chatId);
      } else if (normText === 'cancelar' || normText === 'cancel' || normText === 'salir' || normText === 'no') {
        conversationManager.clearPendingAction(chatId);
        await openwaClient.sendText(chatId, 'Operación cancelada. Si necesitas otra consulta o reporte, avísame.');
        return;
      } else if (customerLookupService.hasIdentifier(userMessage)) {
        conversationManager.clearPendingAction(chatId);
      } else {
        const parsedMonth = parseTargetMonth(userMessage);
        if (parsedMonth) {
          conversationManager.clearPendingAction(chatId);
          console.log(`[Sofía] Mes "${parsedMonth.title}" seleccionado por ${chatId} tras prompt interactivo.`);
          await openwaClient.sendText(chatId, `📊 Generando reporte mensual de sobreconsumo de Bequant BQN para *${parsedMonth.title}* en formato PDF... En unos segundos te lo envío adjunto.`);
          bequantReportService.generateAndSendMonthlyPdf(chatId, parsedMonth).catch(err => {
            console.error('[Sofía] Error en generateAndSendMonthlyPdf:', err);
          });
          return;
        } else {
          await openwaClient.sendText(chatId, '❓ No logré identificar el mes. Por favor indícame el nombre del mes que deseas consultar (por ejemplo: *octubre* o *noviembre*), o escribe *cancelar* para salir.');
          return;
        }
      }
    }

    // 1. Comandos rápidos de monitoreo técnico (NOC)
    if (normText === 'resumen' || normText === 'status' || normText === 'red') {
      console.log(`[Sofía] Comando técnico "resumen" recibido de ${chatId}.`);
      const summaryMsg = await smartoltMonitor.getQuickSummary();
      await openwaClient.sendText(chatId, summaryMsg);
      learningService.logInteraction(chatId, pushname, userMessage, summaryMsg, true);
      return;
    }
    if (normText === 'los' || normText === 'cortes') {
      console.log(`[Sofía] Comando técnico "los" recibido de ${chatId}.`);
      const losMsg = await smartoltMonitor.getLosList();
      await openwaClient.sendText(chatId, losMsg);
      learningService.logInteraction(chatId, pushname, userMessage, losMsg, true);
      return;
    }
    if (normText === 'luz' || normText === 'power' || normText === 'apagon' || normText === 'apagones') {
      console.log(`[Sofía] Comando técnico "luz" recibido de ${chatId}.`);
      const powerMsg = await smartoltMonitor.getActiveOutagesReport();
      await openwaClient.sendText(chatId, powerMsg);
      learningService.logInteraction(chatId, pushname, userMessage, powerMsg, true);
      return;
    }

    // Comando para forzar reporte de autoaprendizaje y consolidación (exclusivo para el directivo)
    if (
      (normText === '!aprender' || normText === '!consolidar' || normText === 'que aprendiste' || normText === 'que aprendiste hoy') &&
      (chatId.includes('584120000001') || chatId === config.REPORT_TARGET_CHAT_ID)
    ) {
      console.log(`[Sofía] Comando de autoaprendizaje recibido de ${chatId}. Ejecutando consolidación...`);
      await openwaClient.sendText(chatId, '🧠 Consolidando telemetría e interacciones de las últimas 24 horas... Ya te envío el reporte.');
      learningService.runDailyConsolidation(llmClient, true).catch(err => {
        console.error('[Sofía] Error en runDailyConsolidation:', err);
      });
      return;
    }

    // Comando de Diagnóstico Clúster Cirion / Lumex (Exclusivo para Cristian Salinas 584120000001)
    const isCirionQuery =
      normText.includes('cirion') ||
      normText.includes('lumex') ||
      normText === '!cirion' ||
      normText === '/cirion' ||
      normText === '#cirion';

    if (isCirionQuery && (chatId.includes('584120000001') || chatId === config.REPORT_TARGET_CHAT_ID)) {
      console.log(`[Sofía] Consulta Cirion/Lumex recibida de Cristian Salinas (${chatId}). Ejecutando diagnóstico en vivo...`);
      await openwaClient.sendText(chatId, '🔍 Diagnosticando Clúster Cirion (Lumex) en tiempo real... Un momento por favor.');
      
      cirionDiagnosticService.runDiagnosisAndSend(chatId).catch(err => {
        console.error('[Sofía] Error en diagnóstico Cirion:', err);
        openwaClient.sendText(chatId, `❌ Error al conectar con el clúster de Cirion: ${err.message}`).catch(() => {});
      });
      return;
    }

    // Comando de Sobreconsumo de Conexiones Bequant BQN (Genera y envía PDF mensual)
    const isOverconsumptionQuery =
      normText.startsWith('!sobreconsumo') ||
      normText.startsWith('#sobreconsumo') ||
      normText.startsWith('/sobreconsumo') ||
      normText === 'sobreconsumo' ||
      normText.includes('reporte sobreconsumo') ||
      normText.includes('reporte de sobreconsumo') ||
      normText.includes('top flujos') ||
      normText.includes('abuso de conexiones') ||
      normText.includes('quienes tienen sobreconsumo');

    if (isOverconsumptionQuery) {
      const targetMonth = parseTargetMonth(userMessage);
      if (targetMonth) {
        console.log(`[Sofía] Comando de Sobreconsumo directo con mes "${targetMonth.title}" detectado de ${chatId}.`);
        await openwaClient.sendText(chatId, `📊 Generando reporte mensual de sobreconsumo de Bequant BQN para *${targetMonth.title}* en formato PDF... En unos segundos te lo envío adjunto.`);
        bequantReportService.generateAndSendMonthlyPdf(chatId, targetMonth).catch(err => {
          console.error('[Sofía] Error en generateAndSendMonthlyPdf:', err);
        });
        return;
      } else {
        console.log(`[Sofía] Comando de Sobreconsumo sin mes especificado de ${chatId}. Solicitando mes interactivo...`);
        conversationManager.setPendingAction(chatId, { action: 'awaiting_sobreconsumo_month', createdAt: Date.now() });
        const askMonthMsg = '📊 Para generar el reporte mensual de sobreconsumo de Bequant BQN, por favor indícame qué mes deseas consultar (por ejemplo: *octubre* o *noviembre*).\n\n_Tip: También puedes escribir directo: #sobreconsumo octubre_';
        await openwaClient.sendText(chatId, askMonthMsg);
        return;
      }
    }

    const isStaff = authorizedMembersManager.isAuthorized(chatId);
    const member = authorizedMembersManager.getMember(chatId);
    const callerName = member?.name || (chatId.includes('584120000001') ? 'Cristian Salinas (Directivo)' : (pushname || 'Colega'));

    let history = conversationManager.getHistory(chatId);
    if (isStaff) {
      // Filtrar mensajes antiguos del historial que puedan contener saludos o preguntas residenciales
      history = history.filter(m => !m.content.includes('Bienvenida a Multicanal') && !m.content.includes('con tu servicio'));
    }

    // 1. Verificar si hay cédula o contrato en el mensaje, o si ya tenemos cliente identificado
    let customerData = isStaff ? null : conversationManager.getCustomerData(chatId);
    const hasIdentifier = customerLookupService.hasIdentifier(userMessage);

    if (hasIdentifier) {
      console.log(`[Sofía] Detectado identificador en mensaje de ${chatId}. Consultando Odoo y SmartOLT...`);
      const lookupRes = await customerLookupService.lookup(userMessage);
      if (lookupRes && lookupRes.found) {
        customerData = lookupRes;
        conversationManager.setCustomerData(chatId, customerData);
        console.log(`[Sofía] ✅ Cliente identificado: ${customerData.client.fullName} (${customerData.client.contrato}) - Módem: ${customerData.technical.status} - Deuda: ${customerData.billing.hasDebt ? '$' + customerData.billing.totalDebt : 'Al día'}`);
      }
    }

    console.log(`[Sofía] Solicitando inferencia LLM para ${chatId} (Historial: ${history.length} msgs, Staff: ${isStaff ? callerName : 'No'}, Cliente: ${customerData ? customerData.client.fullName : 'No identificado'})...`);
    const reply = await llmClient.generateReply(
      history,
      userMessage,
      customerData,
      { isStaff, memberName: callerName, isGroup: false }
    );
    console.log(`[Sofía] Respuesta generada: "${reply}"`);

    // Guardar en el historial conversacional
    conversationManager.addMessage(chatId, 'user', userMessage);
    conversationManager.addMessage(chatId, 'assistant', reply);

    // Enviar exclusivamente por texto
    await openwaClient.sendText(chatId, reply);
    console.log(`[Sofía] Respuesta de texto enviada exitosamente a ${chatId}`);

    // Registrar en el motor de aprendizaje
    try {
      learningService.logInteraction(chatId, pushname, userMessage, reply, false);
    } catch (eLog) {}

    // Detección automática de escalación humana en la respuesta
    const lowerReply = reply.toLowerCase();
    if (
      lowerReply.includes('transfiriendo con uno de nuestros asesores humanos') ||
      lowerReply.includes('te comunico con uno de mis compañeros') ||
      lowerReply.includes('te transfiero') ||
      lowerReply.includes('con un operador')
    ) {
      console.log(`[Sofía] Chat ${chatId} marcado como escalado a humano.`);
      conversationManager.setEscalated(chatId, true);
    }

  } catch (err) {
    console.error(`[Sofía] Error al responder a ${chatId}:`, err.message);
    try {
      const fallbackReply = 'Disculpa, en este momento estoy experimentando una pequeña intermitencia en mi sistema. Por favor permíteme unos minutos o un operador humano te atenderá en breve.';
      await openwaClient.sendText(chatId, fallbackReply);
    } catch (sendErr) {
      console.error('[Sofía] Error enviando mensaje de fallback:', sendErr.message);
    }
  }
}

// Iniciar servidor
const server = app.listen(config.PORT, '0.0.0.0', async () => {
  console.log(`=======================================================`);
  console.log(`🚀 Sofía - Bot de Atención Multicanal activo en puerto ${config.PORT}`);
  console.log(`🎙️  Módulo de Voz: STT NVIDIA Parakeet ACTIVO (Respuestas exclusivamente por texto)`);
  console.log(`⚙️  Modo de pruebas (TEST_MODE): ${config.TEST_MODE}`);
  if (config.TEST_MODE) {
    console.log(`📋 Whitelist activa: ${JSON.stringify(config.WHITELIST)}`);
  }
  console.log(`🌐 Base OpenWA: ${config.OPENWA.BASE_URL}`);
  console.log(`🧠 Base LLM: ${config.LLM.BASE_URL} (Modelo: ${config.LLM.MODEL})`);
  console.log(`=======================================================`);

  // Auto-verificar registro de webhook en OpenWA
  await openwaClient.ensureWebhookRegistered();

  // Iniciar motor de monitoreo SmartOLT en segundo plano
  smartoltMonitor.start();

  // Iniciar programador de autoaprendizaje y consolidación matutina (07:00 AM)
  learningService.startScheduler(llmClient);
});

module.exports = { app, server };
