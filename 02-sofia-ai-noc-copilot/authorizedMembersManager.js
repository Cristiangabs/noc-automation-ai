/**
 * authorizedMembersManager.js
 * Gestor de miembros autorizados para interactuar con Sofía en grupos de WhatsApp
 * y control de presentación única (sin repetición) al unirse a grupos.
 */

const fs = require('fs');
const path = require('path');

const MEMBERS_FILE = path.join(__dirname, 'authorized_group_members.json');
const GROUP_STATE_FILE = path.join(__dirname, 'group_state.json');

const DEFAULT_MEMBERS = [
  { phone: '584120000001', name: 'Cristian Salinas (Directivo)' },
  { phone: '584120000002', name: 'Técnico NOC 1' },
  { phone: '584120000003', name: 'Técnico NOC 2' }
];

class AuthorizedMembersManager {
  constructor() {
    this.members = [];
    this.greetedGroups = new Set();
    this.load();
    this.loadGroupState();
  }

  normalizePhone(raw) {
    if (!raw) return '';
    let clean = String(raw).replace(/[^0-9]/g, '');
    if (clean.startsWith('04')) {
      clean = '58' + clean.slice(1);
    }
    return clean;
  }

  load() {
    try {
      if (fs.existsSync(MEMBERS_FILE)) {
        const raw = fs.readFileSync(MEMBERS_FILE, 'utf8');
        this.members = JSON.parse(raw);
        console.log(`[GroupAuth] Cargados ${this.members.length} miembros autorizados.`);
        return;
      }
    } catch (e) {
      console.error('[GroupAuth] Error leyendo authorized_group_members.json:', e.message);
    }
    this.members = [...DEFAULT_MEMBERS];
    this.save();
  }

  save() {
    try {
      fs.writeFileSync(MEMBERS_FILE, JSON.stringify(this.members, null, 2), 'utf8');
    } catch (e) {
      console.error('[GroupAuth] Error guardando authorized_group_members.json:', e.message);
    }
  }

  loadGroupState() {
    try {
      if (fs.existsSync(GROUP_STATE_FILE)) {
        const data = JSON.parse(fs.readFileSync(GROUP_STATE_FILE, 'utf8'));
        if (Array.isArray(data.greetedGroups)) {
          this.greetedGroups = new Set(data.greetedGroups);
        }
      }
    } catch (e) {
      console.error('[GroupAuth] Error leyendo group_state.json:', e.message);
    }
  }

  saveGroupState() {
    try {
      const data = {
        greetedGroups: Array.from(this.greetedGroups),
        updatedAt: new Date().toISOString()
      };
      fs.writeFileSync(GROUP_STATE_FILE, JSON.stringify(data, null, 2), 'utf8');
    } catch (e) {
      console.error('[GroupAuth] Error guardando group_state.json:', e.message);
    }
  }

  hasGreeted(groupId) {
    if (!groupId) return false;
    return this.greetedGroups.has(groupId);
  }

  markGreeted(groupId) {
    if (!groupId) return;
    this.greetedGroups.add(groupId);
    this.saveGroupState();
    console.log(`[GroupAuth] Grupo ${groupId} registrado como presentado (no se repetirá saludo).`);
  }

  getPresentationMessage() {
    return 'Hola a todos. Soy Sofia, asistente de soporte tecnico y operaciones de Multicanal.\n\nMonitoreare este grupo para emitir alertas de red en tiempo real y responder sus consultas de estado de equipos, cortes y abonados cuando me mencionen directamente. Quedo a su disposicion.';
  }

  isAuthorized(senderRaw) {
    const clean = this.normalizePhone(senderRaw);
    if (!clean) return false;
    return this.members.some(m => {
      const memberPhone = this.normalizePhone(m.phone);
      return clean.includes(memberPhone) || memberPhone.includes(clean);
    });
  }

  getMember(senderRaw) {
    const clean = this.normalizePhone(senderRaw);
    if (!clean) return null;
    return this.members.find(m => {
      const memberPhone = this.normalizePhone(m.phone);
      return clean.includes(memberPhone) || memberPhone.includes(clean);
    }) || null;
  }

  addMember(phone, name) {
    const clean = this.normalizePhone(phone);
    if (!clean) return false;
    const existing = this.members.find(m => this.normalizePhone(m.phone) === clean);
    if (existing) {
      existing.name = name || existing.name;
    } else {
      this.members.push({ phone: clean, name: name || 'Técnico' });
    }
    this.save();
    return true;
  }

  removeMember(phone) {
    const clean = this.normalizePhone(phone);
    this.members = this.members.filter(m => this.normalizePhone(m.phone) !== clean);
    this.save();
  }

  getMembers() {
    return [...this.members];
  }
}

module.exports = new AuthorizedMembersManager();
