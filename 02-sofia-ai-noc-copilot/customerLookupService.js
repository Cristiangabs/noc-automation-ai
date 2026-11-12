const https = require('https');
const { Pool } = require('pg');

// Configuración de Odoo Producción
const odooPool = new Pool({
  host: process.env.ODOO_DB_HOST || '10.0.10.50',
  port: parseInt(process.env.ODOO_DB_PORT || '5432'),
  user: process.env.ODOO_DB_USER || 'odoo',
  password: process.env.ODOO_DB_PASSWORD || 'YOUR_DB_PASSWORD',
  database: process.env.ODOO_DB_NAME || 'multicanal',
  max: 5,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000
});

// Configuración de SmartOLT Oficial
const SMARTOLT_CONFIG = {
  host: 'isp-domain.smartolt.com',
  apiKey: 'YOUR_SMARTOLT_API_TOKEN'
};

// Cliente HTTPS para SmartOLT
function callSmartOlt(endpoint) {
  return new Promise((resolve) => {
    const req = https.request({
      hostname: SMARTOLT_CONFIG.host,
      port: 443,
      path: endpoint,
      method: 'GET',
      headers: {
        'X-Token': SMARTOLT_CONFIG.apiKey
      },
      timeout: 8000
    }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => {
        try {
          resolve(JSON.parse(d));
        } catch(e) {
          resolve(null);
        }
      });
    });
    req.on('error', (err) => {
      console.error('[SmartOLT] Error en petición:', err.message);
      resolve(null);
    });
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
    req.end();
  });
}

class CustomerLookupService {
  // Extraer cédula de un texto (V-12345678, 12345678, etc.)
  extractCedula(text) {
    if (!text) return null;
    const clean = String(text);
    // Buscar patrón V-12345678 o número de 6 a 9 dígitos
    const match = clean.match(/\b(?:[VvEeJj]-?\s*)?(\d{6,9})\b/);
    return match ? match[1] : null;
  }

  // Extraer número de contrato (SUB8456, SUB-8456, contrato 8456)
  extractContrato(text) {
    if (!text) return null;
    const match = String(text).match(/\b(?:SUB-?\s*|CONTRATO\s*:?\s*)(\d{3,6})\b/i);
    return match ? match[1] : null;
  }

  // Extraer tokens de nombre de mensajes técnicos explícitos (ej: "cliente Juan Perez", "buscar Maria Rodriguez")
  extractNameTokens(text) {
    if (!text) return null;
    const clean = String(text).trim();
    const explicit = clean.match(/(?:cliente|buscar|abonado|titular|a nombre de)\s+([a-záéíóúñA-ZÁÉÍÓÚÑ\s]{4,60})/i);
    if (explicit) {
      const candidate = explicit[1].trim().replace(/[.,!?;:]/g, '');
      const tokens = candidate.split(/\s+/).filter(t => t.length > 2);
      if (tokens.length >= 2) return tokens;
    }
    return null;
  }

  hasIdentifier(text) {
    return Boolean(this.extractCedula(text) || this.extractContrato(text) || this.extractNameTokens(text));
  }

  // Verificar si el nombre proporcionado por el usuario coincide razonablemente con el de Odoo
  validateNameMatch(providedName, odooFullName) {
    if (!providedName || !odooFullName) return { matches: false, missingSurname: true };

    const norm = str => str.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").trim();
    const cleanProvided = norm(providedName);
    const cleanOdoo = norm(odooFullName);

    const providedTokens = cleanProvided.split(/\s+/).filter(t => t.length > 2);
    const odooTokens = cleanOdoo.split(/\s+/).filter(t => t.length > 2);

    let matchCount = 0;
    for (const pt of providedTokens) {
      if (odooTokens.some(ot => ot.includes(pt) || pt.includes(ot))) {
        matchCount++;
      }
    }

    const hasMultipleTokens = providedTokens.length >= 2;
    return {
      matches: matchCount >= 1,
      hasFullData: hasMultipleTokens && matchCount >= 2,
      provided: providedName,
      official: odooFullName
    };
  }

