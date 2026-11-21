
const assert = require('node:assert/strict');
const React = {};
    const IDENTITY_TIERS = {
      verified_same_source_row: 3,
      inferred_exact_match: 2,
      unlinked_record_id: 1,
      ambiguous_not_linked: 1,
      source_id: 1,
    };
    const identityTier = (status) => IDENTITY_TIERS[status] || 0;
    // Identity-aware deduplication. Names are descriptive only: two distinct
    // holders may have the same (including generic "شخص حقیقی") name.
    // Merge rows only when the backend supplies an explicit shared source ID,
    // and only when their same-date share counts agree.
    const holderIdentityTokens = (h) => {
      const tokens = new Set();
      if (h.id_scheme && h.shareholder_id !== null && h.shareholder_id !== undefined) {
        tokens.add(`${h.id_scheme}:${h.shareholder_id}`);
      }
      String(h.raw_source_ids || '').split(',').forEach(part => {
        const token = part.trim();
        if (/^(holder_id|share_id|ondate_share_id):\d+$/.test(token)) tokens.add(token);
      });
      return tokens;
    };
    const dedupHolders = (rows) => {
      const items = (rows || []).filter(h => h.shares !== null && h.shares !== undefined && Number(h.shares) > 0);
      const parent = items.map((_, i) => i);
      const find = (x) => parent[x] === x ? x : (parent[x] = find(parent[x]));
      const union = (a, b) => { const ra = find(a), rb = find(b); if (ra !== rb) parent[rb] = ra; };
      const tokens = items.map(holderIdentityTokens);
      const shareKey = h => String(h.shares);
      for (let i = 0; i < items.length; i++) {
        for (let j = i + 1; j < items.length; j++) {
          if (shareKey(items[i]) !== shareKey(items[j])) continue;
          let linked = false;
          for (const token of tokens[i]) {
            if (tokens[j].has(token)) { linked = true; break; }
          }
          if (linked) union(i, j);
        }
      }
      const groups = new Map();
      items.forEach((h, i) => {
        const root = find(i);
        if (!groups.has(root)) groups.set(root, []);
        groups.get(root).push(h);
      });
      return Array.from(groups.values()).map(group => {
        // Prefer the strongest pipeline identity status; ties prefer share_id.
        group.sort((a, b) =>
          identityTier(b.identity_status) - identityTier(a.identity_status) ||
          Number(b.id_scheme === 'share_id') - Number(a.id_scheme === 'share_id'));
        return { ...group[0], _merged: group.length };
      });
    };
    // Rows without a share count remain unquantified; keep distinct IDs distinct.
    const unquantifiedHolders = (rows) => {
      const seen = new Set();
      const out = [];
      for (const h of (rows || [])) {
        if (h.shares !== null && h.shares !== undefined) continue;
        const key = `${h.id_scheme}:${h.shareholder_id}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(h.shareholder_name || key);
      }
      return out;
    };


const sameNameDifferentIds = dedupHolders([
 {id_scheme:'share_id', shareholder_id:1, shareholder_name:'شخص حقیقی', shares:100, identity_status:'source_id'},
 {id_scheme:'share_id', shareholder_id:2, shareholder_name:'شخص حقیقی', shares:100, identity_status:'source_id'}
]);
assert.equal(sameNameDifferentIds.length, 2);
const explicitTwin = dedupHolders([
 {id_scheme:'share_id', shareholder_id:1, raw_source_ids:'share_id:1,ondate_share_id:7', shareholder_name:'الف', shares:100, identity_status:'source_id'},
 {id_scheme:'ondate_share_id', shareholder_id:7, raw_source_ids:'share_id:1,ondate_share_id:7', shareholder_name:'ب', shares:100, identity_status:'inferred_exact_match'}
]);
assert.equal(explicitTwin.length, 1);
assert.equal(explicitTwin[0]._merged, 2);
const contradictoryTwin = dedupHolders([
 {id_scheme:'share_id', shareholder_id:1, raw_source_ids:'share_id:1,ondate_share_id:7', shares:100},
 {id_scheme:'ondate_share_id', shareholder_id:7, raw_source_ids:'share_id:1,ondate_share_id:7', shares:90}
]);
assert.equal(contradictoryTwin.length, 2);
const zeroShare = dedupHolders([
 {id_scheme:'share_id', shareholder_id:9, shareholder_name:'سهام‌دار صفر', shares:0}
]);
assert.equal(zeroShare.length, 0);
console.log('frontend identity dedup checks: 4 passed');
