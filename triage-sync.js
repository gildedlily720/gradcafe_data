/* Private board sync. The server owns identity, timestamps and record versions. */
(() => {
  "use strict";
  class TriageSync {
    constructor({storage, fetcher, changed}) {
      this.storage = storage;
      this.fetcher = fetcher;
      this.changed = changed || (() => {});
      const cache = storage.get("jobsweep:cloud:v1", {}) || {};
      this.records = cache.records || {};
      this.pending = cache.pending || {};
      this.conflicts = cache.conflicts || {};
      this.error = "";
      this.ready = false;
      this.busy = null;
    }
    persist() {
      if (!this.storage.set("jobsweep:cloud:v1", {records:this.records, pending:this.pending, conflicts:this.conflicts})) {
        this.error = "Device storage unavailable; keep this page open until sync succeeds.";
      }
      this.changed();
    }
    view() {
      return {...this.records, ...this.pending};
    }
    stage(uid, status, note, job) {
      const previous = this.pending[uid] || this.records[uid] || {};
      this.pending[uid] = {uid, status, note, job, expected_version:previous.expected_version ?? previous.version ?? 0,
        at:new Date().toISOString(), edit_id:Math.random().toString(36).slice(2)};
      this.persist();
    }
    async refresh() {
      if (this.busy) return this.busy;
      this.busy = this.run().finally(() => { this.busy = null; });
      return this.busy;
    }
    async run() {
      try {
        const res = await this.fetcher("/api/triage", {headers:{Accept:"application/json"}, cache:"no-store"});
        if (!res.ok) throw new Error(res.status === 401 ? "Sign in to sync your applications." : "Cloud sync unavailable; changes remain on this device.");
        const body = await res.json();
        if (!Array.isArray(body.records)) throw new Error("Invalid cloud response; device changes preserved.");
        this.records = Object.fromEntries(body.records.map(r => [r.uid,r]));
        for (const uid of Object.keys(this.conflicts)) this.conflicts[uid] = this.records[uid] || null;
        this.ready = true;
        this.error = "";
        // Batch queued edits. A conflict keeps both versions until the user chooses.
        let batch;
        while ((batch = Object.values(this.pending).filter(r => !Object.hasOwn(this.conflicts,r.uid)).slice(0,10)).length) {
          const response = await this.fetcher("/api/triage", {method:"PUT", headers:{"Content-Type":"application/json",Accept:"application/json"},
            body:JSON.stringify({changes:batch.map(({edit_id,at,...change}) => change)})});
          if (!response.ok && response.status !== 409) throw new Error("Cloud sync unavailable; changes remain on this device.");
          const result = await response.json();
          if (!Array.isArray(result.records) || !Array.isArray(result.conflicts)) throw new Error("Invalid sync response; device changes preserved.");
          const returned = new Map(result.records.map(r => [r.uid,r]));
          const conflicted = new Set(result.conflicts);
          for (const sent of batch) {
            const current = returned.get(sent.uid);
            if (current) this.records[sent.uid] = current;
            if (conflicted.has(sent.uid)) {
              this.conflicts[sent.uid] = current || this.records[sent.uid] || null;
            } else if (!current) {
              throw new Error("Cloud did not confirm an edit; device changes preserved.");
            } else if (this.pending[sent.uid]?.edit_id === sent.edit_id) {
              delete this.pending[sent.uid];
            } else if (this.pending[sent.uid]) {
              this.pending[sent.uid].expected_version = current.version;
            }
          }
          this.persist();
        }
      } catch (err) {
        this.error = err.message || "Cloud sync unavailable; device changes preserved.";
      }
      this.persist();
    }
    resolve(uid, keepDevice) {
      if (!Object.hasOwn(this.conflicts,uid)) return;
      if (keepDevice && this.pending[uid]) {
        this.pending[uid].expected_version = this.conflicts[uid]?.version || 0;
      } else {
        delete this.pending[uid];
      }
      delete this.conflicts[uid];
      this.persist();
      return this.refresh();
    }
    label() {
      const n = Object.keys(this.pending).length;
      if (Object.keys(this.conflicts).length) return "Sync conflict — both versions preserved";
      if (this.error) return this.error;
      if (n) return n + " change(s) saved on this device, waiting to sync";
      return this.ready ? "Synced privately across your devices" : "Connecting to private sync…";
    }
  }
  if (typeof module !== "undefined") module.exports = TriageSync;
  else window.JobSweepTriageSync = TriageSync;
})();
