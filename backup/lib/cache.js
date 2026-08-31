'use strict';
/**
 * freaky-backup · cache.js
 * Minimal in-memory TTL cache with hit/miss stats. No dependencies.
 */

class TTLCache {
  constructor({ ttlMs = 600000, maxEntries = 5000, name = 'cache' } = {}) {
    this.ttlMs = ttlMs;
    this.maxEntries = maxEntries;
    this.name = name;
    this.map = new Map(); // key -> { value, expiresAt }
    this.hits = 0;
    this.misses = 0;
  }

  get(key) {
    const entry = this.map.get(key);
    if (!entry) {
      this.misses += 1;
      return undefined;
    }
    if (Date.now() > entry.expiresAt) {
      this.map.delete(key);
      this.misses += 1;
      return undefined;
    }
    this.hits += 1;
    return entry.value;
  }

  set(key, value, ttlMs = this.ttlMs) {
    if (this.map.size >= this.maxEntries) {
      // drop the oldest ~10% rather than growing unbounded
      const toDelete = Math.max(1, Math.floor(this.maxEntries * 0.1));
      let n = 0;
      for (const k of this.map.keys()) {
        this.map.delete(k);
        if (++n >= toDelete) break;
      }
    }
    this.map.set(key, { value, expiresAt: Date.now() + ttlMs });
  }

  stats() {
    const total = this.hits + this.misses;
    return {
      entries: this.map.size,
      hits: this.hits,
      misses: this.misses,
      hitRate: total ? +(this.hits / total).toFixed(3) : 0
    };
  }

  clear() {
    this.map.clear();
  }
}

module.exports = { TTLCache };