  // Consulta integral en Odoo y SmartOLT
  async lookup(text) {
    const cedula = this.extractCedula(text);
    const contrato = this.extractContrato(text);
    const nameTokens = this.extractNameTokens(text);

    if (!cedula && !contrato && (!nameTokens || nameTokens.length < 2)) {
      return { found: false, reason: 'NO_IDENTIFIER_FOUND' };
    }

    try {
      console.log(`[Lookup] Buscando cliente por Cédula: "${cedula}" / Contrato: "${contrato}" / NombreTokens: ${nameTokens ? JSON.stringify(nameTokens) : 'null'}...`);

      // Construcción dinámica de consulta SQL para Odoo
      let whereClauses = [];
      let queryParams = [];

      if (cedula) {
        queryParams.push(cedula);
        whereClauses.push(`(
          p.vat = $${queryParams.length} 
          OR p.vat = 'V' || $${queryParams.length} 
          OR p.vat = 'V-' || $${queryParams.length} 
          OR p.vat = 'E' || $${queryParams.length} 
          OR p.vat = 'E-' || $${queryParams.length}
          OR p.vat ILIKE '%' || $${queryParams.length} || '%'
        )`);
      }

      if (contrato) {
        queryParams.push(`%SUB%${contrato}%`);
        whereClauses.push(`sub.code ILIKE $${queryParams.length}`);
      }

      if (nameTokens && nameTokens.length >= 2) {
        let nameAnds = [];
        nameTokens.forEach(token => {
          queryParams.push(`%${token}%`);
          nameAnds.push(`p.name ILIKE $${queryParams.length}`);
        });
        whereClauses.push(`(${nameAnds.join(' AND ')})`);
      }

      const q = await odooPool.query(`
        SELECT 
          p.id AS partner_id,
          p.name AS full_name,
          p.vat AS cedula,
          p.phone,
          p.mobile,
          p.street,
          sub.id AS subscription_id,
          sub.code AS contrato,
          sub.stage_id AS stage_id,
          sub.x_total_suscripcion AS mensualidad,
          t.codigo_onu,
          t.serial_onu
        FROM res_partner p
        LEFT JOIN sale_subscription sub ON sub.partner_id = p.id
        LEFT JOIN project_task t ON (t.partner_id = p.id AND (t.codigo_onu IS NOT NULL OR t.serial_onu IS NOT NULL))
        WHERE ${whereClauses.join(' OR ')}
        ORDER BY sub.id DESC NULLS LAST, t.id DESC NULLS LAST
        LIMIT 1;
      `, queryParams);

      if (q.rows.length === 0) {
        return { 
          found: false, 
          reason: 'NOT_FOUND_IN_ODOO',
          searchedCedula: cedula,
          searchedContrato: contrato,
          searchedNameTokens: nameTokens
        };
      }

      const cust = q.rows[0];

      // 2. Consultar facturas pendientes (deudas) en Odoo
      const invRes = await odooPool.query(`
        SELECT id, name, amount_total, amount_residual, payment_state, state, invoice_date_due
        FROM account_move
        WHERE partner_id = $1 
          AND move_type = 'out_invoice' 
          AND state = 'posted' 
          AND payment_state IN ('not_paid', 'partial')
        ORDER BY invoice_date_due ASC;
      `, [cust.partner_id]);

      let totalDebt = 0;
      invRes.rows.forEach(inv => {
        totalDebt += parseFloat(inv.amount_residual || 0);
      });

      const billing = {
        hasDebt: totalDebt > 0.05,
        totalDebt: totalDebt.toFixed(2),
        unpaidInvoicesCount: invRes.rows.length,
        invoices: invRes.rows.map(i => ({
          numero: i.name,
          montoResidual: i.amount_residual,
          vencimiento: i.invoice_date_due ? i.invoice_date_due.toISOString().split('T')[0] : 'N/A'
        }))
      };

      // 3. Consultar SmartOLT en vivo
      let technical = {
        hasOnu: false,
        status: 'DESCONOCIDO',
        online: false,
        rxPower: 'N/A',
        signalQuality: 'N/A',
        lastDownReason: 'N/A',
        zone: 'N/A',
        address: cust.street || 'N/A'
      };

      const gponSn = cust.codigo_onu || cust.serial_onu;
      if (gponSn) {
        technical.hasOnu = true;
        technical.gponSn = gponSn;

        const oltRes = await callSmartOlt(`/api/onu/get_onu_details/${encodeURIComponent(gponSn)}`);
        if (oltRes && oltRes.onu_details) {
          const d = oltRes.onu_details;
          technical.status = d.status || 'Offline';
          technical.online = (d.status === 'Online');
          technical.rxPower = d.signal_1490 ? `${d.signal_1490} dBm` : (d.signal_1310 ? `${d.signal_1310} dBm` : 'N/A');
          technical.signalQuality = d.signal || 'N/A';
          technical.lastDownReason = d.last_down_reason || d.last_dereg_reason || 'N/A';
          technical.zone = d.zone_name || technical.zone;
          technical.address = d.address || technical.address;
        }
      }

      return {
        found: true,
        client: {
          partnerId: cust.partner_id,
          fullName: cust.full_name,
          cedula: cust.cedula,
          contrato: cust.contrato || 'N/A',
          telefono: cust.phone || cust.mobile || 'N/A',
          direccion: cust.street || 'N/A'
        },
        billing,
        technical
      };

    } catch (err) {
      console.error('[Lookup] Error realizando consulta:', err.message);
      return { found: false, error: err.message };
    }
  }
}

module.exports = new CustomerLookupService();
