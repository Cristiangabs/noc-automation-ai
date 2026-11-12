const MAX_HISTORY_LENGTH = 10;
const SESSION_TTL_MS = 2 * 60 * 60 * 1000; // 2 horas de inactividad para resetear contexto

class ConversationManager {
  constructor() {
    this.conversations = new Map();
  }

  getOrCreate(chatId) {
    const now = Date.now();
    let conv = this.conversations.get(chatId);

    if (!conv || (now - conv.lastActive > SESSION_TTL_MS)) {
      conv = {
        messages: [],
        escalatedToHuman: false,
        escalatedAt: null,
        createdAt: now,
        lastActive: now
      };
      this.conversations.set(chatId, conv);
    } else {
      conv.lastActive = now;
    }

    return conv;
  }

  getHistory(chatId) {
    const conv = this.getOrCreate(chatId);
    return conv.messages.map(m => ({ role: m.role, content: m.content }));
  }

  addMessage(chatId, role, content) {
    const conv = this.getOrCreate(chatId);
    conv.messages.push({
      role,
      content,
      timestamp: Date.now()
    });

    if (conv.messages.length > MAX_HISTORY_LENGTH) {
      conv.messages = conv.messages.slice(-MAX_HISTORY_LENGTH);
    }
  }

  isEscalated(chatId) {
    const conv = this.conversations.get(chatId);
    if (!conv || !conv.escalatedToHuman) return false;
    // Si pasaron más de 3 horas desde la escalación, se libera el bot
    if (Date.now() - conv.escalatedAt > 3 * 60 * 60 * 1000) {
      conv.escalatedToHuman = false;
      return false;
    }
    return true;
  }

  setEscalated(chatId, escalated = true) {
    const conv = this.getOrCreate(chatId);
    conv.escalatedToHuman = escalated;
    conv.escalatedAt = escalated ? Date.now() : null;
  }

  getCustomerData(chatId) {
    const conv = this.conversations.get(chatId);
    return conv?.customerData || null;
  }

  setCustomerData(chatId, data) {
    const conv = this.getOrCreate(chatId);
    conv.customerData = data;
  }

  setPendingAction(chatId, actionData) {
    const conv = this.getOrCreate(chatId);
    conv.pendingAction = actionData;
  }

  getPendingAction(chatId) {
    const conv = this.conversations.get(chatId);
    return conv?.pendingAction || null;
  }

  clearPendingAction(chatId) {
    const conv = this.conversations.get(chatId);
    if (conv) {
      delete conv.pendingAction;
    }
  }

  clear(chatId) {
    this.conversations.delete(chatId);
  }
}

module.exports = new ConversationManager();
